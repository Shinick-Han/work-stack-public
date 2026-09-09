"""OpenDocuments source verification and safe open, against synthetic sources only.

Every root here is a fresh ``tempfile.mkdtemp`` created by the test itself; no
NAS share, mapped drive, Notion workspace, API token, network or real desktop
application is touched, and the only "opener" is a recording stub. A green run
says this adapter reports what it actually read and refuses what it says it
refuses. It says nothing about a live OpenDocuments deployment.

Two OS-layer facts are stubbed rather than provoked, and are marked as such
where they appear: a permission-denied read and an undifferentiated ``OSError``
are injected at ``os.stat``/``open`` for one specific path, because producing a
genuine ACL denial or a dead-share error would make the suite depend on machine
state. The filesystem behaviour that *can* be produced honestly -- containment,
junction reparse points, absent files, absent roots, mid-read edits, real
SHA-256 over real bytes -- is produced, not simulated.

Native symlinks need a Windows privilege this account may not hold. Where that
is so, the symlink case skips with a reason and the directory-junction case
still runs; a junction is the sharper test anyway, because ``os.path.islink``
returns ``False`` for one.
"""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from integrations.opendocuments import nas_paths, source_access, source_access_nas
from integrations.opendocuments.nas_paths import (
    ALWAYS_REFUSED_EXTENSIONS,
    COMPONENT_ACCESS_DENIED,
    COMPONENT_INDETERMINATE,
    DEFAULT_PERMITTED_EXTENSIONS,
    LOCATION_REFUSAL_CODES,
)
from integrations.opendocuments.source_access import (
    MAX_VERIFIED_BYTES,
    OPEN_CODES,
    OPEN_STATUSES,
    VERIFICATION_CODES,
    VERIFICATION_STATUSES,
    OpenTarget,
    OriginAttestation,
    OriginRequest,
    SourceMapping,
    SourceMappingError,
    SourceMappingRegistry,
    authorize_open,
    resolve_notion_source,
    unsafe_notion_url_code,
    verify_source,
)

SOURCE_ACCESS = "integrations.opendocuments.source_access"
# The bounded reader and its cap live in the NAS module; the seams a test
# injects at are that module's, while every call below still goes through the
# public ``source_access`` entry points.
SOURCE_ACCESS_NAS = "integrations.opendocuments.source_access_nas"
SAFE_PAGE_URL = "https://www.notion.so/workspace/Quarterly-Plan-8f2c1d"


class RecordingOpener:
    """A stub trusted opener. It records; it never launches anything."""

    def __init__(self) -> None:
        self.calls: list[OpenTarget] = []

    def __call__(self, target: OpenTarget) -> None:
        self.calls.append(target)


class RaisingOpener:
    def __init__(self, root: str) -> None:
        self.root = root

    def __call__(self, target: OpenTarget) -> None:
        # The message names the path on purpose: the decision must not echo it.
        raise RuntimeError("could not launch {}".format(target.path))


def sha256_of(path: Path) -> str:
    """Recomputed independently of the module under test."""

    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def make_junction(link: Path, target: Path) -> bool:
    """Create a directory junction, or report that this host cannot."""

    if os.name != "nt":
        return False
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
    )
    return result.returncode == 0 and link.exists()


