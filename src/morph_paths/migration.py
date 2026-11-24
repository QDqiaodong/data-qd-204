"""Offline migration of signed annotation archives onto revised word-form rules.

Editors may split rules or renumber rule ids while the surface and features a
user sees stay the same.  Archived annotations still name the *old* rule path,
so a downstream consumer that joins analyses by stable reference would lose
the connection.  :func:`migrate_annotations` re-binds every registered stable
reference to a new-version analysis, then rewrites a signed JSONL annotation
archive onto those new analyses and publishes one self-contained package.

The API is purely offline, mutates none of its inputs and keeps no state
between calls.  Both dictionaries are compiled with the existing
:func:`morph_paths.process` semantics in full, so any compile error rejects
the whole request.  Nothing here uses ``queries`` to narrow the form set.

Package format (UTF-8 JSONL, one object per line, canonical key order):

* first the new registry, sorted by ``ref``, one
  ``{"ref": ..., "analysis": {...}}`` object per registered reference;
* then every annotation row in its original order, each with only
  ``analysis`` replaced by the new analysis bound to its ``ref``.

The full contract, including the exact-one-assignment rule, the archive checks
and the failure boundaries, lives in ``docs/contract.md``.
"""
import hashlib
import json
import os
import tempfile
from collections import deque
from pathlib import Path

from .canonical import loads_canonical
from .checks import array, fields, ident
from .model import text as _text
from .service import process

# -- public limits --------------------------------------------------------- #
MAX_RULE_PATH = 300
MAX_APPROVED_PATHS = 8
MAX_ARCHIVE_LINES = 300_000
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_LINE_BYTES = 16 * 1024

# -- package line field sets ----------------------------------------------- #
_ANNOTATION_FIELDS = ("doc", "pos", "text", "ref", "analysis")
_ANALYSIS_FIELDS = ("lemma", "surface", "features", "rules")

_DIGEST_LENGTH = 64


class MigrationError(ValueError):
    """Base class for migration domain rejections.

    Structurally malformed dictionary, registry or approval input raises plain
    ``ValueError``.  The subclasses below distinguish the operational failure
    boundaries: no complete assignment, an ambiguous assignment, an invalid
    archive row, a digest mismatch and a publication failure.
    """


class NoSolutionError(MigrationError):
    """No complete assignment of new analyses to all registered references."""

    def __init__(self, detail, refs=None):
        self.detail = detail
        self.refs = list(refs or ())
        super().__init__(f"no complete assignment: {detail}")


class AmbiguousAssignmentError(MigrationError):
    """More than one complete assignment exists, so none may be guessed."""

    def __init__(self, detail):
        self.detail = detail
        super().__init__(f"ambiguous assignment: {detail}")


class ArchiveError(MigrationError):
    """A source archive row violates the archive contract.

    ``line`` is the one-based line number of the offending row.
    """

    def __init__(self, line, detail):
        self.line = line
        self.detail = detail
        super().__init__(f"archive line {line}: {detail}")


class DigestMismatchError(MigrationError):
    """The consumed archive bytes do not match the approved SHA-256."""

    def __init__(self, expected, actual):
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"archive digest mismatch: approved {expected}, consumed {actual}"
        )


class PublishError(MigrationError):
    """The package could not be published; no target was left behind."""


class TargetExistsError(PublishError):
    """A target file already exists and is never overwritten."""


# --------------------------------------------------------------------------- #
# Structural input validation
# --------------------------------------------------------------------------- #

def _parse_registry(registry, old_index):
    """Validate the registry and resolve each entry's old analysis."""
    if not isinstance(registry, list):
        raise ValueError("registry must be a list")
    refs = []
    by_ref = {}
    identities = set()
    for item in registry:
        fields(item, ("ref", "lemma", "rules"))
        ref = ident(item["ref"])
        lemma = ident(item["lemma"])
        path = tuple(ident(rule) for rule in array(item["rules"], 0, MAX_RULE_PATH))
        identity = (lemma, path)
        if identity in identities:
            raise ValueError("registry entries must name distinct old analyses")
        identities.add(identity)
        old = old_index.get(identity)
        if old is None:
            raise ValueError(
                f"registry ref {ref!r} does not name an analysis the old "
                f"dictionary produces"
            )
        if ref in by_ref:
            raise ValueError(f"duplicate registry ref: {ref!r}")
        refs.append(ref)
        by_ref[ref] = old
    return refs, by_ref


