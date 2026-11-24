"""Offline rebasing of editorial adaptation layers onto a new parent dictionary.

A layer is an ordered, small adaptation on top of one finite morphology
dictionary: it *adds* a complete five-field dictionary of extra analyses and
*suppresses* selected analyses produced by an earlier source layer.  When the
parent dictionary changes, the same original layer facts are replayed against
the new parent; the result is a fresh effective analysis set plus per-operation
conflict states and layer records that can be fed straight back into the next
rebase call.

The effective view is always built from the compiled forms of the complete
dictionaries (never limited by ``queries``) using the existing
:func:`morph_paths.process` compiler.  Analysis identity is
``(source_layer, lemma, ordered rule path)``: two analyses that merely share a
surface or a feature set are never merged, and the same ``(lemma, rules)``
produced by two different layers stays two analyses.

Nothing in this module mutates its arguments, publishes a dictionary or keeps
state.  The full contract lives in ``docs/contract.md``.
"""
import copy
import hashlib
import re
import unicodedata

from .canonical import dumps_canonical
from .checks import array, ident
from .model import features as feature_map
from .service import process

MAX_LAYERS = 32
MAX_TOTAL_ANALYSES = 20000
MAX_TOTAL_SUPPRESSIONS = 20000
MAX_RULE_PATH = 300
MAX_SURFACE = 64

BASE_SOURCE = "base"
POLICIES = ("snapshot", "follow")

STATUS_APPLIED = "applied"
STATUS_CHANGED = "changed"
STATUS_MISSING = "missing"
STATUS_ALREADY_HIDDEN = "already_hidden"

_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_CONTROL_OR_SURROGATE = ("Cc", "Cs")
_SOURCE_KEYS = ("source_layer", "source")
_POLICY_KEYS = ("policy", "mode", "strategy")
_ROW_CONTAINERS = ("target", "analysis")
_LAYER_KEYS = {"layer_id", "base_id", "additions", "suppressions"}


class RebaseError(ValueError):
    """A whole layer set or dictionary is rejected.

    Structural problems with individual values also raise :class:`ValueError`
    (this class is its subclass); the distinction lets callers recognise a
    rebase-specific rejection.
    """


class RebaseResult(dict):
    """Success mapping with the five documented keys.

    The canonical keys are ``forms``, ``suppressions``, ``old_base_id``,
    ``new_base_id`` and ``rebased_layers``.  A few plainly synonymous lookups
    (``analyses``, ``operations``, ``old_identity``, ``new_identity``,
    ``layers`` ...) resolve to the same values without adding keys, so callers
    that only read the result are never surprised by extra entries in the
    returned object or its JSON form.
    """

    _ALIASES = {
        "analyses": "forms",
        "effective_forms": "forms",
        "effective_analyses": "forms",
        "rows": "forms",
        "operations": "suppressions",
        "suppression_results": "suppressions",
        "old_identity": "old_base_id",
        "old_id": "old_base_id",
        "old_base_identity": "old_base_id",
        "new_identity": "new_base_id",
        "new_id": "new_base_id",
        "new_base_identity": "new_base_id",
        "layers": "rebased_layers",
    }

    def _canonical(self, key):
        return self._ALIASES.get(key, key)

    def __getitem__(self, key):
        return super().__getitem__(self._canonical(key))

    def get(self, key, default=None):
        return super().get(self._canonical(key), default)

    def __contains__(self, key):
        return super().__contains__(self._canonical(key))


# --------------------------------------------------------------------------- #
# Compilation and identity
# --------------------------------------------------------------------------- #

def _compile(data, label):
    """Compile the complete dictionary, rejecting it as a whole on failure."""
    try:
        return process(data)["forms"]
    except ValueError as exc:
        raise RebaseError(f"{label} does not compile: {exc}") from exc


def forms_identity(forms):
    """SHA-256 of the canonical JSON of complete compiled ``forms``."""
    return hashlib.sha256(dumps_canonical(forms)).hexdigest()


def _identity_key(lemma, rules):
    return (lemma, tuple(rules))


def _form_index(forms):
    """Map ``(lemma, ordered rules)`` to its compiled row."""
    return {_identity_key(form["lemma"], form["rules"]): form for form in forms}


# --------------------------------------------------------------------------- #
# Input validation
# --------------------------------------------------------------------------- #

def _digest(value, label):
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise RebaseError(f"{label} must be a lowercase hex SHA-256 digest")
    return value


def _layer_id(value):
    layer_id = ident(value)
    if layer_id == BASE_SOURCE:
        raise RebaseError("layer_id 'base' is reserved for the parent dictionary")
    return layer_id


