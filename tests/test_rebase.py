"""Tests for the offline adaptation-layer rebase API.

The fixtures here are hand-built minimal dictionaries.  Every accepted form is
produced by the real ``process`` compiler, so the tests never assert on file
hashes: they assert on business facts (identities, statuses, effective rows).
"""
import copy
import hashlib
import unittest

from morph_paths import process
from morph_paths.canonical import dumps_canonical
from morph_paths.rebase import (
    MAX_LAYERS,
    MAX_TOTAL_SUPPRESSIONS,
    RebaseError,
    rebase_layers,
)


# --------------------------------------------------------------------------- #
# Fixture builders
# --------------------------------------------------------------------------- #

def forms_identity(data):
    return hashlib.sha256(dumps_canonical(process(data)["forms"])).hexdigest()


def parent(suffix="", stem="da", lemma="a", rule_id="r", features=None,
           requires=None):
    """One lemma, one accepting rule; its single surface is ``stem + suffix``."""
    return {
        "states": ["s0", "s1"],
        "accepting": ["s1"],
        "rules": [
            {
                "id": rule_id,
                "from": "s0",
                "to": "s1",
                "prefix": "",
                "suffix": suffix,
                "requires": requires or {},
                "adds": {},
            }
        ],
        "lemmas": [
            {"id": lemma, "stem": stem, "state": "s0", "features": features or {}}
        ],
        "queries": [],
    }


def two_paths_parent():
    """One lemma, two accepting rules that produce the same surface ``das``.

    The two analyses differ only by their ordered rule path, so a rebase must
    never fold them together nor hide both when only one is targeted.
    """
    return {
        "states": ["s0", "p1", "p2", "out1", "out2"],
        "accepting": ["out1", "out2"],
        "rules": [
            {"id": "r1", "from": "s0", "to": "p1", "prefix": "", "suffix": "s",
             "requires": {}, "adds": {"a": "1"}},
            {"id": "r2", "from": "s0", "to": "p2", "prefix": "", "suffix": "s",
             "requires": {}, "adds": {"a": "2"}},
            {"id": "f1", "from": "p1", "to": "out1", "prefix": "", "suffix": "",
             "requires": {}, "adds": {}},
            {"id": "f2", "from": "p2", "to": "out2", "prefix": "", "suffix": "",
             "requires": {}, "adds": {}},
        ],
        "lemmas": [{"id": "a", "stem": "da", "state": "s0", "features": {}}],
        "queries": [],
    }


def same_surface_two_lemmas():
    """Two lemmas, same stem, same accepting rule -> same surface, two rows."""
    return {
        "states": ["s0", "s1"],
        "accepting": ["s1"],
        "rules": [
            {"id": "r", "from": "s0", "to": "s1", "prefix": "", "suffix": "s",
             "requires": {}, "adds": {}}
        ],
        "lemmas": [
            {"id": "a", "stem": "da", "state": "s0", "features": {}},
            {"id": "b", "stem": "da", "state": "s0", "features": {}},
        ],
        "queries": [],
    }


def additions(suffix="z", lemma="w", rule_id="q", stem="ww"):
    return {
        "states": ["t0", "t1"],
        "accepting": ["t1"],
        "rules": [
            {"id": rule_id, "from": "t0", "to": "t1", "prefix": "",
             "suffix": suffix, "requires": {}, "adds": {}}
        ],
        "lemmas": [{"id": lemma, "stem": stem, "state": "t0", "features": {}}],
        "queries": [],
    }


def inert_additions(lemma="n", stem="n", suffix=""):
    """A distinct placeholder addition; every layer must carry a real dict.

    A dictionary needs 1..200 lemmas, so there is no empty addition.  These
    inert dictionaries merely add one unrelated analysis so a layer can exist
    without influencing the base analyses under test.
    """
    return {
        "states": ["u0", "u1"],
        "accepting": ["u1"],
        "rules": [
            {"id": "nr", "from": "u0", "to": "u1", "prefix": "",
             "suffix": suffix, "requires": {}, "adds": {}}
        ],
        "lemmas": [{"id": lemma, "stem": stem, "state": "u0", "features": {}}],
        "queries": [],
    }


