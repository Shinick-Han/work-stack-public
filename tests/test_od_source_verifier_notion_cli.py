"""The Notion mapping union, the protocol round trip, and the real one-shot CLI.

Every operator file here is a synthetic temporary document and every token is a
synthetic string. The two CLI runs are real child processes, and both are cases
that answer *before* any token or socket is reached -- no token configured, and
a revoked mapping -- so this file spawns a process without ever making a network
call. Live Notion acceptance stays explicit outstanding evidence; nothing here
infers it from a canned transport.
"""

from __future__ import annotations

import json
import os
import unittest

from integrations.opendocuments.source_verifier_config import (
    VERIFIER_NAS_MAPPING_FIELDS,
    VERIFIER_NOTION_MAPPING_FIELDS,
    VerifierConfigError,
    load_operator_verifier_config,
)
from integrations.opendocuments import source_verifier_main
from integrations.opendocuments.source_verifier_main import (
    CONFIG_ENVIRONMENT_VARIABLE,
    RESULT_SCHEMA,
    observe_verification_evidence,
)
from integrations.opendocuments.source_verifier_notion import (
    NotionBatch,
    TOKEN_ENVIRONMENT_VARIABLE,
)
from test_od_notion_fixtures import (
    NOTION_REF,
    PAGE_URL,
    TITLE_CANARY,
    TOKEN_CANARY,
    FakeClock,
    RecordingTransport,
    Wire,
    encoded_page,
)
from test_od_source_verifier_config import (
    GRANT,
    NAS_REF,
    OTHER_GRANT,
    VerifierConfigCase,
)
from test_od_source_verifier_main import VerifierProcessCase, _later, sha256_of

NOTION_EXPECTED = "od-origin-7f3ba1d34f50c884"


class NotionMappingMixin:
    """One Notion mapping document, spelled the way the operator file does."""

    def notion_mapping_document(self, **overrides: object) -> dict:
        document = {
            "document_ref": NOTION_REF,
            "corpus": OTHER_GRANT,
            "page_url": PAGE_URL,
            "revoked": False,
        }
        document.update(overrides)
        return document


