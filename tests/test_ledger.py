import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unicodedata
from pathlib import Path

from morph_paths import process
from morph_paths.canonical import dumps_canonical
from morph_paths.ledger import (
    BindingMismatchError,
    DeliveryStatus,
    Ledger,
    LedgerIntegrityError,
    MonotonicSealError,
    RevisionConflictError,
    RevisionGapError,
    UnknownEventError,
    UnknownPackageError,
    UnboundAnalysisError,
)

SRC = Path(__file__).resolve().parent.parent / "src"


def dictionary():
    """Two analyses A/B for NFC surface 'é', one analysis C for surface 'b'."""
    return {
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
        "queries": [
            {"id": "q-e", "surface": "é"},
            {"id": "q-b", "surface": "b"},
        ],
    }


def analyses(data):
    forms = process(data)["forms"]
    by_surface = {}
    for form in forms:
        by_surface.setdefault(form["surface"], []).append(form)
    a = next(f for f in by_surface["é"] if f["lemma"] == "L1")
    b = next(f for f in by_surface["é"] if f["lemma"] == "L2")
    c = next(f for f in by_surface["b"] if f["lemma"] == "L3")
    return copy.deepcopy(a), copy.deepcopy(b), copy.deepcopy(c)


def value(period, analysis, text=None):
    if text is None:
        text = analysis["surface"]
    return {"period": period, "text": text, "analysis": copy.deepcopy(analysis)}


def fact_keys(rows):
    return [(row["period"], row["analysis"]["lemma"], tuple(row["analysis"]["rules"]))
            for row in rows]


def diff_keys(package):
    return [(row["period"], row["analysis"]["lemma"], tuple(row["analysis"]["rules"]),
             row["delta"]) for row in package.rows]


class LedgerTestBase(unittest.TestCase):
    def setUp(self):
        self.data = dictionary()
        self.a, self.b, self.c = analyses(self.data)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "ledger"
        self.ws = Ledger(self.root, self.data)

    def reopen(self):
        return Ledger(self.root, self.data)


class ScenarioOneTests(LedgerTestBase):
    """Acceptance 1: first seal, frozen package, late correction ordering."""

    def _seed_and_seal(self):
        self.ws.write_revisions([
            {"event": "e1", "revision": 1, "value": value(10, self.a)},
            {"event": "e2", "revision": 1, "value": value(10, self.b)},
            {"event": "e3", "revision": 1, "value": value(11, self.c)},
        ])
        self.ws.seal(10)

    def test_first_package_covers_only_sealed_period_and_freezes(self):
        self._seed_and_seal()
        package = self.ws.next_package()
        self.assertEqual(package.status, DeliveryStatus.PACKAGE)
        self.assertEqual(
            diff_keys(package),
            [(10, "L1", ("r1",), 1), (10, "L2", ("r2",), 1)],
        )
        self.assertEqual(package.closed_through, 10)
        # Every fetch returns the identical frozen bytes.
        again = self.ws.next_package()
        self.assertEqual(again.payload_bytes(), package.payload_bytes())
        self.assertEqual(again.package_id, package.package_id)
        # Reopening must not change the frozen package either.
        reopened = self.reopen()
        self.assertEqual(
            reopened.next_package().payload_bytes(), package.payload_bytes()
        )

    def test_correction_while_frozen_changes_query_but_not_package(self):
        self._seed_and_seal()
        package = self.ws.next_package()
        self.ws.write_revisions([
            {"event": "e1", "revision": 2, "value": value(11, self.c)},
        ])
        # Current facts immediately reflect the correction, including p11.
        current = self.ws.current_facts()
        self.assertEqual(
            fact_keys(current),
            [(10, "L2", ("r2",)), (11, "L3", ("r3",))],
        )
        counts = {(row["period"], row["analysis"]["lemma"],
                   tuple(row["analysis"]["rules"])): row["count"]
                  for row in current}
        self.assertEqual(counts[(11, "L3", ("r3",))], 2)
        # The frozen package is untouched.
        self.assertEqual(self.ws.next_package().payload_bytes(),
                         package.payload_bytes())
        # Sealed facts exclude open period 11, so the frozen diff target only
        # loses A from period 10 after confirmation.
        self.ws.confirm(package.package_id)
        second = self.ws.next_package()
        self.assertEqual(diff_keys(second), [(10, "L1", ("r1",), -1)])
        # Open period 11 must not be delivered ahead of sealing.
        self.ws.seal(11)
        self.ws.confirm(second.package_id)
        third = self.ws.next_package()
        self.assertEqual(
            diff_keys(third),
            [(11, "L3", ("r3",), 2)],
        )
        self.ws.confirm(third.package_id)
        self.assertEqual(self.ws.next_package().status, DeliveryStatus.NO_DELIVERY)

    def test_no_delivery_does_not_advance_checkpoints_or_state(self):
        before = self.ws.state()
        result = self.ws.next_package()
        self.assertEqual(result.status, DeliveryStatus.NO_DELIVERY)
        self.assertEqual(self.ws.state(), before)
        self.assertIsNone(self.ws.state()["pending_package_id"])
        # Sealing with no facts still produces no package.
        self.ws.seal(5)
        self.assertEqual(self.ws.next_package().status, DeliveryStatus.NO_DELIVERY)


