"""Offline rule-revision migration demonstration; run from the repository root.

Shows the editorial workflow without any service or persistent state: two
dictionaries (before and after a rule change), a stable-reference registry,
editor-approved candidate paths, and a signed JSONL annotation archive whose
SHA-256 is known.  The API binds every reference to exactly one qualifying
new analysis, then publishes one self-contained package next to the archive.

    python -I examples/offline_migration.py
"""
import hashlib
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from morph_paths import (
    AmbiguousAssignmentError,
    ArchiveError,
    DigestMismatchError,
    NoSolutionError,
    migrate_annotations,
    process,
)


def dictionary(rule_ids, states):
    """One lemma deriving the plural surface through two equivalent rules."""
    start, done = states
    return {
        "states": [start, done],
        "accepting": [done],
        "rules": [
            {
                "id": rule_id,
                "from": start,
                "to": done,
                "prefix": "",
                "suffix": "s",
                "requires": {"class": "N"},
                "adds": {"number": "many"},
            }
            for rule_id in rule_ids
        ],
        "lemmas": [
            {"id": "L", "stem": "dak", "state": start, "features": {"class": "N"}}
        ],
        "queries": [],
    }


def old_form(data, rule_id):
    for form in process(data)["forms"]:
        if form["rules"] == [rule_id]:
            return form
    raise AssertionError("analysis not produced")


def archive(rows):
    return b"".join(
        (
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        for row in rows
    )


def sign(form, ref, doc, pos):
    return {
        "doc": doc,
        "pos": pos,
        "text": form["surface"],
        "ref": ref,
        "analysis": form,
    }


def show_package(path):
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    print(f"  package: {path} ({len(lines)} lines)")
    for line in lines:
        row = json.loads(line)
        if "ref" in row and "doc" not in row:
            print(f"    registry {row['ref']} -> {row['analysis']['rules']}")
        else:
            print(
                f"    row doc={row['doc']} pos={row['pos']} ref={row['ref']} "
                f"surface={row['analysis']['surface']!r} "
                f"rules={row['analysis']['rules']}"
            )


def main():
    old = dictionary(["ra", "rb"], ("s0", "s1"))
    new = dictionary(["rx", "ry"], ("t0", "t1"))
    registry = [
        {"ref": "A", "lemma": "L", "rules": ["ra"]},
        {"ref": "B", "lemma": "L", "rules": ["rb"]},
    ]
    approvals = {"A": [["rx"], ["ry"]], "B": [["rx"]]}

    rows = [sign(old_form(old, "ra"), "A", "doc-1", 0)]
    raw = archive(rows)
    approved_digest = hashlib.sha256(raw).hexdigest()

    with tempfile.TemporaryDirectory(prefix="morph-migration-") as workdir:
        workdir = Path(workdir)
        source = workdir / "signed.jsonl"
        source.write_bytes(raw)
        target = workdir / "migrated.jsonl"

        print("unique assignment even though only A is used by the archive:")
        result = migrate_annotations(
            old, new, registry, approvals, source, approved_digest, target
        )
        show_package(result["path"])
        print("  detail:", json.dumps(
            {key: result[key] for key in ("annotation_lines", "total_lines")}
        ))

        # Making B approve both paths leaves two complete assignments: refuse.
        try:
            migrate_annotations(
                old,
                new,
                registry,
                {"A": [["rx"], ["ry"]], "B": [["rx"], ["ry"]]},
                source,
                approved_digest,
                workdir / "ambiguous.jsonl",
            )
        except AmbiguousAssignmentError as exc:
            print(f"\nrefused: {exc}")

        # Restricting A as well makes no assignment possible: refuse.
        try:
            migrate_annotations(
                old,
                new,
                registry,
                {"A": [["rx"]], "B": [["rx"]]},
                source,
                approved_digest,
                workdir / "unsolvable.jsonl",
            )
        except NoSolutionError as exc:
            print(f"refused: {exc}")

        # A tampered archive fails its approved digest and leaves no package.
        tampered = workdir / "tampered.jsonl"
        tampered.write_bytes(raw.replace(b'"pos":0', b'"pos":7'))
        tampered_digest = hashlib.sha256(tampered.read_bytes()).hexdigest()
        try:
            migrate_annotations(
                old,
                new,
                registry,
                approvals,
                tampered,
                approved_digest,
                workdir / "tampered-package.jsonl",
            )
        except DigestMismatchError as exc:
            print(f"refused: {exc}")
        print(
            "  package absent after refusal:",
            not (workdir / "tampered-package.jsonl").exists(),
        )

        # A row signed with another real old analysis for the same ref fails
        # with the offending line number.
        mismatched = workdir / "mismatched.jsonl"
        bad_row = sign(old_form(old, "rb"), "A", "doc-1", 0)
        mismatched.write_bytes(archive([sign(old_form(old, "ra"), "A", "doc-1", 0), bad_row]))
        try:
            migrate_annotations(
                old,
                new,
                registry,
                approvals,
                mismatched,
                hashlib.sha256(mismatched.read_bytes()).hexdigest(),
                workdir / "mismatched-package.jsonl",
            )
        except ArchiveError as exc:
            print(f"refused: {exc} (line {exc.line})")
        print(
            "  package absent after refusal:",
            not (workdir / "mismatched-package.jsonl").exists(),
        )


if __name__ == "__main__":
    main()
