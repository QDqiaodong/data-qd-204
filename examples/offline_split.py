"""Offline two-batch chaining demo; run from the repository root.

Batch 1 delivers a training corpus together with its exposure record; the
caller merges that record into the running history only after actually
training on the corpus.  Batch 2 uses a revised dictionary (lemma a no longer
derives "daks") and asks to place a new document into the blind side; the old
exposure still blocks it through lemma a, and clearing the history delivers
it.  Nothing here opens a network connection.

    python -I examples/offline_split.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from morph_paths import build_corpora

DAK_A = {"lemma": "a", "rules": ["singular"], "features": {"class": "N", "number": "one"}}
DAKS_B = {"lemma": "b", "rules": ["singular"], "features": {"class": "N", "number": "one"}}


def dictionary(a_plural_blocked):
    """dak only from a; daks from a-plural (unless blocked) or b-singular."""
    plural_requires = {"class": "N", "rare": "yes"} if a_plural_blocked else {"class": "N"}
    return {
        "states": ["base", "number"],
        "accepting": ["number"],
        "rules": [
            {"id": "singular", "from": "base", "to": "number", "prefix": "", "suffix": "",
             "requires": {"class": "N"}, "adds": {"number": "one"}},
            {"id": "plural", "from": "base", "to": "number", "prefix": "", "suffix": "s",
             "requires": plural_requires, "adds": {"number": "many"}},
        ],
        "lemmas": [
            {"id": "a", "stem": "dak", "state": "base", "features": {"class": "N"}},
            {"id": "b", "stem": "daks", "state": "base", "features": {"class": "N"}},
        ],
        "queries": [],
    }


def doc(doc_id, side, cost, text, gold):
    return {"id": doc_id, "side": side, "drop_cost": cost,
            "tokens": [{"text": text, "gold": gold}]}


def show(title, result):
    print(title)
    print("  train:  ", [d["id"] for d in result["train"]])
    print("  holdout:", [d["id"] for d in result["holdout"]])
    print("  dropped:", json.dumps(result["dropped"], ensure_ascii=False, sort_keys=True))
    print("  exposure:", json.dumps(result["exposure"], ensure_ascii=False, sort_keys=True))


def main():
    history = {"doc_ids": [], "surfaces": [], "lemmas": []}

    # --- batch 1: train on the daks->b document with the original dictionary #
    batch1 = build_corpora(
        dictionary(False),
        [doc("train-1", "train", 5, "daks", DAKS_B)],
        history,
    )
    show("batch 1 (original dictionary, one train document):", batch1)

    # The caller actually trains on batch1["train"], then merges the exposure.
    for category in history:
        history[category] = sorted(set(history[category]) | set(batch1["exposure"][category]))
    print("merged history:", json.dumps(history, ensure_ascii=False, sort_keys=True))

    # --- batch 2: revised dictionary, a new holdout-only request ------------ #
    request = [doc("hold-1", "holdout", 3, "dak", DAK_A)]
    batch2 = build_corpora(dictionary(True), request, history)
    show("batch 2 (revised dictionary, history kept):", batch2)

    cleared = build_corpora(dictionary(True), request, None)
    show("batch 2 (revised dictionary, history cleared):", cleared)


if __name__ == "__main__":
    main()
