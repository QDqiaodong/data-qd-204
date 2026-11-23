"""Domain data processing API; synthetic offline baseline."""
from .service import process
from .split import build_corpora
from .release import build_release, Release, bucket_for_surface, BUCKET_IDS
from .receiver import (
    Workspace,
    LookupResult,
    LookupStatus,
    ReceiverError,
    NoCurrentViewError,
    NoStagingError,
    StagingConflictError,
    NotCompleteError,
    DataIntegrityError,
    parse_manifest,
    parse_shard,
)

__all__ = [
    "process",
    "build_corpora",
    "build_release",
    "Release",
    "bucket_for_surface",
    "BUCKET_IDS",
    "Workspace",
    "LookupResult",
    "LookupStatus",
    "ReceiverError",
    "NoCurrentViewError",
    "NoStagingError",
    "StagingConflictError",
    "NotCompleteError",
    "DataIntegrityError",
    "parse_manifest",
    "parse_shard",
]
