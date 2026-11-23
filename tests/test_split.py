import copy
import random
import tracemalloc
import unicodedata
import unittest

from morph_paths import build_corpora, process


def base_dictionary():
    """dak only from a; daks from a-plural or b-singular; mini-dak only from a."""
    return {
        "states": ["base", "number", "done"],
        "accepting": ["number", "done"],
        "rules": [
            {
                "id": "singular",
                "from": "base",
                "to": "number",
                "prefix": "",
                "suffix": "",
                "requires": {"class": "N"},
                "adds": {"number": "one"},
            },
            {
                "id": "plural",
                "from": "base",
                "to": "number",
                "prefix": "",
                "suffix": "s",
                "requires": {"class": "N"},
                "adds": {"number": "many"},
            },
            {
                "id": "small",
                "from": "number",
                "to": "done",
                "prefix": "mini-",
                "suffix": "",
                "requires": {"number": "one"},
                "adds": {"size": "small"},
            },
        ],
        "lemmas": [
            {"id": "a", "stem": "dak", "state": "base", "features": {"class": "N"}},
            {"id": "b", "stem": "daks", "state": "base", "features": {"class": "N"}},
        ],
        "queries": [],
    }


def without_a_plural(data):
    """Same dictionary except lemma a no longer derives daks."""
    data = copy.deepcopy(data)
    data["rules"][1]["requires"] = {"class": "N", "rare": "yes"}
    return data


DAK_A = {"lemma": "a", "rules": ["singular"], "features": {"class": "N", "number": "one"}}
MINI_A = {
    "lemma": "a",
    "rules": ["singular", "small"],
    "features": {"class": "N", "number": "one", "size": "small"},
}
DAKS_B = {"lemma": "b", "rules": ["singular"], "features": {"class": "N", "number": "one"}}


def doc(doc_id, side, cost, tokens):
    return {
        "id": doc_id,
        "side": side,
        "drop_cost": cost,
        "tokens": [{"text": text, "gold": copy.deepcopy(gold)} for text, gold in tokens],
    }


def acceptance_docs():
    return [
        doc("t-dak", "train", 4, [("dak", DAK_A)]),
        doc("t-mini", "train", 4, [("mini-dak", MINI_A)]),
        doc("h-daks", "holdout", 7, [("daks", DAKS_B)]),
    ]


def forms_index(data):
    index = {}
    for form in process(data)["forms"]:
        index.setdefault(form["surface"], []).append(form)
    return index


def doc_keys(doc, index):
    surfaces = set()
    lemmas = set()
    for token in doc["tokens"]:
        surface = unicodedata.normalize("NFC", token["text"])
        surfaces.add(surface)
        lemmas.update(form["lemma"] for form in index.get(surface, ()))
    return surfaces, lemmas


