"""The Notion access observation, against an injected transport and clock only.

No token, network, socket, live Notion call, connector or installed SDK is used
anywhere in this file. Every "response" is bytes this module wrote, handed to
the private transport seam; the production transport is exercised against a
recorder substituted into the module namespace, which proves the pinned request
without opening a connection.

The canaries matter as much as the statuses: a synthetic token, its file path,
a page title and a ``last_edited_time`` all travel through these tests, and
none of them may appear in an observation, a ``repr`` or a stream.
"""

from __future__ import annotations

import http.client
import os
import socket
import ssl
import unittest
from pathlib import Path

from integrations.opendocuments.source_access import SourceMapping
from integrations.opendocuments.source_verifier_notion import (
    BATCH_BUDGET_SECONDS,
    MAX_CALL_TIMEOUT_SECONDS,
    MAX_RESPONSE_BYTES,
    MAX_TOKEN_BYTES,
    MIN_REQUEST_INTERVAL_SECONDS,
    NOTION_HOST,
    NOTION_PATH_PREFIX,
    NOTION_PORT,
    NOTION_VERSION,
    TOKEN_ENVIRONMENT_VARIABLE,
    NotionBatch,
    canonical_page_uuid,
    observe_notion_mapping,
)
from test_od_notion_fixtures import (
    NOTION_REF,
    PAGE_UUID,
    PAGE_URL,
    TITLE_CANARY,
    TOKEN_CANARY,
    FakeClock,
    RecordingTransport,
    Wire,
    encode_document,
    encoded_page,
    page_object,
)


class NotionCase(unittest.TestCase):
    """One temporary operator directory, one synthetic token, no network."""

    def setUp(self) -> None:
        import tempfile

        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.token_file = self.root / "notion-token.txt"
        self.clock = FakeClock()

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

    def write_token(self, raw: bytes = None) -> Path:
        self.token_file.write_bytes(
            (TOKEN_CANARY + "\n").encode("ascii") if raw is None else raw
        )
        self.set_token_environment(str(self.token_file))
        return self.token_file

    def mapping(self, **overrides: object) -> SourceMapping:
        fields: dict[str, object] = {
            "document_id": NOTION_REF,
            "corpus": "notion-handbook",
            "backend": "notion",
            "page_url": PAGE_URL,
            "revoked": False,
        }
        fields.update(overrides)
        return SourceMapping(**fields)  # type: ignore[arg-type]

    def observe(
        self,
        *,
        wires: list = None,
        expected: object = None,
        in_request_corpus: bool = True,
        mapping: SourceMapping = None,
        cost: float = 0.0,
    ) -> tuple[tuple[str, str], RecordingTransport]:
        transport = RecordingTransport(wires or [], clock=self.clock, cost=cost)
        batch = NotionBatch(
            _transport=transport, _clock=self.clock.read, _sleep=self.clock.sleep
        )
        answer = observe_notion_mapping(
            self.mapping() if mapping is None else mapping,
            in_request_corpus=in_request_corpus,
            expected_source_version=expected,
            batch=batch,
        )
        return answer, transport