class NotionConfigUnionTests(NotionMappingMixin, VerifierConfigCase):
    """A mapping is one backend's exact field set, or it is refused."""

    def test_the_two_field_sets_are_disjoint_in_their_backend_key(self) -> None:
        self.assertNotEqual(VERIFIER_NAS_MAPPING_FIELDS, VERIFIER_NOTION_MAPPING_FIELDS)
        self.assertEqual(
            VERIFIER_NAS_MAPPING_FIELDS & VERIFIER_NOTION_MAPPING_FIELDS,
            frozenset({"document_ref", "corpus", "revoked"}),
        )

    def test_a_notion_mapping_is_admitted_as_a_released_source_mapping(self) -> None:
        config = self.load(
            mappings=[self.mapping_document(), self.notion_mapping_document()]
        )
        notion = config.registry.lookup(NOTION_REF)
        nas = config.registry.lookup(NAS_REF)
        assert notion is not None and nas is not None
        self.assertEqual(notion.backend, "notion")
        self.assertEqual(notion.corpus, OTHER_GRANT)
        self.assertEqual(notion.page_url, PAGE_URL)
        self.assertIsNone(notion.allowed_root)
        self.assertIsNone(notion.relative_location)
        self.assertFalse(notion.revoked)
        # The existing NAS mapping in the same file is untouched.
        self.assertEqual(nas.backend, "nas")
        self.assertEqual(nas.allowed_root, self.share.resolve())

    def test_a_revoked_notion_mapping_is_admitted_and_stays_revoked(self) -> None:
        config = self.load(mappings=[self.notion_mapping_document(revoked=True)])
        mapping = config.registry.lookup(NOTION_REF)
        assert mapping is not None
        self.assertTrue(mapping.revoked)

    def test_mixed_and_unknown_mapping_fields_are_refused(self) -> None:
        mixed = dict(self.mapping_document(), page_url=PAGE_URL)
        extra = dict(self.notion_mapping_document(), api_key="ntn_secret")
        short = {k: v for k, v in self.notion_mapping_document().items() if k != "corpus"}
        nas_missing_root = {
            k: v for k, v in self.mapping_document().items() if k != "allowed_root"
        }
        for name, mapping in {
            "nas_plus_page_url": mixed,
            "notion_plus_unknown_key": extra,
            "notion_missing_corpus": short,
            "nas_missing_allowed_root": nas_missing_root,
            "empty": {},
            "not_an_object": ["document_ref"],
        }.items():
            with self.subTest(mapping=name):
                with self.assertRaises(VerifierConfigError) as caught:
                    self.load(mappings=[mapping])
                self.assertClosedError(caught.exception, "invalid_mappings")

    def test_unsafe_page_urls_are_refused_by_the_released_allow_list(self) -> None:
        for name, url in {
            "plain_http": "http://www.notion.so/P-1f2e3d4c5b6a70819243a5b6c7d8e9f0",
            "foreign_host": "https://notion.so.evil.example/P-1f2e3d4c5b6a708192",
            "explicit_port": "https://www.notion.so:8443/P-1f2e3d4c5b6a70819243",
            "userinfo": "https://u:p@www.notion.so/P-1f2e3d4c5b6a70819243a5b6c",
            "query": PAGE_URL + "?v=1",
            "fragment": PAGE_URL + "#block",
            "punycode": "https://xn--notion-ex.notion.site/P-1f2e3d4c5b6a7081",
            "empty": "",
            "oversize": "https://www.notion.so/" + "a" * 2048,
            "not_a_string": 17,
        }.items():
            with self.subTest(page_url=name):
                with self.assertRaises(VerifierConfigError) as caught:
                    self.load(mappings=[self.notion_mapping_document(page_url=url)])
                self.assertClosedError(caught.exception, "invalid_mappings")

    def test_a_notion_corpus_outside_the_grants_is_refused(self) -> None:
        with self.assertRaises(VerifierConfigError) as caught:
            self.load(mappings=[self.notion_mapping_document(corpus="ungranted")])
        self.assertClosedError(caught.exception, "invalid_mappings")

    def test_a_non_boolean_revoked_flag_is_refused(self) -> None:
        for value in ("true", 1, None):
            with self.subTest(revoked=value):
                with self.assertRaises(VerifierConfigError) as caught:
                    self.load(mappings=[self.notion_mapping_document(revoked=value)])
                self.assertClosedError(caught.exception, "invalid_mappings")

    def test_a_page_url_naming_no_page_is_a_configuration_the_file_keeps(self) -> None:
        """An unusable selector is an observation refusal, not a dead file.

        The whole verifier file must not stop loading because one operator URL
        points at a workspace index instead of a page: that document gets
        ``refused``/``source_refused`` and every other mapping keeps working.
        """

        config = self.load(
            mappings=[
                self.mapping_document(),
                self.notion_mapping_document(page_url="https://www.notion.so/team"),
            ]
        )
        self.assertEqual(len(config.registry), 2)