class SourceAccessTestCase(unittest.TestCase):
    """Shared synthetic root, plus the invariants that hold for every answer."""

    def setUp(self) -> None:
        self.root_text = tempfile.mkdtemp(prefix="ns-r2-source-")
        self.addCleanup(shutil.rmtree, self.root_text, ignore_errors=True)
        self.root = Path(self.root_text)
        self.resolved_root = self.root.resolve()

    def nas_mapping(self, **overrides: object) -> SourceMapping:
        fields: dict[str, object] = {
            "document_id": "od-nas-0001",
            "corpus": "payroll",
            "backend": "nas",
            "allowed_root": self.root,
            "relative_location": "reports/quarterly.pdf",
        }
        fields.update(overrides)
        return SourceMapping(**fields)  # type: ignore[arg-type]

    def notion_mapping(self, **overrides: object) -> SourceMapping:
        fields: dict[str, object] = {
            "document_id": "od-notion-0001",
            "corpus": "planning",
            "backend": "notion",
            "page_url": SAFE_PAGE_URL,
        }
        fields.update(overrides)
        return SourceMapping(**fields)  # type: ignore[arg-type]

    def registry(self, *mappings: SourceMapping) -> SourceMappingRegistry:
        return SourceMappingRegistry(mappings)

    def write_document(self, relative: str, body: bytes) -> Path:
        target = self.root.joinpath(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        return target

    def assert_public_and_closed(self, result: object, *, opened: bool = False) -> None:
        """No path may leak, and the vocabulary must stay closed."""

        public = result.as_public_dict()  # type: ignore[attr-defined]
        rendered = repr(public)
        for secret in (
            self.root_text,
            str(self.resolved_root),
            os.path.basename(self.root_text),
            "quarterly.pdf",
            "reports",
        ):
            self.assertNotIn(secret, rendered)
        statuses = OPEN_STATUSES if opened else VERIFICATION_STATUSES
        codes = OPEN_CODES if opened else VERIFICATION_CODES
        self.assertIn(public["status"], statuses)
        self.assertIn(public["code"], codes)

    def verify(self, registry: SourceMappingRegistry, document_id: object, **kwargs):
        result = verify_source(document_id, registry=registry, **kwargs)
        self.assert_public_and_closed(result)
        return result

    def open_document(self, registry: SourceMappingRegistry, document_id: object, **kwargs):
        decision = authorize_open(document_id, registry=registry, **kwargs)
        self.assert_public_and_closed(decision, opened=True)
        return decision


class NasHappyPathTests(SourceAccessTestCase):
    def test_hashes_real_bytes_and_reports_current_only_against_expected(self) -> None:
        target = self.write_document("reports/quarterly.pdf", b"%PDF-1.7 synthetic\n")
        mapping = self.nas_mapping()
        registry = self.registry(mapping)
        actual = sha256_of(target)

        baseline = self.verify(registry, "od-nas-0001")
        self.assertEqual(baseline.status, "unverifiable")
        self.assertEqual(baseline.code, "no_expected_version")
        # The hash is still reported, so a caller can record a first baseline --
        # but a hash with nothing to compare against is not currentness.
        self.assertEqual(baseline.source_version, actual)
        self.assertFalse(baseline.open_allowed)

        current = self.verify(registry, "od-nas-0001", expected_source_version=actual)
        self.assertEqual(current.status, "current")
        self.assertEqual(current.code, "hash_matched")
        self.assertEqual(current.source_version, actual)
        self.assertTrue(current.open_allowed)
        self.assertRegex(current.source_version, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(current.corpus, "payroll")

    def test_edited_file_is_stale_not_current(self) -> None:
        target = self.write_document("reports/quarterly.pdf", b"first revision\n")
        registry = self.registry(self.nas_mapping())
        first = sha256_of(target)

        target.write_bytes(b"second revision, materially different\n")
        stale = self.verify(registry, "od-nas-0001", expected_source_version=first)
        self.assertEqual(stale.status, "stale")
        self.assertEqual(stale.code, "hash_differs")
        self.assertEqual(stale.source_version, sha256_of(target))
        self.assertNotEqual(stale.source_version, first)
        self.assertFalse(stale.open_allowed)

    def test_indexed_digest_alone_never_makes_a_document_current(self) -> None:
        target = self.write_document("reports/quarterly.pdf", b"body\n")
        registry = self.registry(self.nas_mapping())
        actual = sha256_of(target)

        # The index holds a digest that happens to equal the source hash. It is
        # still a digest of what the index holds, not a version of the source.
        echoed = self.verify(registry, "od-nas-0001", indexed_digest=actual)
        self.assertEqual(echoed.status, "unverifiable")
        self.assertEqual(echoed.code, "no_expected_version")
        self.assertEqual(echoed.indexed_digest, actual)
        self.assertFalse(echoed.open_allowed)

        stale = self.verify(
            registry,
            "od-nas-0001",
            expected_source_version="sha256:" + "0" * 64,
            indexed_digest=actual,
        )
        self.assertEqual(stale.status, "stale")


class SafeOpenTests(SourceAccessTestCase):
    def test_open_revalidates_and_passes_a_validated_path_argument(self) -> None:
        target = self.write_document("reports/quarterly.pdf", b"openable\n")
        registry = self.registry(self.nas_mapping())
        opener = RecordingOpener()

        decision = self.open_document(
            registry,
            "od-nas-0001",
            opener=opener,
            expected_source_version=sha256_of(target),
            corpus="payroll",
        )
        self.assertTrue(decision.opened)
        self.assertEqual(decision.status, "opened")
        self.assertEqual(decision.code, "opener_invoked")
        self.assertEqual(len(opener.calls), 1)

        handed = opener.calls[0]
        # The callback gets a validated Path object, never a command string.
        self.assertIsInstance(handed.path, Path)
        self.assertTrue(handed.path.is_absolute())
        self.assertEqual(handed.path.read_bytes(), b"openable\n")
        self.assertTrue(handed.path.resolve().is_relative_to(self.resolved_root))
        self.assertIsNone(handed.url)
        # ...and the public decision does not carry it.
        self.assertNotIn("path", decision.as_public_dict())

    def test_a_command_string_is_not_an_opener(self) -> None:
        self.write_document("reports/quarterly.pdf", b"x\n")
        registry = self.registry(self.nas_mapping())
        version = sha256_of(self.root / "reports" / "quarterly.pdf")

        for bad in ('cmd /c start "" "{}"', "notepad.exe", None, 42):
            with self.subTest(opener=bad):
                decision = self.open_document(
                    registry,
                    "od-nas-0001",
                    opener=bad,
                    expected_source_version=version,
                )
                self.assertFalse(decision.opened)
                self.assertEqual(decision.status, "refused")
                self.assertEqual(decision.code, "invalid_opener")

    def test_opener_failure_is_reported_without_echoing_the_path(self) -> None:
        target = self.write_document("reports/quarterly.pdf", b"x\n")
        registry = self.registry(self.nas_mapping())

        decision = self.open_document(
            registry,
            "od-nas-0001",
            opener=RaisingOpener(self.root_text),
            expected_source_version=sha256_of(target),
        )
        self.assertFalse(decision.opened)
        self.assertEqual(decision.status, "failed")
        self.assertEqual(decision.code, "opener_failed")

    def test_every_non_current_state_disables_the_open(self) -> None:
        target = self.write_document("reports/quarterly.pdf", b"x\n")
        actual = sha256_of(target)
        opener = RecordingOpener()

        cases = {
            "stale": dict(
                registry=self.registry(self.nas_mapping()),
                expected_source_version="sha256:" + "1" * 64,
            ),
            "unverifiable": dict(
                registry=self.registry(self.nas_mapping()),
                expected_source_version=None,
            ),
            "revoked": dict(
                registry=self.registry(self.nas_mapping(revoked=True)),
                expected_source_version=actual,
            ),
            "refused": dict(
                registry=self.registry(self.nas_mapping()),
                document_id="od-nas-9999",
                expected_source_version=actual,
            ),
        }
        for expected_status, kwargs in cases.items():
            with self.subTest(status=expected_status):
                registry = kwargs.pop("registry")
                document_id = kwargs.pop("document_id", "od-nas-0001")
                decision = self.open_document(
                    registry, document_id, opener=opener, **kwargs
                )
                self.assertFalse(decision.opened)
                self.assertEqual(decision.status, expected_status)
        self.assertEqual(opener.calls, [])

    def test_revoked_mapping_denies_although_the_file_is_present(self) -> None:
        target = self.write_document("reports/quarterly.pdf", b"still here\n")
        registry = self.registry(self.nas_mapping(revoked=True))
        opener = RecordingOpener()

        self.assertTrue(target.exists())
        status = self.verify(
            registry, "od-nas-0001", expected_source_version=sha256_of(target)
        )
        self.assertEqual(status.status, "revoked")
        self.assertEqual(status.code, "mapping_revoked")
        self.assertIsNone(status.source_version)
        self.assertFalse(status.open_allowed)

        decision = self.open_document(
            registry,
            "od-nas-0001",
            opener=opener,
            expected_source_version=sha256_of(target),
        )
        self.assertEqual(decision.status, "revoked")
        self.assertEqual(opener.calls, [])


class OpaqueIdentityTests(SourceAccessTestCase):
    def test_a_path_or_url_is_refused_as_a_document_id(self) -> None:
        self.write_document("reports/quarterly.pdf", b"x\n")
        registry = self.registry(self.nas_mapping())

        for hostile in (
            r"C:\Windows\System32\drivers\etc\hosts",
            "C:reports/quarterly.pdf",
            r"\\fileserver\payroll\quarterly.pdf",
            "//fileserver/payroll/quarterly.pdf",
            "../../etc/passwd",
            "https://www.notion.so/leak",
            "file:///C:/secret.pdf",
            self.root_text,
            os.path.join(self.root_text, "reports", "quarterly.pdf"),
            "",
            b"od-nas-0001",
            None,
        ):
            with self.subTest(document_id=hostile):
                result = self.verify(registry, hostile)
                self.assertEqual(result.status, "refused")
                self.assertEqual(result.code, "invalid_document_id")
                self.assertIsNone(result.source_version)
                # The rejected value is never echoed back.
                self.assertEqual(result.document_id, "<rejected>")

    def test_unknown_and_out_of_scope_ids_are_refused(self) -> None:
        self.write_document("reports/quarterly.pdf", b"x\n")
        registry = self.registry(self.nas_mapping())

        unknown = self.verify(registry, "od-nas-0002")
        self.assertEqual(unknown.status, "refused")
        self.assertEqual(unknown.code, "unknown_document")

        wrong_corpus = self.verify(registry, "od-nas-0001", corpus="legal")
        self.assertEqual(wrong_corpus.status, "refused")
        self.assertEqual(wrong_corpus.code, "corpus_mismatch")

    def test_registry_refuses_duplicate_ids_and_foreign_objects(self) -> None:
        with self.assertRaises(SourceMappingError) as duplicate:
            SourceMappingRegistry([self.nas_mapping(), self.nas_mapping()])
        self.assertEqual(duplicate.exception.code, "duplicate_document_id")

        with self.assertRaises(SourceMappingError) as foreign:
            SourceMappingRegistry([{"document_id": "od-nas-0001"}])
        self.assertEqual(foreign.exception.code, "invalid_mapping")

    def test_the_registry_is_not_extendable_after_construction(self) -> None:
        registry = self.registry(self.nas_mapping())
        self.assertFalse(hasattr(registry, "add"))
        self.assertEqual(registry.document_ids(), ("od-nas-0001",))


class ContainmentTests(SourceAccessTestCase):
    """Locations are owner config, and are still refused when malformed."""

    def refusal_for(self, location: str) -> str:
        registry = self.registry(self.nas_mapping(relative_location=location))
        result = self.verify(registry, "od-nas-0001")
        self.assertEqual(result.status, "refused")
        self.assertIn(result.code, LOCATION_REFUSAL_CODES)
        return result.code

    def test_traversal_and_override_shapes_are_refused(self) -> None:
        expected = {
            "../outside.pdf": "location_traversal",
            "reports/../../outside.pdf": "location_traversal",
            "reports/./quarterly.pdf": "location_traversal",
            "/etc/passwd": "location_absolute",
            "\\windows\\win.ini": "location_absolute",
            r"\\fileserver\share\x.pdf": "location_absolute",
            "//fileserver/share/x.pdf": "location_absolute",
            r"C:\payroll\x.pdf": "location_drive_qualified",
            "C:x.pdf": "location_drive_qualified",
            "reports/quarterly.pdf:hidden.txt": "location_drive_qualified",
            "reports//quarterly.pdf": "location_empty_segment",
            "reports/quarterly.pdf ": "location_trailing_dot_or_space",
            "reports/quarterly.pdf.": "location_trailing_dot_or_space",
            "reports/NUL.pdf": "location_reserved_name",
            "reports/con.pdf": "location_reserved_name",
            "reports/*.pdf": "location_forbidden_character",
            "reports/quarterly.pdf\x00.exe": "location_forbidden_character",
            "quarterly": "extension_missing",
        }
        for location, code in expected.items():
            with self.subTest(location=location):
                self.assertEqual(self.refusal_for(location), code)

    def test_an_alternate_data_stream_never_reaches_the_filesystem(self) -> None:
        # The stream suffix is refused as text, so the ".pdf" extension check is
        # never the only thing standing between the caller and other bytes.
        target = self.write_document("reports/quarterly.pdf", b"public\n")
        if os.name == "nt":
            try:
                with open(str(target) + ":hidden", "wb") as stream:
                    stream.write(b"private stream contents\n")
            except OSError:  # pragma: no cover - non-NTFS volume
                pass
        self.assertEqual(
            self.refusal_for("reports/quarterly.pdf:hidden"),
            "location_drive_qualified",
        )

    def test_lexical_escape_is_refused_independently_of_resolution(self) -> None:
        from integrations.opendocuments import nas_paths

        with self.assertRaises(nas_paths.MappedLocationError) as escape:
            nas_paths.lexical_target(self.resolved_root, ("..", "outside.pdf"))
        self.assertEqual(escape.exception.code, "location_escapes_root")

    def test_a_directory_standing_in_for_the_document_is_refused(self) -> None:
        (self.root / "reports" / "quarterly.pdf").mkdir(parents=True)
        registry = self.registry(self.nas_mapping())
        result = self.verify(registry, "od-nas-0001")
        self.assertEqual(result.status, "refused")
        self.assertEqual(result.code, "not_a_regular_file")


class ReparseTests(SourceAccessTestCase):
    def test_a_directory_junction_ancestor_is_refused(self) -> None:
        outside = Path(tempfile.mkdtemp(prefix="ns-r2-source-outside-"))
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        (outside / "quarterly.pdf").write_bytes(b"outside the approved root\n")

        junction = self.root / "reports"
        if not make_junction(junction, outside):
            self.skipTest(
                "directory junctions unavailable on this host; "
                "reparse-ancestor refusal not exercised"
            )
        self.addCleanup(lambda: junction.exists() and junction.rmdir())

        # os.path.islink is False for a junction -- that is exactly why the
        # reparse attribute, not islink alone, decides here.
        self.assertFalse(os.path.islink(junction))
        self.assertTrue(
            os.lstat(junction).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
        )
        registry = self.registry(self.nas_mapping())
        result = self.verify(registry, "od-nas-0001")
        self.assertEqual(result.status, "refused")
        self.assertEqual(result.code, "reparse_component")

    def test_a_native_symlink_component_is_refused(self) -> None:
        outside = Path(tempfile.mkdtemp(prefix="ns-r2-source-outside-"))
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        secret = outside / "quarterly.pdf"
        secret.write_bytes(b"outside the approved root\n")

        (self.root / "reports").mkdir()
        link = self.root / "reports" / "quarterly.pdf"
        try:
            os.symlink(secret, link)
        except (OSError, NotImplementedError, AttributeError) as error:
            self.skipTest(
                "native symlinks unavailable on this host ({}); "
                "junction case covers reparse refusal".format(type(error).__name__)
            )
        self.assertTrue(os.path.islink(link))
        registry = self.registry(self.nas_mapping())
        result = self.verify(registry, "od-nas-0001")
        self.assertEqual(result.status, "refused")
        self.assertEqual(result.code, "symlink_component")

    def test_link_status_names_which_question_went_unanswered(self) -> None:
        # Neither answer may be read as "not a link", and neither may be read
        # as "a reparse point": the OS said two different things.
        with mock.patch("os.lstat", side_effect=OSError(5, "device error")):
            self.assertEqual(
                nas_paths.link_status(self.root / "reports"), COMPONENT_INDETERMINATE
            )
        with mock.patch("os.lstat", side_effect=PermissionError(13, "denied")):
            self.assertEqual(
                nas_paths.link_status(self.root / "reports"), COMPONENT_ACCESS_DENIED
            )
        # And an ordinary directory on the real temp root still answers "no".
        (self.root / "reports").mkdir()
        self.assertIs(nas_paths.link_status(self.root / "reports"), False)


class ExtensionPolicyTests(SourceAccessTestCase):
    def test_the_default_allow_list_holds_no_launcher_format(self) -> None:
        self.assertEqual(DEFAULT_PERMITTED_EXTENSIONS & ALWAYS_REFUSED_EXTENSIONS, frozenset())
        for launcher in (".exe", ".bat", ".cmd", ".ps1", ".lnk", ".url", ".scr", ".js"):
            self.assertIn(launcher, ALWAYS_REFUSED_EXTENSIONS)
            self.assertNotIn(launcher, DEFAULT_PERMITTED_EXTENSIONS)

    def test_executable_and_shortcut_locations_are_refused(self) -> None:
        for location in (
            "reports/payroll.exe",
            "reports/payroll.pdf.exe",
            "reports/payroll.lnk",
            "reports/payroll.ps1",
            "reports/payroll.url",
            "reports/payroll.zip",
        ):
            with self.subTest(location=location):
                registry = self.registry(
                    self.nas_mapping(relative_location=location)
                )
                result = self.verify(registry, "od-nas-0001")
                self.assertEqual(result.status, "refused")
                self.assertEqual(result.code, "extension_not_permitted")

    def test_a_narrowed_allow_list_is_honoured(self) -> None:
        self.write_document("reports/quarterly.pdf", b"x\n")
        registry = self.registry(
            self.nas_mapping(permitted_extensions=frozenset({".docx"}))
        )
        result = self.verify(registry, "od-nas-0001")
        self.assertEqual(result.code, "extension_not_permitted")

    def test_configuration_may_not_widen_the_list_into_an_executable(self) -> None:
        with self.assertRaises(SourceMappingError) as widened:
            self.nas_mapping(
                permitted_extensions=frozenset({".pdf", ".exe"}),
                relative_location="reports/x.pdf",
            )
        self.assertEqual(widened.exception.code, "executable_extension")


class AvailabilityTests(SourceAccessTestCase):
    """Offline, deleted and denied must never be told as the same story."""

    def test_an_unavailable_root_is_not_a_missing_file(self) -> None:
        target = self.write_document("reports/quarterly.pdf", b"x\n")
        registry = self.registry(self.nas_mapping())
        version = sha256_of(target)
        shutil.rmtree(self.root_text)

        result = self.verify(registry, "od-nas-0001", expected_source_version=version)
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.code, "root_unavailable")
        self.assertIsNone(result.source_version)
        self.assertFalse(result.open_allowed)

    def test_a_deleted_file_under_a_live_root_is_missing(self) -> None:
        target = self.write_document("reports/quarterly.pdf", b"x\n")
        registry = self.registry(self.nas_mapping())
        version = sha256_of(target)
        target.unlink()

        result = self.verify(registry, "od-nas-0001", expected_source_version=version)
        self.assertEqual(result.status, "missing")
        self.assertEqual(result.code, "file_absent")

    def test_a_root_that_is_not_a_directory_is_unavailable(self) -> None:
        flat = Path(tempfile.mkdtemp(prefix="ns-r2-source-flat-"))
        self.addCleanup(shutil.rmtree, flat, ignore_errors=True)
        pretend_root = flat / "share"
        pretend_root.write_bytes(b"not a directory\n")
        registry = self.registry(self.nas_mapping(allowed_root=pretend_root))

        result = self.verify(registry, "od-nas-0001")
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.code, "root_unavailable")

    def test_a_denied_read_is_denied_and_not_missing(self) -> None:
        # Stubbed at the OS boundary: a real ACL denial would make this test
        # depend on the account the suite runs as.
        target = self.write_document("reports/quarterly.pdf", b"x\n")
        registry = self.registry(self.nas_mapping())
        real_stat = os.stat

        def denied(path, *args, **kwargs):
            if str(path) == str(target):
                raise PermissionError(13, "Access is denied")
            return real_stat(path, *args, **kwargs)

        with mock.patch("os.stat", side_effect=denied):
            result = self.verify(registry, "od-nas-0001")
        self.assertEqual(result.status, "denied")
        self.assertEqual(result.code, "file_access_denied")
        self.assertTrue(target.exists())

    def test_a_denied_root_is_denied_and_not_unavailable(self) -> None:
        self.write_document("reports/quarterly.pdf", b"x\n")
        registry = self.registry(self.nas_mapping())
        real_stat = os.stat

        def denied(path, *args, **kwargs):
            if str(path) == str(self.resolved_root):
                raise PermissionError(13, "Access is denied")
            return real_stat(path, *args, **kwargs)

        with mock.patch("os.stat", side_effect=denied):
            result = self.verify(registry, "od-nas-0001")
        self.assertEqual(result.status, "denied")
        self.assertEqual(result.code, "root_access_denied")

    def test_an_undistinguishable_failure_stays_unverifiable(self) -> None:
        target = self.write_document("reports/quarterly.pdf", b"x\n")
        registry = self.registry(self.nas_mapping())
        real_stat = os.stat

        def flaky(path, *args, **kwargs):
            if str(path) == str(target):
                raise OSError(1234, "unmapped device failure")
            return real_stat(path, *args, **kwargs)

        with mock.patch("os.stat", side_effect=flaky):
            result = self.verify(registry, "od-nas-0001")
        # "I could not tell" must never be reported as "it was deleted".
        self.assertEqual(result.status, "unverifiable")
        self.assertEqual(result.code, "indeterminate_access")
        self.assertNotEqual(result.status, "missing")