class PageSelectorTests(unittest.TestCase):
    """The operator URL supplies one thing: the terminal page UUID."""

    def test_slug_ending_in_thirty_two_hex_yields_the_canonical_uuid(self) -> None:
        self.assertEqual(canonical_page_uuid(PAGE_URL), PAGE_UUID)

    def test_bare_thirty_two_hex_segment_is_accepted(self) -> None:
        url = "https://www.notion.so/1f2e3d4c5b6a70819243a5b6c7d8e9f0"
        self.assertEqual(canonical_page_uuid(url), PAGE_UUID)

    def test_hyphenated_uuid_segment_is_accepted_and_lowercased(self) -> None:
        url = "https://www.notion.so/1F2E3D4C-5B6A-7081-9243-A5B6C7D8E9F0"
        self.assertEqual(canonical_page_uuid(url), PAGE_UUID)

    def test_nested_workspace_path_uses_the_terminal_segment(self) -> None:
        url = (
            "https://acme-team.notion.site/engineering/"
            "Runbook-1f2e3d4c5b6a70819243a5b6c7d8e9f0"
        )
        self.assertEqual(canonical_page_uuid(url), PAGE_UUID)

    def test_a_host_is_never_read_as_a_page_id(self) -> None:
        # The id lives in the path. Splitting the whole URL would let a host
        # label stand in for a page.
        self.assertIsNone(canonical_page_uuid("https://www.notion.so"))
        self.assertIsNone(canonical_page_uuid("https://www.notion.so/"))

    def test_paths_without_a_page_id_are_refused(self) -> None:
        for path in (
            "/workspace",
            "/Quarterly-Handbook",
            "/1f2e3d4c5b6a70819243a5b6c7d8e9f",  # 31 hex
            "/1f2e3d4c5b6a70819243a5b6c7d8e9f0a",  # 33 hex
            "/1f2e3d4c-5b6a-7081-9243-a5b6c7d8e9f",  # short final group
            "/page/",
        ):
            with self.subTest(path=path):
                self.assertIsNone(canonical_page_uuid("https://www.notion.so" + path))

    def test_the_released_host_allow_list_still_applies(self) -> None:
        for url in (
            "http://www.notion.so/Page-1f2e3d4c5b6a70819243a5b6c7d8e9f0",
            "https://notion.so.evil.example/Page-1f2e3d4c5b6a70819243a5b6c7d8e9f0",
            "https://www.notion.so:8443/Page-1f2e3d4c5b6a70819243a5b6c7d8e9f0",
            "https://user@www.notion.so/Page-1f2e3d4c5b6a70819243a5b6c7d8e9f0",
            "https://www.notion.so/Page-1f2e3d4c5b6a70819243a5b6c7d8e9f0?v=1",
            "https://www.notion.so/Page-1f2e3d4c5b6a70819243a5b6c7d8e9f0#block",
            PAGE_UUID,
            None,
            17,
        ):
            with self.subTest(url=url):
                self.assertIsNone(canonical_page_uuid(url))


class TokenTests(NotionCase):
    """Not configured and configured-but-broken are different public facts."""

    def test_unset_token_is_no_origin_verifier_and_never_reaches_the_wire(self) -> None:
        self.set_token_environment(None)
        answer, transport = self.observe()
        self.assertEqual(answer, ("unverifiable", "no_origin_verifier"))
        self.assertEqual(transport.calls, [])

    def test_empty_token_variable_is_no_origin_verifier(self) -> None:
        self.set_token_environment("")
        answer, transport = self.observe()
        self.assertEqual(answer, ("unverifiable", "no_origin_verifier"))
        self.assertEqual(transport.calls, [])

    def test_unusable_token_files_are_verification_unavailable(self) -> None:
        cases = {
            "relative": lambda: self.set_token_environment("notion-token.txt"),
            "absent": lambda: self.set_token_environment(str(self.root / "gone.txt")),
            "directory": lambda: self.set_token_environment(str(self.root)),
            "empty": lambda: self.write_token(b""),
            "whitespace_only": lambda: self.write_token(b"   \n"),
            "oversize": lambda: self.write_token(b"n" * (MAX_TOKEN_BYTES + 1)),
            "non_ascii": lambda: self.write_token("ntn_éééé".encode()),
            "embedded_space": lambda: self.write_token(b"ntn_ secret value"),
            "too_short": lambda: self.write_token(b"ntn_1"),
            "control_byte": lambda: self.write_token(b"ntn_secret\x07value"),
        }
        for name, prepare in cases.items():
            with self.subTest(token=name):
                prepare()
                answer, transport = self.observe()
                self.assertEqual(answer, ("unverifiable", "verification_unavailable"))
                self.assertEqual(transport.calls, [])

    def test_a_valid_token_is_trimmed_and_handed_to_the_transport_intact(self) -> None:
        self.write_token(("  " + TOKEN_CANARY + "\r\n").encode("ascii"))
        answer, transport = self.observe(wires=[Wire(200, encoded_page())])
        self.assertEqual(answer, ("unverifiable", "no_expected_version"))
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(transport.calls[0].token, TOKEN_CANARY)


