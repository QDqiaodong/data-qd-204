"""Receiver side: offline, shard-subscribed, crash-safe dictionary views.

A :class:`Workspace` keeps its state at one local directory and never depends
on the publisher process, the full source input or unsubscribed shards.

State machine:

* zero or one **current view** (``current.json`` + verified shard blobs);
* zero or one **staged switch** (``staging.json`` + received shard blobs).

All updates are atomic file writes, and activation is a single atomic
descriptor commit, so re-opening the workspace after a crash always shows
either the complete old view or the complete new view.
"""
import hashlib
import os
import re
import tempfile
import unicodedata
from pathlib import Path

from .canonical import dumps_canonical, loads_canonical
from .release import BUCKET_IDS, MANIFEST_VERSION

_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_RELEASE_ID_RE = re.compile(r"[0-9a-f]{64}")
_BUCKET_RE = re.compile(r"[0-9a-f]")
_IDENT_RE = re.compile(r"[A-Za-z0-9_:-]{1,64}")
_STATE_FILENAME = "current.json"
_STAGING_FILENAME = "staging.json"
_BLOBS_DIRNAME = "blobs"
_TMP_DIRNAME = "tmp"


class ReceiverError(RuntimeError):
    """Base class for receiver-side operational errors."""


class NoCurrentViewError(ReceiverError):
    """No current view has been committed to this workspace."""


class NoStagingError(ReceiverError):
    """No staged switch exists."""


class StagingConflictError(ReceiverError):
    """A different staged target already exists; abandon it explicitly."""


class NotCompleteError(ReceiverError):
    """The staged target still lacks shards and cannot be activated."""


class DataIntegrityError(ReceiverError):
    """A shard that the current view declares available is missing or damaged."""


class LookupStatus:
    """Distinguishable outcomes of :meth:`Workspace.lookup`."""

    NO_VIEW = "no_view"
    VERSION_MISMATCH = "version_mismatch"
    NOT_COVERED = "not_covered"
    EMPTY = "empty"
    OK = "ok"


class LookupResult:
    """Result of an inverse lookup.

    ``status`` is one of :class:`LookupStatus`; ``analyses`` is the complete
    ordered list for the surface when ``status == OK`` and otherwise ``[]``.
    """

    __slots__ = ("status", "analyses")

    def __init__(self, status, analyses=()):
        self.status = status
        self.analyses = list(analyses)

    def __repr__(self):
        return f"LookupResult({self.status!r}, {self.analyses!r})"

    def __eq__(self, other):
        if not isinstance(other, LookupResult):
            return NotImplemented
        return self.status == other.status and self.analyses == other.analyses


# --------------------------------------------------------------------------- #
# Validation of untrusted bytes
# --------------------------------------------------------------------------- #

def _is_hex_digest(value):
    return isinstance(value, str) and _DIGEST_RE.fullmatch(value) is not None


def _is_ident(value):
    return isinstance(value, str) and _IDENT_RE.fullmatch(value) is not None


def _normalize_surface(value):
    # Same rules as model.text: bounded Unicode text, no control/surrogate.
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise ValueError("bounded surface text required")
    if any(unicodedata.category(c) in ("Cc", "Cs") for c in value):
        raise ValueError("control or surrogate codepoint in surface")
    return unicodedata.normalize("NFC", value)