class BoundedReadTests(SourceAccessTestCase):
    def test_a_file_edited_during_the_read_is_unverifiable(self) -> None:
        target = self.write_document("reports/quarterly.pdf", b"A" * 4096)
        registry = self.registry(self.nas_mapping())
        original = sha256_of(target)
        real_read = getattr(
            __import__(SOURCE_ACCESS_NAS, fromlist=["_read_chunk"]), "_read_chunk"
        )
        state = {"edited": False}

        def editing_read(handle, size):
            chunk = real_read(handle, size)
            if chunk and not state["edited"]:
                # The race made deterministic: the source grows mid-read.
                state["edited"] = True
                with open(target, "ab") as writer:
                    writer.write(b"appended after the read began\n")
            return chunk

        with mock.patch(SOURCE_ACCESS_NAS + "._read_chunk", side_effect=editing_read):
            result = self.verify(
                registry, "od-nas-0001", expected_source_version=original
            )
        self.assertEqual(result.status, "unverifiable")
        self.assertEqual(result.code, "changed_during_read")
        self.assertIsNone(result.source_version)
        self.assertFalse(result.open_allowed)

    def test_an_oversized_file_is_unverifiable_not_current(self) -> None:
        target = self.write_document("reports/quarterly.pdf", b"B" * 8192)
        registry = self.registry(self.nas_mapping())
        version = sha256_of(target)

        with mock.patch(SOURCE_ACCESS_NAS + ".MAX_VERIFIED_BYTES", 1024):
            result = self.verify(
                registry, "od-nas-0001", expected_source_version=version
            )
        self.assertEqual(result.status, "unverifiable")
        self.assertEqual(result.code, "file_too_large")
        self.assertIsNone(result.source_version)

    def test_the_cap_is_the_documented_sixty_four_mebibytes(self) -> None:
        self.assertEqual(MAX_VERIFIED_BYTES, 64 * 1024 * 1024)

    def test_no_file_body_is_retained_on_the_result(self) -> None:
        body = b"CONFIDENTIAL PAYROLL BODY\n" * 64
        target = self.write_document("reports/quarterly.pdf", body)
        registry = self.registry(self.nas_mapping())
        result = self.verify(
            registry, "od-nas-0001", expected_source_version=sha256_of(target)
        )
        self.assertNotIn("CONFIDENTIAL", repr(result))
        self.assertNotIn("CONFIDENTIAL", repr(result.as_public_dict()))