class AcceptanceConflictTests(unittest.TestCase):
    def test_conflict_resolution_is_global_not_pairwise(self):
        result = build_corpora(base_dictionary(), acceptance_docs())
        # Both train documents share lemma a with the holdout document (daks
        # is also produced by a-plural).  Dropping the two train documents
        # would cost 8; dropping the single holdout document costs 7.
        self.assertEqual([d["id"] for d in result["train"]], ["t-dak", "t-mini"])
        self.assertEqual(result["holdout"], [])
        self.assertEqual(len(result["dropped"]), 1)
        entry = result["dropped"][0]
        self.assertEqual(entry["id"], "h-daks")
        self.assertEqual(entry["reason"], "conflict")
        self.assertEqual(entry["key"], {"category": "lemmas", "value": "a"})
        self.assertEqual(entry["with"], "t-dak")
        # Delivered documents keep id, token order, spelling and the gold.
        self.assertEqual(result["train"][0]["tokens"][0]["gold"], DAK_A)
        self.assertEqual(result["train"][1]["tokens"][0]["gold"], MINI_A)
        # Exposure covers exactly the delivered train documents.
        self.assertEqual(
            result["exposure"],
            {
                "doc_ids": ["t-dak", "t-mini"],
                "surfaces": ["dak", "mini-dak"],
                "lemmas": ["a"],
            },
        )

    def test_single_candidate_removal_delivers_all_three(self):
        data = without_a_plural(base_dictionary())
        result = build_corpora(data, acceptance_docs())
        self.assertEqual([d["id"] for d in result["train"]], ["t-dak", "t-mini"])
        self.assertEqual([d["id"] for d in result["holdout"]], ["h-daks"])
        self.assertEqual(result["dropped"], [])
        self.assertEqual(
            result["exposure"],
            {
                "doc_ids": ["t-dak", "t-mini"],
                "surfaces": ["dak", "mini-dak"],
                "lemmas": ["a"],
            },
        )

    def test_replaced_path_invalidates_signed_annotation(self):
        data = base_dictionary()
        data["rules"][0]["id"] = "singular2"  # same rule, new path identity
        gold_b = {"lemma": "b", "rules": ["singular2"], "features": {"class": "N", "number": "one"}}
        docs = [
            # Second token's signed path [singular] no longer exists, even
            # though an equivalent [singular2] candidate remains.
            doc("t-two", "train", 5, [("daks", gold_b), ("dak", DAK_A)]),
            doc("h-daks", "holdout", 3, [("daks", gold_b)]),
        ]
        result = build_corpora(data, docs)
        self.assertEqual(result["train"], [])  # first token not delivered either
        self.assertEqual([d["id"] for d in result["holdout"]], ["h-daks"])
        self.assertEqual(
            result["dropped"],
            [{"id": "t-two", "reason": "invalid", "token_positions": [1]}],
        )
        # The invalid document neither isolates the holdout document nor
        # produces any training exposure.
        self.assertEqual(
            result["exposure"], {"doc_ids": [], "surfaces": [], "lemmas": []}
        )