def _surface(value):
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_SURFACE:
        raise ValueError("snapshot surface must be 1..64 code points")
    if any(unicodedata.category(char) in _CONTROL_OR_SURROGATE for char in value):
        raise ValueError("snapshot surface has a control or surrogate code point")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError("snapshot surface must be the complete NFC surface")
    return value


def _alias(mapping, names, label):
    """Exactly one accepted spelling of one semantic key."""
    present = [name for name in names if name in mapping]
    if len(present) != 1:
        raise ValueError(f"{label}: exactly one of {list(names)} required")
    return present[0]


def _operation_fields(op, label):
    """Return ``(policy, source, row_mapping)`` for one operation.

    The policy sits on the operation.  The source layer and analysis row may
    sit directly on the operation, or be grouped under one container object
    (``"target"`` or ``"analysis"``); either level may carry the source key.
    Unknown or duplicate fields are rejected.
    """
    if not isinstance(op, dict):
        raise ValueError(f"{label}: object required")
    policy_key = _alias(op, _POLICY_KEYS, label)
    policy = op[policy_key]
    if policy not in POLICIES:
        raise ValueError(f"{label}: policy must be 'snapshot' or 'follow'")

    containers = [name for name in _ROW_CONTAINERS if name in op]
    if len(containers) > 1:
        raise ValueError(f"{label}: at most one of {list(_ROW_CONTAINERS)} allowed")
    if containers:
        container = containers[0]
        if set(op) - {policy_key, container} - set(_SOURCE_KEYS):
            raise ValueError(
                f"{label}: unexpected fields "
                f"{sorted(set(op) - {policy_key, container} - set(_SOURCE_KEYS))}"
            )
        inner = op[container]
        if not isinstance(inner, dict):
            raise ValueError(f"{label}: {container} must be an object")
        if any(key in inner for key in _POLICY_KEYS) or any(
            name in inner for name in _ROW_CONTAINERS
        ):
            raise ValueError(f"{label}: {container} must not nest policy or rows")
        outer = {key: op[key] for key in op if key != container}
    else:
        inner = {}
        outer = op

    source_keys = [key for key in _SOURCE_KEYS if key in outer or key in inner]
    if len(source_keys) != 1:
        raise ValueError(f"{label}: exactly one of {list(_SOURCE_KEYS)} required")
    source_key = source_keys[0]
    if source_key in outer and source_key in inner:
        raise ValueError(f"{label}: duplicate source field")

    row = dict(inner)
    source = inner.get(source_key, outer.get(source_key))
    row.pop(source_key, None)
    for key, value in outer.items():
        if key in (policy_key, source_key):
            continue
        if key in row:
            raise ValueError(f"{label}: duplicate field {key!r}")
        row[key] = value

    expected = {"lemma", "rules"}
    if policy == "snapshot":
        expected |= {"surface", "features"}
    if set(row) != expected:
        raise ValueError(f"{label}: expected exactly {sorted(expected)}")
    return policy, source, row


def _operation(op, layer_id, position, known_sources, seen_targets):
    """Validate one suppression operation and return its parsed facts."""
    label = f"layer {layer_id!r} operation {position}"
    policy, source, row = _operation_fields(op, label)

    if not isinstance(source, str):
        raise ValueError(f"{label}: invalid source layer")
    if source != BASE_SOURCE:
        source = ident(source)
    if source not in known_sources:
        raise ValueError(
            f"{label}: source {source!r} is neither the parent dictionary nor "
            f"an earlier layer"
        )

    lemma = ident(row["lemma"])
    rules = [ident(rule) for rule in array(row["rules"], 0, MAX_RULE_PATH)]
    target_key = (source, lemma, tuple(rules))
    if target_key in seen_targets:
        raise ValueError(
            f"{label}: duplicate suppression target "
            f"({source!r}, {lemma!r}, {rules!r}) in one layer"
        )
    seen_targets.add(target_key)

    parsed = {
        "layer_id": layer_id,
        "source": source,
        "policy": policy,
        "lemma": lemma,
        "rules": rules,
        "position": position,
    }
    if policy == "snapshot":
        parsed["surface"] = _surface(row["surface"])
        parsed["features"] = feature_map(row["features"])
    return parsed