def only_form(data):
    forms = process(data)["forms"]
    assert len(forms) == 1, forms
    return forms[0]


def snapshot_op(source, row):
    return {
        "source": source,
        "policy": "snapshot",
        "lemma": row["lemma"],
        "rules": list(row["rules"]),
        "surface": row["surface"],
        "features": dict(row["features"]),
    }


def follow_op(source, row):
    return {
        "source": source,
        "policy": "follow",
        "lemma": row["lemma"],
        "rules": list(row["rules"]),
    }


def layer(layer_id, base, additions_dict, suppressions):
    return {
        "layer_id": layer_id,
        "base_id": forms_identity(base),
        "additions": additions_dict,
        "suppressions": suppressions,
    }


def surface_rows(result):
    return [
        (row["surface"], row["source_layer"], row["lemma"], tuple(row["rules"]))
        for row in result["forms"]
    ]


def statuses(result):
    return [record["status"] for record in result["suppressions"]]


# --------------------------------------------------------------------------- #
# Identity: same surface, distinct analyses
# --------------------------------------------------------------------------- #

class IdentityTests(unittest.TestCase):
    def test_snapshot_hides_exactly_one_of_two_same_surface_analyses(self):
        base = two_paths_parent()
        target = next(
            f for f in process(base)["forms"]
            if f["lemma"] == "a" and f["rules"] == ["r1", "f1"]
        )
        # sanity: two analyses share the surface "das"
        surfaces = [f["surface"] for f in process(base)["forms"]]
        self.assertEqual(surfaces.count("das"), 2)

        layers = [layer("L1", base, inert_additions(), [snapshot_op("base", target)])]
        result = rebase_layers(base, base, layers)

        remaining = [r for r in result["forms"] if r["source_layer"] == "base"]
        self.assertEqual(
            [(r["lemma"], tuple(r["rules"])) for r in remaining],
            [("a", ("r2", "f2"))],
        )
        self.assertEqual(statuses(result), ["applied"])

    def test_same_identity_from_two_layers_stays_two_analyses(self):
        base = parent()
        layers = [
            layer("L1", base, additions(rule_id="q1"), []),
            layer("L2", base, additions(rule_id="q2"), []),
        ]
        # Both additions produce lemma "w" surface "wwz" via (w, [q1]) and
        # (w, [q2]); identity keeps them apart by source layer and rule path.
        result = rebase_layers(base, base, layers)
        added = [r for r in result["forms"] if r["source_layer"] in {"L1", "L2"}]
        self.assertEqual(
            [(r["surface"], r["source_layer"], r["lemma"], tuple(r["rules"]))
             for r in added],
            [("wwz", "L1", "w", ("q1",)), ("wwz", "L2", "w", ("q2",))],
        )

    def test_same_lemma_rules_in_two_layers_distinguished_by_source(self):
        base = parent()
        layers = [
            layer("L1", base, additions(lemma="w", rule_id="q", stem="ww"), []),
            layer("L2", base, additions(lemma="w", rule_id="q", stem="ww"),
                  []),
        ]
        result = rebase_layers(base, base, layers)
        added = [r for r in result["forms"] if r["lemma"] == "w"]
        self.assertEqual({r["source_layer"] for r in added}, {"L1", "L2"})
        self.assertEqual(len(added), 2)
        self.assertEqual(
            [(r["source_layer"], r["lemma"], tuple(r["rules"])) for r in added],
            [("L1", "w", ("q",)), ("L2", "w", ("q",))],
        )

    def test_snapshot_hides_one_of_two_same_surface_lemmas(self):
        base = same_surface_two_lemmas()
        forms = process(base)["forms"]
        self.assertEqual([(f["lemma"], f["surface"]) for f in forms],
                         [("a", "das"), ("b", "das")])
        target = next(f for f in forms if f["lemma"] == "a")
        layers = [layer("L1", base, inert_additions(), [snapshot_op("base", target)])]
        result = rebase_layers(base, base, layers)
        self.assertEqual(statuses(result), ["applied"])
        remaining = [r for r in result["forms"] if r["source_layer"] == "base"]
        self.assertEqual([(r["lemma"], r["surface"]) for r in remaining],
                         [("b", "das")])

    def test_output_sorted_by_surface_source_lemma_rules(self):
        base = two_paths_parent()
        layers = [layer("L1", base, additions(suffix="a", lemma="c"), [])]
        result = rebase_layers(base, base, layers)
        keys = [
            (r["surface"], r["source_layer"], r["lemma"], tuple(r["rules"]))
            for r in result["forms"]
        ]
        self.assertEqual(keys, sorted(keys))


