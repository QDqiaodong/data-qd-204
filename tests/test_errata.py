import copy
import random
import tracemalloc
import unittest
import unicodedata

from morph_paths import apply_errata, process, trace_analysis
from morph_paths.errata import (
    MAX_ERRATA,
    AnalysisNotFoundError,
    ConflictingSlicesError,
    ErrataError,
    IntervalNotWritableError,
    RevisionRejectedError,
    _nfd_atoms,
)


def dictionary(states, accepting, rules, lemmas, queries=None):
    return {
        "states": states,
        "accepting": accepting,
        "rules": rules,
        "lemmas": lemmas,
        "queries": [] if queries is None else queries,
    }


def rule(rule_id, from_state, to_state, prefix="", suffix="", requires=None, adds=None):
    return {
        "id": rule_id,
        "from": from_state,
        "to": to_state,
        "prefix": prefix,
        "suffix": suffix,
        "requires": {} if requires is None else requires,
        "adds": {} if adds is None else adds,
    }


def lemma(lemma_id, stem, state="s0", features=None):
    return {
        "id": lemma_id,
        "stem": stem,
        "state": state,
        "features": {} if features is None else features,
    }


def combining_dictionary():
    """stem 'a', then suffix U+0301, then suffix U+0327."""
    return dictionary(
        ["s0", "s1", "s2"],
        ["s2"],
        [
            rule("acute", "s0", "s1", suffix="́"),
            rule("cedilla", "s1", "s2", suffix="̧"),
        ],
        [lemma("L", "a")],
    )


def tone_dictionary(suffix="̈́"):
    """stem 'a', one rule appending the suffix; only that path is accepted."""
    return dictionary(
        ["s0", "s1"],
        ["s1"],
        [rule("tone", "s0", "s1", suffix=suffix)],
        [lemma("L", "a")],
    )


def shared_stem_dictionary(stem="ab"):
    """Empty path and the append-'s' path are both accepted."""
    return dictionary(
        ["s0", "s1"],
        ["s0", "s1"],
        [rule("s", "s0", "s1", suffix="s")],
        [lemma("L", stem)],
    )


def request(lemma_id, rules, start, end, replacement, confirmed):
    return {
        "lemma": lemma_id,
        "rules": rules,
        "start": start,
        "end": end,
        "replacement": replacement,
        "confirmed": confirmed,
    }