def _parse_approvals(approvals, known_refs):
    """Validate the per-reference approved new rule paths."""
    if approvals is None:
        return {}
    if not isinstance(approvals, dict):
        raise ValueError("approvals must map refs to lists of rule paths")
    parsed = {}
    for key, value in approvals.items():
        ref = ident(key)
        if ref not in known_refs:
            raise ValueError(f"approval for unregistered ref: {ref!r}")
        paths = array(value, 0, MAX_APPROVED_PATHS)
        seen = set()
        allowed = []
        for entry in paths:
            path = tuple(ident(rule) for rule in array(entry, 0, MAX_RULE_PATH))
            if path in seen:
                raise ValueError(f"duplicate approved path for ref {ref!r}")
            seen.add(path)
            allowed.append(path)
        parsed[ref] = allowed
    return parsed


# --------------------------------------------------------------------------- #
# Assignment
# --------------------------------------------------------------------------- #

def _maximum_matching(refs, candidates):
    """A maximum bipartite matching, or ``None`` if not all refs are covered.

    Hopcroft-Karp.  Each reference has at most eight candidates, and both
    search phases are iterative, so stack depth is independent of the
    registry size.
    """
    infinity = float("inf")
    match_ref = {}
    match_candidate = {}

    def distances():
        distance = {}
        queue = deque()
        for ref in refs:
            if ref not in match_ref:
                distance[ref] = 0
                queue.append(ref)
        found = False
        while queue:
            ref = queue.popleft()
            for candidate in candidates[ref]:
                owner = match_candidate.get(candidate)
                if owner is None:
                    found = True
                elif owner not in distance:
                    distance[owner] = distance[ref] + 1
                    queue.append(owner)
        return distance

    def augment(root, distance):
        stack = [(root, iter(candidates[root]))]
        edges = []
        while stack:
            ref, options = stack[-1]
            descended = False
            for candidate in options:
                owner = match_candidate.get(candidate)
                if owner is None:
                    match_candidate[candidate] = ref
                    match_ref[ref] = candidate
                    for parent, used in reversed(edges):
                        match_candidate[used] = parent
                        match_ref[parent] = used
                    return True
                if distance.get(owner) == distance.get(ref, infinity) + 1:
                    edges.append((ref, candidate))
                    stack.append((owner, iter(candidates[owner])))
                    descended = True
                    break
            if descended:
                continue
            distance[ref] = infinity
            stack.pop()
            if edges:
                edges.pop()
        return False

    while True:
        distance = distances()
        augmented = False
        for ref in refs:
            if ref not in match_ref and augment(ref, distance):
                augmented = True
        if not augmented:
            break
    if len(match_ref) != len(refs):
        return None
    return match_ref


def _is_unique(match_ref, match_candidate, refs, candidates):
    """Whether the maximum matching is the only complete assignment.

    A perfect matching is unique exactly when no reference holds a candidate
    left unused by the matching and the alternating graph has no cycle.
    """
    for ref in refs:
        for candidate in candidates[ref]:
            if candidate == match_ref[ref]:
                continue
            owner = match_candidate.get(candidate)
            if owner is None:
                return False  # a free candidate immediately gives a variant
    arcs = {ref: set() for ref in refs}
    for ref in refs:
        for candidate in candidates[ref]:
            if candidate == match_ref[ref]:
                continue
            arcs[ref].add(match_candidate[candidate])
    state = {}
    for start in refs:
        if state.get(start, 0):
            continue
        state[start] = 1
        stack = [(start, iter(sorted(arcs[start])))]
        while stack:
            node, iterator = stack[-1]
            following = next(iterator, None)
            if following is None:
                state[node] = 2
                stack.pop()
                continue
            seen = state.get(following, 0)
            if seen == 1:
                return False  # alternating cycle -> a second assignment
            if seen == 0:
                state[following] = 1
                stack.append((following, iter(sorted(arcs[following]))))
    return True


def _assign(refs, old_by_ref, new_index, approvals):
    """Bind every reference to exactly one new analysis.

    Returns ``{ref: new_form}`` for the unique complete assignment.  Raises
    :class:`NoSolutionError` when no complete assignment exists and
    :class:`AmbiguousAssignmentError` when more than one does.
    """
    candidates = {}
    empty = []
    for ref in refs:
        old = old_by_ref[ref]
        if ref in approvals:
            allowed = approvals[ref]
        else:
            allowed = [tuple(old["rules"])]
        choices = set()
        for path in allowed:
            new = new_index.get((old["lemma"], path))
            if new is None:
                continue
            if (
                new["surface"] != old["surface"]
                or new["features"] != old["features"]
            ):
                continue
            choices.add((new["lemma"], tuple(new["rules"])))
        candidates[ref] = sorted(choices)
        if not choices:
            empty.append(ref)
    if empty:
        raise NoSolutionError(
            "references without a qualifying new analysis", empty
        )
    ordered = sorted(refs)
    match_ref = _maximum_matching(ordered, candidates)
    if match_ref is None:
        raise NoSolutionError("candidate analyses cannot be assigned one-to-one")
    match_candidate = {candidate: ref for ref, candidate in match_ref.items()}
    if not _is_unique(match_ref, match_candidate, ordered, candidates):
        raise AmbiguousAssignmentError(
            "more than one complete assignment satisfies the approvals"
        )
    return {ref: new_index[candidate] for ref, candidate in match_ref.items()}


