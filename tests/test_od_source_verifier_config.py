"""Operator verifier-file loader against synthetic temp files only.

No network, key, live SSOT, real user document, opener or Notion connector is
used. Paths exist only inside the temporary directory this module creates.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from integrations.opendocuments.source_verifier_config import (
    MAX_CONFIG_BYTES,
    MAX_MAPPINGS,
    VERIFIER_CONFIG_CODES,
    VERIFIER_CONFIG_SCHEMA,
    VerifierConfigError,
    load_operator_verifier_config,
)

ALIAS = "team-nas"
UPSTREAM_UID = "66666666-6666-4666-8666-666666666666"
GRANT = "nas-team-share"
OTHER_GRANT = "notion-handbook"
NAS_REF = "od-file-2c9e51aa77b0c1de4432ff01"
OTHER_REF = "od-file-aaaaaaaa77b0c1de4432ff02"
BODY_CANARY = b"%PDF-1.7 canary-bytes-r20-sv-9f3a\n"
RELATIVE = "reports/quarterly.pdf"


class VerifierConfigCase(unittest.TestCase):
    """One temporary operator directory and one live NAS root."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.share = self.root / "share"
        self.share.mkdir()
        target = self.share / "reports"
        target.mkdir()
        self.document = target / "quarterly.pdf"
        self.document.write_bytes(BODY_CANARY)
        self.config_file = self.root / "od-verifier.json"

    def mapping_document(self, **overrides: object) -> dict:
        document = {
            "document_ref": NAS_REF,
            "corpus": GRANT,
            "allowed_root": str(self.share),
            "relative_location": RELATIVE,
            "revoked": False,
        }
        document.update(overrides)
        return document

    def config_document(self, **overrides: object) -> dict:
        document = {
            "schema": VERIFIER_CONFIG_SCHEMA,
            "connection_alias": ALIAS,
            "upstream_workspace_uid": UPSTREAM_UID,
            "corpus_grants": [GRANT, OTHER_GRANT],
            "mappings": [self.mapping_document()],
        }
        document.update(overrides)
        return document

    def write_config(self, **overrides: object) -> Path:
        self.config_file.write_text(
            json.dumps(self.config_document(**overrides)), encoding="utf-8"
        )
        return self.config_file

    def load(self, **overrides: object):
        return load_operator_verifier_config(str(self.write_config(**overrides)))

    def assertClosedError(self, error: VerifierConfigError, code: str) -> None:
        self.assertIsInstance(error, VerifierConfigError)
        self.assertEqual(error.code, code)
        self.assertIn(error.code, VERIFIER_CONFIG_CODES)
        rendered = str(error) + repr(error)
        for secret in (
            str(self.share),
            str(self.document),
            str(self.config_file),
            os.path.basename(str(self.share)),
            "quarterly.pdf",
            BODY_CANARY.decode("ascii"),
        ):
            self.assertNotIn(secret, rendered)


class LoaderSuccessTests(VerifierConfigCase):
    def test_admits_nas_mapping_with_source_access_defaults(self) -> None:
        config = self.load()
        mapping = config.registry.lookup(NAS_REF)
        self.assertIsNotNone(mapping)
        assert mapping is not None
        self.assertEqual(config.connection_alias, ALIAS)
        self.assertEqual(config.upstream_workspace_uid, UPSTREAM_UID)
        self.assertEqual(config.corpus_grants, (GRANT, OTHER_GRANT))
        self.assertEqual(mapping.document_id, NAS_REF)
        self.assertEqual(mapping.corpus, GRANT)
        self.assertEqual(mapping.backend, "nas")
        self.assertEqual(mapping.relative_location, RELATIVE)
        self.assertFalse(mapping.revoked)
        self.assertEqual(mapping.page_url, None)
        self.assertEqual(mapping.allowed_root, self.share.resolve())

    def test_repr_hides_operator_paths(self) -> None:
        config = self.load()
        rendered = repr(config)
        for secret in (str(self.share), str(self.config_file), str(self.document)):
            self.assertNotIn(secret, rendered)

    def test_does_not_consult_the_environment(self) -> None:
        os.environ["WORKSTACK_OD_VERIFIER_CONFIG"] = str(self.root / "missing.json")
        self.addCleanup(os.environ.pop, "WORKSTACK_OD_VERIFIER_CONFIG", None)
        config = self.load()
        self.assertEqual(config.connection_alias, ALIAS)


