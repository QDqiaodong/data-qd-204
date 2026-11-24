"""Correctable, incremental delivery ledger for sealed morphology usage facts.

A :class:`Ledger` is an offline, persistent workspace at one local directory.
It binds to the content identity of the complete compiled form data of one
five-field dictionary (all ``forms`` from :func:`morph_paths.process`, never
limited by ``queries``), and maintains three independent things:

* revisioned usage facts (one event stream per event id, retractions included);
* a monotonic sealing watermark ``closed_through``;
* the downstream delivery relationship: one frozen, unconfirmed package at
  most, plus the confirmed aggregate baseline.

Every mutating operation validates its whole input first and then commits
exactly one atomically replaced ``ledger.json`` descriptor.  Frozen package
payloads are content-addressed bytes written before that descriptor commit,
so re-opening after a crash or a lost return value always shows either the
complete old state or the complete new state, and every successful operation
is safely replayable.  The full contract lives in ``docs/contract.md``.
"""
import hashlib
import os
import tempfile
import unicodedata
from pathlib import Path

from .canonical import dumps_canonical, loads_canonical
from .checks import ident
from .service import process

_LEDGER_VERSION = 1
_BINDING_FILENAME = "binding.json"
_LEDGER_FILENAME = "ledger.json"
_PACKAGES_DIRNAME = "packages"
_TMP_DIRNAME = "tmp"
_CONTROL_OR_SURROGATE = ("Cc", "Cs")


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #

class LedgerError(RuntimeError):
    """Base class for ledger business conflicts and operational errors."""


class BindingMismatchError(LedgerError):
    """Reopening with a dictionary whose compiled forms differ from the binding."""

    def __init__(self, expected_id, actual_id):
        self.expected_id = expected_id
        self.actual_id = actual_id
        super().__init__(
            f"dictionary content identity mismatch: "
            f"expected {expected_id}, got {actual_id}"
        )


class LedgerIntegrityError(LedgerError):
    """Persisted workspace state is missing, malformed or inconsistent."""


class RevisionError(LedgerError):
    """Base class for revision-stream business conflicts.

    Every occurrence is located by ``event`` id and ``revision`` number.
    """

    def __init__(self, message, event, revision):
        self.event = event
        self.revision = revision
        super().__init__(f"{message} (event={event!r}, revision={revision})")


class RevisionConflictError(RevisionError):
    """An already recorded event/revision pair is retried with different content."""


class RevisionGapError(RevisionError):
    """Revisions do not start at 1 or are not consecutive per event."""


class UnknownEventError(RevisionError):
    """A retraction (or audit lookup) names an event never recorded."""


class UnboundAnalysisError(RevisionError):
    """The value analysis is not produced by the bound dictionary, or its text
    does not normalize to the analysis surface."""


class MonotonicSealError(LedgerError):
    """The sealing watermark can only advance, never move backwards."""

    def __init__(self, current, requested):
        self.current = current
        self.requested = requested
        super().__init__(
            f"closed_through cannot move backwards: {current} -> {requested}"
        )


class UnknownPackageError(LedgerError):
    """Confirmation names an unknown or stale package id."""

    def __init__(self, package_id):
        self.package_id = package_id
        super().__init__(f"unknown or stale package id: {package_id}")


# --------------------------------------------------------------------------- #
# Delivery results
# --------------------------------------------------------------------------- #

class DeliveryStatus:
    """Distinguishable outcomes of :meth:`Ledger.next_package`."""

    PACKAGE = "package"
    NO_DELIVERY = "no_delivery"


class NoDelivery:
    """Explicit result when current sealed facts equal the confirmed baseline."""

    status = DeliveryStatus.NO_DELIVERY
    package_id = None

    def __repr__(self):
        return "NoDelivery()"


