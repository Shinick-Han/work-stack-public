"""The one-shot verifier CLI as a real child, against protocol A.

Every run here spawns the actual ``python -m
integrations.opendocuments.source_verifier_main`` with an explicit interpreter,
an explicit working directory and an explicit environment. stdin is a real
``workstack.knowledge-verify.v1`` document. Protocol A must be present: this
file does not skip, stub or privately reimplement the validators.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from integrations.opendocuments.source_verifier_main import (
    CONFIG_ENVIRONMENT_VARIABLE,
    DIAGNOSTIC_CODES,
    RESULT_SCHEMA,
    VERIFY_SCHEMA,
)
from test_od_source_verifier_config import (
    ALIAS,
    BODY_CANARY,
    GRANT,
    NAS_REF,
    OTHER_GRANT,
    OTHER_REF,
    UPSTREAM_UID,
    VerifierConfigCase,
)
from workstack.knowledge_request_issuer import utc_now_rfc3339

ROOT = Path(__file__).resolve().parents[1]
MODULE = "integrations.opendocuments.source_verifier_main"

WORKSPACE_UID = "55555555-5555-4555-8555-555555555555"
VERIFICATION_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
CAPTURE_ID = "C-1001"


def sha256_of(path: Path) -> str:
    return "sha256-" + hashlib.sha256(path.read_bytes()).hexdigest()


def _later(stamp: str, seconds: int) -> str:
    moment = dt.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S%z")
    return (moment + dt.timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


class VerifierProcessCase(VerifierConfigCase):
    def setUp(self) -> None:
        super().setUp()
        self.now = utc_now_rfc3339()

    def child_environment(self, config: Path | None) -> dict[str, str]:
        environment = {"PYTHONPATH": str(ROOT)}
        if config is not None:
            environment[CONFIG_ENVIRONMENT_VARIABLE] = str(config)
        for inherited in ("SystemRoot", "PATH"):
            if inherited in os.environ:
                environment[inherited] = os.environ[inherited]
        return environment

    def run_child(
        self, payload: bytes, *, config: Path | None = None, argv: list | None = None
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", MODULE] + list(argv or []),
            input=payload,
            capture_output=True,
            cwd=str(ROOT),
            env=self.child_environment(config),
            timeout=90,
        )

    def evidence(
        self,
        *,
        expected: str | None,
        document_ref: str = NAS_REF,
        source_type: str = "nas.file",
    ) -> dict:
        return {
            "document_ref": document_ref,
            "source_type": source_type,
            "expected_source_version": expected,
        }

    def request_document(self, **overrides: object) -> dict:
        document = {
            "schema": VERIFY_SCHEMA,
            "verification_id": VERIFICATION_ID,
            "binding": {
                "workspace_uid": WORKSPACE_UID,
                "capture_id": CAPTURE_ID,
                "capture_revision": 0,
            },
            "connection": {
                "alias": ALIAS,
                "upstream_workspace_uid": UPSTREAM_UID,
                "policy_revision": 1,
            },
            "corpus_refs": [GRANT],
            "evidence": [self.evidence(expected=sha256_of(self.document))],
            "requested_at": self.now,
            "expires_at": _later(self.now, 60),
        }
        document.update(overrides)
        return document

    def run_request(
        self, document: dict, *, config: Path | None = None
    ) -> subprocess.CompletedProcess:
        payload = json.dumps(document).encode("utf-8")
        return self.run_child(payload, config=config or self.write_config())

    def assertRefused(self, completed: subprocess.CompletedProcess) -> None:
        self.assertNotEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, b"")
        rendered = completed.stderr.decode("utf-8", "replace").strip()
        self.assertIn(rendered, DIAGNOSTIC_CODES)
        self.assertNoDisclosure(completed)

    def assertNoDisclosure(self, completed: subprocess.CompletedProcess) -> None:
        streams = (
            completed.stdout.decode("utf-8", "replace")
            + completed.stderr.decode("utf-8", "replace")
        )
        for secret in (
            str(self.share),
            str(self.document),
            str(self.config_file),
            os.path.basename(str(self.share)),
            "quarterly.pdf",
            BODY_CANARY.decode("ascii"),
        ):
            self.assertNotIn(secret, streams)

    def admitted_result(self, completed: subprocess.CompletedProcess, document: dict) -> dict:
        from workstack.knowledge_verification_protocol import (
            validate_verification_request,
            validate_verification_result,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNoDisclosure(completed)
        owner_now = utc_now_rfc3339()
        request = validate_verification_request(document, now=owner_now)
        result = json.loads(completed.stdout.decode("utf-8"))
        admitted = validate_verification_result(result, request=request, now=owner_now)
        self.assertEqual(admitted["schema"], RESULT_SCHEMA)
        self.assertEqual(admitted["verification_id"], VERIFICATION_ID)
        return admitted


class VerifierCliTests(VerifierProcessCase):
    def test_matching_bytes_are_current_on_the_real_cli(self) -> None:
        document = self.request_document()
        result = self.admitted_result(self.run_request(document), document)
        expected = sha256_of(self.document)
        entry = result["evidence"][0]
        self.assertEqual(entry["status"], "current")
        self.assertEqual(entry["code"], "hash_matched")
        self.assertEqual(entry["observed_source_version"], expected)

    def test_different_bytes_are_stale_on_the_real_cli(self) -> None:
        expected = sha256_of(self.document)
        document = self.request_document(evidence=[self.evidence(expected=expected)])
        self.document.write_bytes(b"%PDF-1.7 edited-after-capture\n")
        entry = self.admitted_result(self.run_request(document), document)["evidence"][0]
        self.assertEqual(entry["status"], "stale")
        self.assertEqual(entry["code"], "hash_differs")
        self.assertEqual(entry["observed_source_version"], sha256_of(self.document))

    def test_missing_file_is_file_absent(self) -> None:
        expected = sha256_of(self.document)
        document = self.request_document(evidence=[self.evidence(expected=expected)])
        self.document.unlink()
        entry = self.admitted_result(self.run_request(document), document)["evidence"][0]
        self.assertEqual(entry["status"], "missing")
        self.assertEqual(entry["code"], "file_absent")
        self.assertIsNone(entry["observed_source_version"])

    def test_missing_root_is_root_unavailable(self) -> None:
        expected = sha256_of(self.document)
        document = self.request_document(evidence=[self.evidence(expected=expected)])
        config = self.write_config()
        shutil.rmtree(self.share)
        entry = self.admitted_result(self.run_request(document, config=config), document)[
            "evidence"
        ][0]
        self.assertEqual(entry["status"], "unavailable")
        self.assertEqual(entry["code"], "root_unavailable")
        self.assertIsNone(entry["observed_source_version"])

    def test_no_expected_version_stays_unverifiable(self) -> None:
        document = self.request_document(evidence=[self.evidence(expected=None)])
        entry = self.admitted_result(self.run_request(document), document)["evidence"][0]
        self.assertEqual(entry["status"], "unverifiable")
        self.assertEqual(entry["code"], "no_expected_version")
        self.assertIsNone(entry["observed_source_version"])

    def test_corpus_mismatch_is_source_refused(self) -> None:
        document = self.request_document(corpus_refs=[OTHER_GRANT])
        entry = self.admitted_result(self.run_request(document), document)["evidence"][0]
        self.assertEqual(entry["status"], "refused")
        self.assertEqual(entry["code"], "source_refused")

    def test_revoked_mapping_is_mapping_revoked(self) -> None:
        document = self.request_document()
        config = self.write_config(mappings=[self.mapping_document(revoked=True)])
        entry = self.admitted_result(self.run_request(document, config=config), document)[
            "evidence"
        ][0]
        self.assertEqual(entry["status"], "revoked")
        self.assertEqual(entry["code"], "mapping_revoked")

    def test_missing_mapping_is_source_refused(self) -> None:
        document = self.request_document(
            evidence=[self.evidence(expected=sha256_of(self.document), document_ref=OTHER_REF)]
        )
        entry = self.admitted_result(self.run_request(document), document)["evidence"][0]
        self.assertEqual(entry["code"], "source_refused")
        self.assertEqual(entry["document_ref"], OTHER_REF)

    def test_policy_and_malformed_input_leave_stdout_empty(self) -> None:
        foreign = self.request_document()
        foreign["connection"] = dict(foreign["connection"], alias="other-nas")
        self.assertRefused(self.run_request(foreign))
        widened = self.request_document(corpus_refs=[GRANT, "foreign-corpus"])
        self.assertRefused(self.run_request(widened))
        self.assertRefused(self.run_child(b"{not json", config=self.write_config()))
        self.assertRefused(self.run_child(b"", config=self.write_config()))
        extra = self.request_document()
        extra["unexpected"] = True
        self.assertRefused(self.run_request(extra))
        self.assertRefused(self.run_child(b"{}", config=None))
        self.config_file.write_bytes(b"{not json")
        self.assertRefused(
            self.run_child(
                json.dumps(self.request_document()).encode("utf-8"),
                config=self.config_file,
            )
        )
        self.assertRefused(
            self.run_child(
                json.dumps(self.request_document()).encode("utf-8"),
                config=self.write_config(),
                argv=["--help"],
            )
        )


if __name__ == "__main__":  # pragma: no cover - direct invocation
    unittest.main()
