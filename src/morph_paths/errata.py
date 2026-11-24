"""Per-position source tracing and confirmed surface errata write-back.

Two offline editor APIs on top of the existing :func:`morph_paths.process`
compile semantics.  Neither mutates its inputs; neither keeps any service or
disk state.

* :func:`trace_analysis` reports, for one analysis the dictionary actually
  produces, exactly which original input characters each final surface
  position comes from.
* :func:`apply_errata` turns a batch of editor-confirmed surface errata into
  a complete revised dictionary, delivered only when every requested
  interval maps to one safe source slice and the jointly revised dictionary
  still compiles with every unrequested analysis preserved.

Source attribution follows Unicode canonical equivalence, never index
arithmetic over normalized fields: the original characters participating in
the selected path (in surface concatenation order) and the final surface
characters are both decomposed into tagged NFD atoms, each side is stably
ordered by canonical combining class, and corresponding atoms define the
sources of each final position.  The full contract lives in
``docs/contract.md``.
"""
import copy
import unicodedata

from .checks import array, fields, ident, integer
from .service import process

MAX_ERRATA = 2000
MAX_RULE_PATH = 300
MAX_SURFACE = 64
_CONTROL_OR_SURROGATE = ("Cc", "Cs")

_KIND_LEMMA = "lemma"
_KIND_RULE = "rule"


class ErrataError(ValueError):
    """Base class for trace/apply domain rejections.

    Structurally malformed inputs raise plain ``ValueError``; the subclasses
    below distinguish a missing analysis, an unwritable interval,
    intersecting source slices and a rejected revised dictionary.
    """


class AnalysisNotFoundError(ErrataError):
    """The requested ``(lemma, rules)`` identity is not produced."""

    def __init__(self, lemma, rules):
        self.lemma = lemma
        self.rules = list(rules)
        super().__init__(
            f"analysis not produced: lemma={lemma!r} rules={self.rules!r}"
        )


class IntervalNotWritableError(ErrataError):
    """The interval does not map to one self-contained source slice."""

    def __init__(self, request, lemma, rules, detail):
        self.request = request
        self.lemma = lemma
        self.rules = list(rules)
        self.detail = detail
        super().__init__(
            f"request {request}: interval not writable for "
            f"lemma={lemma!r} rules={self.rules!r}: {detail}"
        )


class ConflictingSlicesError(ErrataError):
    """Requests write the same slice differently or intersecting slices."""

    def __init__(self, field, slices, detail):
        self.field = field
        self.slices = slices
        self.detail = detail
        super().__init__(f"conflicting source slices on {field!r}: {detail}")


class RevisionRejectedError(ErrataError):
    """The jointly revised dictionary fails compilation or preservation."""

    def __init__(self, detail, lemma=None, rules=None, request=None):
        self.detail = detail
        self.lemma = lemma
        self.rules = None if rules is None else list(rules)
        self.request = request
        super().__init__(f"revised dictionary rejected: {detail}")


# --------------------------------------------------------------------------- #
# Canonical-equivalence source alignment
# --------------------------------------------------------------------------- #

def _canonical_order(atoms):
    """Stable NFD ordering of ``(char, tag)`` atoms.

    Combining marks are ordered by canonical combining class within each
    segment after a starter; marks of equal class keep their relative order.
    """
    ordered = []
    segment = []
    for atom in atoms:
        if unicodedata.combining(atom[0]):
            segment.append(atom)
        else:
            ordered.extend(sorted(segment, key=lambda a: unicodedata.combining(a[0])))
            segment = []
            ordered.append(atom)
    ordered.extend(sorted(segment, key=lambda a: unicodedata.combining(a[0])))
    return ordered


def _nfd_atoms(tagged_chars):
    """Tagged NFD atoms of tagged characters, in canonical order."""
    atoms = []
    for char, tag in tagged_chars:
        for part in unicodedata.normalize("NFD", char):
            atoms.append((part, tag))
    return _canonical_order(atoms)


def _surface_segments(lemmas_by_id, rules_by_id, lemma_id, rule_path):
    """Original field texts of one path in surface concatenation order.

    Prefixes of later rules sit outside earlier ones; empty fields
    contribute nothing.
    """
    segments = []
    for rule_id in reversed(rule_path):
        prefix = rules_by_id[rule_id]["prefix"]
        if prefix:
            segments.append(((_KIND_RULE, rule_id, "prefix"), prefix))
    stem = lemmas_by_id[lemma_id]["stem"]
    if stem:  # stems are never empty; kept for symmetry
        segments.append(((_KIND_LEMMA, lemma_id, "stem"), stem))
    for rule_id in rule_path:
        suffix = rules_by_id[rule_id]["suffix"]
        if suffix:
            segments.append(((_KIND_RULE, rule_id, "suffix"), suffix))
    return segments