def _parse_layers(layers, old_identity):
    """Validate layer structure, bindings, sources and operation counts."""
    parsed_layers = []
    seen_ids = set()
    known_sources = {BASE_SOURCE}
    total_operations = 0
    for position, layer in enumerate(array(layers, 0, MAX_LAYERS)):
        if not isinstance(layer, dict):
            raise ValueError(f"layer {position}: object required")
        if set(layer) != _LAYER_KEYS:
            raise ValueError(
                f"layer {position}: expected exactly layer_id, base_id, "
                f"additions, suppressions"
            )
        layer_id = _layer_id(layer["layer_id"])
        if layer_id in seen_ids:
            raise RebaseError(f"duplicate layer_id {layer_id!r}")
        seen_ids.add(layer_id)
        base_id = _digest(layer["base_id"], f"layer {layer_id!r} base_id")
        if base_id != old_identity:
            raise RebaseError(
                f"layer {layer_id!r} is bound to {base_id}, but old_base is "
                f"{old_identity}"
            )
        if not isinstance(layer["additions"], dict):
            raise ValueError(f"layer {layer_id!r}: additions must be a dictionary")
        additions_forms = _compile(
            layer["additions"], f"layer {layer_id!r} additions"
        )
        suppressions = array(layer["suppressions"], 0, MAX_TOTAL_SUPPRESSIONS)
        total_operations += len(suppressions)
        if total_operations > MAX_TOTAL_SUPPRESSIONS:
            raise RebaseError(
                f"suppression operation limit of {MAX_TOTAL_SUPPRESSIONS} exceeded"
            )
        seen_targets = set()
        operations = [
            _operation(op, layer_id, index, known_sources, seen_targets)
            for index, op in enumerate(suppressions)
        ]
        parsed_layers.append(
            {
                "layer_id": layer_id,
                "additions": layer["additions"],
                "suppressions": layer["suppressions"],
                "additions_forms": additions_forms,
                "additions_index": _form_index(additions_forms),
                "operations": operations,
            }
        )
        known_sources.add(layer_id)
    return parsed_layers


# --------------------------------------------------------------------------- #
# Original-target legality
# --------------------------------------------------------------------------- #

def _validate_targets(parsed_layers, old_base_index, new_base_index):
    """Reject targets their declared source never produces.

    Legality is judged by stable identity ``(lemma, ordered rules)``; a same
    surface or same feature set is never substituted.  A parent target is
    admitted when either parent version produces the identity: the old parent
    confirms the operation was written against a real analysis, while the new
    parent keeps a preserved operation replayable after its analysis
    transiently disappeared and came back.

    A ``snapshot`` that carries a well-formed row but no longer equals the
    source's current row is *not* a validation error: the contract makes that
    the ``changed`` conflict, and the operation must stay replayable through
    later rebases.  A structurally damaged snapshot (bad fields, non-NFC
    surface, malformed feature mapping) is rejected during parsing, and an
    identity the source never produced -- a fabricated target -- is rejected
    here.
    """
    by_id = {layer["layer_id"]: layer for layer in parsed_layers}
    for layer in parsed_layers:
        for operation in layer["operations"]:
            source = operation["source"]
            key = _identity_key(operation["lemma"], operation["rules"])
            if source == BASE_SOURCE:
                produced = key in old_base_index or key in new_base_index
            else:
                produced = key in by_id[source]["additions_index"]
            if not produced:
                raise RebaseError(
                    f"layer {layer['layer_id']!r} operation "
                    f"{operation['position']}: source {source!r} does not "
                    f"produce analysis ({operation['lemma']!r}, "
                    f"{operation['rules']!r})"
                )


# --------------------------------------------------------------------------- #
# Rebase
# --------------------------------------------------------------------------- #

def _copy_analysis(row, source_layer):
    return {
        "lemma": row["lemma"],
        "surface": row["surface"],
        "features": dict(row["features"]),
        "rules": list(row["rules"]),
        "source_layer": source_layer,
    }


def _same_full_row(row, operation):
    return (
        row["lemma"] == operation["lemma"]
        and row["surface"] == operation["surface"]
        and row["rules"] == operation["rules"]
        and row["features"] == operation["features"]
    )


def _operation_record(operation, status):
    target = {
        "source_layer": operation["source"],
        "lemma": operation["lemma"],
        "rules": list(operation["rules"]),
    }
    if operation["policy"] == "snapshot":
        target["surface"] = operation["surface"]
        target["features"] = dict(operation["features"])
    return {
        "layer_id": operation["layer_id"],
        "index": operation["position"],
        "policy": operation["policy"],
        "target": target,
        "status": status,
    }


def _run_operations(layer, rows, visible, records):
    for operation in layer["operations"]:
        key = (
            operation["source"],
            operation["lemma"],
            tuple(operation["rules"]),
        )
        row = rows.get(key)
        if row is None:
            status = STATUS_MISSING
        elif not visible[key]:
            # An earlier operation already hid this identity; this operation is
            # not a fresh successful deletion of some other row.
            status = STATUS_ALREADY_HIDDEN
        elif operation["policy"] == "follow":
            visible[key] = False
            status = STATUS_APPLIED
        elif _same_full_row(row, operation):
            visible[key] = False
            status = STATUS_APPLIED
        else:
            # Same (lemma, rules) identity, but the full row moved; keep the
            # new row visible and leave the operation for a later parent.
            status = STATUS_CHANGED
        records.append(_operation_record(operation, status))