class DeliveryPackage:
    """One frozen, content-addressed net-diff package.

    Attributes:
        package_id: lowercase hex SHA-256 of the canonical payload bytes.
        start_checkpoint: content hash of the confirmed baseline at freeze.
        end_checkpoint: content hash of the sealed target state at freeze.
        closed_through: sealing watermark at freeze time.
        rows: ordered net-diff rows, each
            ``{"period", "analysis", "delta"}`` with a non-zero ``delta``.
    """

    status = DeliveryStatus.PACKAGE
    __slots__ = (
        "package_id",
        "start_checkpoint",
        "end_checkpoint",
        "closed_through",
        "rows",
        "_payload",
    )

    def __init__(self, payload):
        object.__setattr__(self, "_payload", bytes(payload))
        content = loads_canonical(self._payload)
        self.package_id = hashlib.sha256(self._payload).hexdigest()
        self.start_checkpoint = content["start_checkpoint"]
        self.end_checkpoint = content["end_checkpoint"]
        self.closed_through = content["closed_through"]
        self.rows = content["rows"]

    def payload_bytes(self):
        """Return the canonical, byte-stable frozen payload."""
        return self._payload

    def to_dict(self):
        return {
            "package_id": self.package_id,
            "start_checkpoint": self.start_checkpoint,
            "end_checkpoint": self.end_checkpoint,
            "closed_through": self.closed_through,
            "rows": self.rows,
        }

    def __repr__(self):
        return f"DeliveryPackage({self.package_id!r}, rows={len(self.rows)})"


# --------------------------------------------------------------------------- #
# Structural validation (plain ValueError, distinct from business conflicts)
# --------------------------------------------------------------------------- #

def _is_bounded_text(value):
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 64
        and not any(unicodedata.category(c) in _CONTROL_OR_SURROGATE for c in value)
    )


def _require_event_id(value):
    if not _is_bounded_text(value):
        raise ValueError("event id must be 1..64 codepoints without Cc/Cs")
    return value


def _require_non_negative_int(value, label):
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _require_positive_int(value, label):
    if type(value) is not int or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _validate_analysis_struct(analysis):
    if not isinstance(analysis, dict) or set(analysis) != {
        "lemma",
        "surface",
        "features",
        "rules",
    }:
        raise ValueError("analysis must contain lemma, surface, features, rules")
    lemma = ident(analysis["lemma"])
    surface = analysis["surface"]
    if not _is_bounded_text(surface):
        raise ValueError("analysis surface must be bounded text without Cc/Cs")
    if unicodedata.normalize("NFC", surface) != surface:
        raise ValueError("analysis surface must be final NFC")
    features = analysis["features"]
    if not isinstance(features, dict) or len(features) > 20:
        raise ValueError("analysis features must be a mapping of at most 20 pairs")
    for key, value in features.items():
        ident(key)
        ident(value)
    rules = analysis["rules"]
    if not isinstance(rules, list) or not all(
        isinstance(rule, str) and ident(rule) == rule for rule in rules
    ):
        raise ValueError("analysis rules must be an ordered list of rule ids")
    return {
        "lemma": lemma,
        "surface": surface,
        "features": dict(features),
        "rules": list(rules),
    }


def _validate_value_struct(value):
    """Return the normalized value structure, or None for a retraction."""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"period", "text", "analysis"}:
        raise ValueError("non-null value must contain period, text, analysis")
    period = _require_non_negative_int(value["period"], "period")
    text = value["text"]
    if not _is_bounded_text(text):
        raise ValueError("original text must be 1..64 codepoints without Cc/Cs")
    analysis = _validate_analysis_struct(value["analysis"])
    return {"period": period, "text": text, "analysis": analysis}


def _validate_batch_struct(batch):
    if not isinstance(batch, list) or not batch:
        raise ValueError("revision batch must be a non-empty array")
    normalized = []
    seen_pairs = set()
    for item in batch:
        if not isinstance(item, dict) or set(item) != {"event", "revision", "value"}:
            raise ValueError("each revision needs exactly event, revision, value")
        event = _require_event_id(item["event"])
        revision = _require_positive_int(item["revision"], "revision")
        if (event, revision) in seen_pairs:
            raise ValueError(f"duplicate event/revision pair in batch: {event}@{revision}")
        seen_pairs.add((event, revision))
        normalized.append((event, revision, _validate_value_struct(item["value"])))
    return normalized


# --------------------------------------------------------------------------- #
# Ledger
# --------------------------------------------------------------------------- #