class ScenarioTwoTests(LedgerTestBase):
    """Acceptance 2: netting against the confirmed baseline, frozen retries."""

    def _confirmed_state(self):
        self.ws.write_revisions([
            {"event": "e1", "revision": 1, "value": value(11, self.c)},
            {"event": "e2", "revision": 1, "value": value(10, self.a)},
        ])
        self.ws.seal(11)
        package = self.ws.next_package()
        self.ws.confirm(package.package_id)

    def test_corrections_that_return_to_baseline_emit_nothing(self):
        self._confirmed_state()
        # e1: p11/C -> p10/A -> back to p11/C before any fetch.
        self.ws.write_revisions([
            {"event": "e1", "revision": 2, "value": value(10, self.a)},
        ])
        self.ws.write_revisions([
            {"event": "e1", "revision": 3, "value": value(11, self.c)},
        ])
        self.assertEqual(self.ws.next_package().status, DeliveryStatus.NO_DELIVERY)
        self.assertIsNone(self.ws.state()["pending_package_id"])

    def test_frozen_package_survives_later_correction_and_compensates(self):
        self._confirmed_state()
        # Freeze while the fact differs from the baseline.
        self.ws.write_revisions([
            {"event": "e1", "revision": 2, "value": value(10, self.a)},
        ])
        package = self.ws.next_package()
        self.assertEqual(
            diff_keys(package),
            [(10, "L1", ("r1",), 1), (11, "L3", ("r3",), -1)],
        )
        # A correction back to the baseline state must not alter the freeze.
        self.ws.write_revisions([
            {"event": "e1", "revision": 3, "value": value(11, self.c)},
        ])
        self.assertEqual(self.ws.next_package().payload_bytes(),
                         package.payload_bytes())
        # After confirmation, the reverse compensation is the next package.
        self.ws.confirm(package.package_id)
        reverse = self.ws.next_package()
        self.assertEqual(
            diff_keys(reverse),
            [(10, "L1", ("r1",), -1), (11, "L3", ("r3",), 1)],
        )
        self.ws.confirm(reverse.package_id)
        self.assertEqual(self.ws.next_package().status,
                         DeliveryStatus.NO_DELIVERY)
        self.assertEqual(
            self.reopen().confirmed_facts(), self.ws.confirmed_facts()
        )