class ExpectedVersionTests(SourceAccessTestCase):
    def test_a_malformed_expected_version_is_refused(self) -> None:
        self.write_document("reports/quarterly.pdf", b"x\n")
        registry = self.registry(self.nas_mapping())
        for bad in ("deadbeef", "md5:" + "a" * 32, "sha256:" + "A" * 64, "sha256:", 7):
            with self.subTest(expected=bad):
                result = self.verify(
                    registry, "od-nas-0001", expected_source_version=bad
                )
                self.assertEqual(result.status, "refused")
                self.assertEqual(result.code, "invalid_expected_version")


class NotionResolverTests(SourceAccessTestCase):
    def test_unsafe_pinned_urls_are_refused_at_configuration_time(self) -> None:
        for url in (
            "http://www.notion.so/page",
            "https://notion.so.attacker.example/page",
            "https://evil.example/www.notion.so/page",
            "https://user:secret@www.notion.so/page",
            "https://www.notion.so:8443/page",
            "https://www.notion.so/page?token=abc",
            "https://www.notion.so/page#fragment",
            "https://xn--ntion-fsa.so/page",
            "https://.notion.site/page",
            "file:///C:/secret.pdf",
            "javascript:alert(1)",
            "",
        ):
            with self.subTest(url=url):
                self.assertEqual(unsafe_notion_url_code(url), "unsafe_source_url")
                with self.assertRaises(SourceMappingError) as refused:
                    self.notion_mapping(page_url=url)
                self.assertEqual(refused.exception.code, "unsafe_source_url")

    def test_a_missing_pinned_url_is_an_incomplete_mapping(self) -> None:
        # A Notion mapping with no URL at all is not an unsafe URL; it is a
        # mapping the owner never finished writing, and says so.
        self.assertEqual(unsafe_notion_url_code(None), "unsafe_source_url")
        for absent in (None, 7, b"https://www.notion.so/page"):
            with self.subTest(page_url=absent):
                with self.assertRaises(SourceMappingError) as refused:
                    self.notion_mapping(page_url=absent)
                self.assertEqual(refused.exception.code, "mapping_incomplete")

    def test_allow_listed_hosts_are_accepted(self) -> None:
        for url in (
            "https://www.notion.so/workspace/Plan-8f2c1d",
            "https://notion.so/Plan-8f2c1d",
            "https://acme-team.notion.site/Plan-8f2c1d",
            # A host name is case-insensitive, so this IS the allow-listed host.
            "https://WWW.NOTION.SO/Plan-8f2c1d",
        ):
            with self.subTest(url=url):
                self.assertIsNone(unsafe_notion_url_code(url))
                self.assertEqual(self.notion_mapping(page_url=url).page_url, url)

    def test_without_a_verifier_the_answer_is_unverifiable_and_open_is_off(self) -> None:
        registry = self.registry(self.notion_mapping())
        opener = RecordingOpener()

        result = self.verify(registry, "od-notion-0001")
        self.assertEqual(result.status, "unverifiable")
        self.assertEqual(result.code, "no_origin_verifier")
        self.assertIsNone(result.source_version)
        self.assertFalse(result.open_allowed)

        decision = self.open_document(
            registry, "od-notion-0001", opener=opener, expected_source_version="v7"
        )
        self.assertFalse(decision.opened)
        self.assertEqual(decision.code, "no_origin_verifier")
        self.assertEqual(opener.calls, [])

    def test_a_current_verifier_result_enables_a_url_open(self) -> None:
        registry = self.registry(self.notion_mapping())
        opener = RecordingOpener()
        seen: list[OriginRequest] = []

        def verifier(request: OriginRequest) -> OriginAttestation:
            seen.append(request)
            return OriginAttestation(document_id=request.document_id, source_version="v7")

        result = self.verify(
            registry,
            "od-notion-0001",
            expected_source_version="v7",
            origin_verifier=verifier,
        )
        self.assertEqual(result.status, "current")
        self.assertEqual(result.code, "origin_verified")
        self.assertEqual(seen[0].url, SAFE_PAGE_URL)
        self.assertEqual(seen[0].document_id, "od-notion-0001")

        decision = self.open_document(
            registry,
            "od-notion-0001",
            opener=opener,
            expected_source_version="v7",
            origin_verifier=verifier,
        )
        self.assertTrue(decision.opened)
        self.assertEqual(opener.calls[0].url, SAFE_PAGE_URL)
        self.assertIsNone(opener.calls[0].path)

    def test_an_unbound_or_versionless_attestation_never_verifies(self) -> None:
        registry = self.registry(self.notion_mapping())
        cases = {
            "verifier_document_mismatch": lambda request: OriginAttestation(
                document_id="od-notion-9999", source_version="v7"
            ),
            "verifier_no_version": lambda request: OriginAttestation(
                document_id=request.document_id
            ),
            "no_origin_verifier": lambda request: None,
            "verifier_failed": lambda request: {"source_version": "v7"},
        }
        for code, verifier in cases.items():
            with self.subTest(code=code):
                result = self.verify(
                    registry,
                    "od-notion-0001",
                    expected_source_version="v7",
                    origin_verifier=verifier,
                )
                self.assertEqual(result.status, "unverifiable")
                self.assertEqual(result.code, code)
                self.assertFalse(result.open_allowed)

    def test_a_raising_verifier_attests_nothing(self) -> None:
        registry = self.registry(self.notion_mapping())

        def verifier(request: OriginRequest) -> OriginAttestation:
            raise RuntimeError("token expired for {}".format(request.url))

        result = self.verify(
            registry,
            "od-notion-0001",
            expected_source_version="v7",
            origin_verifier=verifier,
        )
        self.assertEqual(result.status, "unverifiable")
        self.assertEqual(result.code, "verifier_failed")
        self.assertNotIn("token expired", repr(result.as_public_dict()))

    def test_a_stale_page_is_stale(self) -> None:
        registry = self.registry(self.notion_mapping())
        result = self.verify(
            registry,
            "od-notion-0001",
            expected_source_version="v6",
            origin_verifier=lambda request: OriginAttestation(
                document_id=request.document_id, source_version="v7"
            ),
        )
        self.assertEqual(result.status, "stale")
        self.assertFalse(result.open_allowed)

    def test_the_pinned_resolver_refuses_a_nas_mapping(self) -> None:
        self.write_document("reports/quarterly.pdf", b"x\n")
        registry = self.registry(self.nas_mapping(), self.notion_mapping())
        result = resolve_notion_source("od-nas-0001", registry=registry)
        self.assert_public_and_closed(result)
        self.assertEqual(result.status, "refused")
        self.assertEqual(result.code, "backend_mismatch")

    def test_a_revoked_notion_mapping_denies_before_any_verifier_runs(self) -> None:
        registry = self.registry(self.notion_mapping(revoked=True))
        calls: list[OriginRequest] = []

        result = resolve_notion_source(
            "od-notion-0001",
            registry=registry,
            expected_source_version="v7",
            origin_verifier=lambda request: calls.append(request)
            or OriginAttestation(document_id=request.document_id, source_version="v7"),
        )
        self.assertEqual(result.status, "revoked")
        self.assertEqual(calls, [])