class HistoryChainTests(unittest.TestCase):
    def test_history_blocks_holdout_through_old_lemma(self):
        batch1 = build_corpora(
            base_dictionary(), [doc("t0", "train", 5, [("daks", DAKS_B)])]
        )
        self.assertEqual([d["id"] for d in batch1["train"]], ["t0"])
        # daks is analyzed by both a (plural) and b (singular): both leak.
        self.assertEqual(
            batch1["exposure"],
            {"doc_ids": ["t0"], "surfaces": ["daks"], "lemmas": ["a", "b"]},
        )

        revised = without_a_plural(base_dictionary())
        request = [doc("h1", "holdout", 3, [("dak", DAK_A)])]
        batch2 = build_corpora(revised, request, batch1["exposure"])
        self.assertEqual(batch2["holdout"], [])
        self.assertEqual(
            batch2["dropped"],
            [
                {
                    "id": "h1",
                    "reason": "history",
                    "match": {"category": "lemmas", "value": "a"},
                }
            ],
        )

    def test_cleared_history_allows_delivery(self):
        revised = without_a_plural(base_dictionary())
        request = [doc("h1", "holdout", 3, [("dak", DAK_A)])]
        result = build_corpora(revised, request)
        self.assertEqual([d["id"] for d in result["holdout"]], ["h1"])
        self.assertEqual(result["dropped"], [])

    def test_history_surface_match_without_shared_lemma(self):
        precomposed = {
            "states": ["s"],
            "accepting": ["s"],
            "rules": [],
            "lemmas": [
                {"id": "L2", "stem": "é", "state": "s", "features": {"kind": "precomposed"}}
            ],
            "queries": [],
        }
        batch1 = build_corpora(
            precomposed,
            [doc("t0", "train", 1, [("é", {"lemma": "L2", "rules": [], "features": {"kind": "precomposed"}})])],
        )
        self.assertEqual(
            batch1["exposure"],
            {"doc_ids": ["t0"], "surfaces": ["é"], "lemmas": ["L2"]},
        )

        # Revised dictionary: é is derived only by L1 from decomposed "e".
        decomposed = {
            "states": ["s0", "s1"],
            "accepting": ["s1"],
            "rules": [
                {
                    "id": "r1",
                    "from": "s0",
                    "to": "s1",
                    "prefix": "",
                    "suffix": "́",
                    "requires": {},
                    "adds": {"accent": "yes"},
                }
            ],
            "lemmas": [{"id": "L1", "stem": "e", "state": "s0", "features": {}}],
            "queries": [],
        }
        request = [
            doc(
                "h1",
                "holdout",
                1,
                [("é", {"lemma": "L1", "rules": ["r1"], "features": {"accent": "yes"}})],
            )
        ]
        batch2 = build_corpora(decomposed, request, batch1["exposure"])
        # No lemma in common with the old training, but the NFC surface hits.
        self.assertEqual(
            batch2["dropped"],
            [
                {
                    "id": "h1",
                    "reason": "history",
                    "match": {"category": "surfaces", "value": "é"},
                }
            ],
        )

    def test_decomposed_text_matches_nfc_and_original_preserved(self):
        decomposed = {
            "states": ["s0", "s1"],
            "accepting": ["s1"],
            "rules": [
                {
                    "id": "r1",
                    "from": "s0",
                    "to": "s1",
                    "prefix": "",
                    "suffix": "́",
                    "requires": {},
                    "adds": {"accent": "yes"},
                }
            ],
            "lemmas": [{"id": "L1", "stem": "e", "state": "s0", "features": {}}],
            "queries": [],
        }
        request = [
            doc(
                "h1",
                "holdout",
                1,
                [("é", {"lemma": "L1", "rules": ["r1"], "features": {"accent": "yes"}})],
            )
        ]
        result = build_corpora(decomposed, request)
        self.assertEqual([d["id"] for d in result["holdout"]], ["h1"])
        # The delivered token keeps the signed decomposed spelling.
        self.assertEqual(result["holdout"][0]["tokens"][0]["text"], "é")
        self.assertEqual(
            result["holdout"][0]["tokens"][0]["gold"],
            {"lemma": "L1", "rules": ["r1"], "features": {"accent": "yes"}},
        )

    def test_history_doc_id_blocks_holdout_but_not_train(self):
        data = base_dictionary()
        history = {"doc_ids": ["h-daks"], "surfaces": [], "lemmas": []}
        result = build_corpora(
            data, [doc("h-daks", "holdout", 7, [("daks", DAKS_B)])], history
        )
        self.assertEqual(
            result["dropped"],
            [
                {
                    "id": "h-daks",
                    "reason": "history",
                    "match": {"category": "doc_ids", "value": "h-daks"},
                }
            ],
        )
        # A train document sharing historical keys stays deliverable.
        history = {"doc_ids": ["old"], "surfaces": ["dak"], "lemmas": ["a"]}
        result = build_corpora(
            data, [doc("t-dak", "train", 4, [("dak", DAK_A)])], history
        )
        self.assertEqual([d["id"] for d in result["train"]], ["t-dak"])
        self.assertEqual(result["dropped"], [])


