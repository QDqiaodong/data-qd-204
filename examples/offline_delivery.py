"""Offline publisher/receiver demonstration; run from the repository root.

The publisher side only ever hands over bytes (a manifest and individual
shard arrays); the receiver stores them at a local workspace path without
seeing the source input or the publisher's full release object.  Nothing here
opens a network connection.

    python -I examples/offline_delivery.py
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from morph_paths import build_release, Workspace, LookupStatus

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def show(ws, release_id, surface):
    result = ws.lookup(release_id, surface)
    if result.status == LookupStatus.OK:
        analyses = ", ".join(
            f"{row['lemma']} -> {row['rules']}" for row in result.analyses
        )
        print(f"  lookup {surface!r}: OK [{analyses}]")
    else:
        print(f"  lookup {surface!r}: {result.status}")


def main():
    root = Path(tempfile.mkdtemp(prefix="morph-offline-"))
    print(f"workspace under {root}")
    try:
        # --- publisher side: compile three releases to bytes ------------- #
        release_a = build_release(load("scenario_a"))
        release_b0 = build_release(load("scenario_b0"))
        release_b1 = build_release(load("scenario_b1"))
        print(f"A release id: {release_a.release_id}")
        print(f"B0 release id: {release_b0.release_id} (shards 3,4 unchanged)")
        print(f"B1 release id: {release_b1.release_id} (shard 4 changed)")

        workspace = Workspace(root / "annotator")

        # --- first install: subscribe to buckets 3 and 4 ------------------ #
        workspace.prepare_switch(
            release_a.release_id, release_a.manifest_bytes(), ["3", "4"]
        )
        for bucket, digest in workspace.missing_shards():
            workspace.deliver(
                [{"bucket": bucket, "bytes": release_a.shard_bytes(bucket)}]
            )
        workspace.activate()
        print("\nview A active, coverage 3,4:")
        show(workspace, release_a.release_id, "é")  # two analyses
        show(workspace, release_a.release_id, "e")  # covered, empty
        show(workspace, release_a.release_id, "a")  # not covered

        # --- B0: no new shard bytes needed -------------------------------- #
        workspace.prepare_switch(
            release_b0.release_id, release_b0.manifest_bytes(), ["3", "4"]
        )
        print(f"\nB0 staged, missing shards: {workspace.missing_shards()}")
        workspace.activate()

        # --- B1: only bucket 4 must be re-delivered ----------------------- #
        workspace.prepare_switch(
            release_b1.release_id, release_b1.manifest_bytes(), ["3", "4"]
        )
        print(f"B1 staged, missing shards: {workspace.missing_shards()}")
        for bucket, _digest in workspace.missing_shards():
            workspace.deliver(
                [{"bucket": bucket, "bytes": release_b1.shard_bytes(bucket)}]
            )
        workspace.activate()
        print("\nview B1 active:")
        show(workspace, release_b1.release_id, "é")  # two analyses, both r2
        show(workspace, release_b1.release_id, "b")  # bucket 3 reused
        print("\nstate:", json.dumps(workspace.state(), default=sorted, ensure_ascii=False))
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
