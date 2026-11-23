"""Publisher side: compile a complete morphology release into 16 shards.

The release is content-addressed.  Its identity depends only on the compiled
form data, never on source paths, generation time, ``queries`` selection or the
order of lemmas/rules in the input.  A release is built from the existing
:func:`morph_paths.process` pipeline, so accepted derivations, feature
compatibility, size limits and final NFC semantics are unchanged.
"""
import hashlib

from .canonical import dumps_canonical
from .service import process

MANIFEST_VERSION = 1
BUCKET_COUNT = 16
BUCKET_IDS = tuple(f"{i:x}" for i in range(BUCKET_COUNT))
_DIGEST_RE_PART = r"[0-9a-f]{64}"


def _sha256_hex(raw):
    return hashlib.sha256(raw).hexdigest()


def bucket_for_surface(surface):
    """Return the shard number (``"0"``..``"f"``) for an NFC surface."""
    return _sha256_hex(surface.encode("utf-8"))[0]


class Release:
    """Immutable result of :func:`build_release`.

    Attributes:
        release_id: lowercase hex SHA-256 of the canonical manifest bytes.
        version: manifest format version (currently 1).
        buckets: mapping ``bucket id -> lowercase hex digest`` for all 16
            buckets, including empty ones.
        shards: mapping ``bucket id -> canonical JSON bytes`` (a JSON array).
        manifest: canonical manifest JSON bytes.
        forms: the compiled forms, in their canonical order.
    """

    __slots__ = ("release_id", "version", "buckets", "shards", "manifest", "forms")

    def __init__(self, forms):
        self.forms = forms
        partitions = {bucket: [] for bucket in BUCKET_IDS}
        for form in forms:
            partitions[bucket_for_surface(form["surface"])].append(form)
        self.shards = {}
        self.buckets = {}
        for bucket in BUCKET_IDS:
            raw = dumps_canonical(partitions[bucket])
            self.shards[bucket] = raw
            self.buckets[bucket] = _sha256_hex(raw)
        self.version = MANIFEST_VERSION
        self.manifest = dumps_canonical(
            {"version": self.version, "buckets": self.buckets}
        )
        self.release_id = _sha256_hex(self.manifest)

    def manifest_bytes(self):
        """Return the manifest bytes to hand to the receiver."""
        return self.manifest

    def shard_bytes(self, bucket):
        """Return the canonical shard bytes for one bucket id."""
        if bucket not in self.shards:
            raise ValueError(f"unknown bucket: {bucket}")
        return self.shards[bucket]


def build_release(data):
    """Compile the five-field input into a complete sharded release.

    All accepted ``forms`` are published; input ``queries`` influence nothing
    in the release.  Inputs that fail the original compiler raise ``ValueError``
    and produce no release.  Equivalent form data yields byte-identical shards
    and manifest and therefore the same release id.
    """
    result = process(data)
    return Release(result["forms"])
