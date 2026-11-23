import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from morph_paths import build_release, Workspace, LookupStatus
from morph_paths.canonical import dumps_canonical, loads_canonical
from morph_paths.receiver import (
    StagingConflictError,
    NotCompleteError,
    NoStagingError,
    DataIntegrityError,
    parse_manifest,
    parse_shard,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
SRC = Path(__file__).resolve().parent.parent / "src"


def load_fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def release_for(name):
    return build_release(load_fixture(name))


def batch(release, buckets):
    return [{"bucket": b, "bytes": release.shard_bytes(b)} for b in buckets]


# Helper script run in a fresh interpreter: it only ever sees the bytes handed
# to it, never the publisher's full release object or source input.
_HELPER = r"""
import json, sys
sys.path.insert(0, %(src)r)
from morph_paths import Workspace, LookupStatus

mode = sys.argv[1]
workspace = sys.argv[2]
ws = Workspace(workspace)
if mode == "lookup":
    release_id, surface = sys.argv[3], sys.argv[4]
    result = ws.lookup(release_id, surface)
    print(json.dumps({"status": result.status,
                      "analyses": [[a["lemma"], a["rules"]] for a in result.analyses]}))
elif mode == "state":
    state = ws.state()
    print(json.dumps({
        "current": None if state["current"] is None else
            {"release_id": state["current"]["release_id"],
             "buckets": sorted(state["current"]["buckets"])},
        "staging": None if state["staging"] is None else
            {"release_id": state["staging"]["release_id"],
             "buckets": sorted(state["staging"]["buckets"]),
             "received": sorted(state["staging"]["received"])},
    }))
elif mode == "activate":
    try:
        ws.activate()
        print("activated")
    except Exception as exc:
        print(type(exc).__name__ + ":" + str(exc))
"""


def run_helper(mode, workspace, *args):
    script = tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, dir=tempfile.gettempdir()
    )
    try:
        script.write(_HELPER % {"src": str(SRC)})
        script.close()
        proc = subprocess.run(
            [sys.executable, "-I", script.name, mode, str(workspace), *args],
            capture_output=True,
            text=True,
            check=True,
        )
        return proc.stdout.strip()
    finally:
        os.unlink(script.name)


class ScenarioOneTests(unittest.TestCase):
    def setUp(self):
        self.release = release_for("scenario_a")
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Workspace(Path(self.tmp.name) / "ws")

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_install_with_buckets_3_4(self):
        # Before first install: explicitly no current view.
        self.assertEqual(
            self.ws.lookup(self.release.release_id, "é").status,
            LookupStatus.NO_VIEW,
        )
        self.ws.prepare_switch(
            self.release.release_id, self.release.manifest_bytes(), ["4", "3"]
        )
        self.assertEqual(
            sorted(b for b, _ in self.ws.missing_shards()), ["3", "4"]
        )
        self.ws.deliver(batch(self.release, ["3", "4"]))
        self.assertEqual(self.ws.missing_shards(), [])
        self.ws.activate()

        result = self.ws.lookup(self.release.release_id, "é")
        self.assertEqual(result.status, LookupStatus.OK)
        # Precomposed and decomposed-spelling queries normalize identically.
        self.assertEqual(
            [(a["lemma"], a["rules"]) for a in result.analyses],
            [("L1", ["r1"]), ("L2", ["r2"])],
        )
        decomposed = self.ws.lookup(
            self.release.release_id, "é"
        )
        self.assertEqual(decomposed.analyses, result.analyses)

        # Covered bucket, genuinely no analysis.
        self.assertEqual(
            self.ws.lookup(self.release.release_id, "e").status,
            LookupStatus.EMPTY,
        )
        # Uncovered bucket.
        self.assertEqual(
            self.ws.lookup(self.release.release_id, "a").status,
            LookupStatus.NOT_COVERED,
        )

    def test_reprepare_same_target_keeps_shards_other_target_rejected(self):
        self.ws.prepare_switch(
            self.release.release_id, self.release.manifest_bytes(), {"3", "4"}
        )
        self.ws.deliver(batch(self.release, ["3"]))
        # Same target, different argument order, re-prepared: progress kept.
        self.ws.prepare_switch(
            self.release.release_id, self.release.manifest_bytes(), ["3", "4"]
        )
        self.assertEqual([b for b, _ in self.ws.missing_shards()], ["4"])

        other = release_for("scenario_b1")
        with self.assertRaises(StagingConflictError):
            self.ws.prepare_switch(other.release_id, other.manifest_bytes(), ["3", "4"])
        self.ws.abandon_switch()
        self.assertEqual(
            self.ws.lookup(self.release.release_id, "é").status,
            LookupStatus.NO_VIEW,
        )
        with self.assertRaises(NoStagingError):
            self.ws.missing_shards()

    def test_failed_batch_changes_nothing(self):
        self.ws.prepare_switch(
            self.release.release_id, self.release.manifest_bytes(), ["3", "4"]
        )
        good = self.release.shard_bytes("3")
        bad_shard4 = good  # bytes of bucket 3 presented as bucket 4
        with self.assertRaises(ValueError):
            self.ws.deliver(
                [
                    {"bucket": "3", "bytes": good},
                    {"bucket": "4", "bytes": bad_shard4},
                ]
            )
        # Whole batch rejected: nothing registered, query state unchanged.
        self.assertEqual(
            sorted(b for b, _ in self.ws.missing_shards()), ["3", "4"]
        )
        self.ws.deliver(batch(self.release, ["3"]))
        # Replaying an already received valid shard is a no-op.
        self.ws.deliver(batch(self.release, ["3"]))
        self.assertEqual([b for b, _ in self.ws.missing_shards()], ["4"])
        with self.assertRaises(ValueError):
            self.ws.deliver(batch(self.release, ["5"]))  # not a target bucket


