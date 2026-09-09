"""NAS observation mapping through the verifier, without protocol A.

These tests load a fake operator file and call
:func:`observe_verification_evidence` against synthetic temp files. They do not
import ``knowledge_verification_protocol`` and they do not spawn the CLI. Hash
values are recomputed with hashlib, independently of source_access.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import unittest

from integrations.opendocuments.source_verifier_main import (
    check_verifier_policy,
    observe_verification_evidence,
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

NOTION_REF = "od-page-7f3ba1d34f50c884600112ab"


def sha256_of(path) -> str:
    return "sha256-" + hashlib.sha256(path.read_bytes()).hexdigest()


class ObserveCase(VerifierConfigCase):
    def request(
        self,
        *,
        expected: str | None,
        document_ref: str = NAS_REF,
        source_type: str = "nas.file",
        corpus_refs: list[str] | None = None,
    ) -> dict:
        return {
            "corpus_refs": list(corpus_refs or [GRANT]),
            "evidence": [
                {
                    "document_ref": document_ref,
                    "source_type": source_type,
                    "expected_source_version": expected,
                }
            ],
        }

    def observe(self, config, **kwargs) -> dict:
        entries = observe_verification_evidence(self.request(**kwargs), config)
        self.assertEqual(len(entries), 1)
        return entries[0]

    def assertOpaque(self, entry: dict) -> None:
        rendered = json.dumps(entry, ensure_ascii=False)
        for secret in (
            str(self.share),
            str(self.document),
            str(self.config_file),
            os.path.basename(str(self.share)),
            "quarterly.pdf",
            "reports",
            BODY_CANARY.decode("ascii"),
        ):
            self.assertNotIn(secret, rendered)


class NasObservationTests(ObserveCase):
    def test_matching_bytes_are_current_against_recomputed_hash(self) -> None:
        config = self.load()
        expected = sha256_of(self.document)
        entry = self.observe(config, expected=expected)
        self.assertEqual(entry["status"], "current")
        self.assertEqual(entry["code"], "hash_matched")
        self.assertEqual(entry["observed_source_version"], expected)
        self.assertTrue(entry["observed_source_version"].startswith("sha256-"))
        self.assertNotIn("sha256:", entry["observed_source_version"])
        self.assertEqual(entry["expected_source_version"], expected)
        self.assertEqual(entry["document_ref"], NAS_REF)
        self.assertOpaque(entry)

    def test_different_bytes_are_stale(self) -> None:
        config = self.load()
        expected = sha256_of(self.document)
        self.document.write_bytes(b"%PDF-1.7 edited-after-capture\n")
        actual = sha256_of(self.document)
        self.assertNotEqual(actual, expected)
        entry = self.observe(config, expected=expected)
        self.assertEqual(entry["status"], "stale")
        self.assertEqual(entry["code"], "hash_differs")
        self.assertEqual(entry["observed_source_version"], actual)
        self.assertTrue(entry["observed_source_version"].startswith("sha256-"))
        self.assertNotIn("sha256:", entry["observed_source_version"])
        self.assertOpaque(entry)

    def test_missing_file_is_not_a_missing_root(self) -> None:
        config = self.load()
        expected = sha256_of(self.document)
        self.document.unlink()
        entry = self.observe(config, expected=expected)
        self.assertEqual(entry["status"], "missing")
        self.assertEqual(entry["code"], "file_absent")
        self.assertIsNone(entry["observed_source_version"])
        self.assertOpaque(entry)

    def test_missing_root_is_not_a_missing_file(self) -> None:
        config = self.load()
        expected = sha256_of(self.document)
        shutil.rmtree(self.share)
        entry = self.observe(config, expected=expected)
        self.assertEqual(entry["status"], "unavailable")
        self.assertEqual(entry["code"], "root_unavailable")
        self.assertIsNone(entry["observed_source_version"])
        self.assertOpaque(entry)

    def test_no_expected_version_never_becomes_current(self) -> None:
        config = self.load()
        entry = self.observe(config, expected=None)
        self.assertEqual(entry["status"], "unverifiable")
        self.assertEqual(entry["code"], "no_expected_version")
        self.assertIsNone(entry["observed_source_version"])
        self.assertOpaque(entry)

    def test_colon_internal_hash_is_not_a_wire_version(self) -> None:
        config = self.load()
        internal = "sha256:" + hashlib.sha256(self.document.read_bytes()).hexdigest()
        entry = self.observe(config, expected=internal)
        self.assertEqual(entry["status"], "unverifiable")
        self.assertEqual(entry["code"], "verification_unavailable")
        self.assertIsNone(entry["observed_source_version"])
        self.assertOpaque(entry)

    def test_other_opaque_expected_version_is_not_stale(self) -> None:
        config = self.load()
        entry = self.observe(config, expected="od-origin-7f3ba1d34f50c884")
        self.assertEqual(entry["status"], "unverifiable")
        self.assertEqual(entry["code"], "verification_unavailable")
        self.assertIsNone(entry["observed_source_version"])
        self.assertOpaque(entry)

    def test_corpus_mismatch_refuses_before_a_read(self) -> None:
        config = self.load()
        entry = self.observe(config, expected=sha256_of(self.document), corpus_refs=[OTHER_GRANT])
        self.assertEqual(entry["status"], "refused")
        self.assertEqual(entry["code"], "source_refused")
        self.assertIsNone(entry["observed_source_version"])
        self.assertTrue(self.document.is_file())
        self.assertOpaque(entry)

    def test_revoked_mapping_does_not_current_the_file(self) -> None:
        config = self.load(mappings=[self.mapping_document(revoked=True)])
        entry = self.observe(config, expected=sha256_of(self.document))
        self.assertEqual(entry["status"], "revoked")
        self.assertEqual(entry["code"], "mapping_revoked")
        self.assertIsNone(entry["observed_source_version"])
        self.assertOpaque(entry)

    def test_missing_mapping_is_source_refused(self) -> None:
        config = self.load()
        entry = self.observe(
            config, expected=sha256_of(self.document), document_ref=OTHER_REF
        )
        self.assertEqual(entry["status"], "refused")
        self.assertEqual(entry["code"], "source_refused")
        self.assertEqual(entry["document_ref"], OTHER_REF)
        self.assertIsNone(entry["observed_source_version"])
        self.assertOpaque(entry)

    def test_unknown_source_types_stay_unsupported(self) -> None:
        config = self.load()
        entry = self.observe(
            config,
            expected=None,
            document_ref=NOTION_REF,
            source_type="knowledge.answer",
        )
        self.assertEqual(entry["status"], "unverifiable")
        self.assertEqual(entry["code"], "unsupported_source_type")
        self.assertIsNone(entry["observed_source_version"])
        self.assertOpaque(entry)

    def test_unmapped_notion_page_is_refused_not_unsupported(self) -> None:
        """R24 moved ``notion.page`` off ``unsupported_source_type``.

        An id the operator never mapped is a policy refusal, exactly as it is
        for NAS. It is emphatically not ``missing``: this verifier has no
        opinion about whether a page it was never told about exists.
        """

        config = self.load()
        entry = self.observe(
            config, expected=None, document_ref=NOTION_REF, source_type="notion.page"
        )
        self.assertEqual(entry["status"], "refused")
        self.assertEqual(entry["code"], "source_refused")
        self.assertIsNone(entry["observed_source_version"])
        self.assertEqual(entry["document_ref"], NOTION_REF)
        self.assertOpaque(entry)

    def test_nas_evidence_never_answers_from_a_notion_mapping(self) -> None:
        """A file question filed as a page is refused, not answered elsewhere."""

        config = self.load(
            mappings=[
                {
                    "document_ref": NAS_REF,
                    "corpus": GRANT,
                    "page_url": "https://www.notion.so/"
                    + "Quarterly-1f2e3d4c5b6a70819243a5b6c7d8e9f0",
                    "revoked": False,
                }
            ]
        )
        entry = self.observe(config, expected=sha256_of(self.document))
        self.assertEqual(entry["status"], "refused")
        self.assertEqual(entry["code"], "source_refused")
        self.assertIsNone(entry["observed_source_version"])
        self.assertOpaque(entry)

    def test_repeated_refs_preserve_order_and_count(self) -> None:
        config = self.load()
        expected = sha256_of(self.document)
        request = {
            "corpus_refs": [GRANT],
            "evidence": [
                {
                    "document_ref": NAS_REF,
                    "source_type": "nas.file",
                    "expected_source_version": expected,
                },
                {
                    "document_ref": NAS_REF,
                    "source_type": "nas.file",
                    "expected_source_version": expected,
                },
            ],
        }
        entries = observe_verification_evidence(request, config)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["status"], "current")
        self.assertEqual(entries[1]["status"], "current")
        self.assertEqual(entries[0]["document_ref"], NAS_REF)
        self.assertEqual(entries[1]["document_ref"], NAS_REF)


class PolicyTests(ObserveCase):
    def test_request_corpora_must_be_a_subset_of_grants(self) -> None:
        config = self.load()
        connection = {"alias": ALIAS, "upstream_workspace_uid": UPSTREAM_UID}
        self.assertTrue(check_verifier_policy(connection, [GRANT], config))
        self.assertTrue(check_verifier_policy(connection, [GRANT, OTHER_GRANT], config))
        self.assertFalse(check_verifier_policy(connection, ["foreign-corpus"], config))
        self.assertFalse(
            check_verifier_policy(
                {"alias": "other-nas", "upstream_workspace_uid": UPSTREAM_UID},
                [GRANT],
                config,
            )
        )
        self.assertFalse(
            check_verifier_policy(
                {
                    "alias": ALIAS,
                    "upstream_workspace_uid": "77777777-7777-4777-8777-777777777777",
                },
                [GRANT],
                config,
            )
        )


if __name__ == "__main__":  # pragma: no cover - direct invocation
    unittest.main()