class ScenarioThreeTests(LedgerTestBase):
    """Acceptance 3: late compensation, original spelling, batch rejections."""

    def test_late_event_positive_then_negative_compensation(self):
        self.ws.seal(10)
        self.assertEqual(self.ws.next_package().status,
                         DeliveryStatus.NO_DELIVERY)
        # Late write into already sealed period 9.
        self.ws.write_revisions([
            {"event": "late", "revision": 1, "value": value(9, self.b)},
        ])
        positive = self.ws.next_package()
        self.assertEqual(diff_keys(positive), [(9, "L2", ("r2",), 1)])
        self.ws.confirm(positive.package_id)
        # Retraction produces an equal-magnitude negative compensation.
        self.ws.write_revisions([
            {"event": "late", "revision": 2, "value": None},
        ])
        negative = self.ws.next_package()
        self.assertEqual(diff_keys(negative), [(9, "L2", ("r2",), -1)])
        self.ws.confirm(negative.package_id)
        self.assertEqual(self.ws.next_package().status,
                         DeliveryStatus.NO_DELIVERY)
        self.assertEqual(self.ws.confirmed_facts(), [])

    def test_create_and_retract_inside_one_batch_is_allowed_but_counts_nothing(self):
        self.ws.write_revisions([
            {"event": "e1", "revision": 2, "value": None},
            {"event": "e1", "revision": 1, "value": value(9, self.b)},
        ])
        self.assertEqual(self.ws.get_event("e1")["revision"], 2)
        self.assertIsNone(self.ws.get_event("e1")["value"])
        self.assertEqual(self.ws.current_facts(), [])
        # A standalone retraction of a never-seen event still fails wholesale.
        before = self.ws.state()["state_version"]
        with self.assertRaises(UnknownEventError):
            self.ws.write_revisions([
                {"event": "ghost", "revision": 1, "value": None},
            ])
        self.assertEqual(self.ws.state()["state_version"], before)

    def test_decomposed_original_spelling_kept_but_aggregated_identically(self):
        decomposed = "é"
        self.assertEqual(unicodedata.normalize("NFC", decomposed), "é")
        self.ws.write_revisions([
            {"event": "e1", "revision": 1,
             "value": value(10, self.a, text=decomposed)},
            {"event": "e2", "revision": 1,
             "value": value(10, self.a, text="é")},
        ])
        audit = self.ws.get_event("e1")
        self.assertEqual(audit["value"]["text"], decomposed)
        facts = self.ws.current_facts()
        self.assertEqual(fact_keys(facts), [(10, "L1", ("r1",))])
        self.assertEqual(facts[0]["count"], 2)

    def test_identical_replay_does_not_advance_version(self):
        before = self.ws.state()["state_version"]
        result = self.ws.write_revisions([
            {"event": "e1", "revision": 1, "value": value(10, self.a)},
        ])
        self.assertEqual(result["accepted_new"], 1)
        self.assertEqual(result["state_version"], before + 1)
        replay = self.ws.write_revisions([
            {"event": "e1", "revision": 1, "value": value(10, self.a)},
        ])
        self.assertEqual(replay["accepted_new"], 0)
        self.assertEqual(replay["state_version"], before + 1)
        # Replay may arrive in an array alongside genuinely new consecutive
        # revisions; only the new fact advances the version.
        mixed = self.ws.write_revisions([
            {"event": "e1", "revision": 1, "value": value(10, self.a)},
            {"event": "e1", "revision": 2, "value": value(10, self.b)},
        ])
        self.assertEqual(mixed["accepted_new"], 1)
        self.assertEqual(mixed["replayed"], 1)
        self.assertEqual(mixed["state_version"], before + 2)

    def _snapshot(self):
        return {
            "facts": self.ws.current_facts(),
            "state": self.ws.state(),
            "baseline": self.ws.confirmed_facts(),
        }

    def test_conflicting_revision_rejects_whole_batch(self):
        self.ws.write_revisions([
            {"event": "e1", "revision": 1, "value": value(10, self.a)},
        ])
        snapshot = self._snapshot()
        with self.assertRaises(RevisionConflictError) as caught:
            self.ws.write_revisions([
                {"event": "e1", "revision": 1, "value": value(10, self.b)},
            ])
        self.assertEqual(caught.exception.event, "e1")
        self.assertEqual(caught.exception.revision, 1)
        self.assertEqual(self._snapshot(), snapshot)

    def test_revision_gap_jump_rejects_whole_batch(self):
        with self.assertRaises(RevisionGapError) as caught:
            self.ws.write_revisions([
                {"event": "e1", "revision": 1, "value": value(10, self.a)},
                {"event": "e1", "revision": 3, "value": value(10, self.b)},
            ])
        self.assertEqual(caught.exception.event, "e1")
        self.assertEqual(self.ws.current_facts(), [])
        self.assertEqual(self.ws.state()["state_version"], 0)
        # New events must start at revision 1.
        with self.assertRaises(RevisionGapError):
            self.ws.write_revisions([
                {"event": "e9", "revision": 2, "value": value(10, self.a)},
            ])

    def test_illegal_analysis_rejects_whole_batch(self):
        self.ws.write_revisions([
            {"event": "e1", "revision": 1, "value": value(10, self.a)},
        ])
        self.ws.seal(10)
        package = self.ws.next_package()
        self.ws.confirm(package.package_id)
        snapshot = self._snapshot()

        foreign = copy.deepcopy(self.a)
        foreign["lemma"] = "L404"
        with self.assertRaises(UnboundAnalysisError):
            self.ws.write_revisions([
                {"event": "ok", "revision": 1, "value": value(10, self.b)},
                {"event": "bad", "revision": 1, "value": value(10, foreign)},
            ])
        # Same surface+features but a different rules path must not substitute.
        swapped = copy.deepcopy(self.a)
        swapped["rules"] = ["r2"]
        with self.assertRaises(UnboundAnalysisError):
            self.ws.write_revisions([
                {"event": "bad2", "revision": 1, "value": value(10, swapped)},
            ])
        # Text whose NFC differs from the analysis surface.
        with self.assertRaises(UnboundAnalysisError):
            self.ws.write_revisions([
                {"event": "bad3", "revision": 1,
                 "value": value(10, self.a, text="x")},
            ])
        # Unknown-event retraction is a business conflict too.
        with self.assertRaises(UnknownEventError):
            self.ws.write_revisions([
                {"event": "ghost", "revision": 1, "value": None},
            ])
        self.assertEqual(self._snapshot(), snapshot)

    def test_structural_errors_are_plain_value_error(self):
        with self.assertRaises(ValueError):
            self.ws.write_revisions([])
        with self.assertRaises(ValueError):
            self.ws.write_revisions([{"event": "e1"}])
        with self.assertRaises(ValueError):
            self.ws.write_revisions([
                {"event": "e1", "revision": 0, "value": value(10, self.a)},
            ])
        with self.assertRaises(ValueError):
            self.ws.write_revisions([
                {"event": "e1", "revision": 1.5, "value": value(10, self.a)},
            ])
        with self.assertRaises(ValueError):
            self.ws.write_revisions([
                {"event": "e1", "revision": 1, "value": {"period": -1,
                 "text": "é", "analysis": self.a}},
            ])
        with self.assertRaises(ValueError):
            self.ws.write_revisions([
                {"event": "e1", "revision": 1,
                 "value": {"period": 10, "text": "é", "analysis": dict(self.a, extra=1)}},
            ])
        with self.assertRaises(ValueError):
            self.ws.write_revisions([
                {"event": "e1", "revision": 1, "value": value(10, self.a)},
                {"event": "e1", "revision": 1, "value": value(10, self.b)},
            ])
        self.assertEqual(self.ws.state()["state_version"], 0)

    def test_negative_only_and_cancelling_packages_follow_same_rules(self):
        self.ws.write_revisions([
            {"event": "e1", "revision": 1, "value": value(10, self.a)},
        ])
        self.ws.seal(10)
        first = self.ws.next_package()
        self.ws.confirm(first.package_id)
        self.ws.write_revisions([
            {"event": "e1", "revision": 2, "value": None},
        ])
        negative = self.ws.next_package()
        self.assertEqual(diff_keys(negative), [(10, "L1", ("r1",), -1)])
        # Unknown and stale ids never move the baseline.
        with self.assertRaises(UnknownPackageError):
            self.ws.confirm("0" * 64)
        self.ws.confirm(negative.package_id)
        # Replaying a confirmation is safe; reusing an old id is not.
        self.ws.confirm(negative.package_id)
        with self.assertRaises(UnknownPackageError):
            self.ws.confirm(first.package_id)
        self.assertEqual(self.ws.confirmed_facts(), [])