class LoaderRefusalTests(VerifierConfigCase):
    def test_relative_config_path_is_refused(self) -> None:
        with self.assertRaises(VerifierConfigError) as caught:
            load_operator_verifier_config("od-verifier.json")
        self.assertClosedError(caught.exception, "invalid_config_path")

    def test_missing_file_is_unreadable_not_a_path_echo(self) -> None:
        missing = self.root / "absent.json"
        with self.assertRaises(VerifierConfigError) as caught:
            load_operator_verifier_config(str(missing))
        self.assertClosedError(caught.exception, "config_unreadable")

    def test_malformed_json_is_invalid_config(self) -> None:
        self.config_file.write_bytes(b"{not json")
        with self.assertRaises(VerifierConfigError) as caught:
            load_operator_verifier_config(str(self.config_file))
        self.assertClosedError(caught.exception, "invalid_config")

    def test_unknown_field_including_api_key_is_invalid_config(self) -> None:
        for extra in ({"api_key": "secret"}, {"unexpected": 1}):
            with self.subTest(extra=tuple(extra)):
                document = self.config_document()
                document.update(extra)
                self.config_file.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaises(VerifierConfigError) as caught:
                    load_operator_verifier_config(str(self.config_file))
                self.assertClosedError(caught.exception, "invalid_config")

    def test_wrong_schema_is_unsupported(self) -> None:
        with self.assertRaises(VerifierConfigError) as caught:
            self.load(schema="workstack.opendocuments-driver.v1")
        self.assertClosedError(caught.exception, "unsupported_config_schema")

    def test_invalid_alias_and_upstream_are_closed(self) -> None:
        with self.assertRaises(VerifierConfigError) as caught:
            self.load(connection_alias="Team NAS")
        self.assertClosedError(caught.exception, "invalid_connection_alias")
        with self.assertRaises(VerifierConfigError) as caught:
            self.load(upstream_workspace_uid="not-a-uuid")
        self.assertClosedError(caught.exception, "invalid_upstream_workspace_uid")

    def test_grants_must_be_unique_existing_aliases(self) -> None:
        with self.assertRaises(VerifierConfigError) as caught:
            self.load(corpus_grants=[GRANT, GRANT])
        self.assertClosedError(caught.exception, "invalid_corpus_grants")
        with self.assertRaises(VerifierConfigError) as caught:
            self.load(corpus_grants=[])
        self.assertClosedError(caught.exception, "invalid_corpus_grants")

    def test_mapping_corpus_must_be_a_grant(self) -> None:
        with self.assertRaises(VerifierConfigError) as caught:
            self.load(mappings=[self.mapping_document(corpus="foreign-corpus")])
        self.assertClosedError(caught.exception, "invalid_mappings")

    def test_relative_allowed_root_is_refused(self) -> None:
        with self.assertRaises(VerifierConfigError) as caught:
            self.load(mappings=[self.mapping_document(allowed_root="share")])
        self.assertClosedError(caught.exception, "invalid_mappings")

    def test_duplicate_and_path_shaped_document_refs_are_refused(self) -> None:
        with self.assertRaises(VerifierConfigError) as caught:
            self.load(
                mappings=[self.mapping_document(), self.mapping_document()]
            )
        self.assertClosedError(caught.exception, "invalid_mappings")
        with self.assertRaises(VerifierConfigError) as caught:
            self.load(
                mappings=[self.mapping_document(document_ref=str(self.document))]
            )
        self.assertClosedError(caught.exception, "invalid_mappings")

    def test_revoked_must_be_a_json_boolean(self) -> None:
        with self.assertRaises(VerifierConfigError) as caught:
            self.load(mappings=[self.mapping_document(revoked=1)])
        self.assertClosedError(caught.exception, "invalid_mappings")

    def test_unknown_mapping_field_is_refused(self) -> None:
        mapping = self.mapping_document()
        mapping["page_url"] = "https://www.notion.so/page"
        with self.assertRaises(VerifierConfigError) as caught:
            self.load(mappings=[mapping])
        self.assertClosedError(caught.exception, "invalid_mappings")

    def test_oversized_file_is_unreadable(self) -> None:
        self.config_file.write_bytes(b"x" * (MAX_CONFIG_BYTES + 1))
        with self.assertRaises(VerifierConfigError) as caught:
            load_operator_verifier_config(str(self.config_file))
        self.assertClosedError(caught.exception, "config_unreadable")

    def test_too_many_mappings_are_refused(self) -> None:
        mappings = [
            self.mapping_document(document_ref="od-file-{:022d}".format(index))
            for index in range(MAX_MAPPINGS + 1)
        ]
        with self.assertRaises(VerifierConfigError) as caught:
            self.load(mappings=mappings)
        self.assertClosedError(caught.exception, "invalid_mappings")


if __name__ == "__main__":  # pragma: no cover - direct invocation
    unittest.main()
