"""Offline source-tracing and errata write-back demonstration.

Shows the editor workflow without any service or disk state: trace which
original characters each final surface position comes from, then turn
confirmed errata into a complete revised dictionary that can be handed
straight back to ``process``.

    python -I examples/offline_errata.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from morph_paths import apply_errata, process, trace_analysis
from morph_paths.errata import IntervalNotWritableError


def dictionary(suffix):
    return {
        "states": ["base", "done"],
        "accepting": ["done"],
        "rules": [
            {
                "id": "tone",
                "from": "base",
                "to": "done",
                "prefix": "",
                "suffix": suffix,
                "requires": {},
                "adds": {"tone": "marked"},
            }
        ],
        "lemmas": [{"id": "L", "stem": "a", "state": "base", "features": {}}],
        "queries": [],
    }


def show_sources(traced):
    analysis = traced["analysis"]
    print(f"  analysis: lemma={analysis['lemma']} rules={analysis['rules']} "
          f"surface={analysis['surface']!r}")
    for position in traced["positions"]:
        sources = ", ".join(
            f"{next(iter(s.values()))}.{s['field']}[{s['index']}]"
            for s in position["sources"]
        )
        print(f"  position {position['index']} {position['char']!r} <- {sources}")


def main():
    # --- trace: one suffix written as the single code point U+0344 -------- #
    data = dictionary("̈́")
    print("trace (suffix written as single U+0344):")
    show_sources(trace_analysis(data, "L", ["tone"]))

    # The errata 'delete final position 1, expect ä' cannot be written back:
    # the one source code point also feeds position 0.
    errata = [
        {"lemma": "L", "rules": ["tone"], "start": 1, "end": 2,
         "replacement": "", "confirmed": "ä"}
    ]
    try:
        apply_errata(data, errata)
    except IntervalNotWritableError as exc:
        print(f"\nerrata rejected as not writable: {exc}")

    # --- same dictionary, suffix spelled as U+0308 U+0301 ----------------- #
    split = dictionary("̈́")
    assert process(data)["forms"] == process(split)["forms"]
    print("\ntrace (suffix spelled U+0308 U+0301; identical compiled analysis):")
    show_sources(trace_analysis(split, "L", ["tone"]))

    delivered = apply_errata(split, errata)
    print("\ndelivered writes:")
    print(" ", json.dumps(delivered["writes"], ensure_ascii=False))
    revised_suffix = delivered["dictionary"]["rules"][0]["suffix"]
    revised_surface = process(delivered["dictionary"])["forms"][0]["surface"]
    print(f"revised suffix: U+{ord(revised_suffix):04X}; revised surface: {revised_surface!r}")


if __name__ == "__main__":
    main()
