from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from tests.test_desktop_update import MODULE, manifest_payload


CURRENT_VERSION = "1.0.4"
INSTALLER_NAME = "WorkStack-Setup-1.0.5.ps1"
CHECKSUM_NAME = "WorkStack-Setup-1.0.5.ps1.sha256"


def parse(payload: object):
    return MODULE.parse_update_manifest(json.dumps(payload).encode(), current_version=CURRENT_VERSION)


def payload_with(**overrides: object) -> dict[str, object]:
    payload = manifest_payload()
    payload.update(overrides)
    return payload


def asset_with(kind: str, **overrides: object) -> dict[str, object]:
    payload = manifest_payload()
    payload[kind].update(overrides)  # type: ignore[union-attr]
    return payload


def canonical_sidecar(installer: bytes) -> bytes:
    return f"{hashlib.sha256(installer).hexdigest()}  {INSTALLER_NAME}\n".encode()


def committed_download(root: Path, installer: bytes):
    """Commit one fully verified download and return (manifest, result, sidecar)."""

    sidecar = canonical_sidecar(installer)
    manifest = parse(manifest_payload(installer, sidecar))
    bodies = {manifest.installer.url: installer, manifest.checksum.url: sidecar}
    downloaded = MODULE.download_update(manifest, root, fetch=lambda url, limit: bodies[url])
    return manifest, downloaded, sidecar


class _FakeResponse:
    """The urlopen response surface fetch_url_bytes relies on: a context manager with read(limit)."""

    def __init__(self, body: bytes, read_error: Exception | None = None) -> None:
        self.body = body
        self.read_error = read_error
        self.limits: list[int] = []

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def read(self, limit: int) -> bytes:
        self.limits.append(limit)
        if self.read_error is not None:
            raise self.read_error
        return self.body[:limit]


class _UpdaterCase(unittest.TestCase):
    def assertRefused(self, message: str, function, *args, **kwargs):
        with self.assertRaises(MODULE.UpdateValidationError) as raised:
            function(*args, **kwargs)
        self.assertEqual(message, str(raised.exception))
        return raised.exception


class ManifestDecodeTests(_UpdaterCase):
    def test_rejects_empty_oversized_and_non_bytes_bodies(self) -> None:
        for label, body in (
            ("empty", b""),
            ("oversized", b" " * (MODULE.MAX_MANIFEST_BYTES + 1)),
            ("str", "{}"),
            ("bytearray", bytearray(b"{}")),
        ):
            with self.subTest(body=label):
                self.assertRefused(
                    "update manifest size is invalid",
                    MODULE.parse_update_manifest,
                    body,
                    current_version=CURRENT_VERSION,
                )

    def test_accepts_a_body_of_exactly_the_bounded_size(self) -> None:
        encoded = json.dumps(manifest_payload()).encode()
        padded = encoded + b" " * (MODULE.MAX_MANIFEST_BYTES - len(encoded))
        self.assertEqual(MODULE.MAX_MANIFEST_BYTES, len(padded))
        manifest = MODULE.parse_update_manifest(padded, current_version=CURRENT_VERSION)
        self.assertEqual("1.0.5", manifest.version)

    def test_rejects_bodies_that_are_not_utf8_json(self) -> None:
        for label, body, cause in (
            ("invalid utf-8", b"\xff\xfe{}", UnicodeError),
            ("truncated json", b'{"schema_version": 1', json.JSONDecodeError),
        ):
            with self.subTest(body=label):
                refused = self.assertRefused(
                    "update manifest is not valid UTF-8 JSON",
                    MODULE.parse_update_manifest,
                    body,
                    current_version=CURRENT_VERSION,
                )
                self.assertIsInstance(refused.__cause__, cause)

    def test_rejects_manifests_and_assets_that_are_not_exact_objects(self) -> None:
        without_checksum = {key: value for key, value in manifest_payload().items() if key != "checksum"}
        checksum_without_size = manifest_payload()
        del checksum_without_size["checksum"]["size"]  # type: ignore[union-attr]
        for label, payload, message in (
            ("scalar manifest", 42, "manifest fields are invalid"),
            ("list manifest", [manifest_payload()], "manifest fields are invalid"),
            ("extra manifest field", payload_with(extra=True), "manifest fields are invalid"),
            ("missing manifest field", without_checksum, "manifest fields are invalid"),
            ("installer is a string", payload_with(installer=INSTALLER_NAME), "installer fields are invalid"),
            ("installer extra field", asset_with("installer", signature="abc"), "installer fields are invalid"),
            ("checksum missing size", checksum_without_size, "checksum fields are invalid"),
        ):
            with self.subTest(case=label):
                self.assertRefused(message, parse, payload)

    def test_rejects_unsupported_schema_versions_and_channels(self) -> None:
        for label, payload in (
            ("schema 0", payload_with(schema_version=0)),
            ("schema 2", payload_with(schema_version=2)),
            ("schema as string", payload_with(schema_version="1")),
            ("beta channel", payload_with(channel="beta")),
            ("capitalised channel", payload_with(channel="Stable")),
            ("missing channel value", payload_with(channel=None)),
        ):
            with self.subTest(case=label):
                self.assertRefused("only stable update manifest schema 1 is supported", parse, payload)


