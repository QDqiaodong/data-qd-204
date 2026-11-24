import hashlib
import json
import os
import tempfile
import tracemalloc
import unittest
from pathlib import Path
from unittest import mock

from morph_paths import (
    AmbiguousAssignmentError,
    ArchiveError,
    DigestMismatchError,
    MigrationError,
    NoSolutionError,
    PublishError,
    TargetExistsError,
    migrate_annotations,
    process,
)


# --------------------------------------------------------------------------- #
# Dictionary helpers
# --------------------------------------------------------------------------- #

def dictionary(states, accepting, rules, lemmas):
    return {
        "states": states,
        "accepting": accepting,
        "rules": rules,
        "lemmas": lemmas,
        "queries": [],
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


def twin_dictionary(rule_ids):
    """One lemma with two rules that derive the same surface and features."""
    return dictionary(
        ["s0", "s1"],
        ["s1"],
        [rule(rule_id, "s0", "s1", suffix="s") for rule_id in rule_ids],
        [lemma("L", "dak")],
    )


def form_by_identity(data, lemma_id, path):
    for form in process(data)["forms"]:
        if form["lemma"] == lemma_id and form["rules"] == list(path):
            return form
    raise AssertionError("analysis not produced")


# --------------------------------------------------------------------------- #
# Archive helpers
# --------------------------------------------------------------------------- #

def annotation(doc, pos, text, ref, analysis):
    return {
        "doc": doc,
        "pos": pos,
        "text": text,
        "ref": ref,
        "analysis": {
            "lemma": analysis["lemma"],
            "surface": analysis["surface"],
            "features": dict(sorted(analysis["features"].items())),
            "rules": list(analysis["rules"]),
        },
    }


def archive_bytes(rows):
    return b"".join(
        (
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        for row in rows
    )


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def file_digest(path):
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


class MigrationCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def source(self, raw, name="archive.jsonl"):
        path = self.root / name
        path.write_bytes(raw)
        return path

    def target(self, name="package.jsonl"):
        return self.root / name

    def assert_no_target(self, path):
        self.assertFalse(os.path.lexists(path), f"target exists: {path}")
        leftovers = [
            entry for entry in path.parent.iterdir()
            if entry.name.startswith(".migration-")
        ]
        self.assertEqual(leftovers, [])


# --------------------------------------------------------------------------- #
# Assignment acceptance
# --------------------------------------------------------------------------- #

class AssignmentTests(MigrationCase):
    def setUp(self):
        super().setUp()
        self.old = twin_dictionary(["ra", "rb"])
        self.new = twin_dictionary(["rx", "ry"])
        self.registry = [
            {"ref": "A", "lemma": "L", "rules": ["ra"]},
            {"ref": "B", "lemma": "L", "rules": ["rb"]},
        ]
        self.row_a = annotation(
            "d1", 0, "daks", "A", form_by_identity(self.old, "L", ["ra"])
        )

    def migrate(self, raw, approvals, path=None, sha=None):
        source = self.source(raw)
        target = self.target() if path is None else path
        return migrate_annotations(
            self.old,
            self.new,
            self.registry,
            approvals,
            source,
            digest(raw) if sha is None else sha,
            target,
        )

    def package_rows(self, path):
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
        ]
        return rows

    def test_forced_assignment_even_when_archive_only_uses_a(self):
        raw = archive_bytes([self.row_a])
        result = self.migrate(raw, {"A": [["rx"], ["ry"]], "B": [["rx"]]})
        rows = self.package_rows(Path(result["path"]))
        # First the complete new registry, sorted by ref, then the archive.
        self.assertEqual([row["ref"] for row in rows[:2]], ["A", "B"])
        self.assertEqual(rows[0]["analysis"]["rules"], ["ry"])
        self.assertEqual(rows[1]["analysis"]["rules"], ["rx"])
        self.assertEqual(rows[2]["ref"], "A")
        self.assertEqual(rows[2]["analysis"]["rules"], ["ry"])
        self.assertEqual(result["annotation_lines"], 1)
        self.assertEqual(result["total_lines"], 3)
        self.assertEqual(result["registry"], rows[:2])

    def test_both_refs_receive_distinct_new_analyses(self):
        raw = archive_bytes(
            [
                self.row_a,
                annotation(
                    "d2", 0, "daks", "B", form_by_identity(self.old, "L", ["rb"])
                ),
            ]
        )
        result = self.migrate(raw, {"A": [["rx"], ["ry"]], "B": [["rx"]]})
        rows = self.package_rows(Path(result["path"]))
        bound = {row["ref"]: row["analysis"]["rules"] for row in rows[2:]}
        self.assertEqual(bound, {"A": ["ry"], "B": ["rx"]})
        registry = {row["ref"]: row["analysis"]["rules"] for row in rows[:2]}
        self.assertEqual(registry, bound)

    def test_second_approval_makes_assignment_ambiguous(self):
        raw = archive_bytes([self.row_a])
        with self.assertRaises(AmbiguousAssignmentError):
            self.migrate(raw, {"A": [["rx"], ["ry"]], "B": [["rx"], ["ry"]]})
        self.assert_no_target(self.target())

    def test_restricting_a_makes_assignment_unsolvable(self):
        raw = archive_bytes([self.row_a])
        with self.assertRaises(NoSolutionError):
            self.migrate(raw, {"A": [["rx"]], "B": [["rx"]]})
        self.assert_no_target(self.target())

    def test_unused_registry_ref_participates_in_assignment(self):
        # The archive only names A, but B has no qualifying analysis at all.
        blocked = twin_dictionary(["rx", "ry"])
        blocked["rules"][1]["adds"] = {"extra": "yes"}
        source = self.source(archive_bytes([self.row_a]))
        with self.assertRaises(NoSolutionError):
            migrate_annotations(
                self.old,
                blocked,
                self.registry,
                {"A": [["rx"], ["ry"]], "B": [["rx"]]},
                source,
                file_digest(source),
                self.target(),
            )
        self.assert_no_target(self.target())

    def test_approval_cannot_replace_feature_check(self):
        changed = twin_dictionary(["rx", "ry"])
        changed["rules"][0]["adds"] = {"tone": "high"}
        source = self.source(archive_bytes([self.row_a]))
        with self.assertRaises(NoSolutionError):
            migrate_annotations(
                self.old,
                changed,
                self.registry,
                {"A": [["rx"], ["ry"]], "B": [["rx"]]},
                source,
                file_digest(source),
                self.target(),
            )
        self.assert_no_target(self.target())

    def test_approval_cannot_replace_surface_check(self):
        changed = twin_dictionary(["rx", "ry"])
        changed["rules"][0]["suffix"] = "z"
        source = self.source(archive_bytes([self.row_a]))
        with self.assertRaises(NoSolutionError):
            migrate_annotations(
                self.old,
                changed,
                self.registry,
                {"A": [["rx"], ["ry"]], "B": [["rx"]]},
                source,
                file_digest(source),
                self.target(),
            )
        self.assert_no_target(self.target())

    def test_unchanged_rule_id_is_still_validated(self):
        # B is unlisted, so it may only keep path ["rb"].  The new dictionary
        # reuses the id but changes the features, so it must not inherit.
        changed = twin_dictionary(["ra", "rb"])
        changed["rules"][1]["adds"] = {"tone": "high"}
        raw = archive_bytes(
            [
                annotation(
                    "d2", 0, "daks", "B", form_by_identity(self.old, "L", ["rb"])
                )
            ]
        )
        source = self.source(raw)
        with self.assertRaises(NoSolutionError):
            migrate_annotations(
                self.old,
                changed,
                self.registry,
                {"A": [["ra"]]},
                source,
                digest(raw),
                self.target(),
            )
        self.assert_no_target(self.target())

    def test_approved_same_id_with_changed_features_is_ineligible(self):
        # The approved path id exists in the new version but no longer yields
        # the same features, so the approval cannot stand in for the check.
        changed = twin_dictionary(["ra", "rb"])
        changed["rules"][0]["adds"] = {"tone": "high"}
        raw = archive_bytes([self.row_a])
        source = self.source(raw)
        with self.assertRaises(NoSolutionError):
            migrate_annotations(
                self.old,
                changed,
                self.registry,
                {"A": [["ra"]], "B": [["rb"]]},
                source,
                digest(raw),
                self.target(),
            )
        self.assert_no_target(self.target())

    def test_approved_same_id_with_changed_surface_is_ineligible(self):
        changed = twin_dictionary(["ra", "rb"])
        changed["rules"][0]["suffix"] = "z"
        raw = archive_bytes([self.row_a])
        source = self.source(raw)
        with self.assertRaises(NoSolutionError):
            migrate_annotations(
                self.old,
                changed,
                self.registry,
                {"A": [["ra"]], "B": [["rb"]]},
                source,
                digest(raw),
                self.target(),
            )
        self.assert_no_target(self.target())

    def test_different_lemma_is_not_eligible(self):
        # A new analysis with the right surface and features under another
        # lemma is not the same analysis identity.
        changed = dictionary(
            ["t0", "t1"],
            ["t1"],
            [rule("rx", "t0", "t1", suffix="s"), rule("ry", "t0", "t1", suffix="s")],
            [lemma("M", "dak", state="t0")],
        )
        raw = archive_bytes([self.row_a])
        source = self.source(raw)
        with self.assertRaises(NoSolutionError):
            migrate_annotations(
                self.old,
                changed,
                self.registry,
                {"A": [["rx"], ["ry"]], "B": [["rx"]]},
                source,
                digest(raw),
                self.target(),
            )
        self.assert_no_target(self.target())

    def test_empty_approval_list_allows_no_path(self):
        raw = archive_bytes([self.row_a])
        with self.assertRaises(NoSolutionError):
            self.migrate(raw, {"A": [], "B": [["rx"]]})
        self.assert_no_target(self.target())

    def test_unlisted_ref_keeps_original_path_when_valid(self):
        same = twin_dictionary(["ra", "rb"])
        source = self.source(archive_bytes([self.row_a]))
        result = migrate_annotations(
            self.old, same, self.registry, None, source,
            file_digest(source), self.target(),
        )
        rows = self.package_rows(Path(result["path"]))
        self.assertEqual(rows[0]["analysis"]["rules"], ["ra"])
        self.assertEqual(rows[1]["analysis"]["rules"], ["rb"])

    def test_package_is_byte_deterministic(self):
        raw = archive_bytes([self.row_a])
        first = self.migrate(raw, {"A": [["rx"], ["ry"]], "B": [["rx"]]})
        payload = Path(first["path"]).read_bytes()
        second = self.migrate(
            raw,
            {"A": [["rx"], ["ry"]], "B": [["rx"]]},
            path=self.root / "second.jsonl",
        )
        self.assertEqual(payload, Path(second["path"]).read_bytes())


# --------------------------------------------------------------------------- #
# Structural input validation
# --------------------------------------------------------------------------- #

class StructuralTests(MigrationCase):
    def setUp(self):
        super().setUp()
        self.old = twin_dictionary(["ra", "rb"])
        self.new = twin_dictionary(["rx", "ry"])
        self.raw = archive_bytes(
            [
                annotation(
                    "d1", 0, "daks", "A", form_by_identity(self.old, "L", ["ra"])
                )
            ]
        )

    def call(self, registry, approvals, old=None, new=None):
        source = self.source(self.raw)
        return migrate_annotations(
            self.old if old is None else old,
            self.new if new is None else new,
            registry,
            approvals,
            source,
            digest(self.raw),
            self.target(),
        )

    def registry(self):
        return [
            {"ref": "A", "lemma": "L", "rules": ["ra"]},
            {"ref": "B", "lemma": "L", "rules": ["rb"]},
        ]

    def test_duplicate_ref_rejected(self):
        registry = self.registry()
        registry[1]["ref"] = "A"
        with self.assertRaises(ValueError):
            self.call(registry, {})

    def test_duplicate_old_analysis_rejected(self):
        registry = self.registry()
        registry[1]["rules"] = ["ra"]
        with self.assertRaises(ValueError):
            self.call(registry, {})

    def test_non_produced_old_analysis_rejected(self):
        registry = self.registry()
        registry[1]["rules"] = ["absent"]
        with self.assertRaises(ValueError):
            self.call(registry, {})

    def test_approval_for_unregistered_ref_rejected(self):
        with self.assertRaises(ValueError):
            self.call(self.registry(), {"C": [["rx"]]})

    def test_duplicate_approved_path_rejected(self):
        with self.assertRaises(ValueError):
            self.call(self.registry(), {"A": [["rx"], ["rx"]]})

    def test_more_than_eight_approved_paths_rejected(self):
        with self.assertRaises(ValueError):
            self.call(self.registry(), {"A": [
                ["p0"], ["p1"], ["p2"], ["p3"], ["p4"],
                ["p5"], ["p6"], ["p7"], ["p8"],
            ]})

    def test_extra_registry_field_rejected(self):
        registry = self.registry()
        registry[0]["extra"] = 1
        with self.assertRaises(ValueError):
            self.call(registry, {})

    def test_old_compile_error_blocks_even_unrelated_paths(self):
        broken = twin_dictionary(["ra", "rb"])
        broken["states"].append("loop")
        broken["rules"].append(rule("loop", "loop", "loop"))
        with self.assertRaises(ValueError):
            self.call(self.registry(), {}, old=broken)

    def test_new_compile_error_blocks_delivery(self):
        broken = twin_dictionary(["rx", "ry"])
        broken["rules"][0]["to"] = "missing"
        with self.assertRaises(ValueError):
            self.call(self.registry(), {}, new=broken)
        self.assert_no_target(self.target())

    def test_bad_digest_format_rejected(self):
        source = self.source(self.raw)
        with self.assertRaises(ValueError):
            migrate_annotations(
                self.old, self.new, self.registry(), {},
                source, "ABC", self.target(),
            )


# --------------------------------------------------------------------------- #
# Archive validation
# --------------------------------------------------------------------------- #

class ArchiveTests(MigrationCase):
    def setUp(self):
        super().setUp()
        self.old = twin_dictionary(["ra", "rb"])
        self.new = twin_dictionary(["rx", "ry"])
        self.registry = [
            {"ref": "A", "lemma": "L", "rules": ["ra"]},
            {"ref": "B", "lemma": "L", "rules": ["rb"]},
        ]
        self.approvals = {"A": [["rx"], ["ry"]], "B": [["rx"]]}
        self.form_a = form_by_identity(self.old, "L", ["ra"])
        self.form_b = form_by_identity(self.old, "L", ["rb"])
        self.rows = [
            annotation("d1", 0, "daks", "A", self.form_a),
            annotation("d2", 5, "daks", "A", self.form_a),
            annotation("d2", 9, "daks", "B", self.form_b),
        ]

    def run_migration(self, raw, sha=None):
        source = self.source(raw)
        target = self.target()
        return migrate_annotations(
            self.old,
            self.new,
            self.registry,
            self.approvals,
            source,
            digest(raw) if sha is None else sha,
            target,
        )

    def test_swapped_old_path_on_last_row_reports_its_line(self):
        # Keep ref A but sign the row with the other real old analysis rb.
        rows = list(self.rows)
        rows[-1] = annotation("d3", 0, "daks", "A", self.form_b)
        raw = archive_bytes(rows)
        with self.assertRaises(ArchiveError) as caught:
            self.run_migration(raw)
        self.assertEqual(caught.exception.line, 3)
        self.assertIsInstance(caught.exception, MigrationError)
        self.assert_no_target(self.target())

    def test_digest_mismatch_leaves_no_target(self):
        raw = archive_bytes(self.rows)
        with self.assertRaises(DigestMismatchError):
            self.run_migration(raw, sha="0" * 64)
        self.assert_no_target(self.target())

    def test_existing_target_is_not_overwritten(self):
        target = self.target()
        target.write_bytes(b"keep me")
        raw = archive_bytes(self.rows)
        with self.assertRaises(TargetExistsError):
            self.run_migration(raw)
        self.assertEqual(target.read_bytes(), b"keep me")

    def test_non_increasing_key_rejected(self):
        rows = list(self.rows)
        rows[2] = annotation("d2", 5, "daks", "B", self.form_b)
        with self.assertRaises(ArchiveError) as caught:
            self.run_migration(archive_bytes(rows))
        self.assertEqual(caught.exception.line, 3)
        self.assert_no_target(self.target())

    def test_equal_doc_requires_increasing_pos(self):
        rows = [
            annotation("d1", 4, "daks", "A", self.form_a),
            annotation("d1", 3, "daks", "A", self.form_a),
        ]
        with self.assertRaises(ArchiveError) as caught:
            self.run_migration(archive_bytes(rows))
        self.assertEqual(caught.exception.line, 2)

    def test_unregistered_row_ref_rejected(self):
        rows = list(self.rows)
        rows[0]["ref"] = "Z"
        with self.assertRaises(ArchiveError) as caught:
            self.run_migration(archive_bytes(rows))
        self.assertEqual(caught.exception.line, 1)

    def test_duplicate_json_key_rejected(self):
        row = json.dumps(self.rows[0], ensure_ascii=False, sort_keys=True)
        raw = (row[:-1] + ',"ref":"A"}\n').encode("utf-8")
        with self.assertRaises(ArchiveError) as caught:
            self.run_migration(raw)
        self.assertEqual(caught.exception.line, 1)

    def test_extra_field_rejected(self):
        row = dict(self.rows[0])
        row["note"] = "x"
        with self.assertRaises(ArchiveError):
            self.run_migration(archive_bytes([row]))

    def test_missing_field_rejected(self):
        row = {
            key: value for key, value in self.rows[0].items() if key != "pos"
        }
        with self.assertRaises(ArchiveError):
            self.run_migration(archive_bytes([row]))

    def test_negative_or_boolean_pos_rejected(self):
        for bad in (-1, True):
            row = dict(self.rows[0])
            row["pos"] = bad
            with self.assertRaises(ArchiveError):
                self.run_migration(archive_bytes([row]))

    def test_text_must_normalize_to_old_surface(self):
        row = dict(self.rows[0])
        row["text"] = "dakz"
        with self.assertRaises(ArchiveError):
            self.run_migration(archive_bytes([row]))
        row["text"] = "dak"
        with self.assertRaises(ArchiveError):
            self.run_migration(archive_bytes([row]))

    def test_control_character_text_rejected(self):
        row = dict(self.rows[0])
        row["text"] = "da\x00ks"
        with self.assertRaises(ArchiveError):
            self.run_migration(archive_bytes([row]))

    def test_line_without_lf_rejected(self):
        raw = archive_bytes([self.rows[0]]).rstrip(b"\n")
        with self.assertRaises(ArchiveError) as caught:
            self.run_migration(raw)
        self.assertEqual(caught.exception.line, 1)

    def test_oversized_line_rejected(self):
        # A longer-than-16-KiB line is rejected by the length guard before any
        # JSON parsing, and the error still locates the line.
        row = dict(self.rows[0])
        row["text"] = "d" * 20000
        with self.assertRaises(ArchiveError) as caught:
            self.run_migration(archive_bytes([row]))
        self.assertEqual(caught.exception.line, 1)
        self.assert_no_target(self.target())

    def test_decomposed_text_spelling_is_preserved(self):
        old = dictionary(
            ["s0", "s1"],
            ["s1"],
            [rule("acute", "s0", "s1", suffix="́")],
            [lemma("L", "e")],
        )
        new = dictionary(
            ["t0", "t1"],
            ["t1"],
            [rule("acute", "t0", "t1", suffix="́")],
            [lemma("L", "e", state="t0")],
        )
        form = form_by_identity(old, "L", ["acute"])
        self.assertEqual(form["surface"], "é")
        # The archive keeps its original decomposed spelling: e + U+0301.
        row = annotation("d1", 0, "é", "A", form)
        self.assertNotEqual(row["text"], form["surface"])
        raw = archive_bytes([row])
        source = self.source(raw)
        result = migrate_annotations(
            old,
            new,
            [{"ref": "A", "lemma": "L", "rules": ["acute"]}],
            {},
            source,
            digest(raw),
            self.target(),
        )
        rows = [
            json.loads(line)
            for line in Path(result["path"]).read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(rows[1]["text"], "é")
        self.assertEqual(rows[1]["analysis"]["surface"], "é")

    def test_empty_archive_still_delivers_registry(self):
        raw = b""
        result = self.run_migration(raw)
        rows = [
            json.loads(line)
            for line in Path(result["path"]).read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual([row["ref"] for row in rows], ["A", "B"])
        self.assertEqual(result["annotation_lines"], 0)
        self.assertEqual(result["total_lines"], 2)


# --------------------------------------------------------------------------- #
# Large streaming archive
# --------------------------------------------------------------------------- #

def _feature_pair(index):
    return (("k%02d" % index).ljust(64, "x"), ("v%02d" % index).ljust(64, "y"))


def large_dictionary():
    features = dict(_feature_pair(index) for index in range(20))
    return dictionary(
        ["s"],
        ["s"],
        [],
        [lemma("L", "z" * 64, state="s", features=features)],
    )


def write_large_archive(path, lines, corrupt_last=False):
    data = large_dictionary()
    surface = process(data)["forms"][0]["surface"]
    features = dict(_feature_pair(index) for index in range(20))
    analysis = {
        "lemma": "L",
        "surface": surface,
        "features": dict(sorted(features.items())),
        "rules": [],
    }
    analysis_raw = json.dumps(
        analysis, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    text_raw = json.dumps(surface, ensure_ascii=False).encode("utf-8")
    prefix = b'{"analysis":' + analysis_raw + b',"doc":"d","pos":'
    suffix = b',"ref":"A","text":' + text_raw + b"}\n"
    value = features[_sorted_key(features, 19)]
    broken_analysis = dict(analysis)
    broken_features = dict(analysis["features"])
    broken_features[_sorted_key(features, 19)] = "w" + value[1:]
    broken_analysis["features"] = dict(sorted(broken_features.items()))
    broken_raw = json.dumps(
        broken_analysis, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    broken_prefix = b'{"analysis":' + broken_raw + b',"doc":"d","pos":'
    total = 0
    with open(path, "wb") as handle:
        for pos in range(lines):
            use_broken = corrupt_last and pos == lines - 1
            line_prefix = broken_prefix if use_broken else prefix
            line = line_prefix + str(pos).encode() + suffix
            handle.write(line)
            total += len(line)
    return total


def _sorted_key(features, index):
    return sorted(features)[index]


class MemoryTests(MigrationCase):
    LINES = 40000

    def test_large_archive_streams_within_memory_budget(self):
        data = large_dictionary()
        registry = [{"ref": "A", "lemma": "L", "rules": []}]
        source = self.root / "large.jsonl"
        size = write_large_archive(source, self.LINES)
        self.assertGreaterEqual(size, 96 * 1024 * 1024)
        self.assertLessEqual(size, 256 * 1024 * 1024)
        approved = file_digest(source)

        target = self.target()
        tracemalloc.start()
        try:
            result = migrate_annotations(
                data, data, registry, {}, source, approved, target
            )
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 64 * 1024 * 1024)
        self.assertEqual(result["annotation_lines"], self.LINES)
        self.assertEqual(result["total_lines"], self.LINES + 1)
        self.assertEqual(target.stat().st_size, size + self._registry_bytes(registry))

        with open(target, "rb") as handle:
            first = json.loads(handle.readline())
            second = json.loads(handle.readline())
            count = self._count_lines(target)
            last = self._last_line(target)
        self.assertEqual(first["ref"], "A")
        self.assertEqual(second["pos"], 0)
        self.assertEqual(count, self.LINES + 1)
        self.assertEqual(last["pos"], self.LINES - 1)

    def test_corrupt_last_row_still_leaves_no_target(self):
        data = large_dictionary()
        registry = [{"ref": "A", "lemma": "L", "rules": []}]
        source = self.root / "large.jsonl"
        write_large_archive(source, self.LINES, corrupt_last=True)
        approved = file_digest(source)
        target = self.target()
        with self.assertRaises(ArchiveError) as caught:
            migrate_annotations(data, data, registry, {}, source, approved, target)
        self.assertEqual(caught.exception.line, self.LINES)
        self.assert_no_target(target)

    @staticmethod
    def _registry_bytes(registry):
        data = large_dictionary()
        analysis = {
            "lemma": "L",
            "surface": process(data)["forms"][0]["surface"],
            "features": dict(sorted(analysis_features(data).items())),
            "rules": [],
        }
        line = json.dumps(
            {"ref": "A", "analysis": analysis},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        return len(line) * len(registry)

    @staticmethod
    def _count_lines(path):
        total = 0
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    return total
                total += chunk.count(b"\n")

    @staticmethod
    def _last_line(path):
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 65536))
            tail = handle.read()
        lines = tail.split(b"\n")
        # trailing newline produces an empty final element
        return json.loads(lines[-2])


def analysis_features(data):
    return dict(data["lemmas"][0]["features"])


# --------------------------------------------------------------------------- #
# Publication boundaries
# --------------------------------------------------------------------------- #

class PublishTests(MigrationCase):
    def setUp(self):
        super().setUp()
        self.old = twin_dictionary(["ra", "rb"])
        self.new = twin_dictionary(["rx", "ry"])
        self.registry = [
            {"ref": "A", "lemma": "L", "rules": ["ra"]},
            {"ref": "B", "lemma": "L", "rules": ["rb"]},
        ]
        self.approvals = {"A": [["rx"], ["ry"]], "B": [["rx"]]}
        self.row = annotation(
            "d1", 0, "daks", "A", form_by_identity(self.old, "L", ["ra"])
        )
        self.raw = archive_bytes([self.row])
        self.source = self.source(self.raw)

    def test_link_failure_is_a_publish_error_and_leaves_nothing(self):
        target = self.target()
        with mock.patch("morph_paths.migration.os.link", side_effect=OSError("disk full")):
            with self.assertRaises(PublishError):
                migrate_annotations(
                    self.old, self.new, self.registry, self.approvals,
                    self.source, digest(self.raw), target,
                )
        self.assert_no_target(target)

    def test_unreadable_archive_is_a_publish_error(self):
        missing = self.root / "absent.jsonl"
        with self.assertRaises(PublishError):
            migrate_annotations(
                self.old, self.new, self.registry, self.approvals,
                missing, "0" * 64, self.target(),
            )
        self.assert_no_target(self.target())

    def test_missing_target_parent_directories_are_created_on_success(self):
        target = self.root / "nested" / "deeper" / "package.jsonl"
        result = migrate_annotations(
            self.old, self.new, self.registry, self.approvals,
            self.source, digest(self.raw), target,
        )
        self.assertEqual(result["path"], str(target))
        self.assertTrue(target.is_file())

    def test_partial_directory_creation_does_not_happen_on_failure(self):
        target = self.root / "nested" / "deeper" / "package.jsonl"
        with self.assertRaises(ArchiveError):
            corrupt = dict(self.row)
            corrupt["analysis"] = dict(self.row["analysis"])
            corrupt["analysis"]["rules"] = ["rb"]
            bad = self.root / "bad.jsonl"
            bad.write_bytes(archive_bytes([corrupt]))
            migrate_annotations(
                self.old, self.new, self.registry, self.approvals,
                bad, digest(bad.read_bytes()), target,
            )
        self.assertFalse((self.root / "nested").exists())


if __name__ == "__main__":
    unittest.main()