class TraceTests(unittest.TestCase):
    def test_combining_marks_attributed_by_canonical_order(self):
        # NFC('a' + U+0301 + U+0327) reorders the marks: á + U+0327.
        traced = trace_analysis(combining_dictionary(), "L", ["acute", "cedilla"])
        self.assertEqual(traced["analysis"]["surface"], "á̧")
        positions = traced["positions"]
        self.assertEqual([p["index"] for p in positions], [0, 1])
        self.assertEqual(positions[0]["char"], "á")
        self.assertEqual(
            positions[0]["sources"],
            [
                {"lemma": "L", "field": "stem", "index": 0},
                {"rule": "acute", "field": "suffix", "index": 0},
            ],
        )
        # The cedilla moved before the acute in canonical order; the input
        # mark order must not misattribute the two suffixes.
        self.assertEqual(positions[1]["char"], "̧")
        self.assertEqual(
            positions[1]["sources"],
            [{"rule": "cedilla", "field": "suffix", "index": 0}],
        )

    def test_prefix_outer_inner_order(self):
        data = dictionary(
            ["s0", "s1", "s2"],
            ["s2"],
            [rule("p1", "s0", "s1", prefix="ab"), rule("p2", "s1", "s2", prefix="cd")],
            [lemma("L", "m")],
        )
        traced = trace_analysis(data, "L", ["p1", "p2"])
        self.assertEqual(traced["analysis"]["surface"], "cdabm")
        self.assertEqual(
            [[(next(iter(s)), s["index"]) for s in p["sources"]] for p in traced["positions"]],
            [
                [("rule", 0)],  # c: outer rule p2 prefix[0]
                [("rule", 1)],  # d: p2 prefix[1]
                [("rule", 0)],  # a: inner rule p1 prefix[0]
                [("rule", 1)],  # b: p1 prefix[1]
                [("lemma", 0)],  # m: stem[0]
            ],
        )
        self.assertEqual(
            [p["sources"][0]["rule"] if "rule" in p["sources"][0] else "stem" for p in traced["positions"]],
            ["p2", "p2", "p1", "p1", "stem"],
        )

    def test_homograph_paths_have_own_sources(self):
        data = dictionary(
            ["s0", "s1"],
            ["s0", "s1"],
            [rule("plural", "s0", "s1", suffix="s")],
            [lemma("a", "dak"), lemma("b", "daks")],
        )
        by_b = trace_analysis(data, "b", [])
        by_a = trace_analysis(data, "a", ["plural"])
        self.assertEqual(by_b["analysis"]["surface"], "daks")
        self.assertEqual(by_a["analysis"]["surface"], "daks")
        self.assertEqual(
            [p["sources"] for p in by_b["positions"]],
            [[{"lemma": "b", "field": "stem", "index": i}] for i in range(4)],
        )
        self.assertEqual(
            [p["sources"] for p in by_a["positions"]],
            [[{"lemma": "a", "field": "stem", "index": i}] for i in range(3)]
            + [[{"rule": "plural", "field": "suffix", "index": 0}]],
        )

    def test_empty_path_and_intermediate_acceptance(self):
        data = dictionary(
            ["s0", "s1", "s2"],
            ["s0", "s1"],
            [rule("step", "s0", "s1", suffix="x"), rule("more", "s1", "s2", suffix="y")],
            [lemma("L", "a")],
        )
        empty = trace_analysis(data, "L", [])
        self.assertEqual(empty["analysis"]["rules"], [])
        self.assertEqual(empty["analysis"]["surface"], "a")
        self.assertEqual(
            empty["positions"],
            [{"index": 0, "char": "a", "sources": [{"lemma": "L", "field": "stem", "index": 0}]}],
        )
        # Accepted at the intermediate state s1, not only at a dead end.
        mid = trace_analysis(data, "L", ["step"])
        self.assertEqual(mid["analysis"]["surface"], "ax")
        self.assertEqual(
            mid["positions"][1]["sources"],
            [{"rule": "step", "field": "suffix", "index": 0}],
        )

    def test_invalid_dictionary_rejected_despite_walkable_path(self):
        data = combining_dictionary()
        data["states"].append("unused")
        data["rules"].append(rule("loop", "unused", "unused"))
        with self.assertRaises(ValueError):
            trace_analysis(data, "L", ["acute", "cedilla"])

    def test_queries_do_not_affect_trace(self):
        baseline = trace_analysis(combining_dictionary(), "L", ["acute", "cedilla"])
        data = combining_dictionary()
        data["queries"] = [
            {"id": "q1", "surface": "something"},
            {"id": "q2", "surface": "á̧"},
        ]
        self.assertEqual(trace_analysis(data, "L", ["acute", "cedilla"]), baseline)

    def test_analysis_not_found_is_not_substituted(self):
        data = dictionary(
            ["s0", "s1"],
            ["s0", "s1"],
            [rule("plural", "s0", "s1", suffix="s", requires={"class": "N"})],
            [lemma("a", "dak", features={"class": "N"}), lemma("b", "daks", features={"class": "V"})],
        )
        # 'daks' exists for (b, []) and (a, [plural]); the same surface with
        # lemma b through the plural rule is never produced (features block
        # it), and no homograph analysis may be substituted for it.
        with self.assertRaises(AnalysisNotFoundError) as caught:
            trace_analysis(data, "b", ["plural"])
        self.assertEqual(caught.exception.lemma, "b")
        self.assertEqual(caught.exception.rules, ["plural"])
        with self.assertRaises(AnalysisNotFoundError):
            trace_analysis(data, "b", ["plural", "plural"])
        with self.assertRaises(ValueError):
            trace_analysis(data, "not an id!", [])

    def test_original_indices_of_unnormalized_field(self):
        # Stem written decomposed: indices refer to the original spelling.
        data = dictionary(["s0"], ["s0"], [], [lemma("L", "é")])
        traced = trace_analysis(data, "L", [])
        self.assertEqual(traced["analysis"]["surface"], "é")
        self.assertEqual(
            traced["positions"],
            [
                {
                    "index": 0,
                    "char": "é",
                    "sources": [
                        {"lemma": "L", "field": "stem", "index": 0},
                        {"lemma": "L", "field": "stem", "index": 1},
                    ],
                }
            ],
        )

    def test_0344_single_source_index_feeds_both_positions(self):
        traced = trace_analysis(tone_dictionary(), "L", ["tone"])
        self.assertEqual(traced["analysis"]["surface"], "ä́")
        self.assertEqual(
            traced["positions"][0]["sources"],
            [
                {"lemma": "L", "field": "stem", "index": 0},
                {"rule": "tone", "field": "suffix", "index": 0},
            ],
        )
        self.assertEqual(
            traced["positions"][1]["sources"],
            [{"rule": "tone", "field": "suffix", "index": 0}],
        )

    def test_trace_result_matches_compiled_form_and_inputs_untouched(self):
        data = combining_dictionary()
        snapshot = copy.deepcopy(data)
        traced = trace_analysis(data, "L", ["acute", "cedilla"])
        form = next(
            f
            for f in process(data)["forms"]
            if f["lemma"] == "L" and f["rules"] == ["acute", "cedilla"]
        )
        self.assertEqual(traced["analysis"], form)
        self.assertEqual(data, snapshot)