class ManifestFieldTests(_UpdaterCase):
    def test_rejects_noncanonical_current_version(self) -> None:
        body = json.dumps(manifest_payload()).encode()
        for current in ("1.0", "v1.0.4", "1.0.4.0", ""):
            with self.subTest(current_version=current):
                self.assertRefused(
                    "current_version must be one canonical three-part version",
                    MODULE.parse_update_manifest,
                    body,
                    current_version=current,
                )

    def test_rejects_remote_protocol_outside_its_bounded_range(self) -> None:
        for value in (True, False, "1", 1.0, 65536, -1, None):
            with self.subTest(minimum_remote_protocol=value):
                self.assertRefused(
                    "minimum_remote_protocol is invalid",
                    parse,
                    payload_with(minimum_remote_protocol=value),
                )

    def test_rejects_published_at_that_is_not_a_short_string(self) -> None:
        for label, value in (
            ("int", 20260901),
            ("none", None),
            ("41 chars", "2026-09-01T00:00:00+00:00" + "0" * 16),
        ):
            with self.subTest(case=label):
                self.assertRefused("published_at is invalid", parse, payload_with(published_at=value))

    def test_rejects_published_at_without_an_offset(self) -> None:
        for label, value in (
            ("naive datetime", "2026-09-01T00:00:00"),
            ("date only", "2026-09-01"),
            ("not a timestamp", "invalid"),
            ("empty", ""),
        ):
            with self.subTest(case=label):
                refused = self.assertRefused(
                    "published_at must be an offset timestamp",
                    parse,
                    payload_with(published_at=value),
                )
                self.assertIsInstance(refused.__cause__, ValueError)

    def test_accepts_zulu_and_explicit_offsets_verbatim(self) -> None:
        for value in (
            "2026-09-01T00:00:00Z",
            "2026-09-01T09:00:00+09:00",
            "2026-09-01T00:00:00.123456-05:00",
        ):
            with self.subTest(published_at=value):
                self.assertEqual(value, parse(payload_with(published_at=value)).published_at)

    def test_rejects_asset_digests_that_are_not_lowercase_sha256(self) -> None:
        digest = manifest_payload()["installer"]["sha256"]  # type: ignore[index]
        for label, kind, value in (
            ("uppercase", "installer", digest.upper()),
            ("63 chars", "checksum", digest[:63]),
            ("65 chars", "installer", digest + "0"),
            ("int", "checksum", 12345),
            ("prefixed", "installer", "sha256:" + digest),
        ):
            with self.subTest(case=label, kind=kind):
                self.assertRefused(
                    f"{kind} digest must be lowercase SHA-256",
                    parse,
                    asset_with(kind, sha256=value),
                )

    def test_rejects_asset_sizes_outside_the_bounded_range(self) -> None:
        for label, kind, value in (
            ("bool", "installer", True),
            ("str", "installer", "5"),
            ("float", "checksum", 5.0),
            ("zero", "installer", 0),
            ("negative", "checksum", -1),
            ("above installer maximum", "installer", MODULE.MAX_INSTALLER_BYTES + 1),
            ("above checksum maximum", "checksum", 1025),
        ):
            with self.subTest(case=label, kind=kind):
                self.assertRefused(f"{kind} size is invalid", parse, asset_with(kind, size=value))

    def test_accepts_asset_sizes_at_their_maximum(self) -> None:
        payload = manifest_payload()
        payload["installer"]["size"] = MODULE.MAX_INSTALLER_BYTES  # type: ignore[index]
        payload["checksum"]["size"] = 1024  # type: ignore[index]
        manifest = parse(payload)
        self.assertEqual(MODULE.MAX_INSTALLER_BYTES, manifest.installer.size)
        self.assertEqual(1024, manifest.checksum.size)