# --------------------------------------------------------------------------- #
# snapshot vs follow across a parent change
# --------------------------------------------------------------------------- #

class PolicyTests(unittest.TestCase):
    def test_snapshot_changed_leaves_new_row(self):
        old = parent(suffix="")
        new = parent(suffix="s")
        row = only_form(old)
        layers = [layer("SNAP", old, inert_additions(lemma="s1", stem="s1"),
                        [snapshot_op("base", row)])]
        result = rebase_layers(old, new, layers)
        self.assertEqual(statuses(result), ["changed"])
        base_rows = [r for r in result["forms"] if r["source_layer"] == "base"]
        self.assertEqual(
            [(r["surface"], tuple(r["rules"])) for r in base_rows],
            [("das", ("r",))],
        )
        # The original operation survives unchanged in the rebased layers.
        snap_facts = result["rebased_layers"][0]["suppressions"][0]
        self.assertEqual(snap_facts["policy"], "snapshot")
        self.assertEqual(snap_facts["surface"], "da")

    def test_follow_applied_hides_new_row(self):
        old = parent(suffix="")
        new = parent(suffix="s")
        row = only_form(old)
        layers = [layer("FOLLOW", old, inert_additions(lemma="f1", stem="f1"),
                        [follow_op("base", row)])]
        result = rebase_layers(old, new, layers)
        self.assertEqual(statuses(result), ["applied"])
        self.assertEqual(
            [r for r in result["forms"] if r["source_layer"] == "base"], []
        )
        follow_facts = result["rebased_layers"][0]["suppressions"][0]
        self.assertEqual(follow_facts["policy"], "follow")
        self.assertEqual(follow_facts["rules"], ["r"])

    def test_snapshot_and_follow_in_separate_layers_both_preserved(self):
        old = parent(suffix="")
        new = parent(suffix="s")
        row = only_form(old)
        layers = [
            layer("SNAP", old, inert_additions(lemma="s1", stem="s1"),
                  [snapshot_op("base", row)]),
            layer("FOLLOW", old, inert_additions(lemma="f1", stem="f1"),
                  [follow_op("base", row)]),
        ]
        result = rebase_layers(old, new, layers)
        self.assertEqual(statuses(result), ["changed", "applied"])
        # FOLLOW in the next layer hides the row SNAP reported as changed.
        self.assertEqual(
            [r for r in result["forms"] if r["source_layer"] == "base"], []
        )
        self.assertEqual(
            [lyr["suppressions"][0]["policy"] for lyr in result["rebased_layers"]],
            ["snapshot", "follow"],
        )

    def test_follow_never_retargets_a_new_rule_path(self):
        old = parent(rule_id="r")
        new = parent(rule_id="r2")  # same stem/surface, different rule path
        row = only_form(old)
        layers = [layer("L1", old, inert_additions(), [follow_op("base", row)])]
        result = rebase_layers(old, new, layers)
        self.assertEqual(statuses(result), ["missing"])
        base_rows = [r for r in result["forms"] if r["source_layer"] == "base"]
        # The new-rule analysis is *not* auto-hidden by the old follow.
        self.assertEqual([tuple(r["rules"]) for r in base_rows], [("r2",)])

    def test_follow_hides_changed_features_row(self):
        old = parent()
        # Same rule path and surface, but the accepting rule now adds a feature.
        new = copy.deepcopy(old)
        new["rules"][0]["adds"] = {"mark": "yes"}
        self.assertEqual(only_form(old)["surface"], only_form(new)["surface"])
        self.assertNotEqual(only_form(old)["features"], only_form(new)["features"])
        row = only_form(old)
        layers = [
            layer("L1", old, inert_additions(lemma="k", stem="k"),
                  [follow_op("base", row)])
        ]
        result = rebase_layers(old, new, layers)
        self.assertEqual(statuses(result), ["applied"])
        base_rows = [r for r in result["forms"] if r["source_layer"] == "base"]
        self.assertEqual(base_rows, [])


