from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable


UPDATE_MANIFEST_URL = (
    "https://github.com/Shinick-Han/work-stack-public/"
    "releases/latest/download/workstack-update.json"
)
UPDATE_SETTINGS_FILE = "update-settings.json"
MAX_MANIFEST_BYTES = 64 * 1024
MAX_INSTALLER_BYTES = 100 * 1024 * 1024
SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
MANIFEST_FIELDS = {
    "schema_version",
    "channel",
    "version",
    "published_at",
    "release_url",
    "minimum_remote_protocol",
    "installer",
    "checksum",
}


class UpdateValidationError(RuntimeError):
    pass


class OlderUpdateManifest(UpdateValidationError):
    def __init__(self, version: str, installed_version: str) -> None:
        self.version = version
        self.installed_version = installed_version
        super().__init__("update version must not be older than the installed version")


@dataclass(frozen=True)
class UpdateAsset:
    name: str
    url: str
    sha256: str
    size: int


@dataclass(frozen=True)
class UpdateManifest:
    version: str
    published_at: str
    release_url: str
    minimum_remote_protocol: int
    installer: UpdateAsset
    checksum: UpdateAsset
    is_newer: bool = True


@dataclass(frozen=True)
class DownloadedUpdate:
    version: str
    setup_path: Path
    checksum_path: Path
    release_url: str
    minimum_remote_protocol: int


@dataclass(frozen=True)
class UpdatePreferences:
    auto_check: bool = True
    auto_download: bool = True
    install_on_exit: bool = True


def _version_tuple(value: object, field: str = "version") -> tuple[int, int, int]:
    if not isinstance(value, str) or not SEMVER.fullmatch(value):
        raise UpdateValidationError(f"{field} must be one canonical three-part version")
    return tuple(int(part) for part in value.split("."))  # type: ignore[return-value]