class AuditAndSealTests(LedgerTestBase):
    def test_get_event_tracks_correction_chain_and_original_spelling(self):
        with self.assertRaises(UnknownEventError):
            self.ws.get_event("missing")
        self.ws.write_revisions([
            {"event": "e1", "revision": 1,
             "value": value(10, self.a, text="é")},
            {"event": "e1", "revision": 2, "value": value(11, self.c)},
            {"event": "e1", "revision": 3, "value": None},
        ])
        latest = self.ws.get_event("e1")
        self.assertEqual(latest["revision"], 3)
        self.assertIsNone(latest["value"])
        # Retracted events do not count anywhere.
        self.assertEqual(self.ws.current_facts(), [])

    def test_seal_is_monotonic_and_replayable(self):
        first = self.ws.seal(10)
        self.assertTrue(first["advanced"])
        again = self.ws.seal(10)
        self.assertFalse(again["advanced"])
        self.assertEqual(again["state_version"], first["state_version"])
        with self.assertRaises(MonotonicSealError):
            self.ws.seal(9)

    def test_batch_order_independence_and_multi_revision_batch(self):
        batch = [
            {"event": "e2", "revision": 1, "value": value(10, self.b)},
            {"event": "e1", "revision": 2, "value": value(11, self.c)},
            {"event": "e1", "revision": 1, "value": value(10, self.a)},
        ]
        self.ws.write_revisions(batch)
        self.assertEqual(self.ws.get_event("e1")["revision"], 2)
        self.assertEqual(fact_keys(self.ws.current_facts()),
                         [(10, "L2", ("r2",)), (11, "L3", ("r3",))])

    def test_facts_sorting_is_stable(self):
        self.ws.write_revisions([
            {"event": "e1", "revision": 1, "value": value(12, self.b)},
            {"event": "e2", "revision": 1, "value": value(10, self.a)},
            {"event": "e3", "revision": 1, "value": value(10, self.b)},
        ])
        self.assertEqual(
            fact_keys(self.ws.current_facts()),
            [(10, "L1", ("r1",)), (10, "L2", ("r2",)), (12, "L2", ("r2",))],
        )