class AdapterBoundaryTests(SourceAccessTestCase):
    def test_the_adapter_does_not_import_work_stack_core(self) -> None:
        for module in ("integrations/opendocuments/source_access.py",
                       "integrations/opendocuments/nas_paths.py"):
            source = Path(module).read_text(encoding="utf-8")
            for line in source.splitlines():
                stripped = line.strip()
                if stripped.startswith(("import ", "from ")):
                    self.assertNotIn("workstack", stripped, module)

    def test_the_package_defines_no_shared_initialiser(self) -> None:
        # The client worker owns od_client.py in the same namespace package;
        # neither side may claim a shared __init__.
        self.assertFalse(Path("integrations/opendocuments/__init__.py").exists())

    def test_mapping_configuration_errors_are_loud(self) -> None:
        cases = {
            "invalid_document_id": dict(document_id="../etc/passwd"),
            "invalid_corpus": dict(corpus="pay roll"),
            "invalid_backend": dict(backend="ftp"),
            "root_not_absolute": dict(allowed_root=Path("relative/share")),
            "mapping_incomplete": dict(relative_location=None),
            "mapping_field_conflict": dict(page_url=SAFE_PAGE_URL),
        }
        for code, overrides in cases.items():
            with self.subTest(code=code):
                with self.assertRaises(SourceMappingError) as error:
                    self.nas_mapping(**overrides)
                self.assertEqual(error.exception.code, code)