class ErrataTests(unittest.TestCase):
    def test_0344_delete_rejected_then_split_spelling_writable(self):
        data = tone_dictionary("̈́")
        # The single source code point U+0344 feeds both output positions, so
        # deleting only final position 1 cannot be written back.
        with self.assertRaises(IntervalNotWritableError) as caught:
            apply_errata(data, [request("L", ["tone"], 1, 2, "", "ä")])
        self.assertEqual(caught.exception.request, 0)
        self.assertEqual(caught.exception.lemma, "L")
        self.assertEqual(caught.exception.rules, ["tone"])

        # Same dictionary with the suffix spelled as U+0308 U+0301: the
        # compiled analysis is identical.
        split = tone_dictionary("̈́")
        self.assertEqual(process(data)["forms"], process(split)["forms"])
        # The same errata now touches only the original slice [1, 2).
        delivered = apply_errata(split, [request("L", ["tone"], 1, 2, "", "ä")])
        self.assertEqual(delivered["dictionary"]["rules"][0]["suffix"], "̈")
        self.assertEqual(
            delivered["writes"],
            [{"rule": "tone", "field": "suffix", "start": 1, "end": 2, "old": "́", "new": ""}],
        )
        forms = process(delivered["dictionary"])["forms"]
        self.assertEqual([f["surface"] for f in forms], ["ä"])

    def test_shared_stem_single_request_rejected(self):
        # Only the empty path requested: the unrequested 'abs' would follow.
        with self.assertRaises(RevisionRejectedError) as caught:
            apply_errata(
                shared_stem_dictionary(),
                [request("L", [], 0, 1, "x", "xb")],
            )
        self.assertEqual((caught.exception.lemma, caught.exception.rules), ("L", ["s"]))

    def test_shared_write_applied_once_when_both_request(self):
        delivered = apply_errata(
            shared_stem_dictionary(),
            [
                request("L", [], 0, 1, "x", "xb"),
                request("L", ["s"], 0, 1, "x", "xbs"),
            ],
        )
        self.assertEqual(
            delivered["writes"],
            [{"lemma": "L", "field": "stem", "start": 0, "end": 1, "old": "a", "new": "x"}],
        )
        self.assertEqual(delivered["dictionary"]["lemmas"][0]["stem"], "xb")
        forms = process(delivered["dictionary"])["forms"]
        self.assertEqual(sorted(f["surface"] for f in forms), ["xb", "xbs"])

    def test_same_slice_different_replacement_conflicts(self):
        with self.assertRaises(ConflictingSlicesError) as caught:
            apply_errata(
                shared_stem_dictionary(),
                [
                    request("L", [], 0, 1, "x", "xb"),
                    request("L", ["s"], 0, 1, "y", "ybs"),
                ],
            )
        self.assertEqual(caught.exception.field, {"lemma": "L", "field": "stem"})
        self.assertEqual(caught.exception.slices[0]["requests"], [0, 1])

    def test_intersecting_slices_conflict(self):
        data = shared_stem_dictionary("abcd")
        with self.assertRaises(ConflictingSlicesError):
            apply_errata(
                data,
                [
                    request("L", [], 0, 2, "xy", "xycd"),
                    request("L", ["s"], 1, 3, "w", "awwds"),
                ],
            )

    def test_disjoint_slices_use_old_coordinates(self):
        data = shared_stem_dictionary("abcd")
        delivered = apply_errata(
            data,
            [
                request("L", [], 0, 1, "xyz", "xyzbwd"),
                request("L", ["s"], 2, 3, "w", "xyzbwds"),
            ],
        )
        # Both slices are coordinates in the pre-modification stem; the first
        # replacement growing by two does not shift the second slice.
        self.assertEqual(delivered["dictionary"]["lemmas"][0]["stem"], "xyzbwd")
        self.assertEqual(
            delivered["writes"],
            [
                {"lemma": "L", "field": "stem", "start": 0, "end": 1, "old": "a", "new": "xyz"},
                {"lemma": "L", "field": "stem", "start": 2, "end": 3, "old": "c", "new": "w"},
            ],
        )
        forms = process(delivered["dictionary"])["forms"]
        self.assertEqual(sorted(f["surface"] for f in forms), ["xyzbwd", "xyzbwds"])

    def test_branch_bound_revision_rejected(self):
        # One accepted empty path; a walkable rule to a non-accepting state
        # appends 63 'x'.  Growing the stem to 'bb' pushes that branch's
        # derived surface over the 64-code-point compile bound.
        data = dictionary(
            ["s0", "s1"],
            ["s0"],
            [rule("pad", "s0", "s1", suffix="x" * 63)],
            [lemma("L", "a")],
        )
        with self.assertRaises(RevisionRejectedError) as caught:
            apply_errata(data, [request("L", [], 0, 1, "bb", "bb")])
        self.assertIn("compile", caught.exception.detail)

    def test_analysis_not_found_in_apply(self):
        with self.assertRaises(AnalysisNotFoundError):
            apply_errata(
                shared_stem_dictionary(),
                [request("L", ["absent"], 0, 1, "x", "xbs")],
            )

    def test_request_structure_validation(self):
        data = shared_stem_dictionary()
        good = request("L", [], 0, 1, "x", "xb")
        bad_requests = [
            [{k: v for k, v in good.items() if k != "confirmed"}],  # missing key
            [dict(good, note="extra")],  # extra key
            [dict(good, start=True)],  # boolean is not an integer
            [dict(good, start=1, end=1)],  # empty interval
            [dict(good, end=3)],  # interval outside the surface
            [dict(good, replacement=5)],  # replacement must be a string
            [dict(good, confirmed="é")],  # confirmed must be NFC
            [dict(good, confirmed="")],  # confirmed must be non-empty
            [dict(good, lemma="no id!")],  # invalid lemma id
            [good, dict(good)],  # duplicate analysis identity
        ]
        for batch in bad_requests:
            with self.assertRaises(ValueError, msg=batch):
                apply_errata(data, batch)
        with self.assertRaises(ValueError):
            apply_errata(data, "not-a-list")
        with self.assertRaises(ValueError):
            apply_errata(data, [good] * (MAX_ERRATA + 1))
        # Errors are distinguishable ValueError subclasses.
        self.assertTrue(issubclass(AnalysisNotFoundError, ErrataError))
        self.assertTrue(issubclass(ErrataError, ValueError))

    def test_invalid_dictionary_rejected_before_any_request(self):
        data = shared_stem_dictionary()
        data["states"].append("unused")
        data["rules"].append(rule("loop", "unused", "unused"))
        with self.assertRaises(ValueError):
            apply_errata(data, [request("L", [], 0, 1, "x", "xb")])

    def test_untouched_text_keeps_original_spelling(self):
        # Lemma 'keep' has a decomposed (non-NFC) stem spelling; an errata
        # batch that only touches lemma 'fix' must preserve it byte-identical.
        data = dictionary(
            ["s0"],
            ["s0"],
            [],
            [lemma("keep", "é"), lemma("fix", "bt")],
        )
        delivered = apply_errata(data, [request("fix", [], 1, 2, "d", "bd")])
        stems = {l["id"]: l["stem"] for l in delivered["dictionary"]["lemmas"]}
        self.assertEqual(stems["keep"], "é")  # not normalized to 'é'
        self.assertEqual(stems["fix"], "bd")
        self.assertEqual(
            delivered["writes"],
            [{"lemma": "fix", "field": "stem", "start": 1, "end": 2, "old": "t", "new": "d"}],
        )

    def test_empty_batch_delivers_identical_dictionary(self):
        data = shared_stem_dictionary()
        delivered = apply_errata(data, [])
        self.assertEqual(delivered["dictionary"], data)
        self.assertEqual(delivered["writes"], [])
        self.assertIsNot(delivered["dictionary"], data)

    def test_order_of_requests_does_not_change_delivery(self):
        data = shared_stem_dictionary("abcd")
        batch = [
            request("L", [], 0, 1, "xyz", "xyzbwd"),
            request("L", ["s"], 2, 3, "w", "xyzbwds"),
        ]
        forward = apply_errata(data, batch)
        backward = apply_errata(data, list(reversed(batch)))
        self.assertEqual(forward, backward)

    def test_apply_does_not_mutate_inputs(self):
        data = shared_stem_dictionary()
        batch = [
            request("L", [], 0, 1, "x", "xb"),
            request("L", ["s"], 0, 1, "x", "xbs"),
        ]
        snapshot_data = copy.deepcopy(data)
        snapshot_batch = copy.deepcopy(batch)
        apply_errata(data, batch)
        self.assertEqual(data, snapshot_data)
        self.assertEqual(batch, snapshot_batch)