class WireOutcomeTests(NotionCase):
    """The HTTP half of the R24 matrix, one row at a time."""

    def setUp(self) -> None:
        super().setUp()
        self.write_token()

    def answer(self, wire, expected: object = None) -> tuple[str, str]:
        answer, transport = self.observe(wires=[wire], expected=expected)
        self.assertEqual(len(transport.calls), 1)
        return answer

    def test_readable_page_without_an_expectation_is_no_expected_version(self) -> None:
        self.assertEqual(
            self.answer(Wire(200, encoded_page())),
            ("unverifiable", "no_expected_version"),
        )

    def test_readable_page_with_an_expectation_never_becomes_current(self) -> None:
        # The page object carries a ``last_edited_time``. R22's rejected table
        # would have compared it; this slice must not, whatever it says.
        for expected in ("od-origin-7f3ba1d34f50c884", "2026-05-06T07:08:00.000Z"):
            with self.subTest(expected=expected):
                self.assertEqual(
                    self.answer(Wire(200, encoded_page()), expected=expected),
                    ("unverifiable", "verification_unavailable"),
                )

    def test_archived_and_trashed_pages_are_refused(self) -> None:
        for field in ("archived", "in_trash"):
            with self.subTest(field=field):
                self.assertEqual(
                    self.answer(Wire(200, encoded_page(**{field: True}))),
                    ("refused", "source_refused"),
                )

    def test_absent_archive_flags_do_not_block_a_readable_page(self) -> None:
        page = page_object()
        del page["archived"]
        del page["in_trash"]
        self.assertEqual(
            self.answer(Wire(200, encode_document(page))),
            ("unverifiable", "no_expected_version"),
        )

    def test_unrelated_evolving_fields_are_tolerated(self) -> None:
        self.assertEqual(
            self.answer(Wire(200, encoded_page(public_url=None, request_id="r-1"))),
            ("unverifiable", "no_expected_version"),
        )

    def test_invalid_page_payloads_are_verification_unavailable(self) -> None:
        oversize = b'{"object":"page","pad":"' + b"a" * MAX_RESPONSE_BYTES + b'"}'
        cases = {
            "wrong_object": encoded_page(object="database"),
            "mismatched_id": encoded_page(id="00000000-0000-4000-8000-000000000001"),
            "unparsable_id": encoded_page(id="not-a-uuid"),
            "null_id": encoded_page(id=None),
            "non_boolean_archived": encoded_page(archived="true"),
            "non_boolean_in_trash": encoded_page(in_trash=1),
            "truncated": encoded_page()[:-9],
            "not_json": b"<html>gateway</html>",
            "empty": b"",
            "array": b"[]",
            "oversize": oversize,
        }
        self.assertGreater(len(oversize), MAX_RESPONSE_BYTES)
        for name, body in cases.items():
            with self.subTest(body=name):
                self.assertEqual(
                    self.answer(Wire(200, body)),
                    ("unverifiable", "verification_unavailable"),
                )

    def test_a_body_at_the_bound_is_still_read(self) -> None:
        head = encoded_page()
        padding = MAX_RESPONSE_BYTES - len(head) - len(b',"pad":""')
        body = encoded_page(pad="a" * padding)
        self.assertEqual(len(body), MAX_RESPONSE_BYTES)
        self.assertEqual(
            self.answer(Wire(200, body)), ("unverifiable", "no_expected_version")
        )

    def test_unauthorized_and_forbidden_are_access_denied(self) -> None:
        for status in (401, 403):
            with self.subTest(status=status):
                self.assertEqual(
                    self.answer(Wire(status, b'{"object":"error"}')),
                    ("denied", "access_denied"),
                )

    def test_not_found_redirect_and_other_client_errors_are_refused(self) -> None:
        # A Notion 404 means "absent OR invisible to this integration". Calling
        # it ``file_absent`` would report a deletion that may not have happened,
        # and ``mapping_revoked`` is the operator's word, never the origin's.
        for status in (301, 302, 303, 307, 308, 400, 404, 409, 410, 422):
            with self.subTest(status=status):
                answer = self.answer(Wire(status, b'{"object":"error"}'))
                self.assertEqual(answer, ("refused", "source_refused"))
                self.assertNotIn(answer[1], ("file_absent", "mapping_revoked"))

    def test_rate_limit_and_server_errors_are_root_unavailable(self) -> None:
        for status in (429, 500, 502, 503, 504):
            with self.subTest(status=status):
                self.assertEqual(
                    self.answer(Wire(status, b"")), ("unavailable", "root_unavailable")
                )

    def test_unexpected_statuses_stay_unverifiable(self) -> None:
        for status in (100, 201, 204):
            with self.subTest(status=status):
                self.assertEqual(
                    self.answer(Wire(status, b"")),
                    ("unverifiable", "verification_unavailable"),
                )

    def test_transport_failures_are_root_unavailable_and_echo_nothing(self) -> None:
        for error in (
            socket.timeout("timed out"),
            ssl.SSLError("certificate verify failed"),
            ConnectionResetError("reset by peer"),
            OSError("unreachable " + TOKEN_CANARY),
        ):
            with self.subTest(error=type(error).__name__):
                answer, transport = self.observe(wires=[error])
                self.assertEqual(answer, ("unavailable", "root_unavailable"))
                self.assertNotIn(TOKEN_CANARY, repr(answer))

    def test_no_automatic_retry_after_a_failure(self) -> None:
        answer, transport = self.observe(wires=[socket.timeout("timed out")])
        self.assertEqual(answer, ("unavailable", "root_unavailable"))
        self.assertEqual(len(transport.calls), 1)