def _exact_object(value: object, fields: set[str], field: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != fields:
        raise UpdateValidationError(f"{field} fields are invalid")
    return value


def _asset(value: object, *, kind: str, version: str) -> UpdateAsset:
    raw = _exact_object(value, {"name", "url", "sha256", "size"}, kind)
    expected_name = (
        f"WorkStack-Setup-{version}.ps1"
        if kind == "installer"
        else f"WorkStack-Setup-{version}.ps1.sha256"
    )
    if raw["name"] != expected_name:
        raise UpdateValidationError(f"{kind} filename does not match the release version")
    expected_url = (
        f"https://github.com/Shinick-Han/work-stack-public/releases/download/"
        f"v{version}/{expected_name}"
    )
    if raw["url"] != expected_url:
        raise UpdateValidationError(f"{kind} must use the exact Work Stack GitHub release URL")
    digest = raw["sha256"]
    if not isinstance(digest, str) or not SHA256.fullmatch(digest):
        raise UpdateValidationError(f"{kind} digest must be lowercase SHA-256")
    size = raw["size"]
    maximum = MAX_INSTALLER_BYTES if kind == "installer" else 1024
    if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= maximum:
        raise UpdateValidationError(f"{kind} size is invalid")
    return UpdateAsset(expected_name, expected_url, digest, size)


def _decode_manifest(body: bytes) -> dict[str, object]:
    if not isinstance(body, bytes) or not body or len(body) > MAX_MANIFEST_BYTES:
        raise UpdateValidationError("update manifest size is invalid")
    try:
        raw = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise UpdateValidationError("update manifest is not valid UTF-8 JSON") from error
    return _exact_object(raw, MANIFEST_FIELDS, "manifest")


def _manifest_remote_protocol(raw: dict[str, object]) -> int:
    remote = raw["minimum_remote_protocol"]
    if isinstance(remote, bool) or not isinstance(remote, int) or not 1 <= remote <= 65535:
        raise UpdateValidationError("minimum_remote_protocol is invalid")
    return remote


def _manifest_version(
    raw: dict[str, object], current_version: str
) -> tuple[str, tuple[int, int, int], tuple[int, int, int]]:
    version = raw["version"]
    comparison = _version_tuple(version)
    installed = _version_tuple(current_version, "current_version")
    assert isinstance(version, str)
    if comparison < installed:
        raise OlderUpdateManifest(version, current_version)
    return version, comparison, installed


def _manifest_release_url(raw: dict[str, object], version: str) -> str:
    release_url = (
        f"https://github.com/Shinick-Han/work-stack-public/releases/tag/v{version}"
    )
    if raw["release_url"] != release_url:
        raise UpdateValidationError(
            "release_url must identify the exact Work Stack GitHub release"
        )
    return release_url


def _manifest_published_at(raw: dict[str, object]) -> str:
    published_at = raw["published_at"]
    if not isinstance(published_at, str) or len(published_at) > 40:
        raise UpdateValidationError("published_at is invalid")
    try:
        if datetime.fromisoformat(published_at.replace("Z", "+00:00")).tzinfo is None:
            raise ValueError
    except ValueError as error:
        raise UpdateValidationError("published_at must be an offset timestamp") from error
    return published_at


def parse_update_manifest(body: bytes, *, current_version: str) -> UpdateManifest:
    raw = _decode_manifest(body)
    if raw["schema_version"] != 1 or raw["channel"] != "stable":
        raise UpdateValidationError("only stable update manifest schema 1 is supported")
    remote = _manifest_remote_protocol(raw)
    version, comparison, installed = _manifest_version(raw, current_version)
    release_url = _manifest_release_url(raw, version)
    published_at = _manifest_published_at(raw)
    return UpdateManifest(
        version=version,
        published_at=published_at,
        release_url=release_url,
        minimum_remote_protocol=remote,
        installer=_asset(raw["installer"], kind="installer", version=version),
        checksum=_asset(raw["checksum"], kind="checksum", version=version),
        is_newer=comparison > installed,
    )


def fetch_url_bytes(url: str, limit: int) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/octet-stream",
            "User-Agent": "WorkStack-Desktop-Updater/1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read(limit + 1)
    except (OSError, urllib.error.URLError) as error:
        raise UpdateValidationError("update download failed") from error
    if len(body) > limit:
        raise UpdateValidationError("update download exceeded its bounded size")
    return body


def _verified_body(asset: UpdateAsset, fetch: Callable[[str, int], bytes]) -> bytes:
    body = fetch(asset.url, asset.size)
    if len(body) != asset.size:
        raise UpdateValidationError(f"{asset.name} size does not match the update manifest")
    if hashlib.sha256(body).hexdigest() != asset.sha256:
        raise UpdateValidationError(f"{asset.name} digest does not match the update manifest")
    return body


def _existing_download(manifest: UpdateManifest, destination: Path) -> DownloadedUpdate | None:
    setup = destination / manifest.installer.name
    checksum = destination / manifest.checksum.name
    ready = destination / "ready.json"
    if not ready.is_file():
        return None
    try:
        marker = json.loads(ready.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if marker != {
        "version": manifest.version,
        "installer_sha256": manifest.installer.sha256,
        "checksum_sha256": manifest.checksum.sha256,
    }:
        return None
    for path, asset in ((setup, manifest.installer), (checksum, manifest.checksum)):
        if not path.is_file() or path.stat().st_size != asset.size:
            return None
        if hashlib.sha256(path.read_bytes()).hexdigest() != asset.sha256:
            return None
    return DownloadedUpdate(
        manifest.version,
        setup,
        checksum,
        manifest.release_url,
        manifest.minimum_remote_protocol,
    )


def download_update(
    manifest: UpdateManifest,
    update_root: Path,
    *,
    fetch: Callable[[str, int], bytes] = fetch_url_bytes,
) -> DownloadedUpdate:
    root = update_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    destination = root / manifest.version
    existing = _existing_download(manifest, destination)
    if existing is not None:
        return existing
    if destination.exists():
        raise UpdateValidationError("an unverified update directory already exists for this version")

    staging = root / f".{manifest.version}.staging-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        installer_body = _verified_body(manifest.installer, fetch)
        checksum_body = _verified_body(manifest.checksum, fetch)
        expected_sidecar = f"{manifest.installer.sha256}  {manifest.installer.name}\n".encode("utf-8")
        if checksum_body != expected_sidecar:
            raise UpdateValidationError("checksum sidecar content does not match the installer")
        setup_path = staging / manifest.installer.name
        checksum_path = staging / manifest.checksum.name
        setup_path.write_bytes(installer_body)
        checksum_path.write_bytes(checksum_body)
        marker = {
            "version": manifest.version,
            "installer_sha256": manifest.installer.sha256,
            "checksum_sha256": manifest.checksum.sha256,
        }
        (staging / "ready.json").write_text(
            json.dumps(marker, ensure_ascii=True, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(staging, destination)
        return DownloadedUpdate(
            manifest.version,
            destination / manifest.installer.name,
            destination / manifest.checksum.name,
            manifest.release_url,
            manifest.minimum_remote_protocol,
        )
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def load_update_preferences(state_root: Path) -> UpdatePreferences:
    path = state_root / UPDATE_SETTINGS_FILE
    if not path.is_file():
        return UpdatePreferences()
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return UpdatePreferences(False, False, False)
    if not isinstance(raw, dict) or set(raw) != {"auto_check", "auto_download", "install_on_exit"}:
        return UpdatePreferences(False, False, False)
    if any(type(raw[field]) is not bool for field in raw):
        return UpdatePreferences(False, False, False)
    return UpdatePreferences(raw["auto_check"], raw["auto_download"], raw["install_on_exit"])


def save_update_preferences(state_root: Path, preferences: UpdatePreferences) -> None:
    state_root.mkdir(parents=True, exist_ok=True)
    target = state_root / UPDATE_SETTINGS_FILE
    temporary = state_root / f".{UPDATE_SETTINGS_FILE}.tmp-{uuid.uuid4().hex}"
    body = json.dumps(
        {
            "auto_check": preferences.auto_check,
            "auto_download": preferences.auto_download,
            "install_on_exit": preferences.install_on_exit,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )
    try:
        temporary.write_text(body, encoding="utf-8")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