def parse_manifest(manifest_bytes, expected_release_id):
    """Validate untrusted manifest bytes against an expected release id.

    Returns the mapping ``{"version": 1, "buckets": {..}}``.  Raises
    ``ValueError`` on any structural, format, canonicalization or identity
    mismatch.
    """
    if isinstance(manifest_bytes, str):
        raise ValueError("manifest must be delivered as bytes")
    if not isinstance(manifest_bytes, (bytes, bytearray)):
        raise ValueError("manifest must be bytes")
    if not _is_hex_digest(expected_release_id):
        raise ValueError("invalid expected release id")
    try:
        manifest = loads_canonical(manifest_bytes)
    except ValueError as exc:
        raise ValueError(f"manifest is not valid strict JSON: {exc}") from exc
    if not isinstance(manifest, dict) or set(manifest) != {"version", "buckets"}:
        raise ValueError("manifest must contain exactly version and buckets")
    version = manifest["version"]
    if type(version) is not int or version != MANIFEST_VERSION:
        raise ValueError(f"unsupported manifest version: {version!r}")
    buckets = manifest["buckets"]
    if not isinstance(buckets, dict) or set(buckets) != set(BUCKET_IDS):
        raise ValueError("manifest buckets must map all 16 bucket ids")
    for bucket, digest in buckets.items():
        if not _BUCKET_RE.fullmatch(bucket) or not _is_hex_digest(digest):
            raise ValueError(f"invalid bucket entry: {bucket!r}")
    canonical = dumps_canonical(manifest)
    if canonical != bytes(manifest_bytes):
        raise ValueError("manifest is not in canonical byte form")
    actual_id = hashlib.sha256(canonical).hexdigest()
    if actual_id != expected_release_id:
        raise ValueError("manifest digest does not match expected release id")
    return manifest


def _validate_form(form, bucket):
    if not isinstance(form, dict) or set(form) != {"lemma", "surface", "features", "rules"}:
        raise ValueError("analysis row must contain lemma, surface, features, rules")
    if not _is_ident(form["lemma"]):
        raise ValueError("invalid lemma id in shard")
    surface = _normalize_surface(form["surface"])
    if surface != form["surface"]:
        raise ValueError("shard surface is not in NFC form")
    if hashlib.sha256(surface.encode("utf-8")).hexdigest()[0] != bucket:
        raise ValueError(f"surface does not belong to bucket {bucket}")
    features = form["features"]
    if not isinstance(features, dict) or len(features) > 20:
        raise ValueError("feature mapping required in shard")
    for key, value in features.items():
        if not _is_ident(key) or not _is_ident(value):
            raise ValueError("invalid feature key or value in shard")
    rules = form["rules"]
    if not isinstance(rules, list) or not all(_is_ident(rule) for rule in rules):
        raise ValueError("rule path must be a list of rule ids")
    return surface


def parse_shard(shard_bytes, bucket, expected_digest):
    """Validate one untrusted shard and return its parsed array.

    Checks canonical bytes and digest, v1 row shape, NFC, bucket ownership,
    ordering and unique analysis identities.  Raises ``ValueError`` otherwise.
    """
    if isinstance(shard_bytes, str) or not isinstance(shard_bytes, (bytes, bytearray)):
        raise ValueError("shard must be delivered as bytes")
    shard_bytes = bytes(shard_bytes)
    if not _BUCKET_RE.fullmatch(str(bucket)):
        raise ValueError("invalid bucket id")
    if not _is_hex_digest(expected_digest):
        raise ValueError("invalid expected shard digest")
    if hashlib.sha256(shard_bytes).hexdigest() != expected_digest:
        raise ValueError("shard digest does not match manifest")
    try:
        rows = loads_canonical(shard_bytes)
    except ValueError as exc:
        raise ValueError(f"shard is not valid strict JSON: {exc}") from exc
    if not isinstance(rows, list):
        raise ValueError("shard must be a JSON array")
    if dumps_canonical(rows) != shard_bytes:
        raise ValueError("shard is not in canonical byte form")
    seen_identities = set()
    previous = None
    for form in rows:
        surface = _validate_form(form, bucket)
        identity = (form["lemma"], tuple(form["rules"]))
        if identity in seen_identities:
            raise ValueError("duplicate analysis identity within shard")
        seen_identities.add(identity)
        key = (surface, form["lemma"], form["rules"])
        if previous is not None and key < previous:
            raise ValueError("shard rows are out of canonical order")
        previous = key
    return rows


# --------------------------------------------------------------------------- #
# Workspace
# --------------------------------------------------------------------------- #

