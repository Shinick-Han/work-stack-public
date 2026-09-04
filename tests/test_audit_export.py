from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("audit_export", ROOT / "scripts" / "audit_export.py")
assert SPEC and SPEC.loader
AUDIT_EXPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT_EXPORT)


class ExportAuditTest(unittest.TestCase):
    def test_current_source_tree_passes(self):
        self.assertEqual(AUDIT_EXPORT.audit(ROOT, []), [])

    def test_only_the_exact_frozen_safety_fixture_is_exempt(self):
        frozen = (
            ROOT
            / "contracts"
            / "workstack-conduit-v1"
            / "safety"
            / "snapshot-v1-safety-cases.json"
        ).read_bytes()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "workstack").mkdir()
            (root / "frontend" / "src").mkdir(parents=True)
            target = (
                root
                / "contracts"
                / "workstack-conduit-v1"
                / "safety"
                / "snapshot-v1-safety-cases.json"
            )
            target.parent.mkdir(parents=True)
            target.write_bytes(frozen)
            self.assertEqual(AUDIT_EXPORT.audit(root, []), [])

            target.write_bytes(frozen + b"\n")
            findings = AUDIT_EXPORT.audit(root, [])
            self.assertTrue(any("snapshot-v1-safety-cases.json" in item for item in findings))

    def test_negative_fixtures_are_exempt_but_positive_fixture_is_not(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "workstack").mkdir()
            (root / "frontend" / "src").mkdir(parents=True)
            (root / "contracts").mkdir()
            (root / "workstack" / "app.py").write_text("print('safe')\n", encoding="utf-8")
            negative = {
                "summary": "Contact synthetic@example.invalid",
                "detail": "ATTACHMENT_CANARY_DO_NOT_STORE",
            }
            (root / "contracts" / "capture-packet-v1.negative-raw.json").write_text(
                json.dumps(negative), encoding="utf-8"
            )
            self.assertEqual(AUDIT_EXPORT.audit(root, []), [])

            (root / "contracts" / "positive.json").write_text(
                json.dumps({"summary": "Contact leaked@example.invalid"}), encoding="utf-8"
            )
            findings = AUDIT_EXPORT.audit(root, [])
            self.assertTrue(any("positive.json: email address" in item for item in findings))

    def test_generated_dependency_and_build_directories_are_excluded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "workstack").mkdir()
            (root / "frontend" / "src").mkdir(parents=True)
            (root / "frontend" / "node_modules").mkdir()
            (root / "frontend" / "dist").mkdir()
            (root / "workstack" / "app.py").write_text("print('safe')\n", encoding="utf-8")
            (root / "frontend" / "node_modules" / "dependency.js").write_text(
                "const value = 'leaked@example.invalid'\n", encoding="utf-8"
            )
            (root / "frontend" / "dist" / "bundle.js").write_text(
                "const value = 'leaked@example.invalid'\n", encoding="utf-8"
            )
            self.assertEqual(AUDIT_EXPORT.audit(root, []), [])

    def test_package_lock_allows_public_dependency_contact_metadata_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "workstack").mkdir()
            (root / "frontend" / "src").mkdir(parents=True)
            (root / "frontend" / "package-lock.json").write_text(
                json.dumps({"packages": {"node_modules/example": {"deprecated": "contact maintainer@example.invalid"}}}),
                encoding="utf-8",
            )

            self.assertEqual(AUDIT_EXPORT.audit(root, []), [])

            (root / "frontend" / "package-lock.json").write_text(
                json.dumps({"packages": {"node_modules/example": {"token": "access_token=abcdefghijklmnop"}}}),
                encoding="utf-8",
            )
            findings = AUDIT_EXPORT.audit(root, [])
            self.assertTrue(any("credential" in finding for finding in findings))

    def test_tree_mode_catches_runtime_json_leakage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "captures.json").write_text(
                json.dumps({"captures": [{"summary": "From: a@example.invalid\nTo: b@example.invalid"}]}),
                encoding="utf-8",
            )
            findings = AUDIT_EXPORT.audit(root, [], mode="tree")
            self.assertTrue(any("email address" in item for item in findings))
            self.assertTrue(any("raw mail header block" in item for item in findings))

    def test_tree_mode_scans_generated_named_directories_and_windows_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            generated = root / "dist"
            generated.mkdir()
            windows_path = "C:" + "\\Users\\" + "realname\\" + "secret.json"
            (generated / "runtime.json").write_text(
                json.dumps({"source_path": windows_path}), encoding="utf-8"
            )
            findings = AUDIT_EXPORT.audit(root, [], mode="tree")
            self.assertTrue(any("dist" in item and "personal path" in item for item in findings))

    def test_reply_store_and_minimal_activity_pass_but_receipt_credentials_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reply = {
                "version": 1,
                "replies": [
                    {
                        "id": "R-0001",
                        "body": "Approved plain-text response.",
                        "target": {
                            "resource_type": "mail.message",
                            "connection_ref": "personal-outlook",
                            "container_ref": "mailbox:demo",
                            "object_ref": "message:opaque-source",
                            "version_ref": "change-key:opaque-v1",
                        },
                        "receipt": {
                            "remote_message_ref": "message:opaque-reply",
                            "web_url": "https://outlook.office.com/mail/deeplink/read/opaque",
                        },
                    }
                ],
            }
            activity = {
                "version": 1,
                "activity": [
                    {
                        "id": "E-000001",
                        "type": "reply.sent",
                        "reply_id": "R-0001",
                        "task_id": "T-0001",
                        "details": {"provider": "microsoft-outlook", "state": "sent"},
                    }
                ],
                "idempotency": [
                    {
                        "key": "reply.receipt.0001",
                        "response_ref": {"kind": "reply", "id": "R-0001"},
                    }
                ],
            }
            (root / "replies.json").write_text(json.dumps(reply), encoding="utf-8")
            (root / "activity.json").write_text(json.dumps(activity), encoding="utf-8")
            self.assertEqual(AUDIT_EXPORT.audit(root, [], mode="tree"), [])

            credential_key = "access_" + "token"
            reply["replies"][0]["receipt"][credential_key] = "synthetic-secret-value"
            (root / "replies.json").write_text(json.dumps(reply), encoding="utf-8")
            findings = AUDIT_EXPORT.audit(root, [], mode="tree")
            self.assertTrue(any("credential value" in item for item in findings))

            reply["replies"][0]["receipt"].pop(credential_key)
            reply["replies"][0]["receipt"]["web_url"] = (
                "https://outlook.office.com/mail/read?recipients=alice+bob"
            )
            (root / "replies.json").write_text(json.dumps(reply), encoding="utf-8")
            findings = AUDIT_EXPORT.audit(root, [], mode="tree")
            self.assertTrue(any("recipient assignment" in item for item in findings))

            (root / "captures.json").write_text(
                json.dumps(
                    {
                        "captures": [
                            {"source": {"connection_ref": "recipient:alice"}}
                        ]
                    }
                ),
                encoding="utf-8",
            )
            findings = AUDIT_EXPORT.audit(root, [], mode="tree")
            self.assertTrue(
                any("captures.json: recipient assignment" in item for item in findings)
            )

    def test_tree_mode_catches_url_and_retained_string_credential_material(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            access_name = "access" + "_token"
            credential_values = (
                "https://outlook.office.com/mail/read?{}={}".format(
                    access_name, "synthetic-credential-value"
                ),
                "https://outlook.office.com/mail/read#{}={}".format(
                    access_name, "synthetic-credential-value"
                ),
                "Bearer " + "a" * 32,
                "Bearer%20" + "a" * 32,
                "access%5Ftoken%3D" + "a" * 20,
                (
                    "https://outlook.office.com/mail/read%253F"
                    "access%255Ftoken%253D" + "a" * 20
                ),
                ".".join(("eyJ" + "b" * 12, "c" * 16, "d" * 16)),
            )
            capture_path = root / "captures.json"
            for value in credential_values:
                with self.subTest(value=value):
                    capture_path.write_text(
                        json.dumps({"captures": [{"retained": value}]}),
                        encoding="utf-8",
                    )
                    findings = AUDIT_EXPORT.audit(root, [], mode="tree")
                    self.assertTrue(any("credential material" in item for item in findings))

            for total_layers in (6, 11):
                with self.subTest(total_layers=total_layers):
                    value = "access%5Ftoken%3D" + "a" * 20
                    for _ in range(total_layers - 1):
                        value = value.replace("%", "%25")
                    capture_path.write_text(
                        json.dumps({"captures": [{"retained": value}]}),
                        encoding="utf-8",
                    )
                    findings = AUDIT_EXPORT.audit(root, [], mode="tree")
                    self.assertTrue(
                        any("over-depth percent encoding" in item for item in findings)
                    )

            capture_path.write_text(
                json.dumps(
                    {
                        "captures": [
                            {
                                "source": {
                                    "web_url": (
                                        "https://outlook.office.com/mail/deeplink/read/opaque"
                                        "?ItemID=opaque&tenantId=opaque-tenant"
                                    )
                                }
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(AUDIT_EXPORT.audit(root, [], mode="tree"), [])

    def test_empty_product_lock_and_windows_lock_sentinel_are_allowed(self):
        for lock_value in (b"", b"\0"):
            with self.subTest(lock_value=lock_value), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / ".workstack.lock").write_bytes(lock_value)
                self.assertEqual(AUDIT_EXPORT.audit(root, [], mode="tree"), [])

    def test_product_lock_with_content_and_other_lock_are_rejected(self):
        for lock_value in (b"x", b"owner=unexpected"):
            with self.subTest(lock_value=lock_value), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / ".workstack.lock").write_bytes(lock_value)
                findings = AUDIT_EXPORT.audit(root, [], mode="tree")
                self.assertTrue(any("product lock contains data" in item for item in findings))

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "other.lock").write_bytes(b"")
            findings = AUDIT_EXPORT.audit(root, [], mode="tree")
            self.assertTrue(any("other.lock: unexpected file type" in item for item in findings))

    def test_product_lock_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            target.write_bytes(b"")
            lock = root / ".workstack.lock"
            try:
                lock.symlink_to(target)
            except OSError as error:
                self.skipTest("symbolic links are unavailable: {}".format(error))
            findings = AUDIT_EXPORT.audit(root, [], mode="tree")
            self.assertTrue(any(".workstack.lock: symbolic link" in item for item in findings))


if __name__ == "__main__":
    unittest.main()


APPROVED_FIXTURE = Path("tests/test_sse_event_delivery.py")
APPROVED_CLASS, APPROVED_METHOD = AUDIT_EXPORT.APPROVED_SOURCE_FIXTURES[APPROVED_FIXTURE][:2]
APPROVED_DIGEST = AUDIT_EXPORT.APPROVED_SOURCE_FIXTURES[APPROVED_FIXTURE][3]
BRAND_SVG = ROOT / "frontend" / "src" / "assets" / "WorkStack-Mark-Lime-v2.svg"
# This test file is itself part of the audited source, and the audit allows it
# only the "email address" and "raw-content canary" negative rules. A literal
# private-key banner here would therefore be a genuine finding against this very
# file, so the probe is derived from the scanner's own rule rather than spelled
# out. Nothing is concealed: the derivation is the rule itself.
PRIVATE_KEY_PROBE = AUDIT_EXPORT.PRIVATE_KEY_RE.pattern.replace("[A-Z ]*", "RSA ")


def approved_method_source() -> str:
    """The exact complete source segment the scanner's digest is bound to."""

    text = (ROOT / APPROVED_FIXTURE).read_text(encoding="utf-8")
    tree = ast.parse(text)
    owner = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == APPROVED_CLASS
    )
    method = next(
        node for node in ast.walk(owner)
        if isinstance(node, ast.FunctionDef) and node.name == APPROVED_METHOD
    )
    return ast.get_source_segment(text, method).replace("\r\n", "\n")


class SvgIsScannedTextTest(unittest.TestCase):
    """SVG is XML text, so every ordinary text rule applies to it."""

    def _root(self, temporary: str) -> Path:
        root = Path(temporary)
        (root / "workstack").mkdir()
        (root / "frontend" / "src" / "assets").mkdir(parents=True)
        return root

    def test_the_approved_brand_svg_passes_as_scanned_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self._root(temporary)
            target = root / "frontend" / "src" / "assets" / BRAND_SVG.name
            target.write_bytes(BRAND_SVG.read_bytes())
            self.assertEqual(AUDIT_EXPORT.audit(root, []), [])

    def test_a_poisoned_svg_fails_every_ordinary_text_rule(self):
        cases = {
            "email address": "<svg><desc>owner@example.invalid</desc></svg>\n",
            "personal path": "<svg><desc>C:\\Users\\someone\\Pictures\\</desc></svg>\n",
            "credential value": "<svg><desc>password = \"hunter2000\"</desc></svg>\n",
            "private key": "<svg><desc>{}</desc></svg>\n".format(PRIVATE_KEY_PROBE),
        }
        for label, body in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                root = self._root(temporary)
                (root / "frontend" / "src" / "assets" / "poisoned.svg").write_text(
                    body, encoding="utf-8"
                )
                findings = AUDIT_EXPORT.audit(root, [])
                self.assertIn("poisoned.svg: {}".format(label), "\n".join(findings))

    def test_a_deny_term_and_invalid_utf8_in_an_svg_are_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self._root(temporary)
            assets = root / "frontend" / "src" / "assets"
            (assets / "worded.svg").write_text(
                "<svg><desc>Roadmap</desc></svg>\n", encoding="utf-8"
            )
            findings = AUDIT_EXPORT.audit(root, ["roadmap"])
            self.assertTrue(any("worded.svg: prohibited term" in item for item in findings))

            (assets / "broken.svg").write_bytes(b"<svg>\xff\xfe</svg>\n")
            findings = AUDIT_EXPORT.audit(root, [])
            self.assertTrue(any("broken.svg: non-UTF-8 content" in item for item in findings))

    def test_an_svg_symbolic_link_is_still_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self._root(temporary)
            assets = root / "frontend" / "src" / "assets"
            target = assets / "real.svg"
            target.write_text("<svg></svg>\n", encoding="utf-8")
            link = assets / "linked.svg"
            try:
                link.symlink_to(target)
            except OSError as error:
                self.skipTest("symbolic links are unavailable: {}".format(error))
            findings = AUDIT_EXPORT.audit(root, [])
            self.assertTrue(any("linked.svg: symbolic link" in item for item in findings))

    def test_admitting_svg_grants_nothing_to_binary_assets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self._root(temporary)
            (root / "frontend" / "src" / "assets" / "icon.ico").write_bytes(b"\x00\x00\x01\x00")
            findings = AUDIT_EXPORT.audit(root, [])
            self.assertTrue(any("icon.ico: unexpected file type" in item for item in findings))


class ApprovedSourceFixtureTest(unittest.TestCase):
    """ONE reviewed synthetic fixture occurrence, under SOURCE policy only.

    Source mode audits the working repository, where this negative test must hold the
    poisoned value it proves never reaches the wire. Tree mode audits a prepared export
    of arbitrary bytes for publication, so it stays strict and refuses the very same
    path. The allowance is bound to the exact hashed method source and to the single
    match inside it; nothing else in the file, and no other file, inherits it.
    """

    # Every owner template below indents its OUTER blocks by a single space so the
    # method itself still starts at column four. get_source_segment drops only the
    # first line's indentation, so the complete segment - the def line plus a body
    # indented by eight - is then reproduced byte for byte and keeps the approved
    # digest. The only thing that varies is who lexically owns the method.
    DIRECT_OWNER = "class {cls}:\n    {segment}\n"
    NESTED_FOREIGN_CLASS = "class {cls}:\n class Foreign:\n    {segment}\n"
    LOCAL_FUNCTION = "class {cls}:\n def container(self):\n    {segment}\n"
    RELOCATED_CLASS = "class Outer:\n class {cls}:\n    {segment}\n"

    def _build(self, temporary, *, name=None, body=None, prefix="", suffix="",
               template=None):
        root = Path(temporary)
        (root / "workstack").mkdir()
        (root / "frontend" / "src").mkdir(parents=True)
        (root / "tests").mkdir()
        segment = approved_method_source() if body is None else body
        owner = (template or self.DIRECT_OWNER).format(
            cls=APPROVED_CLASS, segment=segment
        )
        text = "{}{}{}".format(prefix, owner, suffix)
        (root / (name or APPROVED_FIXTURE.as_posix())).write_text(text, encoding="utf-8")
        return root

    def _assert_witness_is_ownership(self, root: Path) -> None:
        """The fixture must be valid Python whose segment still hashes as approved.

        Otherwise a refusal would prove nothing about ownership - it would only be
        the parse guard or the digest guard doing its ordinary job.
        """

        text = (root / APPROVED_FIXTURE).read_text(encoding="utf-8")
        tree = ast.parse(text)
        methods = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == APPROVED_METHOD
        ]
        self.assertEqual(len(methods), 1)
        segment = ast.get_source_segment(text, methods[0])
        self.assertEqual(segment, approved_method_source())
        self.assertEqual(
            hashlib.sha256(segment.encode("utf-8")).hexdigest(), APPROVED_DIGEST
        )

    def _findings(self, root: Path, mode: str = "source", denied=()) -> str:
        return "\n".join(AUDIT_EXPORT.audit(root, list(denied), mode))

    def test_the_exact_approved_fixture_passes_in_source_and_auto_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self._build(temporary)
            self.assertEqual(AUDIT_EXPORT.audit(root, [], "source"), [])
            self.assertEqual(AUDIT_EXPORT.audit(root, []), [])

    def test_tree_mode_refuses_even_the_exact_approved_fixture(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self._build(temporary)
            self.assertIn("personal path", self._findings(root, "tree"))

    def test_a_changed_literal_or_changed_function_loses_the_allowance(self):
        original = approved_method_source()
        cases = {
            "changed literal": original.replace("someone", "another-person"),
            "changed function": original + "\n        self.assertTrue(True)",
        }
        for label, body in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                root = self._build(temporary, body=body)
                self.assertIn("personal path", self._findings(root))

    def test_the_same_literal_elsewhere_in_the_same_file_still_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self._build(
                temporary, suffix="\n\nELSEWHERE = r\"C:\\Users\\someone\\WorkStack\"\n"
            )
            self.assertIn("personal path", self._findings(root))

    def test_a_duplicated_class_or_method_is_never_the_authorized_occurrence(self):
        original = approved_method_source()
        with tempfile.TemporaryDirectory() as temporary:
            root = self._build(
                temporary, suffix="\n\nclass {}:\n    pass\n".format(APPROVED_CLASS)
            )
            self.assertIn("personal path", self._findings(root))
        with tempfile.TemporaryDirectory() as temporary:
            root = self._build(temporary, body=original + "\n\n" + original)
            self.assertIn("personal path", self._findings(root))

    def test_another_filename_and_unparsable_python_lose_the_allowance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self._build(temporary, name="tests/test_other_delivery.py")
            self.assertIn("personal path", self._findings(root))
        with tempfile.TemporaryDirectory() as temporary:
            root = self._build(temporary, suffix="\n\ndef broken(:\n")
            self.assertIn("personal path", self._findings(root))

    def test_a_preceding_multibyte_character_neither_shifts_nor_widens_the_span(self):
        # col_offset counts UTF-8 bytes, not characters. The approved extent must
        # still be located exactly, and must not stretch far enough to swallow a
        # second occurrence that follows it.
        multibyte = "# \u00e9\u00e9\u00e9 \u4f5c\u696d\n\n"
        with tempfile.TemporaryDirectory() as temporary:
            root = self._build(temporary, prefix=multibyte)
            self.assertEqual(AUDIT_EXPORT.audit(root, [], "source"), [])
        with tempfile.TemporaryDirectory() as temporary:
            root = self._build(
                temporary,
                prefix=multibyte,
                suffix="\n\nELSEWHERE = r\"C:\\Users\\someone\\WorkStack\"\n",
            )
            self.assertIn("personal path", self._findings(root))

    # -- lexical ownership -------------------------------------------------
    def test_the_approved_bytes_under_an_unapproved_owner_are_refused(self):
        """Approval is bound to an owner, not merely to bytes that appear below one.

        Each fixture holds the approved method verbatim - same digest, valid Python -
        and differs only in who owns it: a Foreign class nested in the approved class,
        a local function inside it, or the approved class itself relocated under
        another owner. A descendant search would accept all three.
        """

        cases = {
            "nested Foreign class": self.NESTED_FOREIGN_CLASS,
            "local function container": self.LOCAL_FUNCTION,
            "approved class relocated under another owner": self.RELOCATED_CLASS,
        }
        for label, template in cases.items():
            with self.subTest(owner=label), tempfile.TemporaryDirectory() as temporary:
                root = self._build(temporary, template=template)
                self._assert_witness_is_ownership(root)
                self.assertIn("personal path", self._findings(root))

    def test_the_direct_owner_at_module_scope_keeps_the_exception(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self._build(temporary, template=self.DIRECT_OWNER)
            self._assert_witness_is_ownership(root)
            self.assertEqual(AUDIT_EXPORT.audit(root, [], "source"), [])
            self.assertEqual(AUDIT_EXPORT.audit(root, []), [])

    def test_other_rules_and_deny_terms_still_scan_the_approved_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self._build(temporary)
            # "journal" occurs only inside the approved extent.
            self.assertIn("prohibited term", self._findings(root, "source", ["journal"]))
        for label, extra in (
            ("credential value", "\n\nAPI_KEY = \"abcdefghijkl\"\n"),
            ("private key", '\n\nPEM = "{}"\n'.format(PRIVATE_KEY_PROBE)),
            ("email address", "\n\nCONTACT = \"owner@example.invalid\"\n"),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                root = self._build(temporary, suffix=extra)
                findings = self._findings(root)
                self.assertIn(label, findings)
                self.assertNotIn("personal path", findings)


APPROVED_ICO = Path("desktop/python-webview-shell/assets/WorkStack-Mark-Lime-v2.ico")
APPROVED_ICO_SHA = AUDIT_EXPORT.APPROVED_SOURCE_BINARIES[APPROVED_ICO]
ORACLE_MANIFEST = Path("quality/agent-p0-oracle/manifest.v1.json")
PLACEHOLDERS = AUDIT_EXPORT.APPROVED_STRUCTURED_VALUES[ORACLE_MANIFEST]


class SourceRosterTest(unittest.TestCase):
    """Every newly covered root and file is really scanned, with a poisoned witness."""

    def _poison(self, root: Path, relative: str) -> None:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("contact leaked@example.invalid\n", encoding="utf-8")

    def _root(self, temporary: str) -> Path:
        root = Path(temporary)
        (root / "workstack").mkdir()
        (root / "frontend" / "src").mkdir(parents=True)
        return root

    def test_each_added_source_root_and_file_is_scanned(self):
        cases = [
            "desktop/host/notes.md",
            "integrations/service/notes.md",
            ".github/workflows/notes.yml",
            "quality/policy/notes.md",
            "theme/tokens/notes.md",
            "frontend/e2e/spec/notes.ts",
            ".coveragerc",
            ".gitattributes",
            "frontend/eslint.config.js",
            "frontend/playwright.compat.config.ts",
            "frontend/playwright.config.ts",
        ]
        for relative in cases:
            with self.subTest(path=relative), tempfile.TemporaryDirectory() as temporary:
                root = self._root(temporary)
                self._poison(root, relative)
                findings = AUDIT_EXPORT.audit(root, [], "source")
                self.assertTrue(
                    any(Path(relative).name in item for item in findings),
                    "{} must be scanned: {}".format(relative, findings),
                )

    def test_generated_and_dependency_exclusions_still_hold(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self._root(temporary)
            # A healthy scanned file keeps the run from being empty, so the
            # exclusions are proven rather than the "nothing to audit" sentinel.
            (root / "workstack" / "app.py").write_text(
                "print('safe')\n", encoding="utf-8"
            )
            for relative in (
                "desktop/node_modules/dep.js", "quality/dist/out.js",
                "theme/build/out.css", "integrations/__pycache__/x.py",
            ):
                self._poison(root, relative)
            self.assertEqual(AUDIT_EXPORT.audit(root, [], "source"), [])

    def test_cs_is_ordinary_scanned_source_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self._root(temporary)
            (root / "desktop").mkdir()
            (root / "desktop" / "Host.cs").write_text(
                "class Host { const string P = \"x\"; }\n// owner@example.invalid\n",
                encoding="utf-8",
            )
            findings = AUDIT_EXPORT.audit(root, [], "source")
            self.assertTrue(any("Host.cs: email address" in item for item in findings))
            (root / "desktop" / "Host.cs").write_text(
                "class Host { }\n// Roadmap\n", encoding="utf-8"
            )
            self.assertEqual(AUDIT_EXPORT.audit(root, [], "source"), [])
            self.assertTrue(any(
                "Host.cs: prohibited term" in item
                for item in AUDIT_EXPORT.audit(root, ["roadmap"], "source")
            ))


class JsonLinesTest(unittest.TestCase):
    """Text rules on the whole file, plus one JSON value per nonblank line."""

    def _measure(self, body: str, denied=()) -> list:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "workstack").mkdir()
            (root / "frontend" / "src").mkdir(parents=True)
            (root / "data").mkdir()
            (root / "data" / "golden.jsonl").write_text(body, encoding="utf-8")
            return AUDIT_EXPORT.audit(root, list(denied), "source")

    def test_valid_lines_and_blank_separators_pass(self):
        body = '{"a": 1}\n\n{"b": "plain"}\n\n'
        self.assertEqual(self._measure(body), [])

    def test_a_malformed_nonblank_line_is_refused_with_its_line_number(self):
        body = '{"a": 1}\n{"b": \n{"c": 3}\n'
        findings = self._measure(body)
        self.assertTrue(any("line 2: invalid JSON" in item for item in findings), findings)

    def test_structured_rules_reach_nested_and_encoded_line_values(self):
        body = (
            '{"a": 1}\n'
            '{"outer": {"inner": ["contact leaked@example.invalid"]}}\n'
            '{"u": "%2563ontact%2520leaked%2540example.invalid"}\n'
        )
        findings = self._measure(body)
        self.assertTrue(any("line 2" in item for item in findings), findings)
        self.assertTrue(any("line 3" in item for item in findings), findings)

    def test_raw_text_rules_and_deny_terms_scan_the_whole_jsonl_file(self):
        body = '{"a": 1}\n'
        self.assertTrue(any(
            "golden.jsonl: prohibited term" in item for item in self._measure(body, ["a"])
        ))
        findings = self._measure('{"path": "C:\\\\Users\\\\someone\\\\x"}\n')
        self.assertTrue(any("personal path" in item for item in findings), findings)


class ApprovedBinaryTest(unittest.TestCase):
    """Exactly one known binary, by exact path and exact identity, source only."""

    ICO = (ROOT / APPROVED_ICO).read_bytes()

    def _root(self, temporary: str, relative: Path, data: bytes) -> Path:
        root = Path(temporary)
        (root / "workstack").mkdir()
        (root / "frontend" / "src").mkdir(parents=True)
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return root

    def test_the_exact_known_icon_is_admitted_and_counted_apart(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self._root(temporary, APPROVED_ICO, self.ICO)
            self.assertEqual(
                hashlib.sha256(self.ICO).hexdigest(), APPROVED_ICO_SHA
            )
            self.assertEqual(AUDIT_EXPORT.audit(root, [], "source"), [])
            scanned, approved = AUDIT_EXPORT.census(root, "source")
            self.assertEqual(approved, 1)
            self.assertNotIn(
                APPROVED_ICO.name,
                "".join(str(p) for p in [scanned]),
            )

    def test_changed_appended_malformed_or_relocated_icons_refuse(self):
        cases = {
            "changed bytes": (
                APPROVED_ICO, self.ICO[:-1] + bytes([self.ICO[-1] ^ 0xFF])
            ),
            "appended payload": (APPROVED_ICO, self.ICO + b"leaked"),
            "malformed replacement": (APPROVED_ICO, b"not an icon"),
            "wrong path": (
                Path("desktop/python-webview-shell/assets/Other.ico"), self.ICO
            ),
            "another binary suffix": (
                Path("desktop/python-webview-shell/assets/logo.bin"), self.ICO
            ),
        }
        for label, (relative, data) in cases.items():
            with self.subTest(case=label), tempfile.TemporaryDirectory() as temporary:
                root = self._root(temporary, relative, data)
                findings = AUDIT_EXPORT.audit(root, [], "source")
                self.assertTrue(findings, "{} must refuse".format(label))

    def test_tree_mode_still_rejects_the_known_icon(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self._root(temporary, APPROVED_ICO, self.ICO)
            findings = AUDIT_EXPORT.audit(root, [], "tree")
            self.assertTrue(any("unexpected file type" in item for item in findings))

    def test_an_icon_symbolic_link_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self._root(temporary, APPROVED_ICO, self.ICO)
            real = root / APPROVED_ICO
            link = real.with_name("linked.ico")
            try:
                link.symlink_to(real)
            except OSError as error:
                self.skipTest("symbolic links are unavailable: {}".format(error))
            findings = AUDIT_EXPORT.audit(root, [], "source")
            self.assertTrue(any("linked.ico: symbolic link" in item for item in findings))


class StructuredPlaceholderTest(unittest.TestCase):
    """Two exact literal command placeholders, and every way to lose them."""

    def _document(self, values: dict) -> dict:
        """Build the genuine nested document the coordinates actually describe."""

        document = {}
        for coordinate, (_label, value) in values.items():
            names = [step for kind, step in coordinate if kind == "key"]
            self.assertEqual(len(names), len(coordinate), "object members only")
            node = document
            for name in names[:-1]:
                node = node.setdefault(name, {})
            node[names[-1]] = value
        return document

    def _approved(self, index: int) -> tuple:
        """One approved (coordinate, value) pair, in table order."""

        coordinate = list(PLACEHOLDERS)[index]
        return coordinate, PLACEHOLDERS[coordinate][1]

    def _collapse(self, coordinate: tuple, keep: int, value: str) -> dict:
        """The same value under a key whose NAME contains the remaining steps.

        Valid JSON with distinct keys: the parent hierarchy is genuinely
        different, it is not another spelling of the approved one.
        """

        names = [step for kind, step in coordinate if kind == "key"]
        document = {}
        node = document
        for name in names[:keep]:
            node = node.setdefault(name, {})
        node[".".join(names[keep:])] = value
        return document

    def _measure(self, body: str, relative: Path = ORACLE_MANIFEST,
                 mode: str = "source", denied=()) -> list:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "workstack").mkdir()
            (root / "frontend" / "src").mkdir(parents=True)
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
            return AUDIT_EXPORT.audit(root, list(denied), mode)

    def test_the_two_exact_placeholders_are_permitted(self):
        body = json.dumps(self._document(PLACEHOLDERS))
        self.assertEqual(self._measure(body), [])

    def test_every_losing_condition_refuses(self):
        document = self._document(PLACEHOLDERS)
        _recipe_coordinate, recipe = self._approved(0)
        moved = {"elsewhere": document["envelope"]}
        changed = json.loads(json.dumps(document))
        changed["digest_recipes"]["candidate_diff"] += " <b>x</b>"
        cases = {
            "moved structural path": json.dumps(moved),
            "changed value": json.dumps(changed),
            "another file": None,
            "duplicate key ambiguity": (
                '{"digest_recipes": {"candidate_diff": %s, "candidate_diff": %s}}'
                % (json.dumps(recipe), json.dumps(recipe))
            ),
        }
        for label, body in cases.items():
            with self.subTest(case=label):
                if label == "another file":
                    findings = self._measure(
                        json.dumps(document), Path("quality/other.json")
                    )
                else:
                    findings = self._measure(body)
                self.assertTrue(
                    any("HTML source content" in item for item in findings),
                    "{}: {}".format(label, findings),
                )

    # -- ECP-F1: a rendered path is not an identity ------------------------
    def test_a_dotted_key_cannot_impersonate_the_approved_structure(self):
        """The allowance is bound to structure, not to how a path prints.

        Each fixture is valid JSON with distinct keys carrying the approved
        original value, but reached through a genuinely different hierarchy: the
        whole path collapsed into one flat key, or collapsed after the first few
        real steps. All render to the approved diagnostic string and all must
        still be refused.
        """

        for index in (0, 1):
            coordinate, value = self._approved(index)
            depth = len(coordinate)
            # Collapsing needs at least two remaining steps to fold into one
            # key; keeping depth - 1 of them would just rebuild the genuine
            # structure, which is legitimately permitted.
            for keep in range(depth - 1):
                label = "coordinate {} collapsed after {} real steps".format(
                    index, keep
                )
                with self.subTest(case=label):
                    document = self._collapse(coordinate, keep, value)
                    self.assertEqual(
                        json.loads(json.dumps(document)), document, "valid JSON"
                    )
                    findings = self._measure(json.dumps(document))
                    self.assertTrue(
                        any("HTML source content" in item for item in findings),
                        "{}: {}".format(label, findings),
                    )

    def test_an_array_step_is_distinct_from_an_object_member(self):
        coordinate, value = self._approved(0)
        names = [step for kind, step in coordinate if kind == "key"]
        cases = {
            "a key literally named like an array element": {
                names[0]: {names[1] + "[0]": value}
            },
            "the approved name inside a real array": {names[0]: [{names[1]: value}]},
        }
        for label, document in cases.items():
            with self.subTest(case=label):
                findings = self._measure(json.dumps(document))
                self.assertTrue(
                    any("HTML source content" in item for item in findings),
                    "{}: {}".format(label, findings),
                )

    def test_ordinary_dotted_keys_are_still_scanned_and_still_readable(self):
        # Dotted keys are legitimate JSON; they are neither denied nor exempted.
        poisoned = json.dumps({"some.dotted.key": "contact leaked@example.invalid"})
        findings = self._measure(poisoned)
        self.assertTrue(
            any("email address at $.some.dotted.key" in item for item in findings),
            findings,
        )
        benign = json.dumps({"some.dotted.key": "an ordinary value", "a.b": 1})
        self.assertEqual(self._measure(benign), [])

    def test_the_genuine_nested_structures_are_still_permitted(self):
        document = self._document(PLACEHOLDERS)
        self.assertEqual(document["digest_recipes"]["candidate_diff"],
                         self._approved(0)[1])
        self.assertEqual(self._measure(json.dumps(document)), [])

    def test_tree_mode_and_other_rules_still_refuse_inside_the_manifest(self):
        document = self._document(PLACEHOLDERS)
        self.assertTrue(any(
            "HTML source content" in item
            for item in self._measure(json.dumps(document), mode="tree")
        ))
        poisoned = json.loads(json.dumps(document))
        poisoned["contact"] = "leaked@example.invalid"
        self.assertTrue(any(
            "email address" in item for item in self._measure(json.dumps(poisoned))
        ))
        self.assertTrue(any(
            "prohibited term" in item
            for item in self._measure(json.dumps(document), denied=["canonical JSON"])
        ))