def _position_sources(segments, surface):
    """Source set of each final surface position, in code point order.

    ``segments`` carries the original (pre-normalization) field texts, so
    indices are zero-based code point indices into the original input
    fields.  Both sides decompose to the same canonical atom sequence; the
    pairwise alignment attributes every final position its sources.
    """
    tagged_sources = []
    for (kind, owner, field), text in segments:
        for index, char in enumerate(text):
            tagged_sources.append((char, (kind, owner, field, index)))
    source_atoms = _nfd_atoms(tagged_sources)
    output_atoms = _nfd_atoms(
        [(char, position) for position, char in enumerate(surface)]
    )
    if [char for char, _ in source_atoms] != [char for char, _ in output_atoms]:
        raise RuntimeError("canonical decomposition mismatch")
    positions = [set() for _ in surface]
    for (_, source), (_, position) in zip(source_atoms, output_atoms):
        positions[position].add(source)
    return positions


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

def _source_dict(source):
    kind, owner, field, index = source
    return {kind: owner, "field": field, "index": index}


def _parse_identity(lemma, rules):
    lemma_id = ident(lemma)
    rule_path = [ident(rule) for rule in array(rules, 0, MAX_RULE_PATH)]
    return lemma_id, rule_path


def _lemmas_by_id(data):
    return {lemma["id"]: lemma for lemma in data["lemmas"]}


def _rules_by_id(data):
    return {rule["id"]: rule for rule in data["rules"]}


def _find_form(forms, lemma_id, rule_path):
    for form in forms:
        if form["lemma"] == lemma_id and form["rules"] == rule_path:
            return form
    return None


def _trace_positions(data, forms, lemma_id, rule_path):
    """Positions of one produced analysis, or ``None`` if not produced."""
    form = _find_form(forms, lemma_id, rule_path)
    if form is None:
        return None
    segments = _surface_segments(
        _lemmas_by_id(data), _rules_by_id(data), lemma_id, rule_path
    )
    return form, _position_sources(segments, form["surface"])


# --------------------------------------------------------------------------- #
# trace_analysis
# --------------------------------------------------------------------------- #

def trace_analysis(data, lemma, rules):
    """Per-position original sources of one produced analysis.

    ``data`` is the complete five-field dictionary; it is compiled in full
    first, so a dictionary the original compiler rejects raises
    ``ValueError`` even when the requested path itself is walkable.
    ``lemma`` and ``rules`` select the analysis by identity: the empty path
    and paths accepted at an intermediate state are both selectable, another
    analysis with the same surface and features is never substituted, and
    ``queries`` play no role.  Raises :class:`AnalysisNotFoundError` when the
    identity is not produced.

    Returns ``{"analysis": ..., "positions": ...}`` where ``analysis`` is the
    compiled form row (lemma, surface, features, rules) and ``positions``
    lists, per final surface code point in order, ``index``, ``char`` and the
    deduplicated ``sources``.  A source is ``{"lemma": id, "field": "stem",
    "index": i}`` or ``{"rule": id, "field": "prefix"|"suffix", "index": i}``
    with ``i`` a zero-based code point index into the original input field.
    """
    lemma_id, rule_path = _parse_identity(lemma, rules)
    forms = process(data)["forms"]
    traced = _trace_positions(data, forms, lemma_id, rule_path)
    if traced is None:
        raise AnalysisNotFoundError(lemma_id, rule_path)
    form, positions = traced
    return {
        "analysis": {
            "lemma": form["lemma"],
            "surface": form["surface"],
            "features": dict(form["features"]),
            "rules": list(form["rules"]),
        },
        "positions": [
            {
                "index": index,
                "char": char,
                "sources": [_source_dict(source) for source in sorted(sources)],
            }
            for index, (char, sources) in enumerate(zip(form["surface"], positions))
        ],
    }


# --------------------------------------------------------------------------- #
# apply_errata
# --------------------------------------------------------------------------- #

def _confirmed_surface(value):
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_SURFACE:
        raise ValueError("confirmed surface must be 1..64 code points")
    if any(unicodedata.category(char) in _CONTROL_OR_SURROGATE for char in value):
        raise ValueError("confirmed surface has a control or surrogate code point")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError("confirmed surface must be the complete NFC surface")
    return value