class GateOrderTests(NotionCase):
    """Policy answers first, so no secret is opened and no socket is dialled.

    Each case deliberately points the token variable at a *malformed* token
    file. If the implementation read the token before deciding, the answer
    would collapse to ``verification_unavailable``; the specific status
    surviving is the proof that the read never happened.
    """

    def setUp(self) -> None:
        super().setUp()
        self.write_token(b"ntn_ malformed with spaces")

    def test_revocation_precedes_the_token_and_the_network(self) -> None:
        answer, transport = self.observe(mapping=self.mapping(revoked=True))
        self.assertEqual(answer, ("revoked", "mapping_revoked"))
        self.assertEqual(transport.calls, [])

    def test_out_of_request_corpus_precedes_the_token_and_the_network(self) -> None:
        answer, transport = self.observe(in_request_corpus=False)
        self.assertEqual(answer, ("refused", "source_refused"))
        self.assertEqual(transport.calls, [])

    def test_an_unusable_page_selector_precedes_the_token(self) -> None:
        mapping = self.mapping(page_url="https://www.notion.so/workspace")
        answer, transport = self.observe(mapping=mapping)
        self.assertEqual(answer, ("refused", "source_refused"))
        self.assertEqual(transport.calls, [])

    def test_a_revoked_out_of_corpus_mapping_still_refuses(self) -> None:
        answer, transport = self.observe(
            mapping=self.mapping(revoked=True), in_request_corpus=False
        )
        self.assertEqual(answer, ("refused", "source_refused"))
        self.assertEqual(transport.calls, [])