# --------------------------------------------------------------------------- #
# Archive streaming
# --------------------------------------------------------------------------- #

def _analysis_dict(form):
    return {
        "lemma": form["lemma"],
        "surface": form["surface"],
        "features": dict(sorted(form["features"].items())),
        "rules": list(form["rules"]),
    }


def _parse_analysis(value, line):
    if not isinstance(value, dict):
        raise ArchiveError(line, "analysis must be an object")
    if set(value) != set(_ANALYSIS_FIELDS):
        raise ArchiveError(
            line, "analysis must contain exactly lemma, surface, features, rules"
        )
    try:
        lemma = ident(value["lemma"])
        rules = [ident(rule) for rule in array(value["rules"], 0, MAX_RULE_PATH)]
        features = value["features"]
        if not isinstance(features, dict) or len(features) > 20:
            raise ValueError("feature mapping required")
        features = {ident(k): ident(v) for k, v in features.items()}
        surface = value["surface"]
        if not isinstance(surface, str):
            raise ValueError("surface must be a string")
    except ValueError as exc:
        raise ArchiveError(line, f"invalid analysis fields: {exc}") from exc
    return {
        "lemma": lemma,
        "surface": surface,
        "features": features,
        "rules": rules,
    }


def _parse_annotation(raw, line, old_by_ref):
    """Validate one raw line; return ``(doc, pos, text, ref, analysis)``."""
    try:
        payload = loads_canonical(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ArchiveError(line, f"not valid UTF-8 strict JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ArchiveError(line, "row must be a JSON object")
    if set(payload) != set(_ANNOTATION_FIELDS):
        raise ArchiveError(
            line, "row must contain exactly doc, pos, text, ref, analysis"
        )
    try:
        doc = ident(payload["doc"])
        ref = ident(payload["ref"])
    except ValueError as exc:
        raise ArchiveError(line, f"invalid doc or ref: {exc}") from exc
    pos = payload["pos"]
    if type(pos) is not int or pos < 0:
        raise ArchiveError(line, "pos must be a non-negative integer")
    if ref not in old_by_ref:
        raise ArchiveError(line, f"ref {ref!r} is not registered")
    try:
        normalized = _text(payload["text"])
    except ValueError as exc:
        raise ArchiveError(line, f"invalid text: {exc}") from exc
    analysis = _parse_analysis(payload["analysis"], line)
    old = old_by_ref[ref]
    for key in _ANALYSIS_FIELDS:
        if analysis[key] != old[key]:
            raise ArchiveError(
                line, f"analysis does not equal the old analysis for ref {ref!r}"
            )
    if normalized != old["surface"]:
        raise ArchiveError(
            line, "NFC text does not equal the old analysis surface"
        )
    return doc, pos, payload["text"], ref, analysis


def _encode(obj):
    return (
        json.dumps(
            obj,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def _write_archive(handle, output, old_by_ref, new_by_ref):
    """Stream the archive into ``output``, validating every row.

    Both file objects are already open; the source handle is read exactly
    once, so the bytes digested are the bytes used to build the package.
    Returns ``(line_count, digest)``.
    """
    digest = hashlib.sha256()
    total = 0
    line = 0
    previous_key = None
    while True:
        raw = handle.readline(MAX_LINE_BYTES + 1)
        if not raw:
            break
        line += 1
        if line > MAX_ARCHIVE_LINES:
            raise ArchiveError(line, "archive exceeds 300000 lines")
        if len(raw) > MAX_LINE_BYTES:
            raise ArchiveError(
                line, "line exceeds 16384 bytes including the newline"
            )
        if not raw.endswith(b"\n"):
            raise ArchiveError(line, "line must end with LF")
        total += len(raw)
        if total > MAX_ARCHIVE_BYTES:
            raise ArchiveError(line, "archive exceeds 256 MiB")
        digest.update(raw)
        doc, pos, text, ref, _analysis = _parse_annotation(
            raw[:-1], line, old_by_ref
        )
        key = (doc, pos)
        if previous_key is not None and key <= previous_key:
            raise ArchiveError(
                line, "line key (doc, pos) must be strictly increasing"
            )
        previous_key = key
        row = {
            "doc": doc,
            "pos": pos,
            "text": text,
            "ref": ref,
            "analysis": new_by_ref[ref],
        }
        output.write(_encode(row))
    return line, digest.hexdigest()


def _missing_ancestors(path):
    """Directories that ``mkdir(parents=True)`` would have to create."""
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent
    return missing


def _remove_directories(created):
    """Remove directories this call created, deepest first, ignoring failure."""
    for directory in created:
        try:
            directory.rmdir()
        except OSError:
            break


def _nearest_existing_dir(path):
    """Deepest existing directory on the way up from ``path``.

    The staging file must live on the same filesystem as the eventual target
    so the final publish can be a single atomic hard link, while the target's
    parent directories are only created once the package is fully valid.
    """
    current = path
    while True:
        if current.exists():
            if not current.is_dir():
                raise PublishError(f"target parent is not a directory: {current}")
            return current
        parent = current.parent
        if parent == current:
            return current
        current = parent


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def migrate_annotations(
    old_data,
    new_data,
    registry,
    approvals,
    archive_path,
    archive_sha256,
    target_path,
):
    """Migrate a signed annotation archive onto revised rules.

    ``old_data`` and ``new_data`` are complete five-field dictionaries in the
    existing input format; both are compiled in full with :func:`process`.
    ``registry`` is the list of ``{"ref", "lemma", "rules"}`` stable
    references, each naming a distinct analysis the old dictionary produces.
    ``approvals`` maps a registered ``ref`` to up to eight ordered new rule
    paths that may inherit it (an unlisted ref may only keep its original
    path; an explicit empty list allows none).  ``archive_path`` is the UTF-8
    JSONL signed archive, ``archive_sha256`` its approved lowercase hex
    digest, and ``target_path`` the package to create without overwriting.

    On success the package exists at ``target_path`` and this returns the new
    registry, the annotation and total line counts, and the digest actually
    consumed.  Failure raises :class:`NoSolutionError`,
    :class:`AmbiguousAssignmentError`, :class:`ArchiveError` (with ``line``),
    :class:`DigestMismatchError` or :class:`PublishError`; no partial target
    is ever left behind.
    """
    if not isinstance(archive_sha256, str) or len(archive_sha256) != _DIGEST_LENGTH:
        raise ValueError("archive_sha256 must be a lowercase hex SHA-256")
    if any(char not in "0123456789abcdef" for char in archive_sha256):
        raise ValueError("archive_sha256 must be a lowercase hex SHA-256")

    old_forms = process(old_data)["forms"]
    new_forms = process(new_data)["forms"]
    old_index = {(f["lemma"], tuple(f["rules"])): f for f in old_forms}
    new_index = {(f["lemma"], tuple(f["rules"])): f for f in new_forms}

    refs, old_by_ref = _parse_registry(registry, old_index)
    allowed = _parse_approvals(approvals, set(refs))
    assignment = _assign(refs, old_by_ref, new_index, allowed)

    ordered_refs = sorted(refs)
    new_by_ref = {
        ref: _analysis_dict(assignment[ref]) for ref in ordered_refs
    }

    target = Path(target_path)
    if os.path.lexists(target):
        raise TargetExistsError(f"target already exists: {target}")

    staging_dir = _nearest_existing_dir(target.parent)
    temporary = None
    try:
        try:
            descriptor, name = tempfile.mkstemp(
                dir=staging_dir, prefix=".migration-", suffix=".tmp"
            )
        except OSError as exc:
            raise PublishError(f"cannot create temporary package: {exc}") from exc
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                for ref in ordered_refs:
                    handle.write(
                        _encode({"ref": ref, "analysis": new_by_ref[ref]})
                    )

                try:
                    source = open(archive_path, "rb")
                except OSError as exc:
                    raise PublishError(f"cannot read archive: {exc}") from exc
                try:
                    with source:
                        annotation_lines, actual_digest = _write_archive(
                            source, handle, old_by_ref, new_by_ref
                        )
                except OSError as exc:
                    raise PublishError(f"cannot read archive: {exc}") from exc
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise PublishError(f"cannot write package: {exc}") from exc

        if actual_digest != archive_sha256:
            raise DigestMismatchError(archive_sha256, actual_digest)

        created_parents = _missing_ancestors(target.parent)
        if created_parents:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                _remove_directories(created_parents)
                raise PublishError(f"cannot publish package: {exc}") from exc
        try:
            os.link(temporary, target)
        except FileExistsError as exc:
            _remove_directories(created_parents)
            raise TargetExistsError(f"target already exists: {target}") from exc
        except OSError as exc:
            _remove_directories(created_parents)
            raise PublishError(f"cannot publish package: {exc}") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass

    return {
        "path": str(target),
        "registry": [
            {"ref": ref, "analysis": dict(new_by_ref[ref])}
            for ref in ordered_refs
        ],
        "annotation_lines": annotation_lines,
        "total_lines": annotation_lines + len(ordered_refs),
        "archive_sha256": actual_digest,
    }