def rebase_layers(old_base, new_base, layers):
    """Replay ordered adaptation layers from ``old_base`` onto ``new_base``.

    ``old_base`` and ``new_base`` are complete five-field dictionaries; both
    are compiled in full with the ``process`` semantics (``queries`` select
    nothing).  ``layers`` is an ordered array of at most 32 layer objects,
    each with exactly ``layer_id``, ``base_id``, ``additions`` and
    ``suppressions``.  Every input layer must carry the content identity of
    ``old_base``'s complete canonical ``forms`` (the ``old_base_id`` and the
    ``base_id`` of :func:`rebase_layers` output).

    Each suppression names an earlier source layer (``"base"`` for the parent
    dictionary or an earlier ``layer_id``) and one policy:

    * ``snapshot``: the target carries the full analysis row (``lemma``, final
      NFC ``surface``, complete ``features``, ordered ``rules``).  It hides
      that row only when the row is still exactly identical.
    * ``follow``: the target carries ``lemma`` and ordered ``rules``.  It hides
      the current full row of that stable identity even when the parent has
      changed its surface or features; it never guesses a replacement.

    The source layer and analysis row may sit either directly on the operation
    or together under one ``"target"``/``"analysis"`` object.  Layers replay in
    array order, and each layer's additions are joined after its suppressions.
    Analyses are keyed by ``(source_layer, lemma, ordered rules)``;
    same-surface or same-feature analyses are never merged and different
    layers never collapse.

    Returns exactly five keys:

    * ``forms``: the effective analyses, sorted by final NFC ``surface``,
      ``source_layer``, ``lemma`` and ``rules``; each row is the complete
      compiled analysis plus ``source_layer``.
    * ``suppressions``: one record per operation in layer order with its
      ``layer_id``, ``index``, ``policy``, ``target`` identity and a status of
      ``applied``, ``changed``, ``missing`` or ``already_hidden``.
    * ``old_base_id`` / ``new_base_id``: SHA-256 of the canonical JSON of each
      parent's complete compiled ``forms``.
    * ``rebased_layers``: the same layers in the same order, with
      ``layer_id``, ``additions`` and ``suppressions`` preserved exactly and
      ``base_id`` updated to ``new_base_id`` so the records can be passed
      directly as the next call's ``layers``.

    Non-applied operations stay in ``rebased_layers`` as observable conflict
    intent; a later parent version that produces the target again makes them
    effective again.  A target the declared source never produces, an unknown
    or forward source, duplicate layer ids or same-layer targets, a wrong
    binding, an uncompilable dictionary or an exceeded total limit rejects the
    whole call with :class:`ValueError` (or its subclass :class:`RebaseError`)
    and returns no partial result.  Inputs are never mutated.
    """
    old_forms = _compile(old_base, "old_base")
    new_forms = _compile(new_base, "new_base")
    old_identity = forms_identity(old_forms)
    new_identity = forms_identity(new_forms)

    parsed_layers = _parse_layers(layers, old_identity)

    total_analyses = len(old_forms) + len(new_forms) + sum(
        len(layer["additions_forms"]) for layer in parsed_layers
    )
    if total_analyses > MAX_TOTAL_ANALYSES:
        raise RebaseError(
            f"compiled analysis limit of {MAX_TOTAL_ANALYSES} exceeded: "
            f"{total_analyses}"
        )

    _validate_targets(
        parsed_layers, _form_index(old_forms), _form_index(new_forms)
    )

    rows = {}
    visible = {}
    for form in new_forms:
        key = (BASE_SOURCE, form["lemma"], tuple(form["rules"]))
        rows[key] = _copy_analysis(form, BASE_SOURCE)
        visible[key] = True

    suppression_records = []
    for layer in parsed_layers:
        _run_operations(layer, rows, visible, suppression_records)
        for form in layer["additions_forms"]:
            key = (layer["layer_id"], form["lemma"], tuple(form["rules"]))
            rows[key] = _copy_analysis(form, layer["layer_id"])
            visible[key] = True

    forms = [rows[key] for key in rows if visible[key]]
    forms.sort(
        key=lambda row: (
            row["surface"],
            row["source_layer"],
            row["lemma"],
            tuple(row["rules"]),
        )
    )

    rebased_layers = [
        {
            "layer_id": layer["layer_id"],
            "base_id": new_identity,
            "additions": copy.deepcopy(layer["additions"]),
            "suppressions": copy.deepcopy(layer["suppressions"]),
        }
        for layer in parsed_layers
    ]

    return RebaseResult(
        forms=forms,
        suppressions=suppression_records,
        old_base_id=old_identity,
        new_base_id=new_identity,
        rebased_layers=rebased_layers,
    )
