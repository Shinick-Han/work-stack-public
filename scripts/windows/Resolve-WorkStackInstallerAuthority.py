"""Bounded, read-only LOCAL-v3 authority selection for the bundled installer."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "desktop" / "python-webview-shell"))

from connection_registry import (  # noqa: E402
    LocalConnectionProfile, MAX_REGISTRY_BYTES, REGISTRY_FILE,
    registry_from_document, registry_to_document,
)
from local_workspace_rebind import derive_store_runtime_root  # noqa: E402
import profile_inspection as inspection  # noqa: E402
from workstack.store import (  # noqa: E402
    STORE_MANIFEST_NAME, StoreCorruptError, _validate_store_manifest_header,
    _validate_store_manifest_files, _validate_store_manifest_tasks,
)

MANIFEST_READ_LIMIT = 4 * 1024 * 1024


class AuthorityError(RuntimeError):
    pass


def digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def safe_components(path: Path) -> None:
    for component in reversed((path, *path.parents)):
        try:
            metadata = component.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise AuthorityError("evidence_unreadable") from error
        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise AuthorityError("evidence_reparse")


@dataclass(frozen=True)
class Evidence:
    raw: bytes
    identity: tuple[int, int, int, int, int]


def read_optional(path: Path, limit: int) -> Evidence | None:
    safe_components(path)
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise AuthorityError("evidence_unreadable") from error
    if not stat.S_ISREG(before.st_mode):
        raise AuthorityError("evidence_not_regular")
    if before.st_size > limit:
        raise AuthorityError("evidence_too_large")
    try:
        with path.open("rb") as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise AuthorityError("evidence_too_large")
        safe_components(path)
        after = path.lstat()
    except OSError as error:
        raise AuthorityError("evidence_changed") from error
    def identity(metadata):
        return (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)
    if len(raw) != before.st_size or identity(before) != identity(after):
        raise AuthorityError("evidence_changed")
    return Evidence(raw, identity(after))


def document(record: Evidence) -> dict:
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate key")
            value[key] = item
        return value
    try:
        value = json.loads(record.raw.decode("utf-8-sig"), object_pairs_hook=unique)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise AuthorityError("evidence_invalid_json") from error
    if not isinstance(value, dict):
        raise AuthorityError("evidence_invalid_json")
    return value


def record_binding(record: Evidence | None) -> dict:
    if record is None:
        return {"state": "absent"}
    return {"state": "present", "sha256": digest(record.raw), "identity": record.identity}


def resolve_authority(state_root: Path) -> dict:
    registry_path = state_root.absolute() / REGISTRY_FILE
    registry_record = read_optional(registry_path, MAX_REGISTRY_BYTES)
    if registry_record is None:
        if read_optional(registry_path, MAX_REGISTRY_BYTES) is not None:
            raise AuthorityError("registry_changed")
        result = {"status": "absent-registry", "registry": {"state": "absent"}}
        return {**result, "binding": digest(canonical(result))}
    raw_registry = document(registry_record)
    try:
        registry = registry_from_document(raw_registry)
    except RuntimeError as error:
        raise AuthorityError("registry_invalid") from error
    matches = [profile for profile in registry.profiles if profile.profile_id == registry.active_profile_id]
    if len(matches) != 1 or not matches[0].enabled:
        raise AuthorityError("active_profile_invalid")
    profile = matches[0]
    if not isinstance(profile, LocalConnectionProfile):
        raise AuthorityError("active_profile_not_local")
    selected = next(item for item in registry_to_document(registry)["profiles"] if item["profile_id"] == profile.profile_id)
    try:
        data = Path(inspection.validate_local_directory_path(profile.data_dir)).resolve()
        safe_components(data)
        if (data / "store.json").exists():
            raise AuthorityError("unsupported_store_format")
        candidate = inspection.profile_test_candidate_from_document(selected)
        current = inspection.inspect_profile(candidate, enable_format_neutral=True)
    except inspection.ProfileInspectionError as error:
        raise AuthorityError(error.code) from error
    if current.status != "ready" or current.actual_workspace_id != profile.expected_workspace_id or current.authority is None:
        raise AuthorityError("current_authority_mismatch")

    runtime_base = os.environ.get("WORK_STACK_RUNTIME")
    if runtime_base:
        safe_components(Path(runtime_base).expanduser().absolute())
    elif os.environ.get("LOCALAPPDATA"):
        safe_components(Path(os.environ["LOCALAPPDATA"]).expanduser().absolute() / "WorkStack" / "runtime")
    runtime = derive_store_runtime_root(data)
    manifest_path = runtime / STORE_MANIFEST_NAME
    baseline = read_optional(manifest_path, MANIFEST_READ_LIMIT)
    if baseline is not None:
        manifest = document(baseline)
        try:
            _validate_store_manifest_header(manifest)
            _validate_store_manifest_files(manifest.get("files"))
            _validate_store_manifest_tasks(manifest.get("tasks"))
        except StoreCorruptError as error:
            raise AuthorityError("baseline_invalid") from error
        if manifest["workspace_id"] != profile.expected_workspace_id:
            raise AuthorityError("baseline_identity_mismatch")
        try:
            _, snapshots = inspection._read_store_values(data)
        except inspection.ProfileInspectionError as error:
            raise AuthorityError(error.code) from error
        actual = inspection._v3_authority_inspection(current.actual_workspace_id, snapshots)
        if actual != current.authority:
            raise AuthorityError("current_authority_changed")
        if any(manifest["files"][name] != "sha256:" + snapshot[2] for name, snapshot in snapshots.items()):
            raise AuthorityError("baseline_files_mismatch")
    if read_optional(manifest_path, MANIFEST_READ_LIMIT) != baseline:
        raise AuthorityError("baseline_changed")
    if read_optional(registry_path, MAX_REGISTRY_BYTES) != registry_record:
        raise AuthorityError("registry_changed")
    result = {
        "status": "selected", "profile_id": profile.profile_id,
        "profile_sha256": digest(canonical(selected)),
        "registry": record_binding(registry_record), "data_dir": str(data),
        "runtime_dir": str(runtime), "expected_workspace_id": profile.expected_workspace_id,
        "observed_workspace_id": current.actual_workspace_id,
        "authority_sha256": current.authority.authority_manifest_digest,
        "baseline": record_binding(baseline),
    }
    return {**result, "binding": digest(canonical(result))}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", required=True)
    args = parser.parse_args()
    try:
        result = resolve_authority(Path(args.state_root))
    except (AuthorityError, OSError) as error:
        code = str(error) if isinstance(error, AuthorityError) else "evidence_unreadable"
        print(json.dumps({"status": "refused", "code": code}))
        return 4
    print(canonical(result).decode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