# --------------------------------------------------------------------------- #
# missing / restore / already_hidden
# --------------------------------------------------------------------------- #

class ConflictLifecycleTests(unittest.TestCase):
    def test_missing_then_restore_with_rebased_layers(self):
        old = parent(rule_id="r")
        gone = parent(rule_id="r_other")
        row = only_form(old)
        layers = [layer("L1", old, inert_additions(), [follow_op("base", row)])]

        first = rebase_layers(old, gone, layers)
        self.assertEqual(statuses(first), ["missing"])
        self.assertIn("da", [r["surface"] for r in first["forms"]])
        self.assertTrue(
            all(l["base_id"] == forms_identity(gone)
                for l in first["rebased_layers"])
        )

        # Restore the original analysis; rebase the preserved records again.
        second = rebase_layers(gone, old, first["rebased_layers"])
        self.assertEqual(statuses(second), ["applied"])
        base_rows = [r for r in second["forms"] if r["source_layer"] == "base"]
        self.assertEqual(base_rows, [])
        self.assertEqual(first["rebased_layers"][0]["suppressions"],
                         layers[0]["suppressions"])

    def test_already_hidden_reported_not_double_deleted(self):
        base = parent()
        row = only_form(base)
        layers = [
            layer("L1", base, inert_additions(lemma="x1", stem="x1"),
                  [follow_op("base", row)]),
            layer("L2", base, inert_additions(lemma="x2", stem="x2"),
                  [follow_op("base", row)]),
        ]
        result = rebase_layers(base, base, layers)
        self.assertEqual(statuses(result), ["applied", "already_hidden"])
        self.assertEqual(
            [r for r in result["forms"] if r["source_layer"] == "base"], []
        )

    def test_higher_layer_hides_earlier_addition(self):
        base = parent()
        layers = [
            layer("L1", base, additions(lemma="w", rule_id="q"), []),
            layer("L2", base, inert_additions(lemma="z", stem="z"),
                  [follow_op("L1", {"lemma": "w", "rules": ["q"]})]),
        ]
        result = rebase_layers(base, base, layers)
        self.assertEqual(statuses(result), ["applied"])
        self.assertNotIn(
            ("wwz", "L1"), [(r["surface"], r["source_layer"]) for r in result["forms"]]
        )

    def test_higher_layer_cannot_suppress_own_or_future_additions(self):
        base = parent()
        own = layer("L1", base, additions(lemma="w", rule_id="q"),
                    [follow_op("L1", {"lemma": "w", "rules": ["q"]})])
        with self.assertRaises(ValueError):
            rebase_layers(base, base, [own])

        forward = [
            layer("L1", base, inert_additions(),
                  [follow_op("L2", {"lemma": "w", "rules": ["q"]})]),
            layer("L2", base, additions(lemma="w", rule_id="q"), []),
        ]
        with self.assertRaises(ValueError):
            rebase_layers(base, base, forward)


# --------------------------------------------------------------------------- #
# Chaining and bindings
# --------------------------------------------------------------------------- #