class ScenarioTwoTests(unittest.TestCase):
    def _install_a(self, root):
        release = release_for("scenario_a")
        ws = Workspace(root)
        ws.prepare_switch(release.release_id, release.manifest_bytes(), ["3", "4"])
        ws.deliver(batch(release, ["3", "4"]))
        ws.activate()
        return ws, release

    def test_successor_versions_and_reuse(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ws, release_a = self._install_a(Path(tmp.name) / "ws")
        b0 = release_for("scenario_b0")
        b1 = release_for("scenario_b1")

        # B0: buckets 3 and 4 are byte-identical to A, nothing to receive.
        ws.prepare_switch(b0.release_id, b0.manifest_bytes(), ["3", "4"])
        self.assertEqual(ws.missing_shards(), [])
        ws.activate()
        # The old id no longer answers; the new view serves the same analyses.
        self.assertEqual(
            ws.lookup(release_a.release_id, "é").status,
            LookupStatus.VERSION_MISMATCH,
        )
        b0_answer = ws.lookup(b0.release_id, "é")
        self.assertEqual(b0_answer.status, LookupStatus.OK)
        self.assertEqual(
            [(a["lemma"], a["rules"]) for a in b0_answer.analyses],
            [("L1", ["r1"]), ("L2", ["r2"])],
        )

        # Independent receiver state for B1.
        other_root = Path(tmp.name) / "other"
        ws2, _ = self._install_a(other_root)
        ws2.prepare_switch(b1.release_id, b1.manifest_bytes(), ["3", "4"])
        # Bucket 3 reused from A; bucket 4 changed and must be re-delivered.
        self.assertEqual([b for b, _ in ws2.missing_shards()], ["4"])
        with self.assertRaises(NotCompleteError):
            ws2.activate()
        # Old view still answers while the switch is pending.
        old = ws2.lookup(release_a.release_id, "é")
        self.assertEqual([a["rules"] for a in old.analyses], [["r1"], ["r2"]])
        # A's bucket-4 bytes cannot serve B1.
        with self.assertRaises(ValueError):
            ws2.deliver([{"bucket": "4", "bytes": release_a.shard_bytes("4")}])
        ws2.deliver(batch(b1, ["4"]))
        ws2.activate()
        new = ws2.lookup(b1.release_id, "é")
        self.assertEqual(len(new.analyses), 2)
        self.assertEqual([a["rules"] for a in new.analyses], [["r2"], ["r2"]])
        # Bucket 3 was never re-delivered but still answers.
        self.assertEqual(ws2.lookup(b1.release_id, "b").status, LookupStatus.OK)


class ScenarioThreeTests(unittest.TestCase):
    def test_coverage_switch_4_5_empty_shard_and_recovery(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "ws"
        release_a = release_for("scenario_a")
        b1 = release_for("scenario_b1")

        ws = Workspace(root)
        ws.prepare_switch(release_a.release_id, release_a.manifest_bytes(), ["3", "4"])
        ws.deliver(batch(release_a, ["3", "4"]))
        ws.activate()

        ws.prepare_switch(b1.release_id, b1.manifest_bytes(), ["4", "5"])
        self.assertEqual(
            sorted(b for b, _ in ws.missing_shards()), ["4", "5"]
        )
        # Correct bucket 4 plus a wrong bucket 5: whole batch fails, both stay missing.
        wrong5 = dumps_canonical(
            [
                {
                    "lemma": "L9",
                    "surface": "z",
                    "features": {"x": "y"},
                    "rules": ["rx"],
                }
            ]
        )
        with self.assertRaises(ValueError):
            ws.deliver(
                [
                    {"bucket": "4", "bytes": b1.shard_bytes("4")},
                    {"bucket": "5", "bytes": wrong5},
                ]
            )
        self.assertEqual(
            sorted(b for b, _ in ws.missing_shards()), ["4", "5"]
        )
        # Deliver correct bucket 4 alone.
        ws.deliver(batch(b1, ["4"]))
        # Reopen in a brand-new process: only bucket 5 missing.
        state = json.loads(run_helper("state", root))
        self.assertEqual(state["current"]["release_id"], release_a.release_id)
        self.assertEqual(state["staging"]["received"], ["4"])
        # Replay bucket 4: missing set unchanged.
        ws.deliver(batch(b1, ["4"]))
        self.assertEqual([b for b, _ in ws.missing_shards()], ["5"])
        ws.deliver(batch(b1, ["5"]))
        self.assertEqual(ws.missing_shards(), [])

        # Pre-activation semantics on the old view.
        old_b = json.loads(run_helper("lookup", root, release_a.release_id, "b"))
        old_z = json.loads(run_helper("lookup", root, release_a.release_id, "z"))
        self.assertEqual(old_b["status"], LookupStatus.OK)
        self.assertEqual(old_z["status"], LookupStatus.NOT_COVERED)

        ws.activate()
        new_b = json.loads(run_helper("lookup", root, b1.release_id, "b"))
        new_z = json.loads(run_helper("lookup", root, b1.release_id, "z"))
        self.assertEqual(new_b["status"], LookupStatus.NOT_COVERED)
        self.assertEqual(new_z["status"], LookupStatus.EMPTY)
        state = json.loads(run_helper("state", root))
        self.assertIsNone(state["staging"])
        self.assertEqual(state["current"]["buckets"], ["4", "5"])

    def test_misbucketed_shard_rejected_even_when_digest_matches(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        b1 = release_for("scenario_b1")
        # Build a shard whose own digest is genuinely registered for bucket 5,
        # but whose rows belong to another bucket.  We cannot reuse a real
        # digest; instead mutate the manifest mapping so the hash check passes
        # while content ownership must still fail.
        manifest = loads_canonical(b1.manifest_bytes())
        foreign = loads_canonical(b1.shard_bytes("3"))
        foreign_bytes = dumps_canonical(foreign)
        manifest["buckets"]["5"] = __import__("hashlib").sha256(
            foreign_bytes
        ).hexdigest()
        forged_manifest = dumps_canonical(manifest)
        forged_id = __import__("hashlib").sha256(forged_manifest).hexdigest()

        root = Path(tmp.name) / "ws"
        ws = Workspace(root)
        ws.prepare_switch(forged_id, forged_manifest, ["5"])
        with self.assertRaisesRegex(ValueError, "does not belong to bucket 5"):
            ws.deliver([{"bucket": "5", "bytes": foreign_bytes}])
        self.assertEqual([b for b, _ in ws.missing_shards()], ["5"])


class ScenarioFourTests(unittest.TestCase):
    CRASH_HOOK = (
        "import morph_paths.receiver as r\n"
        "r._CRASH_BEFORE_COMMIT = lambda ws: (_ for _ in ()).throw(SystemExit(23))\n"
    )
    CRASH_HOOK_AFTER = (
        "import morph_paths.receiver as r\n"
        "r._CRASH_AFTER_COMMIT = lambda ws: (_ for _ in ()).throw(SystemExit(24))\n"
    )

    def _stage_b1(self, root):
        release_a = release_for("scenario_a")
        b1 = release_for("scenario_b1")
        ws = Workspace(root)
        ws.prepare_switch(release_a.release_id, release_a.manifest_bytes(), ["3", "4"])
        ws.deliver(batch(release_a, ["3", "4"]))
        ws.activate()
        ws.prepare_switch(b1.release_id, b1.manifest_bytes(), ["4", "5"])
        ws.deliver(batch(b1, ["4", "5"]))
        return release_a, b1

    def _crashed_activation(self, root, hook):
        script = tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, dir=tempfile.gettempdir()
        )
        try:
            script.write(
                "import sys\n"
                f"sys.path.insert(0, {str(SRC)!r})\n"
                + hook
                + f"from morph_paths import Workspace\n"
                f"Workspace({str(root)!r}).activate()\n"
            )
            script.close()
            proc = subprocess.run(
                [sys.executable, "-I", script.name], capture_output=True, text=True
            )
            self.assertNotEqual(proc.returncode, 0)
        finally:
            os.unlink(script.name)

    def test_crash_before_commit_keeps_old_view_and_resumable_staging(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "ws"
        release_a, b1 = self._stage_b1(root)
        self._crashed_activation(root, self.CRASH_HOOK)

        # Fresh process, no in-memory objects: old view still serves.
        answer = json.loads(run_helper("lookup", root, release_a.release_id, "b"))
        self.assertEqual(answer["status"], LookupStatus.OK)
        state = json.loads(run_helper("state", root))
        self.assertEqual(state["current"]["release_id"], release_a.release_id)
        self.assertEqual(state["staging"]["received"], ["4", "5"])
        # The complete staged plan can still be activated from the new process.
        self.assertEqual(run_helper("activate", root), "activated")
        answer = json.loads(run_helper("lookup", root, b1.release_id, "z"))
        self.assertEqual(answer["status"], LookupStatus.EMPTY)

    def test_crash_after_commit_is_already_the_new_view(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "ws"
        release_a, b1 = self._stage_b1(root)
        self._crashed_activation(root, self.CRASH_HOOK_AFTER)

        state = json.loads(run_helper("state", root))
        self.assertEqual(state["current"]["release_id"], b1.release_id)
        self.assertIsNone(state["staging"])  # reconciled post-commit state
        answer = json.loads(run_helper("lookup", root, b1.release_id, "z"))
        self.assertEqual(answer["status"], LookupStatus.EMPTY)
        # No re-delivery needed; the old version must not answer anymore.
        old = json.loads(run_helper("lookup", root, release_a.release_id, "z"))
        self.assertEqual(old["status"], LookupStatus.VERSION_MISMATCH)


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.release = release_for("scenario_a")
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Workspace(Path(self.tmp.name) / "ws")

    def tearDown(self):
        self.tmp.cleanup()

    def test_manifest_checks(self):
        raw = self.release.manifest_bytes()
        parse_manifest(raw, self.release.release_id)
        with self.assertRaises(ValueError):
            parse_manifest(raw, "0" * 64)  # id mismatch
        with self.assertRaises(ValueError):
            parse_manifest(raw + b" ", self.release.release_id)  # non-canonical
        with self.assertRaises(ValueError):
            parse_manifest(raw.decode("utf-8"), self.release.release_id)
        tampered = bytearray(raw)
        tampered[-2] = ord("0") if tampered[-2] != ord("0") else ord("1")
        with self.assertRaises(ValueError):
            parse_manifest(bytes(tampered), self.release.release_id)

    def test_shard_checks(self):
        raw = self.release.shard_bytes("4")
        digest = self.release.buckets["4"]
        parse_shard(raw, "4", digest)
        with self.assertRaises(ValueError):
            parse_shard(raw, "3", digest)  # ownership failure
        with self.assertRaises(ValueError):
            parse_shard(raw, "4", "0" * 64)  # digest failure
        # Duplicate JSON keys rejected.
        dup = b'[{"lemma":"x","lemma":"y","surface":"z","features":{},"rules":[]}]'
        with self.assertRaises(ValueError):
            parse_shard(dup, "a", __import__("hashlib").sha256(dup).hexdigest())

    def test_corrupted_covered_shard_is_data_error_not_empty(self):
        self.ws.prepare_switch(
            self.release.release_id, self.release.manifest_bytes(), ["3", "4"]
        )
        self.ws.deliver(batch(self.release, ["3", "4"]))
        self.ws.activate()
        blob = next((Path(self.tmp.name) / "ws" / "blobs").rglob("4.json"))
        blob.write_bytes(b"[]")
        with self.assertRaises(DataIntegrityError):
            self.ws.lookup(self.release.release_id, "é")
        blob.unlink()
        with self.assertRaises(DataIntegrityError):
            self.ws.lookup(self.release.release_id, "é")


if __name__ == "__main__":
    unittest.main()