class FetchUrlBytesTests(_UpdaterCase):
    URL = "https://github.com/Shinick-Han/work-stack-public/releases/download/v1.0.5/WorkStack-Setup-1.0.5.ps1"

    def test_returns_the_body_and_sends_one_bounded_octet_stream_request(self) -> None:
        response = _FakeResponse(b"abc")
        with mock.patch("urllib.request.urlopen", return_value=response) as urlopen:
            self.assertEqual(b"abc", MODULE.fetch_url_bytes(self.URL, 3))
        self.assertEqual([4], response.limits)
        urlopen.assert_called_once()
        request = urlopen.call_args.args[0]
        self.assertIsInstance(request, urllib.request.Request)
        self.assertEqual(self.URL, request.full_url)
        self.assertEqual("GET", request.get_method())
        self.assertEqual("application/octet-stream", request.get_header("Accept"))
        self.assertEqual("WorkStack-Desktop-Updater/1", request.get_header("User-agent"))
        self.assertEqual({"timeout": 20}, urlopen.call_args.kwargs)

    def test_returns_bodies_shorter_than_the_limit_unchanged(self) -> None:
        with mock.patch("urllib.request.urlopen", return_value=_FakeResponse(b"ab")):
            self.assertEqual(b"ab", MODULE.fetch_url_bytes(self.URL, 3))

    def test_rejects_bodies_longer_than_the_limit(self) -> None:
        response = _FakeResponse(b"abcd")
        with mock.patch("urllib.request.urlopen", return_value=response):
            self.assertRefused(
                "update download exceeded its bounded size",
                MODULE.fetch_url_bytes,
                self.URL,
                3,
            )
        self.assertEqual([4], response.limits)

    def test_maps_transport_failures_to_update_validation_errors(self) -> None:
        for label, error in (
            ("os error", OSError("connection refused")),
            ("url error", urllib.error.URLError("name resolution failed")),
            ("http error", urllib.error.HTTPError(self.URL, 404, "Not Found", {}, None)),
            ("timeout", TimeoutError("timed out")),
        ):
            with self.subTest(case=label):
                with mock.patch("urllib.request.urlopen", side_effect=error):
                    refused = self.assertRefused("update download failed", MODULE.fetch_url_bytes, self.URL, 3)
                self.assertIs(error, refused.__cause__)

    def test_maps_read_failures_inside_the_response_to_update_validation_errors(self) -> None:
        error = ConnectionResetError("reset mid-body")
        with mock.patch("urllib.request.urlopen", return_value=_FakeResponse(b"", read_error=error)):
            refused = self.assertRefused("update download failed", MODULE.fetch_url_bytes, self.URL, 3)
        self.assertIs(error, refused.__cause__)