class Ledger:
    """Persistent usage-fact ledger rooted at a local directory.

    Construction compiles ``data`` with the full :func:`process` semantics.
    On an empty path the workspace is created and bound to the compiled form
    identity; an invalid dictionary raises before anything is created.  On an
    existing path the dictionary must produce the same forms or
    :class:`BindingMismatchError` is raised, with the stored state untouched.
    """

    def __init__(self, path, data):
        # Compile first: an invalid dictionary must never create the workspace.
        result = process(data)
        forms = result["forms"]
        forms_bytes = dumps_canonical(forms)
        dictionary_id = hashlib.sha256(forms_bytes).hexdigest()

        self.path = Path(path)
        self._tmp_dir = self.path / _TMP_DIRNAME
        self._packages_dir = self.path / _PACKAGES_DIRNAME
        self._binding_path = self.path / _BINDING_FILENAME
        self._ledger_path = self.path / _LEDGER_FILENAME

        self._forms = forms
        self.dictionary_id = dictionary_id
        self._form_index = {
            (form["lemma"], tuple(form["rules"])): form for form in forms
        }

        if not self._binding_path.exists():
            self._initialize_workspace()
        self._reconcile()

    # -- durable helpers --------------------------------------------------- #

    def _atomic_write(self, target, payload):
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(
            dir=self._tmp_dir, prefix="." + target.name + "-", suffix=".tmp"
        )
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def _write_json(self, target, obj):
        self._atomic_write(Path(target), dumps_canonical(obj))

    def _read_json(self, target):
        try:
            raw = Path(target).read_bytes()
        except FileNotFoundError as exc:
            raise LedgerIntegrityError(f"missing workspace file: {target}") from exc
        try:
            return loads_canonical(raw)
        except ValueError as exc:
            raise LedgerIntegrityError(f"corrupt workspace file: {target}") from exc

    def _initialize_workspace(self):
        self.path.mkdir(parents=True, exist_ok=True)
        self._tmp_dir.mkdir(exist_ok=True)
        self._packages_dir.mkdir(exist_ok=True)
        binding = {
            "version": _LEDGER_VERSION,
            "dictionary_id": self.dictionary_id,
            "forms": self._forms,
        }
        self._write_json(self._binding_path, binding)
        self._write_json(self._ledger_path, self._initial_state())

    def _initial_state(self):
        return {
            "version": _LEDGER_VERSION,
            "dictionary_id": self.dictionary_id,
            "state_version": 0,
            "closed_through": None,
            "events": [],
            "baseline": [],
            "pending_package_id": None,
            "last_confirmed_package_id": None,
        }

    def _reconcile(self):
        binding = self._read_json(self._binding_path)
        if not isinstance(binding, dict) or binding.get("version") != _LEDGER_VERSION:
            raise LedgerIntegrityError("unsupported binding descriptor")
        if binding.get("dictionary_id") != self.dictionary_id:
            raise BindingMismatchError(binding["dictionary_id"], self.dictionary_id)
        stored_forms = binding.get("forms")
        if not isinstance(stored_forms, list) or stored_forms != self._forms:
            raise LedgerIntegrityError("binding forms do not match their identity")
        if hashlib.sha256(dumps_canonical(stored_forms)).hexdigest() != \
                self.dictionary_id:
            raise LedgerIntegrityError("binding forms do not match their identity")
        state = self._read_json(self._ledger_path)
        self._validate_state_shape(state)
        if state["dictionary_id"] != self.dictionary_id:
            raise LedgerIntegrityError("ledger bound to a different dictionary")
        pending_id = state["pending_package_id"]
        if pending_id is not None:
            payload = self._package_payload(pending_id)
            if hashlib.sha256(payload).hexdigest() != pending_id:
                raise LedgerIntegrityError("frozen package content does not match id")
        self._prune_orphan_packages(state)
        self._state = state

    def _validate_state_shape(self, state):
        required = {
            "version",
            "dictionary_id",
            "state_version",
            "closed_through",
            "events",
            "baseline",
            "pending_package_id",
            "last_confirmed_package_id",
        }
        if not isinstance(state, dict) or set(state) != required:
            raise LedgerIntegrityError("ledger descriptor has unexpected shape")
        if state["version"] != _LEDGER_VERSION:
            raise LedgerIntegrityError("unsupported ledger version")

    def _prune_orphan_packages(self, state):
        if not self._packages_dir.exists():
            return
        live = state["pending_package_id"]
        for entry in self._packages_dir.iterdir():
            if entry.is_file() and entry.name != f"{live}.json":
                entry.unlink()

    def _package_path(self, package_id):
        return self._packages_dir / f"{package_id}.json"

    def _package_payload(self, package_id):
        path = self._package_path(package_id)
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise LedgerIntegrityError(
                f"frozen package {package_id} is missing"
            ) from exc

    def _commit(self, new_state, kind):
        """Single descriptor commit point shared by every mutating operation."""
        before = globals().get("_CRASH_BEFORE_COMMIT")
        if before is not None:
            before(kind, self)
        self._write_json(self._ledger_path, new_state)
        after = globals().get("_CRASH_AFTER_COMMIT")
        if after is not None:
            after(kind, self)
        self._state = new_state
        self._prune_orphan_packages(new_state)

    # -- event storage helpers --------------------------------------------- #

    def _events_map(self):
        return {entry["event"]: entry for entry in self._state["events"]}

    def _bound_form(self, analysis):
        form = self._form_index.get(
            (analysis["lemma"], tuple(analysis["rules"]))
        )
        if form is None:
            return None
        if form["surface"] != analysis["surface"] or form["features"] != analysis["features"]:
            return None
        return form

    # -- writes ------------------------------------------------------------- #

    def write_revisions(self, batch):
        """Validate and atomically apply one revision batch.

        The whole batch is checked before any state changes: structural
        problems raise plain ``ValueError``; business conflicts raise
        :class:`RevisionError` subclasses located by event and revision.
        Identical replays of already recorded event/revision pairs are safe
        no-ops and do not advance ``state_version``.
        """
        items = _validate_batch_struct(batch)
        events = self._events_map()

        # Deterministic validation order regardless of the input array order.
        ordered = sorted(items, key=lambda triple: (triple[0], triple[1]))
        per_event = {}
        for event, revision, value in ordered:
            per_event.setdefault(event, []).append((revision, value))

        new_revisions = []
        replayed = 0
        for event, stream in per_event.items():
            entry = events.get(event)
            latest = entry["revisions"][-1]["revision"] if entry is not None else 0
            incoming = sorted(revision for revision, _ in stream)
            new_revision_numbers = [r for r in incoming if r > latest]
            expected_new = list(range(latest + 1, latest + 1 + len(new_revision_numbers)))
            if new_revision_numbers != expected_new:
                raise RevisionGapError(
                    "revisions must extend the event stream consecutively",
                    event,
                    incoming[0],
                )
            # Advances as this same batch introduces the event's earlier
            # revisions, so a later retraction in the same batch is legal.
            event_exists = entry is not None
            for revision, value in stream:
                if entry is not None and revision <= latest:
                    recorded = next(
                        r for r in entry["revisions"] if r["revision"] == revision
                    )
                    if recorded["value"] != value:
                        raise RevisionConflictError(
                            "same event/revision replayed with different content",
                            event,
                            revision,
                        )
                    replayed += 1
                    continue
                if value is None:
                    if not event_exists:
                        raise UnknownEventError(
                            "retraction requires an already recorded event",
                            event,
                            revision,
                        )
                else:
                    form = self._bound_form(value["analysis"])
                    if form is None:
                        raise UnboundAnalysisError(
                            "analysis is not produced by the bound dictionary",
                            event,
                            revision,
                        )
                    if unicodedata.normalize("NFC", value["text"]) != form["surface"]:
                        raise UnboundAnalysisError(
                            "NFC of original text differs from the analysis surface",
                            event,
                            revision,
                        )
                event_exists = True
                new_revisions.append((event, revision, value))

        if not new_revisions:
            return {
                "accepted_new": 0,
                "replayed": replayed,
                "state_version": self._state["state_version"],
            }

        new_state = self._clone_state()
        new_events = {entry["event"]: entry for entry in new_state["events"]}
        for event, revision, value in new_revisions:
            entry = new_events.get(event)
            record = {"revision": revision, "value": value}
            if entry is None:
                entry = {"event": event, "revisions": [record]}
                new_events[event] = entry
            else:
                entry["revisions"].append(record)
        new_state["events"] = [
            new_events[key] for key in sorted(new_events)
        ]
        for entry in new_state["events"]:
            entry["revisions"].sort(key=lambda r: r["revision"])
        new_state["state_version"] += 1
        self._commit(new_state, "write")
        return {
            "accepted_new": len(new_revisions),
            "replayed": replayed,
            "state_version": new_state["state_version"],
        }

    # -- sealing ------------------------------------------------------------ #

    def seal(self, period):
        """Advance ``closed_through`` monotonically; equal periods are replays."""
        _require_non_negative_int(period, "period")
        current = self._state["closed_through"]
        if current is not None and period < current:
            raise MonotonicSealError(current, period)
        if current == period:
            return {"closed_through": current, "advanced": False,
                    "state_version": self._state["state_version"]}
        new_state = self._clone_state()
        new_state["closed_through"] = period
        new_state["state_version"] += 1
        self._commit(new_state, "seal")
        return {"closed_through": period, "advanced": True,
                "state_version": new_state["state_version"]}

    # -- fact queries ------------------------------------------------------- #

    def _live_values(self):
        """Yield (event, latest value) for every non-retracted event."""
        for entry in self._state["events"]:
            value = entry["revisions"][-1]["value"]
            if value is not None:
                yield entry["event"], value

    def _aggregate(self, values):
        counts = {}
        for _event, value in values:
            analysis = value["analysis"]
            key = (value["period"], analysis["lemma"], tuple(analysis["rules"]))
            counts[key] = counts.get(key, 0) + 1
        ordered = sorted(
            counts,
            key=lambda key: (
                key[0],
                self._form_index[(key[1], key[2])]["surface"],
                key[1],
                list(key[2]),
            ),
        )
        return [
            {
                "period": period,
                "analysis": dict(self._form_index[(lemma, rules)]),
                "count": counts[(period, lemma, rules)],
            }
            for period, lemma, rules in ordered
        ]

    def current_facts(self):
        """Aggregate every live fact, including open (unsealed) periods."""
        return self._aggregate(list(self._live_values()))

    def sealed_facts(self):
        """Aggregate live facts whose period is at or below the watermark."""
        watermark = self._state["closed_through"]
        if watermark is None:
            return []
        values = [
            (event, value)
            for event, value in self._live_values()
            if value["period"] <= watermark
        ]
        return self._aggregate(values)

    def confirmed_facts(self):
        """Return the downstream confirmed baseline (aggregate rows)."""
        return [dict(row) for row in self._state["baseline"]]

    def get_event(self, event_id):
        """Return the latest recorded revision (with original spelling) for audit."""
        entry = self._events_map().get(event_id)
        if entry is None:
            raise UnknownEventError("event never recorded", event_id, None)
        latest = entry["revisions"][-1]
        return {
            "event": event_id,
            "revision": latest["revision"],
            "value": None if latest["value"] is None else dict(latest["value"]),
        }

    # -- delivery ----------------------------------------------------------- #

    def _checkpoint(self, rows):
        return hashlib.sha256(dumps_canonical(rows)).hexdigest()

    def _baseline_map(self):
        result = {}
        for row in self._state["baseline"]:
            analysis = row["analysis"]
            key = (row["period"], analysis["lemma"], tuple(analysis["rules"]))
            result[key] = row["count"]
        return result

    def _net_diff_rows(self, sealed_rows, baseline_rows):
        current_map = {
            (row["period"], row["analysis"]["lemma"],
             tuple(row["analysis"]["rules"])): row["count"]
            for row in sealed_rows
        }
        baseline_map = {
            (row["period"], row["analysis"]["lemma"],
             tuple(row["analysis"]["rules"])): row["count"]
            for row in baseline_rows
        }
        keys = sorted(
            current_map.keys() | baseline_map.keys(),
            key=lambda key: (
                key[0],
                self._form_index[(key[1], key[2])]["surface"],
                key[1],
                list(key[2]),
            ),
        )
        rows = []
        for key in keys:
            delta = current_map.get(key, 0) - baseline_map.get(key, 0)
            if delta == 0:
                continue
            period, lemma, rules = key
            rows.append({
                "period": period,
                "analysis": dict(self._form_index[(lemma, rules)]),
                "delta": delta,
            })
        return rows

    def next_package(self):
        """Return the frozen pending package, or freeze the next net diff.

        Repeated calls while a package is unconfirmed return byte-identical
        payloads; facts or seals arriving in between only affect later
        packages.  An empty net diff returns :class:`NoDelivery` and changes
        no state and no checkpoint.
        """
        pending_id = self._state["pending_package_id"]
        if pending_id is not None:
            return DeliveryPackage(self._package_payload(pending_id))

        sealed_rows = self.sealed_facts()
        baseline_rows = self._state["baseline"]
        diff_rows = self._net_diff_rows(sealed_rows, baseline_rows)
        if not diff_rows:
            return NoDelivery()

        payload_content = {
            "version": _LEDGER_VERSION,
            "start_checkpoint": self._checkpoint(baseline_rows),
            "end_checkpoint": self._checkpoint(sealed_rows),
            "closed_through": self._state["closed_through"],
            "rows": diff_rows,
        }
        payload = dumps_canonical(payload_content)
        package_id = hashlib.sha256(payload).hexdigest()

        # Freeze the content-addressed blob before the descriptor commit.
        self._atomic_write(self._package_path(package_id), payload)

        new_state = self._clone_state()
        new_state["pending_package_id"] = package_id
        new_state["state_version"] += 1
        self._commit(new_state, "freeze")
        return DeliveryPackage(payload)

    def confirm(self, package_id):
        """Apply the pending package to the downstream baseline.

        Confirming the current pending id is replay-safe.  Unknown or stale
        ids raise :class:`UnknownPackageError` and change nothing.
        """
        if not isinstance(package_id, str):
            raise ValueError("package id must be a string")
        pending_id = self._state["pending_package_id"]
        if pending_id is None:
            if package_id == self._state["last_confirmed_package_id"]:
                return {
                    "confirmed": package_id,
                    "replayed": True,
                    "state_version": self._state["state_version"],
                }
            raise UnknownPackageError(package_id)
        if package_id != pending_id:
            raise UnknownPackageError(package_id)

        package = DeliveryPackage(self._package_payload(pending_id))
        baseline_map = self._baseline_map()
        for row in package.rows:
            key = (row["period"], row["analysis"]["lemma"],
                   tuple(row["analysis"]["rules"]))
            baseline_map[key] = baseline_map.get(key, 0) + row["delta"]
            if baseline_map[key] == 0:
                del baseline_map[key]
        new_baseline = [
            {
                "period": period,
                "analysis": dict(self._form_index[(lemma, rules)]),
                "count": count,
            }
            for (period, lemma, rules), count in sorted(
                baseline_map.items(),
                key=lambda item: (
                    item[0][0],
                    self._form_index[(item[0][1], item[0][2])]["surface"],
                    item[0][1],
                    list(item[0][2]),
                ),
            )
        ]

        new_state = self._clone_state()
        new_state["baseline"] = new_baseline
        new_state["pending_package_id"] = None
        new_state["last_confirmed_package_id"] = package_id
        new_state["state_version"] += 1
        self._commit(new_state, "confirm")
        return {
            "confirmed": package_id,
            "replayed": False,
            "state_version": new_state["state_version"],
        }

    # -- state view --------------------------------------------------------- #

    def _clone_state(self):
        return loads_canonical(dumps_canonical(self._state))

    def state(self):
        """Plain-data description of the durable workspace state."""
        state = self._state
        return {
            "dictionary_id": state["dictionary_id"],
            "state_version": state["state_version"],
            "closed_through": state["closed_through"],
            "events": sorted(entry["event"] for entry in state["events"]),
            "pending_package_id": state["pending_package_id"],
            "last_confirmed_package_id": state["last_confirmed_package_id"],
            "baseline_rows": len(state["baseline"]),
        }


# Crash-injection hooks, used by the failure-injection tests only.
# Each hook receives (kind, ledger) where kind is one of
# "write", "seal", "freeze", "confirm".
_CRASH_BEFORE_COMMIT = None
_CRASH_AFTER_COMMIT = None