class IndexedDigestShapeTests(SourceAccessTestCase):
    """An index digest is a claim, and a claim must at least look like one."""

    def test_a_canonical_digest_is_accepted_echoed_and_still_not_authority(self) -> None:
        target = self.write_document("reports/quarterly.pdf", b"body\n")
        registry = self.registry(self.nas_mapping())
        actual = sha256_of(target)

        echoed = self.verify(registry, "od-nas-0001", indexed_digest=actual)
        self.assertEqual(echoed.status, "unverifiable")
        self.assertEqual(echoed.code, "no_expected_version")
        # Preserved verbatim in the public projection...
        self.assertEqual(echoed.as_public_dict()["indexed_digest"], actual)
        # ...and still not freshness authority: it equals the real source hash
        # and the document is not current.
        self.assertFalse(echoed.open_allowed)

        current = self.verify(
            registry,
            "od-nas-0001",
            expected_source_version=actual,
            indexed_digest=actual,
        )
        self.assertEqual(current.status, "current")
        self.assertEqual(current.as_public_dict()["indexed_digest"], actual)
        # A digest that matches nothing on disk still changes no status.
        unrelated = "sha256:" + "b" * 64
        other = self.verify(
            registry,
            "od-nas-0001",
            expected_source_version=actual,
            indexed_digest=unrelated,
        )
        self.assertEqual(other.status, "current")
        self.assertEqual(other.source_version, actual)
        self.assertEqual(other.as_public_dict()["indexed_digest"], unrelated)

    def test_a_mapped_absolute_path_offered_as_a_digest_is_refused_not_echoed(self) -> None:
        target = self.write_document("reports/quarterly.pdf", b"body\n")
        registry = self.registry(self.nas_mapping())

        result = self.verify(registry, "od-nas-0001", indexed_digest=str(target))
        self.assertEqual(result.status, "refused")
        self.assertEqual(result.code, "invalid_indexed_digest")
        public = result.as_public_dict()
        self.assertIsNone(public["indexed_digest"])
        # The whole point: the offered text does not cross the projection.
        self.assertNotIn(str(target), repr(public))
        self.assertNotIn(self.root_text, repr(public))
        self.assertFalse(result.open_allowed)

    def test_every_non_canonical_digest_shape_uses_the_same_closed_code(self) -> None:
        self.write_document("reports/quarterly.pdf", b"body\n")
        registry = self.registry(self.nas_mapping())
        digest_body = "a" * 64
        for offered in (
            "",
            "not-a-digest",
            digest_body,  # bare hex, no algorithm
            "sha256:" + digest_body.upper(),  # uppercase is not canonical
            "sha256:" + "a" * 63,  # too short
            "sha256:" + "a" * 65,  # too long
            "sha256:" + "g" * 64,  # not hex
            " sha256:" + digest_body,
            "sha256:" + digest_body + "\n",
            "sha1:" + "a" * 40,
            "https://example.invalid/doc.pdf",
            "\\\\server\\share\\quarterly.pdf",
            b"sha256:" + digest_body.encode(),  # not text at all
            17,
            object(),
        ):
            with self.subTest(offered=repr(offered)[:48]):
                result = self.verify(registry, "od-nas-0001", indexed_digest=offered)
                self.assertEqual(result.status, "refused")
                self.assertEqual(result.code, "invalid_indexed_digest")
                self.assertIsNone(result.indexed_digest)
                self.assertIsNone(result.as_public_dict()["indexed_digest"])

    def test_a_malformed_digest_is_refused_before_the_source_is_touched(self) -> None:
        # No file exists under the mapping at all; the refusal is about the
        # argument, so the filesystem answer never enters into it.
        registry = self.registry(self.nas_mapping())
        result = self.verify(
            registry, "od-nas-0001", indexed_digest="C:\\Windows\\System32"
        )
        self.assertEqual(result.code, "invalid_indexed_digest")
        self.assertIsNone(result.source_version)

    def test_the_notion_backend_refuses_the_same_malformed_shapes(self) -> None:
        registry = self.registry(self.notion_mapping())
        result = self.verify(registry, "od-notion-0001", indexed_digest="not-a-digest")
        self.assertEqual(result.status, "refused")
        self.assertEqual(result.code, "invalid_indexed_digest")
        self.assertIsNone(result.as_public_dict()["indexed_digest"])


