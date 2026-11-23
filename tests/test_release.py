import copy
import hashlib
import json
import unittest
from pathlib import Path

from morph_paths import process, build_release, bucket_for_surface
from morph_paths.canonical import dumps_canonical, loads_canonical
from morph_paths.release import BUCKET_IDS

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text())


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.data = load_fixture("scenario_a.json")
        self.release = build_release(self.data)

    def test_bucket_assignment_uses_first_hex_of_nfc_sha256(self):
        for surface, bucket in [("é", "4"), ("b", "3"), ("e", "3"), ("a", "c"), ("z", "5")]:
            self.assertEqual(
                bucket_for_surface(surface),
                hashlib.sha256(surface.encode("utf-8")).hexdigest()[0],
            )
            self.assertEqual(bucket_for_surface(surface), bucket)

    def test_manifest_contract(self):
        manifest = loads_canonical(self.release.manifest_bytes())
        self.assertEqual(set(manifest), {"version", "buckets"})
        self.assertEqual(manifest["version"], 1)
        self.assertEqual(set(manifest["buckets"]), set(BUCKET_IDS))
        for digest in manifest["buckets"].values():
            self.assertRegex(digest, r"^[0-9a-f]{64}\Z")
        self.assertEqual(
            self.release.release_id,
            hashlib.sha256(self.release.manifest_bytes()).hexdigest(),
        )
        self.assertRegex(self.release.release_id, r"^[0-9a-f]{64}\Z")

    def test_canonical_bytes_and_shard_digests(self):
        for bucket in BUCKET_IDS:
            raw = self.release.shard_bytes(bucket)
            self.assertEqual(raw, dumps_canonical(loads_canonical(raw)))
            self.assertFalse(raw.endswith(b"\n"))
            self.assertEqual(
                self.release.buckets[bucket], hashlib.sha256(raw).hexdigest()
            )
            # Every shard, including empty ones, is a real JSON array.
            self.assertIsInstance(loads_canonical(raw), list)
        self.assertEqual(loads_canonical(self.release.shard_bytes("5")), [])

    def test_shards_preserve_order_rows_and_distinct_analyses(self):
        forms = process(self.data)["forms"]
        shard4 = loads_canonical(self.release.shard_bytes("4"))
        self.assertEqual(
            [(f["lemma"], tuple(f["rules"])) for f in shard4],
            [("L1", ("r1",)), ("L2", ("r2",))],
        )
        # Each shard follows the canonical forms order; the union of shards is
        # exactly the compiled forms.
        collected = []
        for bucket in BUCKET_IDS:
            rows = loads_canonical(self.release.shard_bytes(bucket))
            self.assertEqual(
                rows,
                sorted(rows, key=lambda f: (f["surface"], f["lemma"], f["rules"])),
            )
            collected.extend(rows)
        self.assertEqual(
            sorted(collected, key=lambda f: (f["surface"], f["lemma"], f["rules"])),
            forms,
        )
        for row in collected:
            self.assertEqual(set(row), {"lemma", "surface", "features", "rules"})

    def test_queries_and_input_order_do_not_change_identity(self):
        baseline_id = self.release.release_id
        baseline_shards = dict(self.release.shards)

        reordered = copy.deepcopy(self.data)
        reordered["queries"] = list(reversed(reordered["queries"]))
        reordered["queries"].append({"id": "extra", "surface": "zzz"})
        reordered["lemmas"] = list(reversed(reordered["lemmas"]))
        reordered["rules"] = list(reversed(reordered["rules"]))
        other = build_release(reordered)
        self.assertEqual(other.release_id, baseline_id)
        self.assertEqual(other.shards, baseline_shards)

        dropped = copy.deepcopy(self.data)
        dropped["queries"] = []
        self.assertEqual(build_release(dropped).release_id, baseline_id)

    def test_compiler_failure_produces_no_release(self):
        bad = copy.deepcopy(self.data)
        bad["rules"][0]["to"] = "absent"
        with self.assertRaises(ValueError):
            build_release(bad)

    def test_equivalent_inputs_yield_identical_artifacts(self):
        again = build_release(copy.deepcopy(self.data))
        self.assertEqual(again.release_id, self.release.release_id)
        self.assertEqual(again.manifest_bytes(), self.release.manifest_bytes())
        for bucket in BUCKET_IDS:
            self.assertEqual(again.shard_bytes(bucket), self.release.shard_bytes(bucket))


if __name__ == "__main__":
    unittest.main()