class VerifiedDownloadTests(_UpdaterCase):
    INSTALLER = b"verified setup body"

    def test_rejects_bodies_whose_size_differs_from_the_manifest(self) -> None:
        sidecar = canonical_sidecar(self.INSTALLER)
        manifest = parse(manifest_payload(self.INSTALLER, sidecar))
        for label, bodies, name in (
            ("short installer", {manifest.installer.url: b"short"}, INSTALLER_NAME),
            ("long installer", {manifest.installer.url: self.INSTALLER + b"!"}, INSTALLER_NAME),
            (
                "long checksum",
                {manifest.installer.url: self.INSTALLER, manifest.checksum.url: sidecar + b"\n"},
                CHECKSUM_NAME,
            ),
        ):
            with self.subTest(case=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.assertRefused(
                    f"{name} size does not match the update manifest",
                    MODULE.download_update,
                    manifest,
                    root,
                    fetch=lambda url, limit: bodies[url],
                )
                self.assertEqual([], list(root.iterdir()))

    def test_rejects_checksum_sidecars_that_do_not_describe_the_installer(self) -> None:
        digest = hashlib.sha256(self.INSTALLER).hexdigest()
        for label, sidecar in (
            ("other filename", f"{digest}  Other.ps1\n".encode()),
            ("other digest", f"{'0' * 64}  {INSTALLER_NAME}\n".encode()),
            ("uppercase digest", f"{digest.upper()}  {INSTALLER_NAME}\n".encode()),
            ("single space", f"{digest} {INSTALLER_NAME}\n".encode()),
            ("missing newline", f"{digest}  {INSTALLER_NAME}".encode()),
        ):
            manifest = parse(manifest_payload(self.INSTALLER, sidecar))
            bodies = {manifest.installer.url: self.INSTALLER, manifest.checksum.url: sidecar}
            with self.subTest(case=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.assertRefused(
                    "checksum sidecar content does not match the installer",
                    MODULE.download_update,
                    manifest,
                    root,
                    fetch=lambda url, limit: bodies[url],
                )
                self.assertEqual([], list(root.iterdir()))

    def test_returns_an_existing_verified_download_without_fetching(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, first, sidecar = committed_download(root, self.INSTALLER)
            destination = root.resolve() / "1.0.5"
            self.assertEqual(
                {
                    "version": "1.0.5",
                    "installer_sha256": manifest.installer.sha256,
                    "checksum_sha256": manifest.checksum.sha256,
                },
                json.loads((destination / "ready.json").read_text(encoding="utf-8")),
            )

            fetch = mock.Mock(side_effect=AssertionError("a verified download must not be fetched again"))
            again = MODULE.download_update(manifest, root, fetch=fetch)
            fetch.assert_not_called()
            self.assertEqual(first, again)
            self.assertEqual(destination / INSTALLER_NAME, again.setup_path)
            self.assertEqual(destination / CHECKSUM_NAME, again.checksum_path)
            self.assertEqual(self.INSTALLER, again.setup_path.read_bytes())
            self.assertEqual(sidecar, again.checksum_path.read_bytes())
            self.assertEqual(manifest.release_url, again.release_url)
            self.assertEqual(manifest.minimum_remote_protocol, again.minimum_remote_protocol)
            self.assertEqual(["1.0.5"], [item.name for item in root.iterdir()])

    def test_refuses_unverified_update_directories_without_touching_them(self) -> None:
        digest = hashlib.sha256(self.INSTALLER).hexdigest()
        sidecar = canonical_sidecar(self.INSTALLER)
        marker = {
            "version": "1.0.5",
            "installer_sha256": digest,
            "checksum_sha256": hashlib.sha256(sidecar).hexdigest(),
        }

        def write_marker(destination: Path, value: object) -> None:
            (destination / "ready.json").write_text(json.dumps(value), encoding="utf-8")

        cases = (
            ("no marker", lambda destination: (destination / "ready.json").unlink()),
            ("marker is not json", lambda destination: (destination / "ready.json").write_bytes(b"{")),
            ("marker is not utf-8", lambda destination: (destination / "ready.json").write_bytes(b"\xff\xfe")),
            ("marker is a list", lambda destination: write_marker(destination, [marker])),
            ("marker names another version", lambda destination: write_marker(destination, {**marker, "version": "1.0.6"})),
            (
                "marker lacks the checksum digest",
                lambda destination: write_marker(
                    destination, {key: marker[key] for key in ("version", "installer_sha256")}
                ),
            ),
            ("marker carries an extra field", lambda destination: write_marker(destination, {**marker, "verified": True})),
            ("installer missing", lambda destination: (destination / INSTALLER_NAME).unlink()),
            ("installer size differs", lambda destination: (destination / INSTALLER_NAME).write_bytes(self.INSTALLER + b"!")),
            ("installer digest differs", lambda destination: (destination / INSTALLER_NAME).write_bytes(b"x" * len(self.INSTALLER))),
            ("checksum missing", lambda destination: (destination / CHECKSUM_NAME).unlink()),
            ("checksum size differs", lambda destination: (destination / CHECKSUM_NAME).write_bytes(sidecar + b"\n")),
            ("checksum digest differs", lambda destination: (destination / CHECKSUM_NAME).write_bytes(b"y" * len(sidecar))),
        )
        for label, tamper in cases:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                manifest, _, _ = committed_download(root, self.INSTALLER)
                destination = root.resolve() / "1.0.5"
                tamper(destination)
                remaining = sorted(item.name for item in destination.iterdir())
                fetch = mock.Mock(side_effect=AssertionError("an unverified directory must fail before any fetch"))
                self.assertRefused(
                    "an unverified update directory already exists for this version",
                    MODULE.download_update,
                    manifest,
                    root,
                    fetch=fetch,
                )
                fetch.assert_not_called()
                self.assertEqual(remaining, sorted(item.name for item in destination.iterdir()))
                self.assertEqual(["1.0.5"], [item.name for item in root.iterdir()])

    def test_refuses_an_update_directory_whose_marker_cannot_be_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, _, _ = committed_download(root, self.INSTALLER)
            fetch = mock.Mock(side_effect=AssertionError("an unreadable marker must fail before any fetch"))
            with mock.patch.object(MODULE.Path, "read_text", side_effect=PermissionError("marker locked")):
                self.assertRefused(
                    "an unverified update directory already exists for this version",
                    MODULE.download_update,
                    manifest,
                    root,
                    fetch=fetch,
                )
            fetch.assert_not_called()
            self.assertTrue((root / "1.0.5" / "ready.json").is_file())


class UpdatePreferencesTests(unittest.TestCase):
    DISABLED = MODULE.UpdatePreferences(False, False, False)

    def test_unreadable_or_undecodable_settings_disable_every_automatic_step(self) -> None:
        for label, body in (
            ("truncated json", b"{"),
            ("not utf-8", b"\xff\xfe"),
            ("empty file", b""),
        ):
            with self.subTest(case=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / MODULE.UPDATE_SETTINGS_FILE).write_bytes(body)
                self.assertEqual(self.DISABLED, MODULE.load_update_preferences(root))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            MODULE.save_update_preferences(root, MODULE.UpdatePreferences(True, True, True))
            with mock.patch.object(MODULE.Path, "read_text", side_effect=PermissionError("settings locked")):
                self.assertEqual(self.DISABLED, MODULE.load_update_preferences(root))

    def test_wrong_key_sets_or_non_boolean_values_disable_every_automatic_step(self) -> None:
        exact = {"auto_check": True, "auto_download": True, "install_on_exit": True}
        for label, value in (
            ("list", [True, True, True]),
            ("string", "true"),
            ("missing keys", {"auto_check": True}),
            ("extra key", {**exact, "channel": "stable"}),
            ("renamed key", {"auto_check": True, "auto_download": True, "install_on_close": True}),
            ("int instead of bool", {**exact, "auto_check": 1}),
            ("string instead of bool", {**exact, "auto_download": "true"}),
            ("null instead of bool", {**exact, "install_on_exit": None}),
        ):
            with self.subTest(case=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / MODULE.UPDATE_SETTINGS_FILE).write_text(json.dumps(value), encoding="utf-8")
                self.assertEqual(self.DISABLED, MODULE.load_update_preferences(root))

    def test_accepts_exact_boolean_settings_including_a_utf8_bom(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / MODULE.UPDATE_SETTINGS_FILE).write_text(
                json.dumps({"auto_check": True, "auto_download": False, "install_on_exit": True}),
                encoding="utf-8-sig",
            )
            self.assertEqual(
                MODULE.UpdatePreferences(True, False, True),
                MODULE.load_update_preferences(root),
            )


if __name__ == "__main__":
    unittest.main()