class DeterminismAndShapeTests(LedgerTestBase):
    def test_same_business_state_yields_same_package_id_in_fresh_workspace(self):
        def build(root):
            ws = Ledger(root, self.data)
            ws.write_revisions([
                {"event": "e1", "revision": 1, "value": value(10, self.a)},
                {"event": "e2", "revision": 1, "value": value(10, self.b)},
            ])
            ws.seal(10)
            return ws.next_package()

        first = build(Path(self.tmp.name) / "d1")
        second = build(Path(self.tmp.name) / "d2")
        self.assertEqual(first.package_id, second.package_id)
        self.assertEqual(first.payload_bytes(), second.payload_bytes())
        self.assertEqual(first.start_checkpoint, second.start_checkpoint)
        self.assertEqual(first.end_checkpoint, second.end_checkpoint)

    def test_package_payload_shape_and_checkpoint_semantics(self):
        self.ws.seal(7)
        baseline_package = self.ws.next_package()
        self.assertEqual(baseline_package.status, DeliveryStatus.NO_DELIVERY)
        self.ws.write_revisions([
            {"event": "e1", "revision": 1, "value": value(8, self.a)},
        ])
        self.ws.seal(8)
        package = self.ws.next_package()
        from morph_paths.canonical import loads_canonical
        content = loads_canonical(package.payload_bytes())
        self.assertEqual(set(content), {
            "version", "start_checkpoint", "end_checkpoint",
            "closed_through", "rows",
        })
        self.assertEqual(content["closed_through"], 8)
        # Start checkpoint describes the empty baseline; end checkpoint the
        # sealed aggregate; the id is the hash of the exact frozen bytes.
        import hashlib
        self.assertEqual(
            hashlib.sha256(package.payload_bytes()).hexdigest(),
            package.package_id,
        )
        self.assertEqual(package.start_checkpoint,
                         hashlib.sha256(dumps_canonical([])).hexdigest())

    def test_aggregated_deltas_net_multiple_events_in_one_row(self):
        self.ws.write_revisions([
            {"event": "e1", "revision": 1, "value": value(10, self.c)},
            {"event": "e2", "revision": 1, "value": value(10, self.c)},
        ])
        self.ws.seal(10)
        package = self.ws.next_package()
        self.assertEqual(diff_keys(package), [(10, "L3", ("r3",), 2)])
        self.ws.confirm(package.package_id)
        # One of the two events retracts: a single -1 row, not event-by-event.
        self.ws.write_revisions([
            {"event": "e1", "revision": 2, "value": None},
        ])
        followup = self.ws.next_package()
        self.assertEqual(diff_keys(followup), [(10, "L3", ("r3",), -1)])

    def test_package_checkpoints_chain_and_confirm_replay_is_idempotent(self):
        import hashlib
        self.ws.write_revisions([
            {"event": "e1", "revision": 1, "value": value(10, self.a)},
        ])
        self.ws.seal(10)
        package = self.ws.next_package()
        self.assertEqual(
            package.start_checkpoint,
            hashlib.sha256(dumps_canonical([])).hexdigest(),
        )
        self.assertEqual(
            package.end_checkpoint,
            hashlib.sha256(dumps_canonical(self.ws.sealed_facts())).hexdigest(),
        )
        first_confirm = self.ws.confirm(package.package_id)
        self.assertFalse(first_confirm["replayed"])
        version_after = self.ws.state()["state_version"]
        # Replaying the same confirmation after the pending slot cleared.
        replay = self.ws.confirm(package.package_id)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["state_version"], version_after)
        # The next package starts where the previous one ended.
        self.ws.write_revisions([
            {"event": "e2", "revision": 1, "value": value(10, self.b)},
        ])
        followup = self.ws.next_package()
        self.assertEqual(followup.start_checkpoint, package.end_checkpoint)

    def test_sealing_forward_covers_intermediate_periods(self):
        self.ws.write_revisions([
            {"event": "e1", "revision": 1, "value": value(3, self.a)},
        ])
        self.ws.seal(10)  # periods 0..10 all sealed at once
        self.assertEqual(
            fact_keys(self.ws.sealed_facts()), [(3, "L1", ("r1",))]
        )
        package = self.ws.next_package()
        self.assertEqual(package.closed_through, 10)
        self.assertEqual(diff_keys(package), [(3, "L1", ("r1",), 1)])

    def test_replay_after_reopen_keeps_state_version(self):
        self.ws.write_revisions([
            {"event": "e1", "revision": 1, "value": value(10, self.a)},
        ])
        version = self.ws.state()["state_version"]
        reopened = self.reopen()
        result = reopened.write_revisions([
            {"event": "e1", "revision": 1, "value": value(10, self.a)},
        ])
        self.assertEqual(result["state_version"], version)
        self.assertEqual(reopened.state()["state_version"], version)

    def test_surface_text_original_spelling_round_trip(self):
        original = "é"
        self.ws.write_revisions([
            {"event": "e1", "revision": 1,
             "value": value(10, self.a, text=original)},
        ])
        self.assertEqual(self.ws.get_event("e1")["value"]["text"], original)
        # The stored analysis surface is the precomposed NFC form.
        self.assertEqual(
            self.ws.current_facts()[0]["analysis"]["surface"], "é"
        )

    def test_confirmed_baseline_always_matches_sealed_facts_when_caught_up(self):
        self.ws.write_revisions([
            {"event": "e1", "revision": 1, "value": value(10, self.a)},
            {"event": "e2", "revision": 1, "value": value(11, self.c)},
        ])
        self.ws.seal(10)
        package = self.ws.next_package()
        self.ws.confirm(package.package_id)
        self.assertEqual(self.ws.confirmed_facts(), self.ws.sealed_facts())
        # A late correction to an already sealed period, then catch up again.
        self.ws.write_revisions([
            {"event": "e1", "revision": 2, "value": value(10, self.b)},
        ])
        followup = self.ws.next_package()
        self.ws.confirm(followup.package_id)
        self.assertEqual(self.ws.confirmed_facts(), self.ws.sealed_facts())
        self.assertEqual(self.ws.next_package().status,
                         DeliveryStatus.NO_DELIVERY)


