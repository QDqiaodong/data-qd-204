"""Offline usage-fact ledger demonstration; run from the repository root.

A single local directory holds revisioned morphology usage facts, the sealing
watermark and the downstream delivery relationship.  Nothing here opens a
network connection, modifies a dictionary or publishes a shard.

    python -I examples/offline_ledger.py
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from morph_paths import DeliveryStatus, Ledger

DICTIONARY = {
    "states": ["s0", "s1"],
    "accepting": ["s1"],
    "rules": [
        {
            "id": "r1",
            "from": "s0",
            "to": "s1",
            "prefix": "",
            "suffix": "́",
            "requires": {"kind": "accent"},
            "adds": {"accent": "yes"},
        },
        {
            "id": "r2",
            "from": "s0",
            "to": "s1",
            "prefix": "",
            "suffix": "",
            "requires": {"kind": "precomposed"},
            "adds": {"tone": "base"},
        },
        {
            "id": "r3",
            "from": "s0",
            "to": "s1",
            "prefix": "",
            "suffix": "",
            "requires": {"kind": "plain"},
            "adds": {"tone": "base"},
        },
    ],
    "lemmas": [
        {"id": "L1", "stem": "e", "state": "s0", "features": {"kind": "accent"}},
        {"id": "L2", "stem": "é", "state": "s0",
         "features": {"kind": "precomposed"}},
        {"id": "L3", "stem": "b", "state": "s0", "features": {"kind": "plain"}},
    ],
    "queries": [{"id": "q-e", "surface": "é"}],
}


def pick(forms, lemma):
    return next(dict(form) for form in forms if form["lemma"] == lemma)


def show_facts(title, rows):
    print(f"\n{title}")
    if not rows:
        print("  (empty)")
    for row in rows:
        analysis = row["analysis"]
        print(
            f"  period={row['period']} count={row['count']} "
            f"lemma={analysis['lemma']} rules={analysis['rules']}"
        )


def show_package(title, package):
    if package.status == DeliveryStatus.NO_DELIVERY:
        print(f"\n{title}: no pending delivery")
        return
    print(f"\n{title}: package {package.package_id[:12]}…")
    print(f"  checkpoints {package.start_checkpoint[:12]}… "
          f"-> {package.end_checkpoint[:12]}…, closed_through="
          f"{package.closed_through}")
    for row in package.rows:
        analysis = row["analysis"]
        print(
            f"  period={row['period']} delta={row['delta']:+d} "
            f"lemma={analysis['lemma']} rules={analysis['rules']}"
        )


def main():
    root = Path(tempfile.mkdtemp(prefix="morph-ledger-"))
    print(f"ledger workspace under {root}")
    try:
        ledger = Ledger(root, DICTIONARY)
        forms = ledger._forms  # compiled forms are what the workspace binds
        a = pick(forms, "L1")
        b = pick(forms, "L2")
        c = pick(forms, "L3")
        print(f"bound dictionary id: {ledger.dictionary_id}")

        ledger.write_revisions([
            {"event": "e1", "revision": 1,
             "value": {"period": 10, "text": "é", "analysis": a}},
            {"event": "e2", "revision": 1,
             "value": {"period": 10, "text": "é", "analysis": b}},
            {"event": "e3", "revision": 1,
             "value": {"period": 11, "text": "b", "analysis": c}},
        ])
        ledger.seal(10)
        show_facts("current facts (open period 11 included):",
                   ledger.current_facts())

        first = ledger.next_package()
        show_package("first delivery (sealed period 10 only)", first)
        # Re-fetching returns the same frozen bytes while unconfirmed.
        assert ledger.next_package().payload_bytes() == first.payload_bytes()

        # A late correction changes the live query but never the frozen package.
        ledger.write_revisions([
            {"event": "e1", "revision": 2,
             "value": {"period": 11, "text": "b", "analysis": c}},
        ])
        show_facts("current facts after the correction:", ledger.current_facts())
        assert ledger.next_package().package_id == first.package_id

        ledger.confirm(first.package_id)
        compensation = ledger.next_package()
        show_package("second delivery (negative compensation in period 10)",
                     compensation)
        ledger.confirm(compensation.package_id)

        ledger.seal(11)
        third = ledger.next_package()
        show_package("third delivery (period 11 becomes sealed)", third)
        ledger.confirm(third.package_id)
        show_package("next delivery after catching up", ledger.next_package())

        print("\nfinal state:", json.dumps(
            ledger.state(), ensure_ascii=False, sort_keys=True))
        print("\naudit e1:", json.dumps(
            ledger.get_event("e1"), ensure_ascii=False, sort_keys=True))
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