class NotionProtocolTests(NotionMappingMixin, VerifierProcessCase):
    """Observations run in process, then admitted by protocol A as a result."""

    def setUp(self) -> None:
        super().setUp()
        self.clock = FakeClock()
        self.set_token_environment(None)

    def set_token_environment(self, value: str | None) -> None:
        previous = os.environ.get(TOKEN_ENVIRONMENT_VARIABLE)

        def restore() -> None:
            if previous is None:
                os.environ.pop(TOKEN_ENVIRONMENT_VARIABLE, None)
            else:
                os.environ[TOKEN_ENVIRONMENT_VARIABLE] = previous

        self.addCleanup(restore)
        if value is None:
            os.environ.pop(TOKEN_ENVIRONMENT_VARIABLE, None)
        else:
            os.environ[TOKEN_ENVIRONMENT_VARIABLE] = value

    def write_token(self, text: str = TOKEN_CANARY) -> str:
        path = self.root / "notion-token.txt"
        path.write_text(text + "\n", encoding="ascii")
        self.set_token_environment(str(path))
        return str(path)

    def admitted(self, document: dict, *, wires: list, mappings: list) -> dict:
        from workstack.knowledge_verification_protocol import (
            validate_verification_request,
            validate_verification_result,
        )

        config = load_operator_verifier_config(
            str(self.write_config(mappings=mappings))
        )
        request = validate_verification_request(document, now=self.now)
        transport = RecordingTransport(wires, clock=self.clock)
        batch = NotionBatch(
            _transport=transport, _clock=self.clock.read, _sleep=self.clock.sleep
        )
        entries = observe_verification_evidence(request, config, _notion_batch=batch)
        result = {
            "schema": RESULT_SCHEMA,
            "verification_id": request["verification_id"],
            "checked_at": self.now,
            "evidence": entries,
        }
        self.transport = transport
        return validate_verification_result(result, request=request, now=self.now)

    def notion_evidence(self, expected: object = None) -> dict:
        return {
            "document_ref": NOTION_REF,
            "source_type": "notion.page",
            "expected_source_version": expected,
        }

    def test_ten_repeated_notion_entries_keep_order_count_and_expectation(self) -> None:
        self.write_token()
        expectations = [None] + [NOTION_EXPECTED] * 9
        document = self.request_document(
            corpus_refs=[GRANT, OTHER_GRANT],
            evidence=[self.notion_evidence(value) for value in expectations],
        )
        admitted = self.admitted(
            document,
            wires=[Wire(200, encoded_page())] * 10,
            mappings=[self.mapping_document(), self.notion_mapping_document()],
        )
        self.assertEqual(len(admitted["evidence"]), 10)
        self.assertEqual(len(self.transport.calls), 10)
        for entry, expected in zip(admitted["evidence"], expectations):
            self.assertEqual(entry["document_ref"], NOTION_REF)
            self.assertEqual(entry["source_type"], "notion.page")
            self.assertEqual(entry["expected_source_version"], expected)
            self.assertIsNone(entry["observed_source_version"])
            self.assertEqual(entry["status"], "unverifiable")
        self.assertEqual(admitted["evidence"][0]["code"], "no_expected_version")
        for entry in admitted["evidence"][1:]:
            self.assertEqual(entry["code"], "verification_unavailable")

    def test_a_matching_last_edited_time_is_never_current(self) -> None:
        """The R22 timestamp table stays rejected, even when it would 'match'."""

        from workstack.knowledge_verification_protocol import (
            VerificationError,
            validate_verification_request,
        )

        # The protocol's opaque grammar already refuses a raw RFC3339 stamp,
        # so a timestamp cannot even be *asked* about on this wire.
        with self.assertRaises(VerificationError):
            validate_verification_request(
                self.request_document(
                    corpus_refs=[OTHER_GRANT],
                    evidence=[self.notion_evidence("2026-05-06T07:08:00.000Z")],
                ),
                now=self.now,
            )
        # An opaque version derived from that same stamp still never matches.
        self.write_token()
        document = self.request_document(
            corpus_refs=[OTHER_GRANT],
            evidence=[self.notion_evidence("20260506T070800.000Z")],
        )
        entry = self.admitted(
            document,
            wires=[Wire(200, encoded_page())],
            mappings=[self.notion_mapping_document()],
        )["evidence"][0]
        self.assertEqual(entry["status"], "unverifiable")
        self.assertEqual(entry["code"], "verification_unavailable")
        self.assertIsNone(entry["observed_source_version"])

    def test_nas_and_notion_evidence_answer_in_one_request(self) -> None:
        self.set_token_environment(None)
        document = self.request_document(
            corpus_refs=[GRANT, OTHER_GRANT],
            evidence=[
                self.evidence(expected=sha256_of(self.document)),
                self.notion_evidence(),
            ],
        )
        admitted = self.admitted(
            document,
            wires=[],
            mappings=[self.mapping_document(), self.notion_mapping_document()],
        )
        nas, notion = admitted["evidence"]
        self.assertEqual((nas["status"], nas["code"]), ("current", "hash_matched"))
        self.assertEqual(nas["observed_source_version"], sha256_of(self.document))
        self.assertEqual(
            (notion["status"], notion["code"]), ("unverifiable", "no_origin_verifier")
        )
        self.assertEqual(self.transport.calls, [])

    def test_an_out_of_request_corpus_notion_page_is_refused(self) -> None:
        self.write_token()
        document = self.request_document(
            corpus_refs=[GRANT], evidence=[self.notion_evidence()]
        )
        entry = self.admitted(
            document,
            wires=[],
            mappings=[self.mapping_document(), self.notion_mapping_document()],
        )["evidence"][0]
        self.assertEqual((entry["status"], entry["code"]), ("refused", "source_refused"))
        self.assertEqual(self.transport.calls, [])

    def test_no_page_content_reaches_the_admitted_result(self) -> None:
        self.write_token()
        document = self.request_document(
            corpus_refs=[OTHER_GRANT], evidence=[self.notion_evidence()]
        )
        admitted = self.admitted(
            document,
            wires=[Wire(200, encoded_page())],
            mappings=[self.notion_mapping_document()],
        )
        rendered = json.dumps(admitted, ensure_ascii=False)
        for secret in (TOKEN_CANARY, TITLE_CANARY, PAGE_URL, "last_edited_time"):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, rendered)