class Workspace:
    """Persistent receiver workspace rooted at ``path``."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self._blobs_dir = self.path / _BLOBS_DIRNAME
        self._tmp_dir = self.path / _TMP_DIRNAME
        self._blobs_dir.mkdir(exist_ok=True)
        self._tmp_dir.mkdir(exist_ok=True)
        self._reconcile()

    # -- durable helpers --------------------------------------------------- #

    def _atomic_write(self, target, payload):
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(
            dir=self._tmp_dir, prefix="." + target.name + "-", suffix=".tmp"
        )
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def _write_json(self, target, obj):
        self._atomic_write(Path(target), dumps_canonical(obj))

    def _read_json(self, target):
        try:
            raw = Path(target).read_bytes()
        except FileNotFoundError:
            return None
        return loads_canonical(raw)

    def _blob_path(self, release_id, bucket):
        if not _RELEASE_ID_RE.fullmatch(release_id):
            raise ValueError("invalid release id")
        if not _BUCKET_RE.fullmatch(bucket):
            raise ValueError("invalid bucket id")
        return self._blobs_dir / release_id / f"{bucket}.json"

    def _materialize_blob(self, release_id, bucket, payload):
        target = self._blob_path(release_id, bucket)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.read_bytes() != payload:
                # Same path exists with different bytes: content addressing
                # says this cannot happen legitimately; replace atomically.
                self._atomic_write(target, payload)
        else:
            self._atomic_write(target, payload)

    def _blob_is_good(self, release_id, bucket, digest):
        path = self._blob_path(release_id, bucket)
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest() == digest
        except FileNotFoundError:
            return False

    # -- crash recovery ---------------------------------------------------- #

    def _reconcile(self):
        """Recognize pre-/post-commit states after an interrupted switch."""
        current = self._read_json(self.path / _STATE_FILENAME)
        staging = self._read_json(self.path / _STAGING_FILENAME)
        if current is not None and staging is not None:
            if staging["release_id"] == current["release_id"]:
                # Activation descriptor committed; only cleanup was missing.
                if self._all_blobs_present(current):
                    self._discard_staging(staging)
                else:
                    # Committed view cannot read its own data: do not mask it.
                    raise DataIntegrityError(
                        "committed view is missing shard blobs after recovery"
                    )
        self._prune_orphan_blobs()

    def _prune_orphan_blobs(self):
        current = self._read_json(self.path / _STATE_FILENAME)
        staging = self._read_json(self.path / _STAGING_FILENAME)
        live = set()
        if current is not None:
            live.add(current["release_id"])
        if staging is not None:
            live.add(staging["release_id"])
        if not self._blobs_dir.exists():
            return
        for entry in self._blobs_dir.iterdir():
            if entry.is_dir() and entry.name not in live:
                for blob in entry.iterdir():
                    blob.unlink()
                entry.rmdir()

    def _all_blobs_present(self, descriptor):
        manifest = descriptor["manifest"]
        for bucket in descriptor["buckets"]:
            if not self._blob_is_good(
                descriptor["release_id"], bucket, manifest["buckets"][bucket]
            ):
                return False
        return True

    def _discard_staging(self, staging):
        staging_path = self.path / _STAGING_FILENAME
        staging_path.unlink(missing_ok=True)
        current = self._read_json(self.path / _STATE_FILENAME)
        current_id = current["release_id"] if current is not None else None
        if staging["release_id"] != current_id:
            stale_dir = self._blobs_dir / staging["release_id"]
            if stale_dir.is_dir():
                for blob in stale_dir.iterdir():
                    blob.unlink()
                stale_dir.rmdir()

    # -- public state ------------------------------------------------------ #

    def state(self):
        """Return a plain, identifiable description of the workspace state."""
        current = self._read_json(self.path / _STATE_FILENAME)
        staging = self._read_json(self.path / _STAGING_FILENAME)
        current_view = None
        if current is not None:
            current_view = {
                "release_id": current["release_id"],
                "buckets": frozenset(current["buckets"]),
            }
        staged = None
        if staging is not None:
            staged = {
                "release_id": staging["release_id"],
                "buckets": frozenset(staging["buckets"]),
                "received": frozenset(
                    bucket
                    for bucket in staging["buckets"]
                    if self._blob_is_good(
                        staging["release_id"],
                        bucket,
                        staging["manifest"]["buckets"][bucket],
                    )
                ),
            }
        return {"current": current_view, "staging": staged}

    # -- prepare ----------------------------------------------------------- #

    def prepare_switch(self, expected_release_id, manifest_bytes, wanted_buckets):
        """Register (or re-register) a staged switch target.

        Re-preparing the same target keeps already received shards.  A
        different pending target raises :class:`StagingConflictError`.
        """
        manifest = parse_manifest(manifest_bytes, expected_release_id)
        if not isinstance(wanted_buckets, (list, tuple, set, frozenset)):
            raise ValueError("wanted bucket collection required")
        wanted = [str(bucket) for bucket in wanted_buckets]
        if not wanted:
            raise ValueError("wanted bucket set must be non-empty")
        if any(not _BUCKET_RE.fullmatch(bucket) for bucket in wanted):
            raise ValueError("invalid wanted bucket id")
        wanted_set = frozenset(wanted)
        if len(wanted_set) != len(wanted):
            raise ValueError("duplicate wanted bucket id")

        staging_path = self.path / _STAGING_FILENAME
        existing = self._read_json(staging_path)
        if existing is not None and existing["release_id"] != expected_release_id:
            raise StagingConflictError(
                "another staged target exists; call abandon_switch first"
            )

        current = self._read_json(self.path / _STATE_FILENAME)
        staged_descriptor = {
            "release_id": expected_release_id,
            "buckets": sorted(wanted_set),
            "manifest": manifest,
        }

        # Reuse verified current-view shards: same bucket id AND same digest.
        # Same-version re-prepares also keep blobs already received for this id.
        reusable_ids = {expected_release_id}
        if current is not None:
            reusable_ids.add(current["release_id"])
            current_manifest = current["manifest"]
        for bucket in wanted_set:
            if (
                current is not None
                and current_manifest["buckets"].get(bucket)
                == manifest["buckets"][bucket]
            ):
                source = self._blob_path(current["release_id"], bucket)
                if source.exists():
                    self._materialize_blob(
                        expected_release_id, bucket, source.read_bytes()
                    )
                    continue
            already = self._blob_path(expected_release_id, bucket)
            if already.exists() and self._blob_is_good(
                expected_release_id, bucket, manifest["buckets"][bucket]
            ):
                continue  # previously received shard for the same target id

        if existing is not None:
            # Same target re-prepared: keep received shards, only update scope.
            staged_descriptor["buckets"] = sorted(wanted_set)
        self._write_json(staging_path, staged_descriptor)
        self._prune_orphan_blobs()
        return self.state()["staging"]

    def abandon_switch(self):
        """Remove the staged target and its blobs; the current view is untouched."""
        staging = self._read_json(self.path / _STAGING_FILENAME)
        if staging is None:
            raise NoStagingError("no staged switch to abandon")
        self._discard_staging(staging)

    # -- deliver ----------------------------------------------------------- #

    def missing_shards(self):
        """Return ``[(bucket, digest), ...]`` still required by the staging."""
        staging = self._read_json(self.path / _STAGING_FILENAME)
        if staging is None:
            raise NoStagingError("no staged switch")
        buckets = staging["manifest"]["buckets"]
        return [
            (bucket, buckets[bucket])
            for bucket in staging["buckets"]
            if not self._blob_is_good(
                staging["release_id"], bucket, buckets[bucket]
            )
        ]

    def deliver(self, batch):
        """Validate and register one batch of complete target shards.

        ``batch`` is a list of ``{"bucket": ..., "bytes": ...}`` items.  The
        whole batch validates before anything is registered; a failed batch
        changes no state.  Re-delivering an already received valid shard is a
        no-op.
        """
        staging = self._read_json(self.path / _STAGING_FILENAME)
        if staging is None:
            raise NoStagingError("no staged switch")
        if not isinstance(batch, list) or not batch:
            raise ValueError("delivery batch must be a non-empty list")
        target_buckets = set(staging["buckets"])
        manifest = staging["manifest"]

        seen = set()
        prepared = []
        for item in batch:
            if not isinstance(item, dict) or set(item) != {"bucket", "bytes"}:
                raise ValueError("batch items need bucket and bytes")
            bucket = item["bucket"]
            if not isinstance(bucket, str) or not _BUCKET_RE.fullmatch(bucket):
                raise ValueError("invalid bucket id in batch")
            if bucket not in target_buckets:
                raise ValueError(f"bucket {bucket} is not part of the staged target")
            if bucket in seen:
                raise ValueError(f"bucket {bucket} appears twice in batch")
            seen.add(bucket)
            payload = item["bytes"]
            if isinstance(payload, str) or not isinstance(payload, (bytes, bytearray)):
                raise ValueError("shard bytes required")
            payload = bytes(payload)
            # Full validation, including that hashes alone cannot pass it.
            parse_shard(payload, bucket, manifest["buckets"][bucket])
            prepared.append((bucket, payload))

        # All items valid: register the batch atomically-ish (content-addressed).
        for bucket, payload in prepared:
            self._materialize_blob(staging["release_id"], bucket, payload)
        return self.missing_shards()

    # -- activate ---------------------------------------------------------- #

    def activate(self):
        """Commit the staged target as the new current view."""
        staging = self._read_json(self.path / _STAGING_FILENAME)
        if staging is None:
            raise NoStagingError("no staged switch")
        missing = [
            bucket
            for bucket in staging["buckets"]
            if not self._blob_is_good(
                staging["release_id"],
                bucket,
                staging["manifest"]["buckets"][bucket],
            )
        ]
        if missing:
            raise NotCompleteError(f"staged target is missing shards: {sorted(missing)}")

        descriptor = {
            "release_id": staging["release_id"],
            "buckets": list(staging["buckets"]),
            "manifest": staging["manifest"],
        }
        # Crash hooks for deterministic failure-injection tests.
        before_commit = globals().get("_CRASH_BEFORE_COMMIT")
        if before_commit:
            before_commit(self)
        self._write_json(self.path / _STATE_FILENAME, descriptor)
        after_commit = globals().get("_CRASH_AFTER_COMMIT")
        if after_commit:
            after_commit(self)
        # Descriptor is durable; dropping the staging plan is post-commit cleanup.
        self._discard_staging(staging)
        return self.state()["current"]

    # -- lookup ------------------------------------------------------------ #

    def lookup(self, expected_release_id, surface):
        """Inverse lookup in the current view only.

        Returns a :class:`LookupResult` whose status distinguishes no view,
        version mismatch, uncovered bucket, a covered-but-empty surface and a
        real answer.  Damaged shards inside the covered range raise
        :class:`DataIntegrityError` instead of looking empty.
        """
        if not isinstance(expected_release_id, str) or not _RELEASE_ID_RE.fullmatch(
            expected_release_id
        ):
            raise ValueError("invalid expected release id")
        normalized = _normalize_surface(surface)
        current = self._read_json(self.path / _STATE_FILENAME)
        if current is None:
            return LookupResult(LookupStatus.NO_VIEW)
        if current["release_id"] != expected_release_id:
            return LookupResult(LookupStatus.VERSION_MISMATCH)
        bucket = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[0]
        if bucket not in current["buckets"]:
            return LookupResult(LookupStatus.NOT_COVERED)
        digest = current["manifest"]["buckets"][bucket]
        blob = self._blob_path(current["release_id"], bucket)
        try:
            raw = blob.read_bytes()
        except FileNotFoundError as exc:
            raise DataIntegrityError(
                f"covered shard {bucket} is missing"
            ) from exc
        if hashlib.sha256(raw).hexdigest() != digest:
            raise DataIntegrityError(f"covered shard {bucket} is corrupted")
        rows = loads_canonical(raw)
        analyses = [row for row in rows if row["surface"] == normalized]
        return LookupResult(LookupStatus.OK if analyses else LookupStatus.EMPTY, analyses)


# Crash-injection hooks (used by the failure-injection tests only).
_CRASH_BEFORE_COMMIT = None
_CRASH_AFTER_COMMIT = None