class BatchBudgetTests(NotionCase):
    """One request, one rate limit, one wall budget, one per-call timeout."""

    def setUp(self) -> None:
        super().setUp()
        self.write_token()

    def run_batch(self, count: int, *, cost: float) -> tuple[list, RecordingTransport]:
        transport = RecordingTransport(
            [Wire(200, encoded_page())] * count, clock=self.clock, cost=cost
        )
        batch = NotionBatch(
            _transport=transport, _clock=self.clock.read, _sleep=self.clock.sleep
        )
        answers = [
            observe_notion_mapping(
                self.mapping(),
                in_request_corpus=True,
                expected_source_version=None,
                batch=batch,
            )
            for _ in range(count)
        ]
        return answers, transport

    def test_ten_entries_are_spaced_by_the_minimum_interval(self) -> None:
        answers, transport = self.run_batch(10, cost=0.01)
        self.assertEqual(len(transport.calls), 10)
        self.assertEqual(
            answers, [("unverifiable", "no_expected_version")] * 10
        )
        self.assertEqual(len(self.clock.sleeps), 9)
        for gap in self.clock.sleeps:
            self.assertGreater(gap, 0.0)
            self.assertLessEqual(gap, MIN_REQUEST_INTERVAL_SECONDS)
        issued = [call.at for call in transport.calls]
        for earlier, later in zip(issued, issued[1:]):
            self.assertGreaterEqual(
                round(later - earlier, 6), MIN_REQUEST_INTERVAL_SECONDS
            )

    def test_a_slow_call_never_exceeds_the_per_call_ceiling(self) -> None:
        _, transport = self.run_batch(2, cost=0.01)
        for call in transport.calls:
            self.assertLessEqual(call.timeout, MAX_CALL_TIMEOUT_SECONDS)
            self.assertGreater(call.timeout, 0.0)

    def build(self, wires: list, *, cost: float = 0.0):
        """One batch a test drives call by call, with the clock in its hand."""

        transport = RecordingTransport(wires, clock=self.clock, cost=cost)
        batch = NotionBatch(
            _transport=transport, _clock=self.clock.read, _sleep=self.clock.sleep
        )
        return batch, transport

    def once(self, batch) -> tuple[str, str]:
        return observe_notion_mapping(
            self.mapping(),
            in_request_corpus=True,
            expected_source_version=None,
            batch=batch,
        )

    def test_an_exhausted_batch_budget_refuses_before_the_wire(self) -> None:
        batch, transport = self.build([Wire(200, encoded_page())])
        first = self.once(batch)
        self.clock.advance(BATCH_BUDGET_SECONDS)
        second = self.once(batch)
        self.assertEqual(first, ("unverifiable", "no_expected_version"))
        self.assertEqual(second, ("unavailable", "root_unavailable"))
        self.assertEqual(len(transport.calls), 1)

    def test_a_response_after_its_deadline_is_not_a_timely_observation(self) -> None:
        """A socket timeout bounds one read, not the exchange.

        The transport here *succeeds* -- it returns a perfectly valid page --
        but it takes longer than the deadline the batch granted it. Accepting
        that answer would report an observation from outside the window the
        request authorised, so the batch discards it.
        """

        batch, transport = self.build(
            [Wire(200, encoded_page())], cost=MAX_CALL_TIMEOUT_SECONDS + 1.0
        )
        self.assertEqual(self.once(batch), ("unavailable", "root_unavailable"))
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(transport.calls[0].timeout, MAX_CALL_TIMEOUT_SECONDS)

    def test_a_response_inside_its_deadline_is_accepted(self) -> None:
        batch, transport = self.build(
            [Wire(200, encoded_page())], cost=MAX_CALL_TIMEOUT_SECONDS - 0.1
        )
        self.assertEqual(self.once(batch), ("unverifiable", "no_expected_version"))
        self.assertEqual(len(transport.calls), 1)

    def test_the_deadline_shrinks_to_the_remaining_batch_budget(self) -> None:
        batch, transport = self.build([Wire(200, encoded_page())] * 2, cost=2.0)
        self.clock.advance(BATCH_BUDGET_SECONDS - 3.0)
        self.assertEqual(self.once(batch), ("unverifiable", "no_expected_version"))
        granted = transport.calls[0].timeout
        self.assertLess(granted, MAX_CALL_TIMEOUT_SECONDS)
        self.assertAlmostEqual(granted, 3.0, places=6)

    def test_overrunning_the_remaining_budget_is_late_not_current(self) -> None:
        batch, transport = self.build([Wire(200, encoded_page())], cost=4.0)
        self.clock.advance(BATCH_BUDGET_SECONDS - 3.0)
        # Three seconds left, a four second call: the answer arrives, and it
        # arrives too late to be this batch's observation.
        self.assertEqual(self.once(batch), ("unavailable", "root_unavailable"))
        self.assertAlmostEqual(transport.calls[0].timeout, 3.0, places=6)


class PausingClock(FakeClock):
    """A fake clock whose scheduler may pause *between* two reads.

    Each entry in ``pauses`` is wall time that elapses immediately after the
    matching ``read``. Nothing exotic is being modelled: this is a process
    losing its slice between two ordinary ``time.monotonic`` calls, which is
    why a budget decision assembled out of two samples that are *assumed* to
    be the same instant is not a budget decision at all.
    """

    def __init__(self, pauses: list) -> None:
        super().__init__()
        self._pauses = list(pauses)
        self.reads = 0

    def read(self) -> float:
        value = self.now
        self.reads += 1
        if self._pauses:
            self.now += self._pauses.pop(0)
        return value