class MemoryTests(unittest.TestCase):
    @staticmethod
    def layered_dictionary(layers=14):
        states = [f"s{i}" for i in range(layers + 1)]
        rules = [
            rule(f"r{i}{variant}", f"s{i}", f"s{i + 1}")
            for i in range(layers)
            for variant in ("a", "b")
        ]
        return dictionary(
            states,
            [states[-1]],
            rules,
            [lemma("L", "x" * 64)],
        )

    def test_single_trace_result_and_memory_bound(self):
        # 2**14 == 16,384 accepted homograph paths over a 64-code-point stem.
        data = self.layered_dictionary()
        path = [f"r{i}{'ab'[i % 2]}" for i in range(14)]
        tracemalloc.start()
        try:
            traced = trace_analysis(data, "L", path)
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 48 * 1024 * 1024)
        self.assertEqual(traced["analysis"]["rules"], path)
        self.assertEqual(traced["analysis"]["surface"], "x" * 64)
        self.assertEqual(
            traced["positions"],
            [
                {
                    "index": i,
                    "char": "x",
                    "sources": [{"lemma": "L", "field": "stem", "index": i}],
                }
                for i in range(64)
            ],
        )


class CanonicalAlignmentTests(unittest.TestCase):
    def test_nfd_atoms_match_unicodedata_nfd(self):
        rng = random.Random(20261123)
        alphabet = ["a", "e", "á", "́", "̈", "̧", "̃", "̈́", "β", "ς", "m", "-"]
        for _ in range(300):
            text = "".join(rng.choice(alphabet) for _ in range(rng.randrange(1, 12)))
            atoms = _nfd_atoms([(char, index) for index, char in enumerate(text)])
            self.assertEqual(
                "".join(char for char, _ in atoms),
                unicodedata.normalize("NFD", text),
            )

    def test_fuzzed_trace_invariants(self):
        rng = random.Random(7301)
        alphabet = ["a", "e", "o", "́", "̈", "̧", "̃", "̈́", "x"]
        for _ in range(120):
            stem = "".join(rng.choice(alphabet) for _ in range(rng.randrange(1, 6)))
            prefix = "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 4)))
            suffix = "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 4)))
            data = dictionary(
                ["s0", "s1"],
                ["s0", "s1"],
                [rule("r", "s0", "s1", prefix=prefix, suffix=suffix)],
                [lemma("L", stem)],
            )
            for path in ([], ["r"]):
                traced = trace_analysis(data, "L", path)
                positions = traced["positions"]
                self.assertEqual(
                    len(positions), len(traced["analysis"]["surface"])
                )
                seen = set()
                for position in positions:
                    self.assertTrue(position["sources"])
                    for source in position["sources"]:
                        owner = source.get("lemma", source.get("rule"))
                        field = source["field"]
                        if "lemma" in source:
                            self.assertEqual(owner, "L")
                            self.assertEqual(field, "stem")
                            original = stem
                        else:
                            self.assertEqual(owner, "r")
                            original = {"prefix": prefix, "suffix": suffix}[field]
                        self.assertLess(source["index"], len(original))
                        seen.add((owner, field, source["index"]))
                # Every original character of every participating non-empty
                # field contributes to at least one final position.
                expected = {("L", "stem", i) for i in range(len(stem))}
                if path:
                    expected |= {("r", "prefix", i) for i in range(len(prefix))}
                    expected |= {("r", "suffix", i) for i in range(len(suffix))}
                self.assertEqual(seen, expected)
                # Deterministic across calls.
                self.assertEqual(trace_analysis(data, "L", path), traced)


if __name__ == "__main__":
    unittest.main()