class BindingTests(LedgerTestBase):
    """Acceptance 4 (binding half): content identity and benign changes."""

    def test_invalid_dictionary_creates_nothing(self):
        bad = dictionary()
        bad["rules"][0]["to"] = "absent"
        fresh = Path(self.tmp.name) / "never"
        with self.assertRaises(ValueError):
            Ledger(fresh, bad)
        self.assertFalse(fresh.exists())

    def test_queries_and_meaningless_order_do_not_change_binding(self):
        reordered = dictionary()
        reordered["queries"] = [
            {"id": "q-only", "surface": "b"},
        ]
        reordered["lemmas"] = list(reversed(reordered["lemmas"]))
        reordered["rules"] = list(reversed(reordered["rules"]))
        # Reopening succeeds and exposes the same identity.
        reopened = Ledger(self.root, reordered)
        self.assertEqual(reopened.dictionary_id, self.ws.dictionary_id)

    def test_real_form_change_is_rejected_and_state_untouched(self):
        changed = dictionary()
        changed["lemmas"][0]["features"] = {"kind": "plain"}
        with self.assertRaises(BindingMismatchError) as caught:
            Ledger(self.root, changed)
        self.assertNotEqual(caught.exception.expected_id,
                            caught.exception.actual_id)
        # The original workspace remains intact and openable.
        self.assertEqual(self.reopen().dictionary_id, self.ws.dictionary_id)

        other = dictionary()
        other["rules"][0]["adds"] = {"accent": "other"}
        with self.assertRaises(BindingMismatchError):
            Ledger(self.root, other)

    def test_corrupt_persisted_state_is_an_integrity_error(self):
        (self.root / "ledger.json").write_bytes(b"{not json")
        with self.assertRaises(LedgerIntegrityError):
            self.reopen()