class BatchDeadlineRaceTests(NotionCase):
    """R24-BUDGET-01: one issue sample, and an absolute batch edge.

    The batch budget is thirty seconds of wall clock, so an answer that
    completes at 30.21 s is not this batch's answer no matter which pair of
    clock readings the arithmetic happened to use.
    """

    def setUp(self) -> None:
        super().setUp()
        self.write_token()

    def build(self, pauses: list, wires: list, *, cost: float = 0.0):
        self.clock = PausingClock(pauses)
        transport = RecordingTransport(wires, clock=self.clock, cost=cost)
        batch = NotionBatch(
            _transport=transport, _clock=self.clock.read, _sleep=self.clock.sleep
        )
        return batch, transport

    def once(self, batch) -> tuple[str, str]:
        return observe_notion_mapping(
            self.mapping(),
            in_request_corpus=True,
            expected_source_version=None,
            batch=batch,
        )

    def test_a_pause_between_two_clock_reads_cannot_extend_the_batch(self) -> None:
        """The counterexample the two-sample version accepted.

        The batch is at 29.9 s when the call is admitted, so 0.1 s of budget
        remains. The scheduler then pauses 0.3 s before the request actually
        goes out, and a perfectly valid page comes back 0.01 s later -- at
        30.21 s, past the batch's 30.0 s edge.

        Code that derived the remainder at 29.9 s and *then* stamped the issue
        instant at 30.2 s granted this call a window ending at 30.30 s and
        accepted the answer, reporting a post-budget observation as the
        batch's own. One issue sample cannot do that: the window it grants
        ends at 30.0 s, the answer is late, and the reader is told the root
        did not answer in time.
        """

        batch, transport = self.build(
            [0.0, 0.3], [Wire(200, encoded_page())], cost=0.01
        )
        self.clock.advance(BATCH_BUDGET_SECONDS - 0.1)
        answer = self.once(batch)
        self.assertEqual(answer, ("unavailable", "root_unavailable"))
        # The request itself did go out: a scheduler can pause after any check,
        # and this module claims no atomic check-then-socket-start. What it
        # does claim is that the answer is never accepted.
        self.assertEqual(len(transport.calls), 1)
        self.assertAlmostEqual(transport.calls[0].timeout, 0.1, places=6)
        self.assertAlmostEqual(self.clock.now, BATCH_BUDGET_SECONDS + 0.21, places=6)

    def test_a_pause_before_admission_refuses_before_the_wire(self) -> None:
        """An exhausted batch is settled at its own admission sample."""

        batch, transport = self.build(
            [0.0, 0.0, 0.0, 0.5], [Wire(200, encoded_page())]
        )
        first = self.once(batch)
        self.clock.advance(BATCH_BUDGET_SECONDS - 0.2)
        # The spacing read is the one that crosses the edge, half a second
        # before the admission sample is even taken.
        second = self.once(batch)
        self.assertEqual(first, ("unverifiable", "no_expected_version"))
        self.assertEqual(second, ("unavailable", "root_unavailable"))
        self.assertEqual(len(transport.calls), 1)
        self.assertGreater(self.clock.now, BATCH_BUDGET_SECONDS)

    def test_a_modest_pause_leaves_a_timely_observation_timely(self) -> None:
        """The correction must not turn every pause into a refusal."""

        batch, transport = self.build(
            [0.0, 0.3], [Wire(200, encoded_page())], cost=0.5
        )
        self.clock.advance(20.0)
        self.assertEqual(self.once(batch), ("unverifiable", "no_expected_version"))
        self.assertEqual(len(transport.calls), 1)
        self.assertAlmostEqual(
            transport.calls[0].timeout, MAX_CALL_TIMEOUT_SECONDS, places=6
        )

    def test_the_granted_window_never_ends_past_the_batch_deadline(self) -> None:
        """Per-call ceiling and batch edge bound every call, whichever bites."""

        transport = RecordingTransport(
            [Wire(200, encoded_page())] * 3, clock=self.clock, cost=0.05
        )
        batch = NotionBatch(
            _transport=transport, _clock=self.clock.read, _sleep=self.clock.sleep
        )
        deadline = BATCH_BUDGET_SECONDS
        for offset in (0.0, BATCH_BUDGET_SECONDS - 8.0, 2.0):
            self.clock.advance(offset)
            self.once(batch)
        self.assertEqual(len(transport.calls), 3)
        for call in transport.calls:
            self.assertGreater(call.timeout, 0.0)
            self.assertLessEqual(call.timeout, MAX_CALL_TIMEOUT_SECONDS)
            # ``at`` is the issue sample itself here: this clock does not pause.
            self.assertLessEqual(round(call.at + call.timeout, 6), deadline)


