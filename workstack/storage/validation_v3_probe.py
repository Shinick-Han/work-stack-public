"""Legacy (v3) authority probe: read the candidate, never write to it.

The candidate is copied into a throwaway directory and only that copy is
opened by ``Store``. The source bytes are digested before, during and after
the copy through the caller-supplied digest reader, so any change under the
probe fails closed as ``V3_SOURCE_CHANGED`` rather than reporting a store
that no longer exists on disk.
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from pathlib import Path
from typing import Callable

from ..store import Store
from .validation_primitives import (
    StoragePathValidationReport,
    StorageValidationIssue,
    build_report,
)


SourceDigests = Callable[[Path], dict[str, str]]


class V3SourceChangedError(RuntimeError):
    pass


def _copy_v3_files(
    root: Path, candidate_root: Path, source_digests: SourceDigests
) -> dict[str, str]:
    digests = source_digests(root)
    for name in digests:
        body = (root / name).read_bytes()
        if hashlib.sha256(body).hexdigest() != digests[name]:
            raise V3SourceChangedError
        (candidate_root / name).write_bytes(body)
    if source_digests(root) != digests:
        raise V3SourceChangedError
    return digests


def validate_v3(
    root: Path, *, source_digests: SourceDigests
) -> StoragePathValidationReport:
    with tempfile.TemporaryDirectory(prefix="workstack-v3-validate-") as temporary:
        candidate_root = Path(temporary) / "authority"
        candidate_root.mkdir()
        try:
            digests = _copy_v3_files(root, candidate_root, source_digests)
            store = Store(candidate_root)
            runtime_root = store.runtime_root
            try:
                readiness = store.initialize()
            finally:
                shutil.rmtree(runtime_root, ignore_errors=True)
            if source_digests(root) != digests:
                raise V3SourceChangedError
        except V3SourceChangedError:
            return build_report(3, None, 0, [StorageValidationIssue("V3_SOURCE_CHANGED")])
        except (OSError, ValueError):
            return build_report(3, None, 0, [StorageValidationIssue("V3_INVALID")])
    return build_report(3, readiness.workspace_uid, readiness.task_count, [])