class DistinguishedAccessFailureTests(SourceAccessTestCase):
    """Denied, unreachable and unknown are three answers, not one.

    The OS-boundary stubs here are the ones the rest of this suite already
    uses for machine-dependent access states: one path's ``lstat``, ``stat`` or
    ``resolve`` is redirected and every other call is delegated to the real one,
    because producing a genuine ACL denial or a dead-share error would make the
    suite depend on the account and the network it runs as. Every stub is
    applied *after* the mapping is built, so the owner's root validation runs
    unpatched against the real temporary root in every test here. Each test's
    positive control runs unpatched too, and one control checks that the
    injection harness itself does not disturb an answer it was not aimed at.
    """

    def _deny_lstat_for(self, denied_path, error):
        real_lstat = os.lstat

        def side_effect(path, *args, **kwargs):
            if os.fspath(path) == os.fspath(denied_path):
                raise error
            return real_lstat(path, *args, **kwargs)

        return mock.patch.object(nas_paths.os, "lstat", side_effect=side_effect)

    def _fail_stat_for(self, failing_path, error):
        real_stat = os.stat

        def side_effect(path, *args, **kwargs):
            if os.fspath(path) == os.fspath(failing_path):
                raise error
            return real_stat(path, *args, **kwargs)

        return mock.patch.object(source_access_nas.os, "stat", side_effect=side_effect)

    def _fail_resolve_for(self, failing_path, error):
        """Raise from ``Path.resolve`` for one path; delegate every other call.

        This is the third face of the same OS boundary. ``resolved_within``
        asks the OS to place the root and the mapped target; that question can
        be denied or fail unexplained exactly like the ``lstat`` above it, and
        neither answer means the location left the approved root.
        """

        real_resolve = Path.resolve
        failing = os.fspath(failing_path)

        def side_effect(path, *args, **kwargs):
            if os.fspath(path) == failing:
                raise error
            return real_resolve(path, *args, **kwargs)

        return mock.patch.object(
            Path, "resolve", autospec=True, side_effect=side_effect
        )

    def _redirect_resolve_for(self, redirected_path, replacement):
        """Return a *different* real location for one path's resolution.

        Unlike the failure above, this is an answered question, and the answer
        places the target outside the root. It must still be refused as an
        escape: the correction tells the two apart, it does not delete the
        escape finding.
        """

        real_resolve = Path.resolve
        redirected = os.fspath(redirected_path)

        def side_effect(path, *args, **kwargs):
            if os.fspath(path) == redirected:
                return replacement
            return real_resolve(path, *args, **kwargs)

        return mock.patch.object(
            Path, "resolve", autospec=True, side_effect=side_effect
        )

    def mapped_root(self):
        root = self.registry(self.nas_mapping()).lookup("od-nas-0001").allowed_root
        assert root is not None
        return root

    def mapped_target(self):
        return self.mapped_root() / "reports" / "quarterly.pdf"

    def test_the_unpatched_mapping_verifies_current_against_the_real_root(self) -> None:
        target = self.write_document("reports/quarterly.pdf", b"real bytes\n")
        registry = self.registry(self.nas_mapping())
        result = self.verify(
            registry, "od-nas-0001", expected_source_version=sha256_of(target)
        )
        self.assertEqual(result.status, "current")
        self.assertEqual(result.code, "hash_matched")

    def test_denied_ancestor_metadata_is_denied_not_an_invented_reparse_finding(self) -> None:
        target = self.write_document("reports/quarterly.pdf", b"real bytes\n")
        registry = self.registry(self.nas_mapping())

        with self._deny_lstat_for(self.root / "reports", PermissionError(13, "denied")):
            result = self.verify(
                registry, "od-nas-0001", expected_source_version=sha256_of(target)
            )
        self.assertEqual(result.status, "denied")
        self.assertEqual(result.code, COMPONENT_ACCESS_DENIED)
        self.assertNotEqual(result.code, "reparse_component")
        # Still fail-closed: nothing was read and nothing is current.
        self.assertIsNone(result.source_version)
        self.assertFalse(result.open_allowed)

    def test_a_denied_ancestor_also_disables_the_open(self) -> None:
        target = self.write_document("reports/quarterly.pdf", b"real bytes\n")
        registry = self.registry(self.nas_mapping())
        opener = RecordingOpener()

        with self._deny_lstat_for(self.root / "reports", PermissionError(13, "denied")):
            decision = self.open_document(
                registry,
                "od-nas-0001",
                opener=opener,
                expected_source_version=sha256_of(target),
            )
        self.assertFalse(decision.opened)
        self.assertEqual(decision.status, "denied")
        self.assertEqual(decision.code, COMPONENT_ACCESS_DENIED)
        self.assertEqual(opener.calls, [])

    def test_an_undifferentiated_ancestor_error_stays_unverifiable(self) -> None:
        target = self.write_document("reports/quarterly.pdf", b"real bytes\n")
        registry = self.registry(self.nas_mapping())

        with self._deny_lstat_for(self.root / "reports", OSError(5, "device error")):
            result = self.verify(
                registry, "od-nas-0001", expected_source_version=sha256_of(target)
            )
        self.assertEqual(result.status, "unverifiable")
        self.assertEqual(result.code, "indeterminate_access")
        # Not a reparse finding, not missing, not offline.
        self.assertNotEqual(result.code, "reparse_component")
        self.assertIsNone(result.source_version)

    def test_a_genuine_reparse_ancestor_keeps_its_own_code(self) -> None:
        # Guard against "fixing" the misclassification by deleting the finding:
        # a real junction must still say reparse_component.
        outside = Path(tempfile.mkdtemp(prefix="ns-r2-source-outside-"))
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        (outside / "quarterly.pdf").write_bytes(b"outside\n")
        junction = self.root / "reports"
        if not make_junction(junction, outside):
            self.skipTest(
                "directory junctions unavailable on this host; the genuine "
                "reparse-versus-denied separation is not exercised"
            )
        self.addCleanup(lambda: junction.exists() and junction.rmdir())
        registry = self.registry(self.nas_mapping())
        result = self.verify(registry, "od-nas-0001")
        self.assertEqual(result.status, "refused")
        self.assertEqual(result.code, "reparse_component")

    def test_an_undifferentiated_root_error_is_unverifiable_not_offline(self) -> None:
        target = self.write_document("reports/quarterly.pdf", b"real bytes\n")
        registry = self.registry(self.nas_mapping())

        with self._fail_stat_for(
            self.mapped_root(), OSError(1234, "unknown root error")
        ):
            result = self.verify(
                registry, "od-nas-0001", expected_source_version=sha256_of(target)
            )
        self.assertEqual(result.status, "unverifiable")
        self.assertEqual(result.code, "indeterminate_access")
        self.assertNotEqual(result.status, "unavailable")
        self.assertNotEqual(result.status, "missing")
        self.assertFalse(result.open_allowed)

    def test_an_unreachable_share_errno_is_still_reported_unavailable(self) -> None:
        target = self.write_document("reports/quarterly.pdf", b"real bytes\n")
        registry = self.registry(self.nas_mapping())

        for name in ("EHOSTUNREACH", "ENETDOWN", "ENODEV", "ETIMEDOUT"):
            code = getattr(errno, name, None)
            if code is None:  # pragma: no cover - platform dependent
                continue
            with self.subTest(errno=name):
                with self._fail_stat_for(
                    self.mapped_root(), OSError(code, "the share is gone")
                ):
                    result = self.verify(
                        registry,
                        "od-nas-0001",
                        expected_source_version=sha256_of(target),
                    )
                self.assertEqual(result.status, "unavailable")
                self.assertEqual(result.code, "root_unavailable")

    def test_a_denied_root_is_still_denied(self) -> None:
        target = self.write_document("reports/quarterly.pdf", b"real bytes\n")
        registry = self.registry(self.nas_mapping())

        with self._fail_stat_for(self.mapped_root(), PermissionError(13, "denied")):
            result = self.verify(
                registry, "od-nas-0001", expected_source_version=sha256_of(target)
            )
        self.assertEqual(result.status, "denied")
        self.assertEqual(result.code, "root_access_denied")

    def test_the_resolution_stub_leaves_an_unaimed_answer_current(self) -> None:
        # Control for the tests below: with Path.resolve patched but aimed at a
        # path this mapping never resolves, the ordinary answer is unchanged
        # and the open still happens. A refusal in the next tests is therefore
        # the injected failure, not the harness.
        target = self.write_document("reports/quarterly.pdf", b"real bytes\n")
        registry = self.registry(self.nas_mapping())
        opener = RecordingOpener()
        elsewhere = self.mapped_root() / "reports" / "unrelated.pdf"

        with self._fail_resolve_for(elsewhere, PermissionError(13, "denied")):
            result = self.verify(
                registry, "od-nas-0001", expected_source_version=sha256_of(target)
            )
            decision = self.open_document(
                registry,
                "od-nas-0001",
                opener=opener,
                expected_source_version=sha256_of(target),
            )
        self.assertEqual((result.status, result.code), ("current", "hash_matched"))
        self.assertTrue(decision.opened)
        self.assertEqual(len(opener.calls), 1)

    def test_a_denied_target_resolution_is_denied_not_an_escape(self) -> None:
        target = self.write_document("reports/quarterly.pdf", b"real bytes\n")
        registry = self.registry(self.nas_mapping())

        with self._fail_resolve_for(
            self.mapped_target(), PermissionError(13, "denied resolving target")
        ):
            result = self.verify(
                registry, "od-nas-0001", expected_source_version=sha256_of(target)
            )
        self.assertEqual(result.status, "denied")
        self.assertEqual(result.code, COMPONENT_ACCESS_DENIED)
        # The containment check never completed, so it may not report that
        # containment failed.
        self.assertNotEqual(result.code, "location_escapes_root")
        self.assertIsNone(result.source_version)
        self.assertFalse(result.open_allowed)

    def test_a_denied_target_resolution_also_disables_the_open(self) -> None:
        target = self.write_document("reports/quarterly.pdf", b"real bytes\n")
        registry = self.registry(self.nas_mapping())
        opener = RecordingOpener()

        with self._fail_resolve_for(
            self.mapped_target(), PermissionError(13, "denied resolving target")
        ):
            decision = self.open_document(
                registry,
                "od-nas-0001",
                opener=opener,
                expected_source_version=sha256_of(target),
            )
        self.assertFalse(decision.opened)
        self.assertEqual(decision.status, "denied")
        self.assertEqual(decision.code, COMPONENT_ACCESS_DENIED)
        self.assertEqual(opener.calls, [])

    def test_an_undifferentiated_target_resolution_stays_unverifiable(self) -> None:
        target = self.write_document("reports/quarterly.pdf", b"real bytes\n")
        registry = self.registry(self.nas_mapping())

        with self._fail_resolve_for(
            self.mapped_target(), OSError(1234, "unknown resolving target")
        ):
            result = self.verify(
                registry, "od-nas-0001", expected_source_version=sha256_of(target)
            )
        self.assertEqual(result.status, "unverifiable")
        self.assertEqual(result.code, "indeterminate_access")
        self.assertNotEqual(result.code, "location_escapes_root")
        self.assertNotEqual(result.status, "refused")
        self.assertNotEqual(result.status, "missing")
        self.assertIsNone(result.source_version)
        self.assertFalse(result.open_allowed)

    def test_an_undifferentiated_target_resolution_also_disables_the_open(self) -> None:
        target = self.write_document("reports/quarterly.pdf", b"real bytes\n")
        registry = self.registry(self.nas_mapping())
        opener = RecordingOpener()

        with self._fail_resolve_for(
            self.mapped_target(), OSError(1234, "unknown resolving target")
        ):
            decision = self.open_document(
                registry,
                "od-nas-0001",
                opener=opener,
                expected_source_version=sha256_of(target),
            )
        self.assertFalse(decision.opened)
        self.assertEqual(decision.status, "unverifiable")
        self.assertEqual(decision.code, "indeterminate_access")
        self.assertEqual(opener.calls, [])

    def test_a_denied_root_resolution_is_denied_not_an_escape(self) -> None:
        # The other branch of the same containment query. The root is resolved
        # again inside the check, and a denial there is a denial too.
        target = self.write_document("reports/quarterly.pdf", b"real bytes\n")
        registry = self.registry(self.nas_mapping())
        opener = RecordingOpener()

        with self._fail_resolve_for(
            self.mapped_root(), PermissionError(13, "denied resolving root")
        ):
            result = self.verify(
                registry, "od-nas-0001", expected_source_version=sha256_of(target)
            )
            decision = self.open_document(
                registry,
                "od-nas-0001",
                opener=opener,
                expected_source_version=sha256_of(target),
            )
        self.assertEqual(result.status, "denied")
        self.assertEqual(result.code, COMPONENT_ACCESS_DENIED)
        self.assertNotEqual(result.code, "location_escapes_root")
        self.assertFalse(decision.opened)
        self.assertEqual(decision.code, COMPONENT_ACCESS_DENIED)
        self.assertEqual(opener.calls, [])

    def test_an_undifferentiated_root_resolution_stays_unverifiable(self) -> None:
        target = self.write_document("reports/quarterly.pdf", b"real bytes\n")
        registry = self.registry(self.nas_mapping())
        opener = RecordingOpener()

        with self._fail_resolve_for(
            self.mapped_root(), OSError(1234, "unknown resolving root")
        ):
            result = self.verify(
                registry, "od-nas-0001", expected_source_version=sha256_of(target)
            )
            decision = self.open_document(
                registry,
                "od-nas-0001",
                opener=opener,
                expected_source_version=sha256_of(target),
            )
        self.assertEqual(result.status, "unverifiable")
        self.assertEqual(result.code, "indeterminate_access")
        self.assertNotEqual(result.code, "location_escapes_root")
        self.assertFalse(decision.opened)
        self.assertEqual(decision.status, "unverifiable")
        self.assertEqual(opener.calls, [])

    def test_a_target_that_really_resolves_outside_is_still_an_escape(self) -> None:
        # The answered case must not be softened into "unverifiable" by the
        # correction: a resolution that lands outside the approved root is a
        # containment failure and keeps its refusal.
        target = self.write_document("reports/quarterly.pdf", b"real bytes\n")
        outside = Path(tempfile.mkdtemp(prefix="ns-r2-source-outside-"))
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        registry = self.registry(self.nas_mapping())
        opener = RecordingOpener()

        with self._redirect_resolve_for(
            self.mapped_target(), (outside / "quarterly.pdf").resolve()
        ):
            result = self.verify(
                registry, "od-nas-0001", expected_source_version=sha256_of(target)
            )
            decision = self.open_document(
                registry,
                "od-nas-0001",
                opener=opener,
                expected_source_version=sha256_of(target),
            )
        self.assertEqual(result.status, "refused")
        self.assertEqual(result.code, "location_escapes_root")
        self.assertFalse(decision.opened)
        self.assertEqual(decision.code, "location_escapes_root")
        self.assertEqual(opener.calls, [])

    def test_the_unanswered_component_codes_are_in_the_closed_vocabulary(self) -> None:
        for code in (COMPONENT_ACCESS_DENIED, COMPONENT_INDETERMINATE):
            self.assertIn(code, LOCATION_REFUSAL_CODES)
        self.assertIn(COMPONENT_ACCESS_DENIED, VERIFICATION_CODES)
        self.assertIn("indeterminate_access", VERIFICATION_CODES)