# --------------------------------------------------------------------------- #
# Subprocess crash/recovery tests
# --------------------------------------------------------------------------- #

_HELPER = r"""
import json, sys
sys.path.insert(0, %(src)r)
from morph_paths.ledger import Ledger
import morph_paths.ledger as ledger_mod

root = sys.argv[1]
mode = sys.argv[2]
hook_kind = sys.argv[3] if len(sys.argv) > 3 else None
hook_phase = sys.argv[4] if len(sys.argv) > 4 else None

data = json.loads(sys.stdin.buffer.read())
ws = Ledger(root, data)

if hook_kind:
    def hook(kind, ledger):
        if kind == hook_kind:
            raise SystemExit(25)
    if hook_phase == "before":
        ledger_mod._CRASH_BEFORE_COMMIT = hook
    else:
        ledger_mod._CRASH_AFTER_COMMIT = hook

A, B, C = None, None, None
forms = ws._forms
for f in forms:
    if f["surface"] == "é" and f["lemma"] == "L1":
        A = f
    elif f["surface"] == "é" and f["lemma"] == "L2":
        B = f
    elif f["surface"] == "b":
        C = f

def v(period, analysis):
    return {"period": period, "text": analysis["surface"],
            "analysis": dict(analysis)}

if mode == "write":
    print(json.dumps(ws.write_revisions([
        {"event": "e1", "revision": 1, "value": v(10, A)},
        {"event": "e2", "revision": 1, "value": v(10, B)},
    ])))
elif mode == "seal":
    print(json.dumps(ws.seal(10)))
elif mode == "freeze":
    package = ws.next_package()
    print(json.dumps({"status": package.status,
                      "package_id": package.package_id,
                      "payload": package.payload_bytes().decode("utf-8")}))
elif mode == "confirm":
    print(json.dumps(ws.confirm(sys.argv[-1])))
elif mode == "inspect":
    pending = ws.state()["pending_package_id"]
    package = None
    if pending is not None:
        p = ws.next_package()
        package = {"package_id": p.package_id,
                   "payload": p.payload_bytes().decode("utf-8")}
    print(json.dumps({
        "facts": [(r["period"], r["analysis"]["lemma"],
                   list(r["analysis"]["rules"]), r["count"])
                  for r in ws.current_facts()],
        "sealed": ws.state()["closed_through"],
        "state_version": ws.state()["state_version"],
        "pending": pending,
        "package": package,
        "baseline": [(r["period"], r["analysis"]["lemma"],
                      list(r["analysis"]["rules"]), r["count"])
                     for r in ws.confirmed_facts()],
        "next_status": ws.next_package().status,
    }, ensure_ascii=False))
"""


class CrashRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.data = dictionary()
        self.a, self.b, self.c = analyses(self.data)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "ledger"

    def _run(self, mode, hook=None, *extra, expect_exit=None):
        script = tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, dir=tempfile.gettempdir()
        )
        args = [mode]
        if hook is not None:
            args.extend(hook)
        args.extend(extra)
        try:
            script.write(_HELPER % {"src": str(SRC)})
            script.close()
            proc = subprocess.run(
                [sys.executable, "-I", script.name, str(self.root), *args],
                input=json.dumps(self.data),
                capture_output=True,
                text=True,
            )
            if expect_exit is not None:
                self.assertEqual(proc.returncode, expect_exit, proc.stderr)
                return None
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return json.loads(proc.stdout)
        finally:
            os.unlink(script.name)

    def _inspect(self):
        return self._run("inspect")

    def test_write_crash_before_and_after_commit(self):
        for phase in ("before", "after"):
            root = Path(self.tmp.name) / f"write-{phase}"
            self.root = root
            self._run("write", ("write", phase), expect_exit=25)
            observed = self._inspect()
            if phase == "before":
                self.assertEqual(observed["facts"], [])
                self.assertEqual(observed["state_version"], 0)
            else:
                self.assertEqual(
                    [(p, l) for p, l, _, _ in observed["facts"]],
                    [(10, "L1"), (10, "L2")],
                )
                self.assertEqual(observed["state_version"], 1)
            # Safe retry from a fresh process converges to the new state.
            self._run("write")
            observed = self._inspect()
            self.assertEqual(len(observed["facts"]), 2)
            self.assertEqual(observed["state_version"], 1)  # replay no bump

    def test_seal_crash_before_and_after_commit(self):
        self._run("write")
        for phase in ("before", "after"):
            root = Path(self.tmp.name) / f"seal-{phase}"
            ws = Ledger(root, self.data)
            ws.write_revisions([
                {"event": "e1", "revision": 1, "value": value(10, self.a)},
            ])
            self.root = root
            self._run("seal", ("seal", phase), expect_exit=25)
            observed = self._inspect()
            expected = None if phase == "before" else 10
            self.assertEqual(observed["sealed"], expected)
            self._run("seal")
            self.assertEqual(self._inspect()["sealed"], 10)

    def test_freeze_crash_after_freeze_but_before_return_keeps_package(self):
        self._run("write")
        self._run("seal")
        # Crash after the descriptor commit but before the caller got bytes.
        self._run("freeze", ("freeze", "after"), expect_exit=25)
        observed = self._inspect()
        self.assertIsNotNone(observed["pending"])
        self.assertEqual(observed["next_status"], DeliveryStatus.PACKAGE)
        # A fresh fetch must return the exact original frozen package.
        refetched = self._run("freeze")
        self.assertEqual(refetched["package_id"], observed["package"]["package_id"])
        self.assertEqual(refetched["payload"], observed["package"]["payload"])
        # Pre-commit crash leaves no pending package and allows a fresh freeze.
        root2 = Path(self.tmp.name) / "freeze-before"
        ws = Ledger(root2, self.data)
        ws.write_revisions([
            {"event": "e1", "revision": 1, "value": value(10, self.a)},
        ])
        ws.seal(10)
        self.root = root2
        self._run("freeze", ("freeze", "before"), expect_exit=25)
        observed = self._inspect()
        self.assertIsNone(observed["pending"])
        self.assertEqual(observed["next_status"], DeliveryStatus.PACKAGE)

    def test_confirm_crash_does_not_redeliver_confirmed_diff(self):
        self._run("write")
        self._run("seal")
        frozen = self._run("freeze")
        package_id = frozen["package_id"]
        for phase in ("before", "after"):
            root = Path(self.tmp.name) / f"confirm-{phase}"
            ws = Ledger(root, self.data)
            ws.write_revisions([
                {"event": "e1", "revision": 1, "value": value(10, self.a)},
            ])
            ws.seal(10)
            package = ws.next_package()
            self.root = root
            self._run("confirm", ("confirm", phase), package.package_id,
                      expect_exit=25)
            observed = self._inspect()
            if phase == "before":
                self.assertEqual(observed["pending"], package.package_id)
                self.assertEqual(observed["baseline"], [])
            else:
                self.assertIsNone(observed["pending"])
                self.assertEqual(
                    [(p, l) for p, l, _, _ in observed["baseline"]],
                    [(10, "L1")],
                )
            # Retry confirmation from a fresh process.
            self._run("confirm", None, package.package_id)
            observed = self._inspect()
            self.assertIsNone(observed["pending"])
            self.assertEqual(observed["next_status"], DeliveryStatus.NO_DELIVERY)


if __name__ == "__main__":
    unittest.main()
