from __future__ import annotations

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
