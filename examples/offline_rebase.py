"""Offline adaptation-layer rebase demonstration; run from the repo root.

A regional dictionary team keeps small ordered layers on top of a shared
finite morphology dictionary: each layer adds a few analyses and suppresses
selected analyses from the parent or an earlier layer.  When the parent
dictionary is updated, every layer's original intent is replayed onto the new
parent.  Nothing here opens a network connection, mutates a dictionary or
publishes a shard.

    python -I examples/offline_rebase.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from morph_paths import process, rebase_layers, forms_identity


def parent(suffix=""):
    """One lemma ``a`` surfacing as ``da`` + suffix through rule ``r``."""
    return {
        "states": ["s0", "s1"],
        "accepting": ["s1"],
        "rules": [
            {"id": "r", "from": "s0", "to": "s1", "prefix": "",
             "suffix": suffix, "requires": {}, "adds": {}}
        ],
        "lemmas": [{"id": "a", "stem": "da", "state": "s0", "features": {}}],
        "queries": [],
    }


def additions(suffix, lemma):
    return {
        "states": ["t0", "t1"],
        "accepting": ["t1"],
        "rules": [
            {"id": "q", "from": "t0", "to": "t1", "prefix": "",
             "suffix": suffix, "requires": {}, "adds": {}}
        ],
        "lemmas": [{"id": lemma, "stem": "rgn", "state": "t0", "features": {}}],
        "queries": [],
    }


def one_form(data):
    forms = process(data)["forms"]
    assert len(forms) == 1, forms
    return forms[0]


def show(title, result):
    print(title)
    rows = [
        (row["surface"], row["source_layer"], row["lemma"], row["rules"])
        for row in result["forms"]
    ]
    print("  effective:", rows)
    print("  statuses: ", [
        (record["layer_id"], record["policy"], record["status"])
        for record in result["suppressions"]
    ])


def main():
    v1 = parent()
    row = one_form(v1)

    layers = [
        {
            "layer_id": "snapshot-layer",
            "base_id": forms_identity(process(v1)["forms"]),
            "additions": additions("z", "w"),
            "suppressions": [
                {"source": "base", "policy": "snapshot", "lemma": "a",
                 "rules": ["r"], "surface": row["surface"],
                 "features": row["features"]},
            ],
        },
        {
            "layer_id": "follow-layer",
            "base_id": forms_identity(process(v1)["forms"]),
            "additions": additions("k", "v"),
            "suppressions": [
                # target is the snapshot layer's own added analysis
                {"source": "snapshot-layer", "policy": "follow", "lemma": "w",
                 "rules": ["q"]},
            ],
        },
    ]

    same = rebase_layers(v1, v1, layers)
    show("\nrebase onto the unchanged parent:", same)

    # The parent now changes the base analysis surface: snapshot reports
    # 'changed' and leaves the new row; follow still hides it.
    v2 = parent("s")
    changed = rebase_layers(v1, v2, layers)
    show("\nrebase onto a changed parent (surface da -> das):", changed)

    # Feed the preserved records straight into the next rebase.  v3 stops
    # producing the base analysis entirely; its next update brings it back, and
    # the follow intent becomes effective again without any manual edit.
    v3 = parent()
    v3["rules"] = []  # lemma a sits in non-accepting s0 -> no base forms
    vanished = rebase_layers(v2, v3, changed["rebased_layers"])
    show("\nrebase onto a parent that drops the analysis:", vanished)

    restored = rebase_layers(v3, v1, vanished["rebased_layers"])
    show("\nrebase the preserved layers onto a restoring parent:", restored)
    print("\nrebased layer base_id equals the new parent identity:",
          all(l["base_id"] == forms_identity(process(v1)["forms"])
              for l in restored["rebased_layers"]))
    print("inputs untouched:", layers[0]["base_id"]
          == forms_identity(process(v1)["forms"]))


if __name__ == "__main__":
    main()