class OptimalityTests(unittest.TestCase):
    def _reference_min_drop(self, index, docs, history):
        """Independent exhaustive search for the minimum total drop cost."""
        usable = []
        cost = 0
        for d in docs:
            surfaces, lemmas = doc_keys(d, index)
            valid = all(
                any(
                    form["lemma"] == token["gold"]["lemma"]
                    and form["rules"] == token["gold"]["rules"]
                    and form["features"] == token["gold"]["features"]
                    for form in index.get(unicodedata.normalize("NFC", token["text"]), ())
                )
                for token in d["tokens"]
            )
            if not valid:
                cost += d["drop_cost"]
                continue
            if d["side"] == "holdout" and (
                d["id"] in history["doc_ids"]
                or surfaces & set(history["surfaces"])
                or lemmas & set(history["lemmas"])
            ):
                cost += d["drop_cost"]
                continue
            usable.append((d, surfaces, lemmas))
        best_kept = 0
        for mask in range(1 << len(usable)):
            kept = [usable[i] for i in range(len(usable)) if mask >> i & 1]
            trains = [k for k in kept if k[0]["side"] == "train"]
            holds = [k for k in kept if k[0]["side"] == "holdout"]
            if any(
                t[1] & h[1] or t[2] & h[2] for t in trains for h in holds
            ):
                continue
            best_kept = max(best_kept, sum(k[0]["drop_cost"] for k in kept))
        return cost + sum(u[0]["drop_cost"] for u in usable) - best_kept

    def test_exhaustive_small_batches_reach_global_optimum(self):
        rng = random.Random(20261123)
        for trial in range(40):
            surfaces = [f"w{i}" for i in range(rng.randint(1, 3))]
            lemmas = [
                {
                    "id": f"l{j}",
                    "stem": rng.choice(surfaces),
                    "state": "s",
                    "features": {"tag": f"l{j}"},
                }
                for j in range(rng.randint(1, 4))
            ]
            data = {
                "states": ["s"],
                "accepting": ["s"],
                "rules": [],
                "lemmas": lemmas,
                "queries": [],
            }
            index = forms_index(data)
            analyzable = sorted(index)
            docs = []
            for i in range(rng.randint(1, 8)):
                tokens = []
                for _ in range(rng.randint(1, 3)):
                    surface = rng.choice(analyzable)
                    form = rng.choice(index[surface])
                    gold = {
                        "lemma": form["lemma"],
                        "rules": form["rules"],
                        "features": form["features"],
                    }
                    if rng.random() < 0.15:  # inject an unusable annotation
                        gold = {"lemma": "zz", "rules": [], "features": {}}
                    tokens.append((surface, gold))
                docs.append(
                    doc(f"d{i}", rng.choice(["train", "holdout"]), rng.randint(1, 9), tokens)
                )
            history = {"doc_ids": [], "surfaces": [], "lemmas": []}
            if rng.random() < 0.5:
                history = {
                    "doc_ids": [f"x{i}" for i in range(rng.randint(0, 2))],
                    "surfaces": rng.sample(surfaces, rng.randint(0, len(surfaces))),
                    "lemmas": rng.sample(
                        [l["id"] for l in lemmas], rng.randint(0, len(lemmas))
                    ),
                }
            result = build_corpora(data, docs, history)
            by_id = {d["id"]: d for d in docs}
            dropped_cost = sum(by_id[e["id"]]["drop_cost"] for e in result["dropped"])
            self.assertEqual(
                dropped_cost,
                self._reference_min_drop(index, docs, history),
                f"trial {trial}",
            )
            self._check_result_shape(result, docs, index, history)

    def _check_result_shape(self, result, docs, index, history):
        by_id = {d["id"]: d for d in docs}
        train_ids = {d["id"] for d in result["train"]}
        hold_ids = {d["id"] for d in result["holdout"]}
        dropped_ids = {e["id"] for e in result["dropped"]}
        # Every input document lands in exactly one of the three outcomes.
        self.assertEqual(train_ids | hold_ids | dropped_ids, set(by_id))
        self.assertEqual(len(train_ids) + len(hold_ids) + len(dropped_ids), len(docs))
        # Delivered sides share no key.
        train_surfaces, train_lemmas = set(), set()
        for doc_id in train_ids:
            surfaces, lemmas = doc_keys(by_id[doc_id], index)
            train_surfaces |= surfaces
            train_lemmas |= lemmas
        for doc_id in hold_ids:
            surfaces, lemmas = doc_keys(by_id[doc_id], index)
            self.assertFalse(train_surfaces & surfaces)
            self.assertFalse(train_lemmas & lemmas)
        # Every drop reason points at real delivered or historical data.
        for entry in result["dropped"]:
            source = by_id[entry["id"]]
            surfaces, lemmas = doc_keys(source, index)
            if entry["reason"] == "invalid":
                self.assertEqual(
                    set(entry),
                        {"id", "reason", "token_positions"},
                )
                self.assertTrue(entry["token_positions"])
                for position in entry["token_positions"]:
                    token = source["tokens"][position]
                    gold = token["gold"]
                    self.assertFalse(
                        any(
                            form["lemma"] == gold["lemma"]
                            and form["rules"] == gold["rules"]
                            and form["features"] == gold["features"]
                            for form in index.get(
                                unicodedata.normalize("NFC", token["text"]), ()
                            )
                        )
                    )
            elif entry["reason"] == "history":
                self.assertEqual(source["side"], "holdout")
                match = entry["match"]
                self.assertIn(match["value"], history[match["category"]])
                own = {
                    "doc_ids": {source["id"]},
                    "surfaces": surfaces,
                    "lemmas": lemmas,
                }
                self.assertIn(match["value"], own[match["category"]])
            else:
                self.assertEqual(entry["reason"], "conflict")
                other_side = hold_ids if source["side"] == "train" else train_ids
                self.assertIn(entry["with"], other_side)
                kept_surfaces, kept_lemmas = doc_keys(by_id[entry["with"]], index)
                shared = {
                    "surfaces": surfaces & kept_surfaces,
                    "lemmas": lemmas & kept_lemmas,
                }
                self.assertIn(
                    entry["key"]["value"], shared[entry["key"]["category"]]
                )
        # The exposure record is exactly the delivered train key union.
        self.assertEqual(
            result["exposure"],
            {
                "doc_ids": sorted(train_ids),
                "surfaces": sorted(train_surfaces),
                "lemmas": sorted(train_lemmas),
            },
        )