class OpenTargetCoherenceTests(SourceAccessTestCase):
    """The version in the decision must describe the file the opener is handed."""

    def test_the_mapped_target_is_resolved_once_per_open(self) -> None:
        target = self.write_document("reports/quarterly.pdf", b"openable\n")
        registry = self.registry(self.nas_mapping())
        opener = RecordingOpener()
        real_nas_target = source_access_nas._nas_target

        with mock.patch.object(
            source_access_nas, "_nas_target", side_effect=real_nas_target
        ) as resolved:
            decision = self.open_document(
                registry,
                "od-nas-0001",
                opener=opener,
                expected_source_version=sha256_of(target),
            )
        self.assertTrue(decision.opened)
        # One resolution, so there is no interval between "the path I hashed"
        # and "the path I handed over" for a write to land in.
        self.assertEqual(resolved.call_count, 1)

    def test_the_callback_receives_the_bytes_the_version_describes(self) -> None:
        target = self.write_document("reports/quarterly.pdf", b"openable\n")
        registry = self.registry(self.nas_mapping())
        observed: list[str] = []

        def opener(handed) -> None:
            assert handed.path is not None
            observed.append(
                "sha256:" + hashlib.sha256(handed.path.read_bytes()).hexdigest()
            )
            observed.append(handed.source_version)

        decision = self.open_document(
            registry,
            "od-nas-0001",
            opener=opener,
            expected_source_version=sha256_of(target),
        )
        self.assertTrue(decision.opened)
        self.assertEqual(observed[0], observed[1])
        self.assertEqual(observed[0], decision.source_version)

    def test_a_write_during_target_resolution_is_caught_before_the_callback(self) -> None:
        # The reviewer's counterexample, aimed at the step that still exists:
        # an ordinary content rewrite lands while the mapped target is being
        # resolved. Because the hash now follows that resolution rather than
        # preceding a second one, the change is inside the checked window.
        target = self.write_document("reports/quarterly.pdf", b"original\n")
        registry = self.registry(self.nas_mapping())
        expected = sha256_of(target)
        opener = RecordingOpener()
        real_nas_target = source_access_nas._nas_target

        def resolve_then_edit(mapping):
            resolved = real_nas_target(mapping)
            target.write_bytes(b"changed-before-open\n")
            return resolved

        with mock.patch.object(
            source_access_nas, "_nas_target", side_effect=resolve_then_edit
        ):
            decision = self.open_document(
                registry,
                "od-nas-0001",
                opener=opener,
                expected_source_version=expected,
            )
        self.assertFalse(decision.opened)
        self.assertEqual(decision.status, "stale")
        self.assertEqual(decision.code, "hash_differs")
        self.assertEqual(opener.calls, [])
        self.assertEqual(target.read_bytes(), b"changed-before-open\n")

    def test_a_write_completed_before_the_call_is_stale(self) -> None:
        target = self.write_document("reports/quarterly.pdf", b"original\n")
        registry = self.registry(self.nas_mapping())
        expected = sha256_of(target)
        target.write_bytes(b"changed-before-the-call\n")
        opener = RecordingOpener()

        decision = self.open_document(
            registry, "od-nas-0001", opener=opener, expected_source_version=expected
        )
        self.assertEqual(decision.status, "stale")
        self.assertEqual(opener.calls, [])

    def test_a_write_after_the_final_hash_is_the_documented_remaining_gap(self) -> None:
        # This asserts the *limit*, not a guarantee. A write that lands after
        # the final hash -- here from inside the callback, standing in for the
        # external application's own read window -- is not detected, and the
        # module says so rather than implying atomic semantics.
        target = self.write_document("reports/quarterly.pdf", b"original\n")
        registry = self.registry(self.nas_mapping())
        expected = sha256_of(target)
        seen: list[bytes] = []

        def mutating_opener(handed) -> None:
            assert handed.path is not None
            seen.append(handed.path.read_bytes())
            handed.path.write_bytes(b"changed-after-the-check\n")

        decision = self.open_document(
            registry,
            "od-nas-0001",
            opener=mutating_opener,
            expected_source_version=expected,
        )
        self.assertTrue(decision.opened)
        # What the callback was handed did match the version it was given...
        self.assertEqual(seen, [b"original\n"])
        self.assertEqual(decision.source_version, expected)
        # ...and what happens after that is outside this adapter's promise.
        self.assertEqual(target.read_bytes(), b"changed-after-the-check\n")

    def test_a_revoked_mapping_denies_before_any_target_resolution(self) -> None:
        self.write_document("reports/quarterly.pdf", b"openable\n")
        registry = self.registry(self.nas_mapping(revoked=True))
        opener = RecordingOpener()

        with mock.patch.object(source_access_nas, "_nas_target") as resolved:
            decision = self.open_document(registry, "od-nas-0001", opener=opener)
        self.assertEqual(decision.status, "revoked")
        self.assertEqual(decision.code, "mapping_revoked")
        self.assertEqual(resolved.call_count, 0)
        self.assertEqual(opener.calls, [])


if __name__ == "__main__":  # pragma: no cover
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