class ChainTests(unittest.TestCase):
    def test_two_rebases_rebind_each_new_base(self):
        first_base = parent(suffix="")
        second_base = parent(suffix="s")
        third_base = parent(suffix="ss")
        row = only_form(first_base)
        layers = [layer("L1", first_base, additions(lemma="w", rule_id="q"),
                        [snapshot_op("base", row)])]

        step1 = rebase_layers(first_base, second_base, layers)
        self.assertEqual(
            [l["base_id"] for l in step1["rebased_layers"]],
            [forms_identity(second_base)],
        )
        self.assertEqual(step1["old_base_id"], forms_identity(first_base))
        self.assertEqual(step1["new_base_id"], forms_identity(second_base))
        # Non-base fields preserved exactly.
        for before, after in zip(layers, step1["rebased_layers"]):
            self.assertEqual(before["layer_id"], after["layer_id"])
            self.assertEqual(before["additions"], after["additions"])
            self.assertEqual(before["suppressions"], after["suppressions"])

        step2 = rebase_layers(second_base, third_base, step1["rebased_layers"])
        self.assertEqual(
            [l["base_id"] for l in step2["rebased_layers"]],
            [forms_identity(third_base)],
        )
        self.assertEqual(step2["new_base_id"], forms_identity(third_base))
        self.assertEqual(statuses(step2), [step1["suppressions"][0]["status"]])

    def test_empty_rebase_matches_process(self):
        base = two_paths_parent()
        result = rebase_layers(base, base, [])
        expected = [
            {
                "lemma": f["lemma"],
                "surface": f["surface"],
                "features": f["features"],
                "rules": f["rules"],
                "source_layer": "base",
            }
            for f in process(base)["forms"]
        ]
        self.assertEqual(result["forms"], expected)
        self.assertEqual(result["suppressions"], [])
        self.assertEqual(result["rebased_layers"], [])

    def test_new_base_forms_match_process_when_no_layers(self):
        base = parent()
        other = two_paths_parent()
        result = rebase_layers(base, other, [])
        self.assertEqual(
            [(r["surface"], r["lemma"], r["rules"]) for r in result["forms"]],
            [(f["surface"], f["lemma"], f["rules"])
             for f in process(other)["forms"]],
        )


# --------------------------------------------------------------------------- #
# Insignificant ordering
# --------------------------------------------------------------------------- #

class OrderingTests(unittest.TestCase):
    def test_queries_and_meaningless_order_do_not_change_result(self):
        base = two_paths_parent()
        row = next(
            f for f in process(base)["forms"] if f["rules"] == ["r1", "f1"]
        )
        layers = [layer("L1", base, additions(lemma="w", rule_id="q"),
                        [snapshot_op("base", row)])]
        baseline = rebase_layers(base, base, layers)

        # queries selection differs; states/rules/lemmas arrays reordered.
        shuffled = copy.deepcopy(base)
        shuffled["queries"] = [{"id": "q1", "surface": "das"},
                              {"id": "q2", "surface": "nope"}]
        shuffled["rules"] = list(reversed(shuffled["rules"]))
        shuffled["states"] = list(reversed(shuffled["states"]))
        shuffled_layers = [copy.deepcopy(layers[0])]
        shuffled_layers[0]["additions"]["lemmas"] = list(
            reversed(shuffled_layers[0]["additions"]["lemmas"])
        )
        result = rebase_layers(shuffled, shuffled, shuffled_layers)
        self.assertEqual(surface_rows(result), surface_rows(baseline))
        self.assertEqual(result["new_base_id"], baseline["new_base_id"])

    def test_layer_order_is_business_significant(self):
        base = parent()
        row = only_form(base)
        l1 = layer("L1", base, inert_additions(lemma="x", stem="x"),
                   [follow_op("base", row)])
        l2 = layer("L2", base, additions(lemma="w", rule_id="q"),
                   [follow_op("L1", {"lemma": "x", "rules": ["nr"]})])
        result = rebase_layers(base, base, [l1, l2])
        # L2 targets L1's addition (visible only after L1 ran); identity by
        # (source_layer, lemma, rules) is preserved, so this is applied.
        self.assertEqual(statuses(result), ["applied", "applied"])


# --------------------------------------------------------------------------- #
# Rejections and input immutability
# --------------------------------------------------------------------------- #

