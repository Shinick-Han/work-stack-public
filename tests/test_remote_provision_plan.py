from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
CLI = ROOT / "scripts" / "remote_provision_plan.py"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SHELL) not in sys.path:
    sys.path.insert(0, str(SHELL))
MODULE_PATH = SHELL / "remote_provision_plan.py"
SPEC = importlib.util.spec_from_file_location("remote_provision_plan_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
OTHER_WORKSPACE_ID = "22222222-2222-4222-8222-222222222222"
ARTIFACT_DIGEST = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
OTHER_DIGEST = "sha256:fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210"


def valid_document() -> dict[str, object]:
    return {
        "schema_version": 1,
        "target": {
            "os": "linux",
            "install_root": "/workstack-fixture/alice/.local/share/work-stack/app",
            "data_root": "/workstack-fixture/alice/.local/share/work-stack/data",
            "owner": "alice",
            "expected_workspace_id": WORKSPACE_ID,
        },
        "artifact": {
            "product_version": MODULE.__version__,
            "protocol_version": MODULE.REMOTE_PROTOCOL_VERSION,
            "digest": ARTIFACT_DIGEST,
        },
        "facts": {
            "os": "linux",
            "python": {
                "path": "/usr/bin/python3",
                "version": "3.12.10",
            },
            "install": {
                "exists": False,
                "owner": None,
                "symlink": False,
                "product_version": None,
                "protocol_version": None,
                "digest": None,
            },
            "data": {
                "exists": False,
                "owner": None,
                "symlink": False,
                "workspace_id": None,
            },
        },
    }


def encoded(document: dict[str, object]) -> bytes:
    return json.dumps(document, separators=(",", ":")).encode("utf-8")


def huge_schema_version_document() -> bytes:
    payload = encoded(valid_document()).replace(
        b'"schema_version":1',
        b'"schema_version":' + (b"5" * 5000),
        1,
    )
    assert len(payload) <= MODULE.MAX_DOCUMENT_BYTES
    return payload


def codes(document: dict[str, object]) -> set[str]:
    return {item["code"] for item in document["diagnostics"]}


def assert_plan_only_metadata(test: unittest.TestCase, document: dict[str, object]) -> None:
    test.assertEqual(document["mode"], "plan_only")
    test.assertEqual(document["verification"], "pending_live_probe")
    test.assertEqual(
        document["provenance"],
        {
            "facts": "supplied",
            "manifest": "supplied",
            "observed": "unverified",
        },
    )
    test.assertNotEqual(document["verification"], "verified")
    for item in document["diagnostics"]:
        detail = str(item["detail"]).lower()
        test.assertNotIn("verified remotely", detail)
        test.assertNotIn("live probe succeeded", detail)
        test.assertNotIn("observed on the remote", detail)


def present_install(document: dict[str, object], digest: str | None = ARTIFACT_DIGEST) -> None:
    install = document["facts"]["install"]
    install["exists"] = True
    install["owner"] = "alice"
    install["symlink"] = False
    install["product_version"] = MODULE.__version__
    install["protocol_version"] = MODULE.REMOTE_PROTOCOL_VERSION
    install["digest"] = digest


def tree_hashes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class RemoteProvisionPlanParseTest(unittest.TestCase):
    def assert_invalid(self, raw: bytes | str) -> str:
        with self.assertRaises(MODULE.PlanError) as raised:
            MODULE.plan_remote_provision(raw)
        self.assertEqual(raised.exception.code, "INVALID_DOCUMENT")
        self.assertLessEqual(len(raised.exception.detail), MODULE.MAX_DETAIL_LENGTH)
        self.assertLessEqual(len(raised.exception.code), MODULE.MAX_CODE_LENGTH)
        return raised.exception.detail

    def test_python_dict_entry_is_rejected(self) -> None:
        with self.assertRaises(MODULE.PlanError) as raised:
            MODULE.plan_remote_provision(valid_document())  # type: ignore[arg-type]
        self.assertEqual(raised.exception.code, "INVALID_DOCUMENT")
        self.assertNotIn("alice", raised.exception.detail)

    def test_bytearray_entry_is_rejected(self) -> None:
        with self.assertRaises(MODULE.PlanError) as raised:
            MODULE.plan_remote_provision(bytearray(encoded(valid_document())))  # type: ignore[arg-type]
        self.assertEqual(raised.exception.code, "INVALID_DOCUMENT")

    def test_utf8_string_json_is_accepted(self) -> None:
        text = json.dumps(valid_document(), separators=(",", ":"))
        result = MODULE.plan_remote_provision(text)
        self.assertEqual(result["decision"], "install_needed")

    def test_nonstring_keys_do_not_typeerror(self) -> None:
        with self.assertRaises(MODULE.PlanError) as raised:
            MODULE._exact_keys({1: "linux"}, MODULE.TARGET_KEYS, "target")  # type: ignore[dict-item]
        self.assertEqual(raised.exception.code, "INVALID_DOCUMENT")
        self.assertIn("keys must be strings", raised.exception.detail)

    def test_unknown_top_level_key_is_rejected(self) -> None:
        document = valid_document()
        document["secrets"] = {"token": "nope"}
        detail = self.assert_invalid(encoded(document))
        self.assertIn("unsupported fields", detail)
        self.assertNotIn("secrets", detail)
        self.assertNotIn("token", detail)
        self.assertNotIn("nope", detail)

    def test_unknown_nested_keys_are_rejected(self) -> None:
        cases = (
            ("target", "ssh_host_alias", "work-linux"),
            ("artifact", "url", "https://example.invalid/app.tgz"),
            ("facts", "environment", {"PATH": "/usr/bin"}),
        )
        for section, key, value in cases:
            with self.subTest(section=section, key=key):
                document = valid_document()
                document[section][key] = value
                detail = self.assert_invalid(encoded(document))
                self.assertNotIn(key, detail)
                if isinstance(value, str):
                    self.assertNotIn(value, detail)

    def test_invalid_utf8_is_structured_refusal(self) -> None:
        detail = self.assert_invalid(b"\xff\xfe{}")
        self.assertEqual(detail, "request is not UTF-8")

    def test_deeply_nested_json_is_structured_refusal(self) -> None:
        depth = 1200
        payload = (b'{"a":' * depth) + b"1" + (b"}" * depth)
        self.assertLessEqual(len(payload), MODULE.MAX_DOCUMENT_BYTES)
        with self.assertRaises(MODULE.PlanError) as raised:
            MODULE.plan_remote_provision(payload)
        self.assertEqual(raised.exception.code, "INVALID_DOCUMENT")
        self.assertLessEqual(len(raised.exception.detail), MODULE.MAX_DETAIL_LENGTH)
        self.assertNotIn("RecursionError", raised.exception.detail)

    def test_oversize_integer_is_structured_refusal(self) -> None:
        with self.assertRaises(MODULE.PlanError) as raised:
            MODULE.plan_remote_provision(huge_schema_version_document())
        self.assertEqual(raised.exception.code, "INVALID_DOCUMENT")
        self.assertLessEqual(len(raised.exception.detail), MODULE.MAX_DETAIL_LENGTH)
        self.assertNotIn("ValueError", raised.exception.detail)
        self.assertNotIn("5" * 32, raised.exception.detail)

    def test_python_compatible_flag_is_an_unknown_key(self) -> None:
        document = valid_document()
        document["facts"]["python"]["compatible"] = True
        self.assert_invalid(encoded(document))

    def test_duplicate_keys_are_rejected(self) -> None:
        payload = encoded(valid_document()).replace(
            b'"os":"linux"',
            b'"os":"linux","os":"linux"',
            1,
        )
        self.assertIn("duplicate JSON key", self.assert_invalid(payload))

    def test_nan_and_infinity_are_rejected(self) -> None:
        for constant in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(constant=constant):
                payload = encoded(valid_document()).replace(
                    b'"protocol_version":1',
                    f'"protocol_version":{constant}'.encode("ascii"),
                    1,
                )
                self.assert_invalid(payload)

    def test_boolean_protocol_version_is_rejected(self) -> None:
        document = valid_document()
        document["artifact"]["protocol_version"] = True
        self.assert_invalid(encoded(document))

    def test_oversize_document_is_rejected(self) -> None:
        payload = encoded(valid_document())
        padded = payload + (b" " * (MODULE.MAX_DOCUMENT_BYTES - len(payload) + 1))
        self.assertIn("inspect/plan bound", self.assert_invalid(padded))

    def test_empty_document_is_rejected(self) -> None:
        self.assert_invalid(b"")

    def test_product_version_bound_is_enforced(self) -> None:
        document = valid_document()
        document["artifact"]["product_version"] = "1" * (MODULE.MAX_PRODUCT_VERSION_LENGTH + 1)
        self.assertIn("out of bounds", self.assert_invalid(encoded(document)))

    def test_protocol_version_bound_is_enforced(self) -> None:
        document = valid_document()
        document["artifact"]["protocol_version"] = MODULE.MAX_PROTOCOL_VERSION + 1
        self.assert_invalid(encoded(document))

    def test_nil_and_noncanonical_workspace_ids_are_rejected(self) -> None:
        document = valid_document()
        document["target"]["expected_workspace_id"] = "00000000-0000-0000-0000-000000000000"
        self.assert_invalid(encoded(document))
        document = valid_document()
        document["target"]["expected_workspace_id"] = "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"
        self.assert_invalid(encoded(document))

    def test_path_injection_candidates_are_rejected(self) -> None:
        paths = (
            "/tmp/x;rm -rf /",
            "/tmp/$(whoami)",
            "/tmp/`id`",
            "/workstack-fixture/alice/../alice/app",
            "/workstack-fixture/alice/.local/share/work-stack/app/",
            "workstack-fixture/alice/app",
            "D:/workstack-fixture/alice/app",
            "/workstack-fixture/alice/app && reboot",
            "//workstack-fixture/alice/app",
            "/",
        )
        for value in paths:
            with self.subTest(path=value):
                document = valid_document()
                document["target"]["install_root"] = value
                detail = self.assert_invalid(encoded(document))
                self.assertNotIn(value, detail)
                self.assertLessEqual(len(detail), MODULE.MAX_DETAIL_LENGTH)

    def test_app_and_data_paths_must_be_separate(self) -> None:
        same = valid_document()
        same["target"]["data_root"] = same["target"]["install_root"]
        self.assertIn("separate", self.assert_invalid(encoded(same)))
        nested = valid_document()
        nested["target"]["data_root"] = nested["target"]["install_root"] + "/state"
        self.assertIn("separate", self.assert_invalid(encoded(nested)))

    def test_missing_install_cannot_carry_identity(self) -> None:
        document = valid_document()
        document["facts"]["install"]["digest"] = ARTIFACT_DIGEST
        self.assert_invalid(encoded(document))

    def test_missing_path_cannot_be_a_symlink(self) -> None:
        document = valid_document()
        document["facts"]["install"]["symlink"] = True
        self.assert_invalid(encoded(document))

    def test_filesystem_inventory_keys_are_rejected(self) -> None:
        document = valid_document()
        document["facts"]["install"]["entries"] = ["/workstack-fixture/alice/.ssh/id_rsa"]
        self.assert_invalid(encoded(document))


class RemoteProvisionPlanDecisionTest(unittest.TestCase):
    def plan(self, document: dict[str, object] | None = None) -> dict[str, object]:
        payload = encoded(valid_document() if document is None else document)
        result = MODULE.plan_remote_provision(payload)
        assert_plan_only_metadata(self, result)
        return result

    def test_missing_python_is_a_prerequisite(self) -> None:
        document = valid_document()
        document["facts"]["python"] = None
        result = self.plan(document)
        self.assertEqual(result["decision"], "prerequisite_missing")
        self.assertEqual(result["plan"]["action"], "none")
        self.assertIn(MODULE.REMOTE_PYTHON_REQUIRED, codes(result))

    def test_python_floor_follows_service_contract_not_windows_bundle(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("Python 3.10 or newer for the local service and CLI", readme)
        self.assertEqual(MODULE.MIN_PYTHON, (3, 10))
        self.assertNotEqual(MODULE.MIN_PYTHON, (3, 12))

    def test_python_3_9_is_too_old_for_the_service_contract(self) -> None:
        document = valid_document()
        document["facts"]["python"]["version"] = "3.9.18"
        result = self.plan(document)
        self.assertEqual(result["decision"], "prerequisite_missing")
        self.assertEqual(result["plan"]["action"], "none")
        self.assertIn(MODULE.REMOTE_PYTHON_TOO_OLD, codes(result))

    def test_python_3_10_meets_the_remote_service_floor(self) -> None:
        document = valid_document()
        document["facts"]["python"]["version"] = "3.10.12"
        result = self.plan(document)
        self.assertEqual(result["decision"], "install_needed")
        self.assertIn("REMOTE_PYTHON_COMPATIBLE", codes(result))
        self.assertNotIn(MODULE.REMOTE_PYTHON_TOO_OLD, codes(result))

    def test_python_3_11_is_not_too_old_for_linux_remote(self) -> None:
        document = valid_document()
        document["facts"]["python"]["version"] = "3.11.9"
        result = self.plan(document)
        self.assertEqual(result["decision"], "install_needed")
        self.assertIn("REMOTE_PYTHON_COMPATIBLE", codes(result))

    def test_python_3_12_10_is_compatible(self) -> None:
        result = self.plan()
        self.assertIn("REMOTE_PYTHON_COMPATIBLE", codes(result))
        self.assertEqual(result["decision"], "install_needed")

    def test_artifact_version_mismatch_is_refused(self) -> None:
        document = valid_document()
        document["artifact"]["product_version"] = "0.0.0"
        result = self.plan(document)
        self.assertEqual(result["decision"], "refused")
        self.assertIn("REMOTE_APP_MISMATCH", codes(result))

    def test_artifact_protocol_mismatch_is_refused(self) -> None:
        document = valid_document()
        document["artifact"]["protocol_version"] = 0
        result = self.plan(document)
        self.assertEqual(result["decision"], "refused")
        self.assertIn("REMOTE_PROTOCOL_INVALID", codes(result))

    def test_artifact_digest_must_be_sha256(self) -> None:
        document = valid_document()
        document["artifact"]["digest"] = "md5:00"
        with self.assertRaises(MODULE.PlanError):
            self.plan(document)

    def test_workspace_mismatch_is_refused(self) -> None:
        document = valid_document()
        document["facts"]["data"] = {
            "exists": True,
            "owner": "alice",
            "symlink": False,
            "workspace_id": OTHER_WORKSPACE_ID,
        }
        result = self.plan(document)
        self.assertEqual(result["decision"], "refused")
        self.assertIn("REMOTE_WORKSPACE_MISMATCH", codes(result))

    def test_symlink_is_reparse_ambiguity(self) -> None:
        document = valid_document()
        present_install(document)
        document["facts"]["install"]["symlink"] = True
        result = self.plan(document)
        self.assertEqual(result["decision"], "refused")
        self.assertIn("TARGET_REPARSE_AMBIGUITY", codes(result))

    def test_owner_mismatch_is_refused(self) -> None:
        document = valid_document()
        present_install(document)
        document["facts"]["install"]["owner"] = "root"
        result = self.plan(document)
        self.assertEqual(result["decision"], "refused")
        self.assertIn("TARGET_OWNERSHIP_MISMATCH", codes(result))

    def test_missing_install_needs_install(self) -> None:
        result = self.plan()
        self.assertEqual(result["decision"], "install_needed")
        self.assertEqual(result["plan"]["action"], "install")
        self.assertIn("INSTALL_STATE_MISSING", codes(result))

    def test_unknown_existing_install_fields_refuse_ordinary_install(self) -> None:
        fields = ("product_version", "protocol_version", "digest")
        for field in fields:
            with self.subTest(unknown=field):
                document = valid_document()
                present_install(document)
                document["facts"]["install"][field] = None
                result = self.plan(document)
                self.assertEqual(result["decision"], "refused")
                self.assertEqual(result["plan"]["action"], "none")
                self.assertIn("INSTALL_STATE_UNKNOWN", codes(result))
                self.assertNotIn("INSTALL_STATE_PARTIAL", codes(result))

    def test_conflicting_existing_install_fields_refuse_ordinary_install(self) -> None:
        conflicts = (
            ("product_version", "0.0.1"),
            ("protocol_version", 0),
            ("digest", OTHER_DIGEST),
        )
        for field, value in conflicts:
            with self.subTest(conflict=field):
                document = valid_document()
                present_install(document)
                document["facts"]["install"][field] = value
                result = self.plan(document)
                self.assertEqual(result["decision"], "refused")
                self.assertEqual(result["plan"]["action"], "none")
                self.assertIn("INSTALL_CONTENT_CONFLICT", codes(result))
                self.assertNotEqual(result["plan"]["action"], "install")

    def test_current_install_is_noop(self) -> None:
        document = valid_document()
        present_install(document)
        result = self.plan(document)
        self.assertEqual(result["decision"], "current")
        self.assertEqual(result["plan"]["action"], "noop")
        self.assertIn("INSTALL_STATE_CURRENT", codes(result))
        self.assertEqual(result["verification"], "pending_live_probe")
        self.assertEqual(result["provenance"]["observed"], "unverified")

    def test_existing_data_without_workspace_id_refuses(self) -> None:
        document = valid_document()
        present_install(document)
        document["facts"]["data"] = {
            "exists": True,
            "owner": "alice",
            "symlink": False,
            "workspace_id": None,
        }
        result = self.plan(document)
        self.assertEqual(result["decision"], "refused")
        self.assertEqual(result["plan"]["action"], "none")
        self.assertIn("DATA_STATE_UNKNOWN", codes(result))
        self.assertNotEqual(result["plan"]["action"], "install")
        self.assertEqual(result["mode"], "plan_only")
        self.assertEqual(result["verification"], "pending_live_probe")

    def test_existing_data_with_matching_workspace_can_be_current(self) -> None:
        document = valid_document()
        present_install(document)
        document["facts"]["data"] = {
            "exists": True,
            "owner": "alice",
            "symlink": False,
            "workspace_id": WORKSPACE_ID,
        }
        result = self.plan(document)
        self.assertEqual(result["decision"], "current")
        self.assertEqual(result["plan"]["action"], "noop")
        self.assertEqual(result["provenance"]["observed"], "unverified")

    def test_plan_never_contains_shell_or_ssh(self) -> None:
        encoded_plan = MODULE.encode_plan(self.plan())
        for fragment in (b"ssh ", b"bash -lc", b"&&", b"$(", b"os.system"):
            self.assertNotIn(fragment, encoded_plan)


class RemoteProvisionPlanProvenanceTest(unittest.TestCase):
    def test_results_are_plan_only_pending_live_probe(self) -> None:
        result = MODULE.plan_remote_provision(encoded(valid_document()))
        assert_plan_only_metadata(self, result)

    def test_every_decision_stays_plan_only_and_unverified(self) -> None:
        missing_python = valid_document()
        missing_python["facts"]["python"] = None
        refused = valid_document()
        refused["artifact"]["product_version"] = "0.0.0"
        current = valid_document()
        present_install(current)
        for document, decision in (
            (valid_document(), "install_needed"),
            (missing_python, "prerequisite_missing"),
            (refused, "refused"),
            (current, "current"),
        ):
            with self.subTest(decision=decision):
                result = MODULE.plan_remote_provision(encoded(document))
                self.assertEqual(result["decision"], decision)
                assert_plan_only_metadata(self, result)

    def test_identical_input_is_deterministic(self) -> None:
        payload = encoded(valid_document())
        first = MODULE.encode_plan(MODULE.plan_remote_provision(payload))
        second = MODULE.encode_plan(MODULE.plan_remote_provision(payload))
        self.assertEqual(first, second)

    def test_no_side_effects_on_temp_or_subprocess(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            marker = root / "marker.txt"
            marker.write_text("untouched\n", encoding="utf-8")
            before = tree_hashes(root)
            with (
                mock.patch("subprocess.Popen") as popen,
                mock.patch("os.system") as system,
                mock.patch("socket.socket") as sock,
            ):
                MODULE.plan_remote_provision(encoded(valid_document()))
                popen.assert_not_called()
                system.assert_not_called()
                sock.assert_not_called()
            self.assertEqual(tree_hashes(root), before)

    def test_module_does_not_import_ssh_or_subprocess(self) -> None:
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        imported: set[str] = set()
        called: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".", 1)[0])
            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    called.add(func.id)
                elif isinstance(func, ast.Attribute):
                    called.add(func.attr)
        forbidden = {"ssh", "paramiko", "subprocess", "socket", "http", "urllib", "requests"}
        self.assertFalse(imported & forbidden)
        self.assertFalse(called & {"system", "popen", "Popen", "urlopen"})


class RemoteProvisionPlanCliTest(unittest.TestCase):
    def run_cli(self, payload: bytes) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [sys.executable, "-B", str(CLI)],
            input=payload,
            cwd=str(ROOT),
            capture_output=True,
            check=False,
        )

    def test_cli_emits_sorted_plan_json(self) -> None:
        payload = encoded(valid_document())
        completed = self.run_cli(payload)
        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", "replace"))
        parsed = json.loads(completed.stdout.decode("ascii"))
        self.assertEqual(parsed["decision"], "install_needed")
        assert_plan_only_metadata(self, parsed)
        self.assertEqual(completed.stdout, MODULE.encode_plan(parsed))

    def test_cli_rejects_invalid_document(self) -> None:
        completed = self.run_cli(b"{")
        self.assertEqual(completed.returncode, 2)
        error = json.loads(completed.stderr.decode("ascii"))
        self.assertEqual(error["code"], "INVALID_DOCUMENT")
        self.assertLessEqual(len(completed.stderr), 512)
        self.assertNotIn(b"Traceback", completed.stderr)

    def test_cli_secret_key_is_not_echoed(self) -> None:
        document = valid_document()
        document["api_token"] = "super-secret-value"
        completed = self.run_cli(encoded(document))
        self.assertEqual(completed.returncode, 2)
        self.assertNotIn(b"Traceback", completed.stderr)
        self.assertNotIn(b"api_token", completed.stderr)
        self.assertNotIn(b"super-secret-value", completed.stderr)
        error = json.loads(completed.stderr.decode("ascii"))
        self.assertEqual(error["code"], "INVALID_DOCUMENT")
        self.assertIn("unsupported fields", error["detail"])

    def test_cli_malformed_utf8_is_structured_refusal(self) -> None:
        completed = self.run_cli(b"\xff\xfe{")
        self.assertEqual(completed.returncode, 2)
        self.assertNotIn(b"Traceback", completed.stderr)
        self.assertNotIn(b"UnicodeDecodeError", completed.stderr)
        error = json.loads(completed.stderr.decode("ascii"))
        self.assertEqual(error["code"], "INVALID_DOCUMENT")
        self.assertLessEqual(len(error["detail"]), MODULE.MAX_DETAIL_LENGTH)

    def test_cli_deeply_nested_json_is_structured_refusal(self) -> None:
        depth = 1200
        payload = (b'{"a":' * depth) + b"1" + (b"}" * depth)
        self.assertLessEqual(len(payload), MODULE.MAX_DOCUMENT_BYTES)
        completed = self.run_cli(payload)
        self.assertEqual(completed.returncode, 2)
        self.assertNotIn(b"Traceback", completed.stderr)
        self.assertNotIn(b"RecursionError", completed.stderr)
        error = json.loads(completed.stderr.decode("ascii"))
        self.assertEqual(error["code"], "INVALID_DOCUMENT")
        self.assertLessEqual(len(completed.stderr), 512)

    def test_cli_oversize_integer_is_structured_refusal(self) -> None:
        completed = self.run_cli(huge_schema_version_document())
        self.assertEqual(completed.returncode, 2)
        self.assertNotIn(b"Traceback", completed.stderr)
        self.assertNotIn(b"ValueError", completed.stderr)
        self.assertNotIn(b"5" * 32, completed.stderr)
        self.assertLessEqual(len(completed.stderr), 512)
        error = json.loads(completed.stderr.decode("ascii"))
        self.assertEqual(error["code"], "INVALID_DOCUMENT")
        self.assertLessEqual(len(error["detail"]), MODULE.MAX_DETAIL_LENGTH)

    def test_cli_oversize_read_is_capped_and_not_echoed(self) -> None:
        marker = b"CANARY_OVERSIZE_PAYLOAD"
        payload = b"{" + marker * ((MODULE.MAX_DOCUMENT_BYTES // len(marker)) + 4)
        self.assertGreater(len(payload), MODULE.MAX_DOCUMENT_BYTES + 1)
        completed = self.run_cli(payload)
        self.assertEqual(completed.returncode, 2)
        self.assertLessEqual(len(completed.stderr), 512)
        self.assertNotIn(marker, completed.stderr)
        error = json.loads(completed.stderr.decode("ascii"))
        self.assertEqual(error["code"], "INVALID_DOCUMENT")
        self.assertIn("inspect/plan bound", error["detail"])
        self.assertLessEqual(len(error["detail"]), MODULE.MAX_DETAIL_LENGTH)

    def test_cli_does_not_write_the_result_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            before = tuple(sorted(root.iterdir()))
            env = dict(os.environ)
            env["TMP"] = str(root)
            env["TEMP"] = str(root)
            env["TMPDIR"] = str(root)
            completed = subprocess.run(
                [sys.executable, "-B", str(CLI)],
                input=encoded(valid_document()),
                cwd=str(ROOT),
                capture_output=True,
                check=False,
                env=env,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", "replace"))
            self.assertEqual(tuple(sorted(root.iterdir())), before)


if __name__ == "__main__":
    unittest.main()