class NotionCliTests(NotionMappingMixin, VerifierProcessCase):
    """Two real child runs, both settled before a token or a socket."""

    def child_environment(self, config, token: str | None = None) -> dict:
        environment = super().child_environment(config)
        if token is not None:
            environment[TOKEN_ENVIRONMENT_VARIABLE] = token
        return environment

    def run_notion(self, *, mappings: list, token: str | None = None, **overrides):
        import subprocess
        import sys
        from test_od_source_verifier_main import MODULE, ROOT

        document = self.request_document(
            corpus_refs=[GRANT, OTHER_GRANT],
            evidence=[
                {
                    "document_ref": NOTION_REF,
                    "source_type": "notion.page",
                    "expected_source_version": None,
                }
            ],
            **overrides,
        )
        config = self.write_config(mappings=mappings)
        completed = subprocess.run(
            [sys.executable, "-m", MODULE],
            input=json.dumps(document).encode("utf-8"),
            capture_output=True,
            cwd=str(ROOT),
            env=self.child_environment(config, token),
            timeout=90,
        )
        return self.admitted_result(completed, document), completed

    def assertNoNotionDisclosure(self, completed) -> None:
        streams = completed.stdout.decode("utf-8", "replace") + completed.stderr.decode(
            "utf-8", "replace"
        )
        for secret in (TOKEN_CANARY, TITLE_CANARY, PAGE_URL, "api.notion.com"):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, streams)

    def test_no_configured_token_is_no_origin_verifier_on_the_real_cli(self) -> None:
        result, completed = self.run_notion(
            mappings=[self.mapping_document(), self.notion_mapping_document()]
        )
        entry = result["evidence"][0]
        self.assertEqual(entry["document_ref"], NOTION_REF)
        self.assertEqual(entry["source_type"], "notion.page")
        self.assertEqual(entry["status"], "unverifiable")
        self.assertEqual(entry["code"], "no_origin_verifier")
        self.assertIsNone(entry["observed_source_version"])
        self.assertNotIn(TOKEN_ENVIRONMENT_VARIABLE, self.child_environment(None))
        self.assertNoNotionDisclosure(completed)

    def test_a_revoked_notion_mapping_answers_without_reading_the_token(self) -> None:
        token_file = self.root / "notion-token.txt"
        token_file.write_text(TOKEN_CANARY + "\n", encoding="ascii")
        result, completed = self.run_notion(
            mappings=[self.notion_mapping_document(revoked=True)],
            token=str(token_file),
        )
        entry = result["evidence"][0]
        self.assertEqual(entry["status"], "revoked")
        self.assertEqual(entry["code"], "mapping_revoked")
        self.assertIsNone(entry["observed_source_version"])
        self.assertNoNotionDisclosure(completed)
        self.assertNotIn(str(token_file), completed.stdout.decode("utf-8"))

    def test_an_unmapped_notion_page_is_refused_on_the_real_cli(self) -> None:
        result, completed = self.run_notion(mappings=[self.mapping_document()])
        entry = result["evidence"][0]
        self.assertEqual(entry["status"], "refused")
        self.assertEqual(entry["code"], "source_refused")
        self.assertNoNotionDisclosure(completed)