class RejectionTests(unittest.TestCase):
    def test_duplicate_layer_id(self):
        base = parent()
        layers = [layer("L", base, inert_additions(), []),
                  layer("L", base, inert_additions(lemma="y", stem="y"), [])]
        with self.assertRaises(RebaseError):
            rebase_layers(base, base, layers)

    def test_wrong_base_identity(self):
        base = parent()
        other = parent(suffix="q")
        bad = layer("L", other, inert_additions(), [])
        with self.assertRaises(RebaseError):
            rebase_layers(base, base, [bad])

    def test_illegal_rule_graph_in_additions(self):
        base = parent()
        broken = additions()
        broken["states"].append("loop")
        broken["rules"].append(
            {"id": "self", "from": "loop", "to": "loop", "prefix": "",
             "suffix": "", "requires": {}, "adds": {}}
        )
        layers = [layer("L", base, broken, [])]
        with self.assertRaises(RebaseError):
            rebase_layers(base, base, layers)

    def test_damaged_full_snapshot_structure_rejected(self):
        base = parent()
        row = only_form(base)
        op = snapshot_op("base", row)
        op["surface"] = "é"  # decomposed; not the complete NFC surface
        layers = [layer("L", base, inert_additions(), [op])]
        with self.assertRaises(ValueError):
            rebase_layers(base, base, layers)

    def test_fabricated_target_with_missing_rule_rejected(self):
        base = parent()
        op = {"source": "base", "policy": "follow", "lemma": "a",
              "rules": ["nonexistent"]}
        layers = [layer("L", base, inert_additions(), [op])]
        with self.assertRaises(RebaseError):
            rebase_layers(base, base, layers)

    def test_unknown_source_rejected(self):
        base = parent()
        op = {"source": "ghost", "policy": "follow", "lemma": "a",
              "rules": ["r"]}
        layers = [layer("L", base, inert_additions(), [op])]
        with self.assertRaises(ValueError):
            rebase_layers(base, base, layers)

    def test_duplicate_target_in_one_layer_rejected(self):
        base = parent()
        row = only_form(base)
        layers = [layer("L", base, inert_additions(),
                        [follow_op("base", row), snapshot_op("base", row)])]
        with self.assertRaises(ValueError):
            rebase_layers(base, base, layers)

    def test_too_many_layers(self):
        base = parent()
        layers = [
            layer(f"L{i}", base, inert_additions(lemma=f"n{i}", stem=f"n{i}"), [])
            for i in range(MAX_LAYERS + 1)
        ]
        with self.assertRaises((ValueError, RebaseError)):
            rebase_layers(base, base, layers)

    def test_too_many_suppressions(self):
        # 200 base lemmas (200 forms) plus three layers of 200 additions each
        # make 800 distinct visible identities.  Twenty-six later layers may
        # each legally suppress every one of them (duplicates are only
        # forbidden inside one layer): 26 * 800 = 20,800 operations, over the
        # 20,000 total, while the layer count (29) and the compiled-analysis
        # total (800) stay legal.
        base = {
            "states": ["s0", "s1"],
            "accepting": ["s1"],
            "rules": [
                {"id": "r", "from": "s0", "to": "s1", "prefix": "",
                 "suffix": "", "requires": {}, "adds": {}}
            ],
            "lemmas": [
                {"id": f"l{i}", "stem": f"s{i}", "state": "s0",
                 "features": {}}
                for i in range(200)
            ],
            "queries": [],
        }
        add_dicts = []
        for k in range(3):
            add_dicts.append({
                "states": ["t0", "t1"],
                "accepting": ["t1"],
                "rules": [
                    {"id": "ar", "from": "t0", "to": "t1", "prefix": "",
                     "suffix": "", "requires": {}, "adds": {}}
                ],
                "lemmas": [
                    {"id": f"a{k}_{i}", "stem": f"a{k}x{i}", "state": "t0",
                     "features": {}}
                    for i in range(200)
                ],
                "queries": [],
            })
        layers = [
            layer("A0", base, add_dicts[0], []),
            layer("A1", base, add_dicts[1], []),
            layer("A2", base, add_dicts[2], []),
        ]
        targets = []
        for index in range(200):
            targets.append(("base", f"l{index}", ("r",)))
        for k in range(3):
            for index in range(200):
                targets.append((f"A{k}", f"a{k}_{index}", ("ar",)))
        self.assertEqual(len(targets), 800)
        for n in range(26):
            ops = [
                {"source": source, "policy": "follow", "lemma": lemma,
                 "rules": list(rules)}
                for source, lemma, rules in targets
            ]
            layers.append(
                layer(f"B{n}", base, inert_additions(lemma=f"b{n}", stem=f"b{n}x"),
                      ops)
            )
        self.assertLessEqual(len(layers), MAX_LAYERS)
        total = sum(len(lyr["suppressions"]) for lyr in layers)
        self.assertGreater(total, MAX_TOTAL_SUPPRESSIONS)
        with self.assertRaises(RebaseError):
            rebase_layers(base, base, layers)

    def test_too_many_compiled_analyses(self):
        # Each dictionary stays under the original 20,000-form cap, but the
        # combined total of old_base + new_base + all additions exceeds the
        # rebase-wide 20,000 limit: 9,800 + 9,800 + 9,800 = 29,400.
        def wide(prefix):
            states = ["s0"] + [f"{prefix}a{i}" for i in range(49)]
            rules = [
                {"id": f"{prefix}r{i}", "from": "s0", "to": f"{prefix}a{i}",
                 "prefix": "", "suffix": chr(0x430 + i), "requires": {},
                 "adds": {}}
                for i in range(49)
            ]
            lemmas = [
                {"id": f"{prefix}l{i}", "stem": f"{prefix}s{i}", "state": "s0",
                 "features": {}}
                for i in range(200)
            ]
            return {"states": states, "accepting": states[1:], "rules": rules,
                    "lemmas": lemmas, "queries": []}

        old = wide("o")
        new = wide("n")
        self.assertLessEqual(len(process(old)["forms"]), 20000)
        self.assertLessEqual(len(process(new)["forms"]), 20000)
        layers = [layer("L1", old, wide("x"), [])]
        with self.assertRaises(RebaseError):
            rebase_layers(old, new, layers)

    def test_inputs_not_mutated(self):
        base = two_paths_parent()
        row = next(
            f for f in process(base)["forms"] if f["rules"] == ["r1", "f1"]
        )
        layers = [layer("L1", base, additions(lemma="w", rule_id="q"),
                        [snapshot_op("base", row)])]
        base_before = copy.deepcopy(base)
        layers_before = copy.deepcopy(layers)
        new_before = copy.deepcopy(base)
        rebase_layers(base, base, layers)
        self.assertEqual(base, base_before)
        self.assertEqual(layers, layers_before)
        self.assertEqual(base, new_before)

    def test_uncompilable_parent_rejected(self):
        base = parent()
        broken_new = parent()
        broken_new["states"].append("orphan")
        broken_new["accepting"].append("orphan")  # in states but unreachable is fine
        # Make it actually cyclic instead:
        broken_new["rules"].append(
            {"id": "back", "from": "s1", "to": "s1", "prefix": "",
             "suffix": "", "requires": {}, "adds": {}}
        )
        with self.assertRaises(RebaseError):
            rebase_layers(base, broken_new, [])


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #

class DeterminismTests(unittest.TestCase):
    def test_repeated_calls_identical(self):
        base = two_paths_parent()
        row = next(
            f for f in process(base)["forms"] if f["rules"] == ["r1", "f1"]
        )
        layers = [layer("L1", base, additions(lemma="w", rule_id="q"),
                        [snapshot_op("base", row)])]
        first = rebase_layers(base, base, layers)
        second = rebase_layers(base, base, layers)
        self.assertEqual(first, second)
        self.assertEqual(first["new_base_id"], second["new_base_id"])

    def test_rebased_layers_roundtrip_through_json(self):
        base = parent()
        row = only_form(base)
        layers = [layer("L1", base, additions(lemma="w", rule_id="q"),
                        [snapshot_op("base", row)])]
        first = rebase_layers(base, base, layers)
        # The rebased records survive a serialise/parse round trip and feed
        # the next call unchanged.
        import json
        roundtrip = json.loads(json.dumps(first["rebased_layers"]))
        second = rebase_layers(base, base, roundtrip)
        self.assertEqual(statuses(second), statuses(first))


if __name__ == "__main__":
    unittest.main()
