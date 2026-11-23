"""Canonical JSON bytes used by release artifacts.

Canonical form: UTF-8, object keys sorted, non-ASCII never escaped, no
insignificant whitespace and no trailing newline.  Parsing additionally rejects
duplicate object keys and non-finite constants.
"""
import json


def dumps_canonical(obj):
    """Return the canonical byte representation of ``obj``."""
    return json.dumps(
        obj,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _pairs_no_duplicates(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError(f"nonfinite JSON value: {value}")


def loads_canonical(raw):
    """Strictly parse JSON bytes/str, rejecting duplicate keys."""
    if isinstance(raw, bytearray):
        raw = bytes(raw)
    if not isinstance(raw, (bytes, str)):
        raise ValueError("JSON payload must be bytes or str")
    if isinstance(raw, bytes):
        text = raw.decode("utf-8")
    else:
        text = raw
    return json.loads(
        text,
        object_pairs_hook=_pairs_no_duplicates,
        parse_constant=_reject_constant,
    )