def _parse_requests(errata):
    """Validate the batch structurally; keep the original request index."""
    requests = []
    for index, item in enumerate(array(errata, 0, MAX_ERRATA)):
        fields(item, ("lemma", "rules", "start", "end", "replacement", "confirmed"))
        lemma_id, rule_path = _parse_identity(item["lemma"], item["rules"])
        start = integer(item["start"], 0, MAX_SURFACE)
        end = integer(item["end"], 0, MAX_SURFACE)
        if start >= end:
            raise ValueError(f"request {index}: empty interval")
        replacement = item["replacement"]
        if not isinstance(replacement, str):
            raise ValueError(f"request {index}: replacement must be a string")
        requests.append(
            {
                "index": index,
                "lemma": lemma_id,
                "rules": rule_path,
                "start": start,
                "end": end,
                "replacement": replacement,
                "confirmed": _confirmed_surface(item["confirmed"]),
            }
        )
    identities = {(request["lemma"], tuple(request["rules"])) for request in requests}
    if len(identities) != len(requests):
        raise ValueError("duplicate analysis identity in errata batch")
    return requests


def _writable_slice(request, positions, surface_length):
    """The original-field slice one interval maps to, if safely writable."""
    start, end = request["start"], request["end"]
    if end > surface_length:
        raise ValueError(
            f"request {request['index']}: interval [{start},{end}) outside "
            f"surface of {surface_length} code points"
        )
    involved = set()
    for position in range(start, end):
        involved.update(positions[position])
    owners = {(kind, owner, field) for kind, owner, field, _ in involved}
    if len(owners) != 1:
        raise IntervalNotWritableError(
            request["index"],
            request["lemma"],
            request["rules"],
            "interval sources span more than one source field",
        )
    (kind, owner, field), = owners
    indices = sorted(index for _, _, _, index in involved)
    low, high = indices[0], indices[-1] + 1
    if indices != list(range(low, high)):
        raise IntervalNotWritableError(
            request["index"],
            request["lemma"],
            request["rules"],
            "interval sources do not form one contiguous original slice",
        )
    for position, sources in enumerate(positions):
        if start <= position < end:
            continue
        for kind2, owner2, field2, index2 in sources:
            if (kind2, owner2, field2) == (kind, owner, field) and low <= index2 < high:
                raise IntervalNotWritableError(
                    request["index"],
                    request["lemma"],
                    request["rules"],
                    f"source {field}[{index2}] also contributes to surface "
                    f"position {position} outside the interval",
                )
    return (kind, owner, field, low, high)


def _field_ref(kind, owner, field):
    return {kind: owner, "field": field}


def _slice_record(slice_key, requests):
    kind, owner, field, low, high = slice_key
    return {"start": low, "end": high, "requests": sorted(requests)}


def _group_slices(writes):
    """Writes grouped by source field, slices sorted by original offset."""
    groups = {}
    for (kind, owner, field, low, high), write in writes.items():
        key = (kind, owner, field)
        groups.setdefault(key, []).append((low, high, write))
    for slices in groups.values():
        slices.sort(key=lambda item: (item[0], item[1]))
    return groups


def _check_disjoint(writes):
    """Reject intersecting slices inside one source field."""
    for (kind, owner, field), slices in _group_slices(writes).items():
        for (low1, high1, first), (low2, high2, second) in zip(slices, slices[1:]):
            if low2 < high1:
                raise ConflictingSlicesError(
                    _field_ref(kind, owner, field),
                    [
                        {"start": low1, "end": high1, "requests": sorted(first["requests"])},
                        {"start": low2, "end": high2, "requests": sorted(second["requests"])},
                    ],
                    "intersecting source slices",
                )