class NotionExpiryOracleTests(NotionMappingMixin, VerifierProcessCase):
    """R24-CLOCK-ORACLE-01: the clock moves *during* the observation.

    The two protocol tests above hand ``validate_verification_result`` a
    ``checked_at`` the test itself chose, so they cannot show what the child
    does when an observation genuinely outlives its request. This one drives
    the child's own ``_run`` with a clock that only advances because the Notion
    transport spent time, and reads the whole outcome off that.
    """

    def setUp(self) -> None:
        super().setUp()
        self.token_file = self.root / "notion-token.txt"
        self.token_file.write_text(TOKEN_CANARY, encoding="ascii")
        self.set_environment(TOKEN_ENVIRONMENT_VARIABLE, str(self.token_file))
        self.set_environment(
            CONFIG_ENVIRONMENT_VARIABLE,
            str(self.write_config(mappings=[self.notion_mapping_document()])),
        )

    def set_environment(self, name: str, value: str) -> None:
        previous = os.environ.get(name)

        def restore() -> None:
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous

        self.addCleanup(restore)
        os.environ[name] = value

    def patch(self, name: str, value: object) -> None:
        previous = getattr(source_verifier_main, name)
        self.addCleanup(setattr, source_verifier_main, name, previous)
        setattr(source_verifier_main, name, value)

    def run_child_in_process(self, *, cost: float) -> bytes:
        """One ``_run`` whose only clock source is the time the wire cost.

        Both existing seams are used as they stand: the module-level
        ``NotionBatch`` the observation constructs, and the module-level
        ``utc_now_rfc3339`` it reads before and after. Whole seconds are what
        the RFC3339 clock has ever exposed, so the wall reading is derived from
        the same fake monotonic clock the transport advances.
        """

        clock = FakeClock()
        transport = RecordingTransport(
            [Wire(200, encoded_page())], clock=clock, cost=cost
        )
        self.patch(
            "NotionBatch",
            lambda: NotionBatch(
                _transport=transport, _clock=clock.read, _sleep=clock.sleep
            ),
        )
        self.patch(
            "utc_now_rfc3339", lambda: _later(self.now, int(clock.now))
        )
        self.transport = transport
        document = self.request_document(
            corpus_refs=[OTHER_GRANT],
            evidence=[
                {
                    "document_ref": NOTION_REF,
                    "source_type": "notion.page",
                    "expected_source_version": None,
                }
            ],
        )
        return source_verifier_main._run(json.dumps(document).encode("utf-8"))

    def test_a_slow_observation_is_refused_rather_than_backdated(self) -> None:
        # Two seconds on the wire: inside the batch budget and inside the
        # sixty-second request window. The reported instant is the one the
        # observation actually finished at, not the one it started at.
        admitted = json.loads(self.run_child_in_process(cost=2.0).decode("utf-8"))
        self.assertEqual(admitted["checked_at"], _later(self.now, 2))
        self.assertNotEqual(admitted["checked_at"], self.now)
        self.assertEqual(
            (admitted["evidence"][0]["status"], admitted["evidence"][0]["code"]),
            ("unverifiable", "no_expected_version"),
        )

        # Sixty-one seconds: the batch discards its own late answer, and the
        # completion instant is at or past ``expires_at``. Backdating the
        # result to the admission instant would have let it through.
        with self.assertRaises(source_verifier_main._Refused) as raised:
            self.run_child_in_process(cost=61.0)
        self.assertEqual(raised.exception.code, "verifier_output_refused")
        self.assertEqual(len(self.transport.calls), 1)


if __name__ == "__main__":  # pragma: no cover - direct invocation
    unittest.main()