class PinnedRequestTests(NotionCase):
    """Where the request goes is a constant, not a setting."""

    def test_the_endpoint_constants_are_the_frozen_ones(self) -> None:
        self.assertEqual(NOTION_HOST, "api.notion.com")
        self.assertEqual(NOTION_PORT, 443)
        self.assertEqual(NOTION_PATH_PREFIX, "/v1/pages/")
        self.assertEqual(NOTION_VERSION, "2025-09-03")

    def test_the_transport_receives_the_canonical_uuid_only(self) -> None:
        self.write_token()
        _, transport = self.observe(wires=[Wire(200, encoded_page())])
        self.assertEqual(transport.calls[0].page_uuid, PAGE_UUID)
        self.assertNotIn("Quarterly", transport.calls[0].page_uuid)

    def test_production_batches_use_the_stdlib_transport(self) -> None:
        import integrations.opendocuments.source_verifier_notion as notion

        batch = NotionBatch()
        self.assertIs(
            getattr(batch, "_transport"), getattr(notion, "_stdlib_transport")
        )
        self.assertEqual(repr(batch), "NotionBatch()")

    def test_the_module_reads_exactly_one_environment_variable(self) -> None:
        import integrations.opendocuments.source_verifier_notion as notion

        source = Path(notion.__file__).read_text(encoding="utf-8")
        self.assertEqual(source.count("os.environ"), 1)
        self.assertIn("os.environ.get(TOKEN_ENVIRONMENT_VARIABLE)", source)
        self.assertEqual(TOKEN_ENVIRONMENT_VARIABLE, "WORKSTACK_OD_NOTION_TOKEN_FILE")

    def test_the_stdlib_transport_issues_one_pinned_unredirected_get(self) -> None:
        import integrations.opendocuments.source_verifier_notion as notion

        original = http.client.HTTPSConnection
        http.client.HTTPSConnection = _ConnectionRecorder  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(http.client, "HTTPSConnection", original))
        wire = getattr(notion, "_stdlib_transport")(PAGE_UUID, TOKEN_CANARY, 4.5)
        recorder = _ConnectionRecorder.last
        assert recorder is not None
        self.assertEqual(wire.status, 200)
        self.assertEqual(recorder.host, NOTION_HOST)
        self.assertEqual(recorder.port, NOTION_PORT)
        self.assertEqual(recorder.timeout, 4.5)
        self.assertIsInstance(recorder.context, ssl.SSLContext)
        self.assertTrue(recorder.context.check_hostname)
        self.assertEqual(recorder.context.verify_mode, ssl.CERT_REQUIRED)
        self.assertEqual(recorder.method, "GET")
        self.assertEqual(recorder.path, NOTION_PATH_PREFIX + PAGE_UUID)
        self.assertEqual(recorder.headers["Host"], NOTION_HOST)
        self.assertEqual(recorder.headers["Notion-Version"], NOTION_VERSION)
        self.assertEqual(recorder.headers["Authorization"], "Bearer " + TOKEN_CANARY)
        self.assertEqual(recorder.read_limit, MAX_RESPONSE_BYTES + 1)
        self.assertTrue(recorder.closed)
        # ``HTTPSConnection`` hands a 3xx back as a status. There is no opener,
        # redirect handler or cookie jar anywhere in the module, so a redirect
        # is refused rather than followed.
        source = Path(notion.__file__).read_text(encoding="utf-8")
        for forbidden in ("urllib.request", "build_opener", "HTTPRedirectHandler"):
            self.assertNotIn(forbidden, source)


class _ConnectionRecorder:
    """A stand-in for ``HTTPSConnection`` that never opens a socket."""

    last: "_ConnectionRecorder | None" = None

    def __init__(self, host, port, timeout=None, context=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.context = context
        self.closed = False
        self.headers: dict = {}
        self.method = None
        self.path = None
        self.read_limit = None
        self.status = 200
        _ConnectionRecorder.last = self

    def request(self, method, path, headers=None):
        self.method = method
        self.path = path
        self.headers = dict(headers or {})

    def getresponse(self):
        return self

    def read(self, amount):
        self.read_limit = amount
        return encoded_page()

    def close(self):
        self.closed = True


class DisclosureTests(NotionCase):
    """Nothing secret and nothing about the page's content is a return value."""

    def test_no_canary_reaches_an_observation_or_a_repr(self) -> None:
        self.write_token()
        answer, transport = self.observe(wires=[Wire(200, encoded_page())])
        batch = NotionBatch(_transport=transport)
        rendered = repr(answer) + repr(batch) + repr(self.mapping())
        for secret in (
            TOKEN_CANARY,
            str(self.token_file),
            TITLE_CANARY,
            "last_edited_time",
            "2026-05-06T07:08:00.000Z",
        ):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, rendered)
        self.assertEqual(answer, ("unverifiable", "no_expected_version"))


if __name__ == "__main__":  # pragma: no cover - direct invocation
    unittest.main()