def apply_errata(data, errata):
    """Deliver the complete revised dictionary for a batch of confirmed errata.

    ``data`` is the complete unmodified five-field dictionary; ``errata`` is
    a list of at most :data:`MAX_ERRATA` requests, each with exactly
    ``lemma``, ``rules``, ``start``, ``end``, ``replacement`` and
    ``confirmed``.  All slices are coordinates in the original fields before
    any modification; disjoint slices are applied jointly, so a length change
    in one replacement never shifts another slice's target.

    On success returns ``{"dictionary": ..., "writes": ...}``: the complete
    revised dictionary in the original input format (ready to hand back to
    :func:`morph_paths.process`) and the actually written source slices with
    old and new text, sorted by owner, field and offset.  The same request
    facts produce the same delivery regardless of request order.  On any
    rejection no partial revision is returned: structurally malformed input
    raises ``ValueError``, an unproduced analysis raises
    :class:`AnalysisNotFoundError`, an interval that is not one
    self-contained source slice raises :class:`IntervalNotWritableError`,
    intersecting or disagreeing slices raise :class:`ConflictingSlicesError`,
    and a jointly revised dictionary that fails the full compile or the
    surface/feature constraints raises :class:`RevisionRejectedError`.
    """
    requests = _parse_requests(errata)
    original_forms = process(data)["forms"]
    lemmas_by_id = _lemmas_by_id(data)
    rules_by_id = _rules_by_id(data)

    writes = {}
    confirmed = {}
    for request in requests:
        form = _find_form(original_forms, request["lemma"], request["rules"])
        if form is None:
            raise AnalysisNotFoundError(request["lemma"], request["rules"])
        segments = _surface_segments(
            lemmas_by_id, rules_by_id, request["lemma"], request["rules"]
        )
        positions = _position_sources(segments, form["surface"])
        slice_key = _writable_slice(request, positions, len(form["surface"]))
        existing = writes.get(slice_key)
        if existing is not None:
            if existing["replacement"] != request["replacement"]:
                raise ConflictingSlicesError(
                    _field_ref(*slice_key[:3]),
                    [_slice_record(slice_key, existing["requests"] + [request["index"]])],
                    "same source slice with different replacements",
                )
            existing["requests"].append(request["index"])
        else:
            writes[slice_key] = {
                "replacement": request["replacement"],
                "requests": [request["index"]],
            }
        identity = (request["lemma"], tuple(request["rules"]))
        confirmed[identity] = request["confirmed"]
    _check_disjoint(writes)

    revised = copy.deepcopy(data)
    revised_lemmas = _lemmas_by_id(revised)
    revised_rules = _rules_by_id(revised)
    for (kind, owner, field), slices in _group_slices(writes).items():
        target = (revised_lemmas if kind == _KIND_LEMMA else revised_rules)[owner]
        original_text = target[field]
        parts = []
        cursor = 0
        for low, high, write in slices:
            parts.append(original_text[cursor:low])
            parts.append(write["replacement"])
            cursor = high
        parts.append(original_text[cursor:])
        target[field] = "".join(parts)

    try:
        revised_forms = process(revised)["forms"]
    except ValueError as exc:
        raise RevisionRejectedError(
            f"revised dictionary does not compile: {exc}"
        ) from exc

    before = {(f["lemma"], tuple(f["rules"])): f for f in original_forms}
    after = {(f["lemma"], tuple(f["rules"])): f for f in revised_forms}
    for identity in sorted(before):
        if identity not in after:
            lemma_id, rule_path = identity
            raise RevisionRejectedError(
                "analysis is no longer produced", lemma_id, list(rule_path)
            )
    for identity in sorted(after):
        if identity not in before:
            lemma_id, rule_path = identity
            raise RevisionRejectedError(
                "revision introduces a new analysis", lemma_id, list(rule_path)
            )
    for identity in sorted(before):
        lemma_id, rule_path = identity
        old, new = before[identity], after[identity]
        if identity in confirmed:
            if new["features"] != old["features"]:
                raise RevisionRejectedError(
                    "requested analysis changed features", lemma_id, list(rule_path)
                )
            if new["surface"] != confirmed[identity]:
                raise RevisionRejectedError(
                    f"surface {new['surface']!r} does not equal the confirmed "
                    f"{confirmed[identity]!r}",
                    lemma_id,
                    list(rule_path),
                )
        elif new["surface"] != old["surface"] or new["features"] != old["features"]:
            raise RevisionRejectedError(
                "unrequested analysis changed surface or features",
                lemma_id,
                list(rule_path),
            )

    records = sorted(
        (kind, owner, field, low, high, write["replacement"])
        for (kind, owner, field, low, high), write in writes.items()
    )
    return {
        "dictionary": revised,
        "writes": [
            {
                kind: owner,
                "field": field,
                "start": low,
                "end": high,
                "old": (lemmas_by_id if kind == _KIND_LEMMA else rules_by_id)[owner][
                    field
                ][low:high],
                "new": replacement,
            }
            for kind, owner, field, low, high, replacement in records
        ],
    }