class ScaleTests(unittest.TestCase):
    def test_large_shared_surface_batch_keeps_whole_holdout_side(self):
        data = {
            "states": ["s"],
            "accepting": ["s"],
            "rules": [],
            "lemmas": [
                {"id": "a", "stem": "dak", "state": "s", "features": {"class": "N"}}
            ],
            "queries": [],
        }
        gold = {"lemma": "a", "rules": [], "features": {"class": "N"}}
        docs = [
            {
                "id": f"t{i:05}",
                "side": "train",
                "drop_cost": 1,
                "tokens": [{"text": "dak", "gold": dict(gold)}],
            }
            for i in range(6000)
        ] + [
            {
                "id": f"h{i:05}",
                "side": "holdout",
                "drop_cost": 2,
                "tokens": [{"text": "dak", "gold": dict(gold)}],
            }
            for i in range(6000)
        ]
        history = {"doc_ids": [], "surfaces": [], "lemmas": []}
        tracemalloc.start()
        result = build_corpora(data, docs, history)
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        self.assertLessEqual(peak, 128 * 1024 * 1024)
        # Dropping all train documents costs 6000, the holdout side 12000:
        # the complete holdout side must be delivered, nothing sampled away.
        self.assertEqual(len(result["holdout"]), 6000)
        self.assertEqual(result["train"], [])
        self.assertEqual(len(result["dropped"]), 6000)
        self.assertTrue(
            all(entry["reason"] == "conflict" for entry in result["dropped"])
        )
        for entry in result["dropped"][:10]:
            self.assertEqual(entry["with"], "h00000")
            self.assertEqual(
                entry["key"], {"category": "surfaces", "value": "dak"}
            )
        self.assertEqual(
            result["exposure"], {"doc_ids": [], "surfaces": [], "lemmas": []}
        )


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.data = base_dictionary()
        self.docs = acceptance_docs()

    def test_structural_errors_reject_whole_request(self):
        bad_dictionary = base_dictionary()
        bad_dictionary["rules"][0]["to"] = "absent"
        with self.assertRaises(ValueError):
            build_corpora(bad_dictionary, self.docs)

        duplicated = self.docs + [doc("t-dak", "train", 1, [("dak", DAK_A)])]
        with self.assertRaises(ValueError):
            build_corpora(self.data, duplicated)

        for mutate in (
            lambda d: d.update(side="both"),
            lambda d: d.update(drop_cost=0),
            lambda d: d.update(drop_cost=1000001),
            lambda d: d.update(drop_cost="4"),
            lambda d: d.update(drop_cost=True),
            lambda d: d.update(tokens=[]),
            lambda d: d.update(tokens="dak"),
            lambda d: d.pop("side"),
            lambda d: d.update(extra=1),
        ):
            docs = acceptance_docs()
            mutate(docs[0])
            with self.assertRaises(ValueError):
                build_corpora(self.data, docs)

        for bad_token in (
            {},
            {"text": "dak"},
            {"text": "", "gold": DAK_A},
            {"text": "dak\u0001", "gold": DAK_A},
            {"text": "dak", "gold": {"lemma": "a", "rules": ["singular"]}},
            {"text": "dak", "gold": {"lemma": "a!", "rules": ["singular"], "features": {}}},
            {"text": "dak", "gold": {"lemma": "a", "rules": "singular", "features": {}}},
            {"text": "dak", "gold": {"lemma": "a", "rules": ["singular"], "features": []}},
        ):
            with self.assertRaises(ValueError):
                build_corpora(self.data, [doc("x", "train", 1, []) | {"tokens": [bad_token]}])

        with self.assertRaises(ValueError):
            build_corpora(self.data, self.docs, {"doc_ids": [], "surfaces": []})
        with self.assertRaises(ValueError):
            build_corpora(
                self.data,
                self.docs,
                {"doc_ids": [], "surfaces": ["é"], "lemmas": []},
            )
        with self.assertRaises(ValueError):
            build_corpora(
                self.data,
                self.docs,
                {"doc_ids": ["bad id"], "surfaces": [], "lemmas": []},
            )

    def test_batch_size_limits(self):
        data = self.data
        many = [doc(f"d{i}", "train", 1, [("dak", DAK_A)]) for i in range(12001)]
        with self.assertRaises(ValueError):
            build_corpora(data, many)
        tokens = [("dak", DAK_A)] * 60001
        with self.assertRaises(ValueError):
            build_corpora(data, [doc("big", "train", 1, tokens)])

    def test_unfindable_references_are_business_invalid(self):
        docs = [
            doc("t-unknown-lemma", "train", 1, [("dak", {"lemma": "zz", "rules": ["singular"], "features": {"class": "N", "number": "one"}})]),
            doc("t-unknown-rule", "train", 1, [("dak", {"lemma": "a", "rules": ["nope"], "features": {"class": "N", "number": "one"}})]),
            doc("t-wrong-features", "train", 1, [("dak", {"lemma": "a", "rules": ["singular"], "features": {"class": "N", "number": "two"}})]),
            doc("t-no-form", "train", 1, [("zzz", DAK_A)]),
        ]
        result = build_corpora(self.data, docs)
        self.assertEqual(result["train"], [])
        self.assertEqual(
            result["dropped"],
            [
                {"id": "t-no-form", "reason": "invalid", "token_positions": [0]},
                {"id": "t-unknown-lemma", "reason": "invalid", "token_positions": [0]},
                {"id": "t-unknown-rule", "reason": "invalid", "token_positions": [0]},
                {"id": "t-wrong-features", "reason": "invalid", "token_positions": [0]},
            ],
        )

    def test_permuted_inputs_give_identical_result(self):
        docs = acceptance_docs() + [
            doc("h-mini", "holdout", 2, [("mini-dak", MINI_A)]),
            doc("t-bad", "train", 1, [("dak", {"lemma": "a", "rules": ["nope"], "features": {}})]),
        ]
        history = {"doc_ids": ["old-1"], "surfaces": ["daks"], "lemmas": ["b"]}
        baseline = build_corpora(base_dictionary(), docs, history)

        shuffled_docs = [docs[2], docs[4], docs[0], docs[3], docs[1]]
        shuffled_history = {
            "doc_ids": ["old-1", "old-1"],
            "surfaces": ["daks"],
            "lemmas": ["b"],
        }
        data = base_dictionary()
        data["rules"] = list(reversed(data["rules"]))
        data["lemmas"] = list(reversed(data["lemmas"]))
        data["states"] = list(reversed(data["states"]))
        data["accepting"] = list(reversed(data["accepting"]))
        again = build_corpora(data, shuffled_docs, shuffled_history)
        self.assertEqual(again, baseline)

    def test_empty_batch_and_single_sided_batch(self):
        result = build_corpora(self.data, [])
        self.assertEqual(
            result,
            {
                "train": [],
                "holdout": [],
                "dropped": [],
                "exposure": {"doc_ids": [], "surfaces": [], "lemmas": []},
            },
        )
        result = build_corpora(self.data, [doc("t-dak", "train", 4, [("dak", DAK_A)])])
        self.assertEqual([d["id"] for d in result["train"]], ["t-dak"])
        self.assertEqual(result["holdout"], [])


if __name__ == "__main__":
    unittest.main()
