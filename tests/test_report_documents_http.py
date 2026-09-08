"""Oracle HTTP tests for the authoritative report routes (§13, §16 batch B2).

Every case here runs against a real loopback `WorkStackHTTPServer` over a real
disposable `WorkStack` on a temporary directory: the routes are the registered
ones, the handler is the released one, the transaction is the released one and
`reports.json` is a real file that is really rewritten. No handler is
subclassed, no store is faked and no report service is stubbed, so a route
that stopped being registered, a mixin that stopped being composed or an
admission that stopped running would fail here rather than keep passing
against a local double.

Expectations are restated by hand from `docs/REPORT-DOCUMENT-STORAGE-CONTRACT.md`
rather than imported from the modules under test, except where the assertion
*is* about agreement between two modules — the status table's coverage of the
declared refusal codes, which is checked against the raisers' own message maps
on purpose.

The four questions this suite is built around:

1. Does the transport hand the service its inputs unchanged and answer with
   the service's own status and body — including a replay that must come back
   as recorded after the source day moved and later mutations landed?
2. Does every route refuse a request that is not this workspace's, is not
   carrying a well-formed retry identity, is not same-origin, or is not within
   the shipped content bounds — content-free, and without writing?
3. Do the nine other Store documents and the shipped daily preview survive a
   full report lifecycle byte-identically, so authoring a report never edits a
   source fact?
4. Does the generic report-UID route leave the two shipped preview reads
   alone?
"""

from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest import mock
from urllib.parse import urlencode

from workstack import report_documents_http as transport
from workstack import report_json, report_queries, report_repository_service
from workstack.capture import canonical_digest
from workstack.report_documents_http import REPORT_REFUSAL_STATUS
from workstack.reporting import MAX_MARKDOWN_CHARS, TEMPLATE_DAILY_V1
from workstack.server import (
    DEFAULT_BODY_LIMIT,
    IDEMPOTENT_POST_ROUTES,
    V1_GET_ROUTES,
    V1_POST_ROUTES,
    create_server,
)
from workstack.service import WorkStack
from workstack.store import DEFAULTS, Store


DAY = "2026-09-06"
OTHER_DAY = "2026-09-05"
GENERATED = "2026-09-06T11:00:00Z"
FROZEN_PREVIEW_NOW = "2026-09-06T11:59:00Z"
FOREIGN_WORKSPACE = "0f50a123-3da8-4c82-8f16-8ee1a57260c4"
NIL_WORKSPACE = "00000000-0000-0000-0000-000000000000"
UNKNOWN_REPORT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

# The nine documents a report write must never touch.
SOURCE_DOCUMENTS = tuple(sorted(name for name in DEFAULTS if name != "reports.json"))

# §13, restated. The list item carries no markdown and no staleness; the read
# adds the authored history and the staleness flag.
LIST_ITEM_KEYS = {
    "uid", "template", "period", "state", "revision", "content_revision",
    "source_digest", "archived_from_state", "created_at", "updated_at",
}
READ_KEYS = LIST_ITEM_KEYS | {"revisions", "source_stale"}
LIST_ENVELOPE_KEYS = {"workspace_uid", "reports", "omitted_count", "cursor"}

# §12, restated by hand. This is the whole declared taxonomy for these routes.
DECLARED_STATUS = {
    "invalid_query": 400,
    "workspace_mismatch": 409,
    "store_sync_required": 409,
    "idempotency_key_required": 400,
    "invalid_idempotency_key": 400,
    "idempotency_conflict": 409,
    "report_idempotency_capacity": 429,
    "report_revision_conflict": 409,
    "report_source_changed": 409,
    "report_not_found": 404,
    "report_state_invalid": 409,
    "report_duplicate_period": 409,
    "report_revision_limit": 409,
    "report_document_limit": 409,
    "report_storage_full": 409,
    "report_body_invalid": 400,
    "report_cursor_invalid": 400,
    "report_template_unsupported": 422,
    "report_capability_unavailable": 422,
}


class _ReportHttpCase(unittest.TestCase):
    """One disposable workspace, one real loopback server, one real handler."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.stack = WorkStack(Store(self.root))
        self.task = self.stack.add_task("Report HTTP")
        with self.stack.store.consistent_read() as readiness:
            self.workspace_uid = readiness.workspace_uid
        self.server = create_server(self.stack, "127.0.0.1", 0)
        self.port = self.server.actual_port
        self.host = "127.0.0.1:{}".format(self.port)
        self.csrf = self.server.csrf_token
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.keys = 0

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    # -- transport -------------------------------------------------------
    def next_key(self) -> str:
        self.keys += 1
        return "report-http-key-{:04d}".format(self.keys)

    def call(
        self,
        method: str,
        path: str,
        *,
        raw: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        sent = {"Host": self.host}
        sent.update(headers or {})
        connection.request(method, path, body=raw, headers=sent)
        response = connection.getresponse()
        body = response.read()
        status = response.status
        seen = {key.casefold(): value for key, value in response.getheaders()}
        connection.close()
        decoded = json.loads(body.decode("utf-8")) if body else {}
        return status, decoded, seen

    def get(
        self, path: str, headers: dict[str, str] | None = None
    ) -> tuple[int, dict[str, Any]]:
        status, body, _headers = self.call("GET", path, headers=headers)
        return status, body

    def post(
        self,
        path: str,
        body: object,
        *,
        key: str | None = None,
        headers: dict[str, str] | None = None,
        omit_csrf: bool = False,
        omit_origin: bool = False,
    ) -> tuple[int, dict[str, Any]]:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        sent = {"Content-Type": "application/json"}
        if not omit_origin:
            sent["Origin"] = "http://" + self.host
        if not omit_csrf:
            sent["X-WorkStack-CSRF"] = self.csrf
        if key is not None:
            sent["Idempotency-Key"] = key
        sent.update(headers or {})
        status, decoded, _headers = self.call("POST", path, raw=raw, headers=sent)
        return status, decoded

    # -- paths -----------------------------------------------------------
    def query(self, **overrides: object) -> str:
        values: dict[str, object] = {"workspace_uid": self.workspace_uid}
        values.update(overrides)
        return "?" + urlencode({k: v for k, v in values.items() if v is not None})

    def list_path(self, **overrides: object) -> str:
        return "/api/v1/reports" + self.query(**overrides)

    def read_path(self, report_uid: str, **overrides: object) -> str:
        return "/api/v1/reports/" + report_uid + self.query(**overrides)

    def action_path(self, report_uid: str, action: str, **overrides: object) -> str:
        return "/api/v1/reports/{}/{}".format(report_uid, action) + self.query(
            **overrides
        )

    # -- source ----------------------------------------------------------
    def source_digest(self, date: str = DAY) -> str:
        projection = self.stack.review_projection(date, 1)
        return canonical_digest({"date": date, "day": projection["day"]})

    def change_source(self, date: str = DAY, done: str = "Closed one gate") -> None:
        """Move the source day by writing one ordinary worklog entry."""

        self.stack.add_worklog_v1(
            {
                "date": date,
                "task_id": self.task["id"],
                "done": [done],
                "next": [],
                "blockers": [],
            },
            self.next_key(),
        )

    # -- bodies ----------------------------------------------------------
    def create_body(self, date: str = DAY, **overrides: object) -> dict[str, Any]:
        body: dict[str, Any] = {
            "workspace_uid": self.workspace_uid,
            "template": TEMPLATE_DAILY_V1,
            "period": {"kind": "day", "date": date},
            "source_digest": self.source_digest(date),
            "source_generated_at": GENERATED,
            "markdown": "# Report for {}".format(date),
        }
        body.update(overrides)
        return body

    def create(
        self, date: str = DAY, **overrides: object
    ) -> tuple[int, dict[str, Any]]:
        return self.post(
            self.list_path(), self.create_body(date, **overrides), key=self.next_key()
        )

    def created_report(self, date: str = DAY) -> dict[str, Any]:
        status, body = self.create(date)
        self.assertEqual(status, 201)
        return body["data"]

    # -- documents -------------------------------------------------------
    def source_bytes(self) -> dict[str, bytes]:
        return {name: (self.root / name).read_bytes() for name in SOURCE_DOCUMENTS}

    def reports_bytes(self) -> bytes:
        return (self.root / "reports.json").read_bytes()

    # -- assertions ------------------------------------------------------
    def assert_refusal(
        self,
        response: tuple[int, dict[str, Any]],
        code: str,
        *,
        field: str | None = None,
    ) -> dict[str, Any]:
        status, body = response
        self.assertEqual(set(body), {"error"})
        error = body["error"]
        self.assertEqual(set(error), {"code", "message", "details"})
        self.assertEqual(error["code"], code)
        self.assertEqual(status, DECLARED_STATUS.get(code, status))
        self.assertIsInstance(error["message"], str)
        self.assertNotEqual(error["message"], "")
        if field is not None:
            self.assertEqual(error["details"].get("field"), field)
        return error

    def assert_mutation_envelope(
        self, body: dict[str, Any], *, replayed: bool
    ) -> dict[str, Any]:
        self.assertEqual(set(body), {"data", "meta"})
        self.assertEqual(body["meta"], {"replayed": replayed})
        self.assertNotIn("revisions", body["data"])
        return body["data"]


class ReportRouteRegistrationTest(_ReportHttpCase):
    """The registration is real, and it does not take the previews' paths."""

    def test_routes_are_registered_with_the_report_handlers(self) -> None:
        get_handlers = {route.pattern.pattern: route.handler for route in V1_GET_ROUTES}
        self.assertEqual(
            get_handlers["/api/v1/reports"], "_get_report_documents"
        )
        self.assertEqual(
            get_handlers[
                "/api/v1/reports/(?!daily-preview$|weekly-preview$)([^/]+)"
            ],
            "_get_report_document",
        )
        post_handlers = {route.name: route.handler for route in V1_POST_ROUTES}
        self.assertEqual(post_handlers["report_create"], "_post_report_create")
        self.assertEqual(post_handlers["report_action"], "_post_report_action")

    def test_both_mutation_routes_are_idempotency_bearing(self) -> None:
        self.assertIn("report_create", IDEMPOTENT_POST_ROUTES)
        self.assertIn("report_action", IDEMPOTENT_POST_ROUTES)

    def test_the_uid_route_cannot_match_either_preview_segment(self) -> None:
        """Structural, not positional: the pattern itself excludes them."""

        pattern = next(
            route.pattern
            for route in V1_GET_ROUTES
            if route.handler == "_get_report_document"
        )
        self.assertIsNone(pattern.fullmatch("/api/v1/reports/daily-preview"))
        self.assertIsNone(pattern.fullmatch("/api/v1/reports/weekly-preview"))
        self.assertIsNotNone(pattern.fullmatch("/api/v1/reports/" + UNKNOWN_REPORT))

    def test_the_daily_preview_still_answers_its_own_route(self) -> None:
        status, body = self.get(
            "/api/v1/reports/daily-preview?"
            + urlencode(
                {
                    "date": DAY,
                    "template": TEMPLATE_DAILY_V1,
                    "workspace_uid": self.workspace_uid,
                }
            )
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            set(body["data"]), {"workspace_uid", "source_digest", "preview"}
        )

    def test_the_weekly_preview_still_answers_its_own_route(self) -> None:
        status, body = self.get(
            "/api/v1/reports/weekly-preview?"
            + urlencode(
                {
                    "end_date": DAY,
                    "template": "weekly-v1",
                    "workspace_uid": self.workspace_uid,
                }
            )
        )
        # The weekly preview owns its own admission; all that matters here is
        # that it is the module that answered, not the report-document read.
        self.assertNotEqual(status, 404)
        if status == 200:
            self.assertIn("preview", body["data"])
        else:
            self.assertNotIn(
                body["error"]["code"], {"report_not_found", "report_body_invalid"}
            )

    def test_an_unknown_report_subpath_is_not_found(self) -> None:
        status, body = self.get("/api/v1/reports/" + UNKNOWN_REPORT + "/history")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")


class ReportRouteAliasTest(_ReportHttpCase):
    """One report route has one spelling, and no other spelling is that route.

    `urlparse` splits a trailing `;params` run off the last path segment
    before the router is ever given the path, so `/api/v1/reports;shadow` and
    `/api/v1/reports` arrive at the route table as one string — and, on the
    two POSTs, would bind their receipts to one canonical `path`. That is the
    merge §13 refuses everywhere else, and it is worse than a merely accepted
    alias: the alias writes, and the canonical route replaying the same
    `Idempotency-Key` afterwards is answered about a request that was never
    sent to it.

    So every case here asks the same three things of an alias: is it refused
    before any handler, did it leave `reports.json` byte-identical, and is its
    key still free for the canonical route to spend on a *fresh* mutation. The
    last one is the load-bearing question — a refusal that had still recorded
    a receipt would answer the canonical request `replayed: true`.
    """

    # Every spelling `urlparse` folds into the canonical path: a named
    # parameter, an empty one, one carrying a value, and two in a row.
    ALIASES = (";shadow", ";", ";a=b", ";one;two")

    # The same question for the other delimiter `urlparse` folds away. These
    # go after the query, which is the position the path comparison alone
    # cannot see: `urlparse` cuts the fragment off before it splits the query,
    # so both sides of that comparison lose it at once and the target reads as
    # canonical while the route, the decoded query and the receipt `path` all
    # already have the fragment gone.
    FRAGMENTS = ("#shadow", "#", "#a=b", "#/api/v1/reports")

    # Every mutation route the guard covers, with a body the canonical route
    # would accept on a draft report at revision 1. The alias is refused
    # before any of them is read, so the bodies exist to make the refusal a
    # statement about the spelling and not about the content.
    ACTION_BODIES = {
        "revisions": {"expected_revision": 1, "markdown": "# Shadowed", "note": None},
        "finalize": {"expected_revision": 1},
        "archive": {"expected_revision": 1, "note": None},
        "restore": {"expected_revision": 1},
    }

    def alias_of(self, path: str, suffix: str) -> str:
        """The same route, respelt: the suffix goes on the path, not the query."""

        head, _separator, query = path.partition("?")
        return head + suffix + ("?" + query if query else "")

    def fragment_after_query(self, path: str, suffix: str) -> str:
        """The same route, respelt with the fragment behind the whole query."""

        return path + suffix

    def action_body(self, action: str) -> dict[str, Any]:
        body: dict[str, Any] = {"workspace_uid": self.workspace_uid}
        body.update(self.ACTION_BODIES[action])
        return body

    def assert_not_a_route(self, response: tuple[int, dict[str, Any]]) -> None:
        """The dispatcher's own unknown-route answer, content-free."""

        status, body = response
        self.assertEqual(status, 404)
        self.assertEqual(
            body,
            {
                "error": {
                    "code": "not_found",
                    "message": "API endpoint not found",
                    "details": {},
                }
            },
        )

    # -- reads -----------------------------------------------------------
    def test_the_list_route_refuses_every_path_parameter_spelling(self) -> None:
        self.created_report()
        for suffix in self.ALIASES:
            with self.subTest(suffix=suffix):
                self.assert_not_a_route(
                    self.get(self.alias_of(self.list_path(), suffix))
                )
        self.assertEqual(self.get(self.list_path())[0], 200)

    def test_the_read_route_refuses_every_path_parameter_spelling(self) -> None:
        uid = self.created_report()["uid"]
        for suffix in self.ALIASES:
            with self.subTest(suffix=suffix):
                self.assert_not_a_route(
                    self.get(self.alias_of(self.read_path(uid), suffix))
                )
        status, body = self.get(self.read_path(uid))
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["uid"], uid)

    # -- writes ----------------------------------------------------------
    def test_the_create_route_refuses_a_path_parameter_spelling_unwritten(self) -> None:
        before = self.reports_bytes()
        for suffix in self.ALIASES:
            with self.subTest(suffix=suffix):
                self.assert_not_a_route(
                    self.post(
                        self.alias_of(self.list_path(), suffix),
                        self.create_body(),
                        key=self.next_key(),
                    )
                )
        self.assertEqual(self.reports_bytes(), before)
        self.assertEqual(self.get(self.list_path())[1]["data"]["reports"], [])

    def test_every_action_route_refuses_a_path_parameter_spelling_unwritten(
        self,
    ) -> None:
        uid = self.created_report()["uid"]
        before = self.reports_bytes()
        for action in self.ACTION_BODIES:
            for suffix in self.ALIASES:
                with self.subTest(action=action, suffix=suffix):
                    self.assert_not_a_route(
                        self.post(
                            self.alias_of(self.action_path(uid, action), suffix),
                            self.action_body(action),
                            key=self.next_key(),
                        )
                    )
        self.assertEqual(self.reports_bytes(), before)

    # -- the receipt the alias must not have taken ------------------------
    def test_an_alias_create_leaves_its_key_free_for_the_canonical_route(self) -> None:
        """The point of the guard: a refused alias records nothing to replay."""

        key = self.next_key()
        self.assert_not_a_route(
            self.post(
                self.alias_of(self.list_path(), ";shadow"),
                self.create_body(),
                key=key,
            )
        )
        status, body = self.post(self.list_path(), self.create_body(), key=key)
        self.assertEqual(status, 201)
        data = self.assert_mutation_envelope(body, replayed=False)
        self.assertEqual(data["revision"], 1)
        self.assertEqual(
            [item["uid"] for item in self.get(self.list_path())[1]["data"]["reports"]],
            [data["uid"]],
        )

    def test_an_alias_action_leaves_its_key_free_for_the_canonical_route(self) -> None:
        """The alias is refused on a report the canonical route then finalizes fresh."""

        uid = self.created_report()["uid"]
        key = self.next_key()
        transition = {"workspace_uid": self.workspace_uid, "expected_revision": 1}
        self.assert_not_a_route(
            self.post(
                self.alias_of(self.action_path(uid, "finalize"), ";shadow"),
                transition,
                key=key,
            )
        )
        self.assertEqual(self.get(self.read_path(uid))[1]["data"]["state"], "draft")
        status, body = self.post(self.action_path(uid, "finalize"), transition, key=key)
        self.assertEqual(status, 200)
        data = self.assert_mutation_envelope(body, replayed=False)
        self.assertEqual(data["state"], "finalized")
        self.assertEqual(data["revision"], 2)

    # -- spellings that are not path parameters ---------------------------
    def test_an_encoded_semicolon_is_not_a_report_route(self) -> None:
        """`%3B` stays in the path, so it never reaches a route at all."""

        self.assert_not_a_route(self.get("/api/v1/reports%3Bshadow" + self.query()))
        self.assert_not_a_route(
            self.post(
                "/api/v1/reports%3Bshadow" + self.query(),
                self.create_body(),
                key=self.next_key(),
            )
        )
        uid = self.created_report()["uid"]
        self.assert_not_a_route(
            self.post(
                "/api/v1/reports/{}/finalize%3Bshadow".format(uid) + self.query(),
                {"workspace_uid": self.workspace_uid, "expected_revision": 1},
                key=self.next_key(),
            )
        )

    def test_an_encoded_semicolon_in_the_uid_reaches_the_service_refusal(self) -> None:
        """The `{uid}` segment is not percent-decoded, so this is one more uid.

        This is the module's documented behaviour and it is unchanged: the
        alias guard has nothing to say about a segment `urlparse` did not
        touch, and the identity check one layer down refuses it.
        """

        uid = self.created_report()["uid"]
        before = self.reports_bytes()
        self.assert_refusal(
            self.get(self.read_path(uid + "%3Bshadow")),
            "invalid_query",
            field="report_uid",
        )
        self.assertEqual(self.reports_bytes(), before)

    def test_a_semicolon_inside_the_uid_segment_is_refused_without_writing(
        self,
    ) -> None:
        """`urlparse` only splits the last segment, so this uid arrives intact."""

        uid = self.created_report()["uid"]
        before = self.reports_bytes()
        key = self.next_key()
        self.assert_refusal(
            self.post(
                "/api/v1/reports/{};shadow/finalize".format(uid) + self.query(),
                {"workspace_uid": self.workspace_uid, "expected_revision": 1},
                key=key,
            ),
            "report_body_invalid",
            field="report_uid",
        )
        self.assertEqual(self.reports_bytes(), before)
        status, body = self.post(
            self.action_path(uid, "finalize"),
            {"workspace_uid": self.workspace_uid, "expected_revision": 1},
            key=key,
        )
        self.assertEqual(status, 200)
        self.assert_mutation_envelope(body, replayed=False)

    def test_a_fragment_or_an_absolute_form_target_is_not_a_report_route(self) -> None:
        """Both are spellings `urlparse` folds away, and both are refused."""

        self.assert_not_a_route(self.get("/api/v1/reports#shadow" + self.query()))
        self.assert_not_a_route(self.get("http://" + self.host + self.list_path()))
        before = self.reports_bytes()
        self.assert_not_a_route(
            self.post(
                "http://" + self.host + self.list_path(),
                self.create_body(),
                key=self.next_key(),
            )
        )
        self.assertEqual(self.reports_bytes(), before)

    # -- the fragment that hides behind the query -------------------------
    def test_a_fragment_after_the_query_is_not_a_report_route_on_either_read(
        self,
    ) -> None:
        """The position the path comparison is blind to, asked on both reads.

        A fragment written *ahead* of the query survives into the path and is
        caught by the lossless comparison. Written after it, `urlparse` has
        already cut it off both sides, so the target the router matched and
        the target as written compare equal — and the route would answer a
        spelling it never saw. Every position is one answer.
        """

        uid = self.created_report()["uid"]
        for suffix in self.FRAGMENTS:
            with self.subTest(suffix=suffix):
                self.assert_not_a_route(
                    self.get(self.fragment_after_query(self.list_path(), suffix))
                )
                self.assert_not_a_route(
                    self.get(self.fragment_after_query(self.read_path(uid), suffix))
                )
        self.assertEqual(self.get(self.list_path())[0], 200)
        self.assertEqual(self.get(self.read_path(uid))[1]["data"]["uid"], uid)

    def test_a_fragment_after_the_query_refuses_create_unwritten(self) -> None:
        before = self.reports_bytes()
        for suffix in self.FRAGMENTS:
            with self.subTest(suffix=suffix):
                self.assert_not_a_route(
                    self.post(
                        self.fragment_after_query(self.list_path(), suffix),
                        self.create_body(),
                        key=self.next_key(),
                    )
                )
        self.assertEqual(self.reports_bytes(), before)
        self.assertEqual(self.get(self.list_path())[1]["data"]["reports"], [])

    def test_a_fragment_after_the_query_refuses_every_action_unwritten(self) -> None:
        uid = self.created_report()["uid"]
        before = self.reports_bytes()
        for action in self.ACTION_BODIES:
            for suffix in self.FRAGMENTS:
                with self.subTest(action=action, suffix=suffix):
                    self.assert_not_a_route(
                        self.post(
                            self.fragment_after_query(
                                self.action_path(uid, action), suffix
                            ),
                            self.action_body(action),
                            key=self.next_key(),
                        )
                    )
        self.assertEqual(self.reports_bytes(), before)
        self.assertEqual(self.get(self.read_path(uid))[1]["data"]["state"], "draft")

    def test_a_fragment_create_leaves_its_key_free_for_the_canonical(
        self,
    ) -> None:
        """The load-bearing question, asked of the fragment spelling too."""

        key = self.next_key()
        self.assert_not_a_route(
            self.post(
                self.fragment_after_query(self.list_path(), "#shadow"),
                self.create_body(),
                key=key,
            )
        )
        status, body = self.post(self.list_path(), self.create_body(), key=key)
        self.assertEqual(status, 201)
        data = self.assert_mutation_envelope(body, replayed=False)
        self.assertEqual(data["revision"], 1)
        self.assertEqual(
            [item["uid"] for item in self.get(self.list_path())[1]["data"]["reports"]],
            [data["uid"]],
        )

    def test_a_fragment_action_leaves_its_key_free_on_every_action(self) -> None:
        """Each action's own key is refused, then spent fresh on that action.

        The four transitions run in the order the state machine allows, so
        each canonical call is a real mutation rather than a refusal that
        would have hidden a recorded receipt.
        """

        uid = self.created_report()["uid"]
        expected = {"revisions": 200, "finalize": 200, "archive": 200, "restore": 200}
        revision = 1
        for action, status_code in expected.items():
            with self.subTest(action=action):
                key = self.next_key()
                body = self.action_body(action)
                body["expected_revision"] = revision
                self.assert_not_a_route(
                    self.post(
                        self.fragment_after_query(
                            self.action_path(uid, action), "#shadow"
                        ),
                        body,
                        key=key,
                    )
                )
                status, answered = self.post(
                    self.action_path(uid, action), body, key=key
                )
                self.assertEqual(status, status_code)
                data = self.assert_mutation_envelope(answered, replayed=False)
                revision += 1
                self.assertEqual(data["revision"], revision)

    def test_an_encoded_hash_is_data_and_still_reaches_the_service(self) -> None:
        """`%23` is a value, not a fragment cut, so the guard leaves it alone.

        Both places a caller can write one are checked: inside a query value,
        where the query leaf refuses it as the cursor it is not, and inside
        the `{uid}` segment, which this module does not percent-decode and
        which comes back from the identity check as one more invalid uid. A
        `not_found` in either position would mean the guard had started
        refusing content instead of spelling.
        """

        uid = self.created_report()["uid"]
        before = self.reports_bytes()
        self.assert_refusal(
            self.get(self.list_path(cursor="#" * 80)),
            "report_cursor_invalid",
            field="cursor",
        )
        self.assert_refusal(
            self.get(self.read_path(uid + "%23shadow")),
            "invalid_query",
            field="report_uid",
        )
        self.assertEqual(self.reports_bytes(), before)

    # -- the scope of the guard -------------------------------------------
    def test_the_shipped_previews_keep_their_spelling_tolerance(self) -> None:
        """A scope boundary, recorded rather than endorsed.

        The two preview reads are frozen for this batch. They allocate no
        identity, bind no receipt and write nothing, so an alias of one of
        them cannot split a report in two — the defect this guard exists for
        cannot occur there, and nothing here justifies changing an endpoint
        outside the batch.
        """

        preview = "/api/v1/reports/daily-preview?" + urlencode(
            {
                "date": DAY,
                "template": TEMPLATE_DAILY_V1,
                "workspace_uid": self.workspace_uid,
            }
        )
        self.assertEqual(self.get(preview)[0], 200)
        self.assertEqual(self.get(self.alias_of(preview, ";shadow"))[0], 200)

    def test_endpoints_outside_this_batch_keep_their_spelling_tolerance(self) -> None:
        self.assertEqual(self.get("/api/v1/health;shadow")[0], 200)
        self.assertEqual(self.get("/api/v1/session;shadow")[0], 200)

    def test_the_guarded_handlers_are_the_registered_report_handlers(self) -> None:
        """The guard keys on handler names, so a rename must fail here.

        `REPORT_ROUTE_HANDLERS` is checked against the mixin's own four
        handlers and against the route tables, so a handler renamed on one
        side and not the other leaves a route unguarded loudly.
        """

        self.assertEqual(
            transport.REPORT_ROUTE_HANDLERS,
            {
                name
                for name in vars(transport.ReportDocumentsHttpMixin)
                if name.startswith(("_get_report", "_post_report"))
            },
        )
        registered = {route.handler for route in V1_GET_ROUTES} | {
            route.handler for route in V1_POST_ROUTES
        }
        self.assertLessEqual(transport.REPORT_ROUTE_HANDLERS, registered)

    def test_the_alias_predicate_decides_nothing_about_other_handlers(self) -> None:
        self.assertTrue(
            transport.is_report_route_alias(
                "_get_report_documents", "/api/v1/reports;x"
            )
        )
        self.assertFalse(
            transport.is_report_route_alias("_get_report_documents", "/api/v1/reports")
        )
        self.assertFalse(
            transport.is_report_route_alias(
                "_get_report_documents", "/api/v1/reports?workspace_uid=x"
            )
        )
        for handler in ("_get_daily_report_preview", "_get_health", "_post_task_create"):
            with self.subTest(handler=handler):
                self.assertFalse(
                    transport.is_report_route_alias(handler, "/api/v1/anything;x")
                )
                self.assertFalse(
                    transport.is_report_route_alias(handler, "/api/v1/anything?a=b#x")
                )

    def test_the_alias_predicate_answers_a_fragment_in_every_position(self) -> None:
        """The clause the path comparison cannot carry, stated directly.

        The middle two targets are the regression: `urlparse` removes the
        fragment before it splits the query, so the comparison alone reads
        them as canonical. The last two show the rule is about the literal
        delimiter — `%23` is data and keeps its route.
        """

        aliases = (
            "/api/v1/reports#shadow",
            "/api/v1/reports?workspace_uid=x#shadow",
            "/api/v1/reports/aaa/finalize?workspace_uid=x#shadow",
            "/api/v1/reports?workspace_uid=x#",
        )
        for target in aliases:
            with self.subTest(target=target):
                self.assertTrue(
                    transport.is_report_route_alias("_get_report_documents", target)
                )
        for target in (
            "/api/v1/reports?cursor=a%23b",
            "/api/v1/reports/aaa%23shadow?workspace_uid=x",
        ):
            with self.subTest(target=target):
                self.assertFalse(
                    transport.is_report_route_alias("_get_report_documents", target)
                )


class ReportLifecycleHttpTest(_ReportHttpCase):
    """Create, read, list and every documented transition over real routes."""

    def test_create_returns_the_declared_created_envelope(self) -> None:
        status, body = self.create()
        self.assertEqual(status, 201)
        data = self.assert_mutation_envelope(body, replayed=False)
        self.assertEqual(data["state"], "draft")
        self.assertEqual(data["revision"], 1)
        self.assertEqual(data["period"], {"kind": "day", "date": DAY})
        self.assertEqual(data["source_stale"], False)
        self.assertEqual(data["content_entry"]["content_revision"], 1)

    def test_read_returns_the_item_its_history_and_staleness(self) -> None:
        created = self.created_report()
        status, body = self.get(self.read_path(created["uid"]))
        self.assertEqual(status, 200)
        self.assertEqual(set(body), {"data"})
        self.assertEqual(set(body["data"]), READ_KEYS)
        self.assertEqual(body["data"]["source_stale"], False)
        self.assertEqual(len(body["data"]["revisions"]), 1)

    def test_list_returns_the_declared_envelope_without_markdown(self) -> None:
        self.created_report()
        status, body = self.get(self.list_path())
        self.assertEqual(status, 200)
        page = body["data"]
        self.assertEqual(set(page), LIST_ENVELOPE_KEYS)
        self.assertEqual(page["workspace_uid"], self.workspace_uid)
        self.assertEqual(page["omitted_count"], 0)
        self.assertIsNone(page["cursor"])
        self.assertEqual(set(page["reports"][0]), LIST_ITEM_KEYS)

    def test_the_full_transition_set_answers_with_its_declared_status(self) -> None:
        created = self.created_report()
        uid = created["uid"]
        status, revised = self.post(
            self.action_path(uid, "revisions"),
            {
                "workspace_uid": self.workspace_uid,
                "expected_revision": 1,
                "markdown": "# Revised",
                "note": "second pass",
            },
            key=self.next_key(),
        )
        self.assertEqual(status, 200)
        data = self.assert_mutation_envelope(revised, replayed=False)
        self.assertEqual(data["revision"], 2)
        self.assertEqual(data["reopened"], False)
        self.assertEqual(data["content_entry"]["content_revision"], 2)

        status, finalized = self.post(
            self.action_path(uid, "finalize"),
            {"workspace_uid": self.workspace_uid, "expected_revision": 2},
            key=self.next_key(),
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            self.assert_mutation_envelope(finalized, replayed=False)["state"],
            "finalized",
        )

        status, reopened = self.post(
            self.action_path(uid, "revisions"),
            {
                "workspace_uid": self.workspace_uid,
                "expected_revision": 3,
                "markdown": "# Reopened",
                "note": None,
            },
            key=self.next_key(),
        )
        self.assertEqual(status, 200)
        data = self.assert_mutation_envelope(reopened, replayed=False)
        self.assertEqual(data["state"], "draft")
        self.assertEqual(data["reopened"], True)

        status, archived = self.post(
            self.action_path(uid, "archive"),
            {
                "workspace_uid": self.workspace_uid,
                "expected_revision": 4,
                "note": "done with it",
            },
            key=self.next_key(),
        )
        self.assertEqual(status, 200)
        data = self.assert_mutation_envelope(archived, replayed=False)
        self.assertEqual(data["state"], "archived")
        self.assertEqual(data["archived_from_state"], "draft")

        status, restored = self.post(
            self.action_path(uid, "restore"),
            {"workspace_uid": self.workspace_uid, "expected_revision": 5},
            key=self.next_key(),
        )
        self.assertEqual(status, 200)
        data = self.assert_mutation_envelope(restored, replayed=False)
        self.assertEqual(data["state"], "draft")
        self.assertIsNone(data["archived_from_state"])

    def test_state_filters_separate_active_from_archived(self) -> None:
        kept = self.created_report(DAY)
        gone = self.created_report(OTHER_DAY)
        self.post(
            self.action_path(gone["uid"], "archive"),
            {
                "workspace_uid": self.workspace_uid,
                "expected_revision": 1,
                "note": None,
            },
            key=self.next_key(),
        )
        active = self.get(self.list_path())[1]["data"]["reports"]
        archived = self.get(self.list_path(state="archived"))[1]["data"]["reports"]
        every = self.get(self.list_path(state="all"))[1]["data"]["reports"]
        self.assertEqual([item["uid"] for item in active], [kept["uid"]])
        self.assertEqual([item["uid"] for item in archived], [gone["uid"]])
        self.assertEqual({item["uid"] for item in every}, {kept["uid"], gone["uid"]})

    def test_list_order_is_updated_at_descending_then_uid_ascending(self) -> None:
        for date in ("2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"):
            self.created_report(date)
        page = self.get(self.list_path())[1]["data"]
        rows = page["reports"]
        self.assertEqual(len(rows), 4)
        expected = sorted(rows, key=lambda item: item["uid"])
        expected.sort(key=lambda item: item["updated_at"], reverse=True)
        self.assertEqual(rows, expected)

    def test_a_state_transition_that_is_not_allowed_refuses(self) -> None:
        created = self.created_report()
        response = self.post(
            self.action_path(created["uid"], "restore"),
            {"workspace_uid": self.workspace_uid, "expected_revision": 1},
            key=self.next_key(),
        )
        self.assert_refusal(response, "report_state_invalid")

    def test_a_second_report_for_one_period_is_a_duplicate(self) -> None:
        self.created_report()
        response = self.post(
            self.list_path(), self.create_body(), key=self.next_key()
        )
        self.assert_refusal(response, "report_duplicate_period", field="period")

    def test_an_unknown_uid_is_not_found_on_read_and_on_every_action(self) -> None:
        self.assert_refusal(
            self.get(self.read_path(UNKNOWN_REPORT)), "report_not_found"
        )
        for action, body in (
            ("revisions", {"expected_revision": 1, "markdown": "# x", "note": None}),
            ("finalize", {"expected_revision": 1}),
            ("archive", {"expected_revision": 1, "note": None}),
            ("restore", {"expected_revision": 1}),
        ):
            with self.subTest(action=action):
                payload = {"workspace_uid": self.workspace_uid, **body}
                self.assert_refusal(
                    self.post(
                        self.action_path(UNKNOWN_REPORT, action),
                        payload,
                        key=self.next_key(),
                    ),
                    "report_not_found",
                )


class ReportRevisionTypingHttpTest(_ReportHttpCase):
    """`expected_revision` is an integer, and a stale one refuses without writing."""

    def test_a_non_integer_expected_revision_is_a_body_defect(self) -> None:
        created = self.created_report()
        for spoiled in ("1", 1.0, True, None, [1]):
            with self.subTest(spoiled=repr(spoiled)):
                before = self.reports_bytes()
                response = self.post(
                    self.action_path(created["uid"], "finalize"),
                    {
                        "workspace_uid": self.workspace_uid,
                        "expected_revision": spoiled,
                    },
                    key=self.next_key(),
                )
                self.assert_refusal(
                    response, "report_body_invalid", field="expected_revision"
                )
                self.assertEqual(self.reports_bytes(), before)

    def test_a_stale_expected_revision_conflicts_on_every_action(self) -> None:
        created = self.created_report()
        uid = created["uid"]
        for action, body in (
            ("revisions", {"expected_revision": 99, "markdown": "# x", "note": None}),
            ("finalize", {"expected_revision": 99}),
            ("archive", {"expected_revision": 99, "note": None}),
            ("restore", {"expected_revision": 99}),
        ):
            with self.subTest(action=action):
                before = self.reports_bytes()
                response = self.post(
                    self.action_path(uid, action),
                    {"workspace_uid": self.workspace_uid, **body},
                    key=self.next_key(),
                )
                self.assert_refusal(
                    response, "report_revision_conflict", field="expected_revision"
                )
                self.assertEqual(self.reports_bytes(), before)


class ReportQueryAdmissionHttpTest(_ReportHttpCase):
    """The query is decoded before anything else, on every route."""

    def routes(self) -> tuple[tuple[str, str, str, dict[str, Any] | None], ...]:
        return (
            ("GET", "list", "/api/v1/reports", None),
            ("GET", "read", "/api/v1/reports/" + UNKNOWN_REPORT, None),
            ("POST", "create", "/api/v1/reports", self.create_body()),
            (
                "POST",
                "revise",
                "/api/v1/reports/{}/revisions".format(UNKNOWN_REPORT),
                {
                    "workspace_uid": self.workspace_uid,
                    "expected_revision": 1,
                    "markdown": "# x",
                    "note": None,
                },
            ),
            (
                "POST",
                "finalize",
                "/api/v1/reports/{}/finalize".format(UNKNOWN_REPORT),
                {"workspace_uid": self.workspace_uid, "expected_revision": 1},
            ),
            (
                "POST",
                "archive",
                "/api/v1/reports/{}/archive".format(UNKNOWN_REPORT),
                {
                    "workspace_uid": self.workspace_uid,
                    "expected_revision": 1,
                    "note": None,
                },
            ),
            (
                "POST",
                "restore",
                "/api/v1/reports/{}/restore".format(UNKNOWN_REPORT),
                {"workspace_uid": self.workspace_uid, "expected_revision": 1},
            ),
        )

    def send(self, method: str, path: str, body: dict[str, Any] | None):
        if method == "GET":
            return self.get(path)
        return self.post(path, body, key=self.next_key())

    def test_a_missing_workspace_uid_refuses_on_every_route(self) -> None:
        for method, name, path, body in self.routes():
            with self.subTest(route=name):
                self.assert_refusal(self.send(method, path, body), "invalid_query")

    def test_a_repeated_workspace_uid_refuses_on_every_route(self) -> None:
        repeated = "?" + urlencode(
            [
                ("workspace_uid", self.workspace_uid),
                ("workspace_uid", self.workspace_uid),
            ]
        )
        for method, name, path, body in self.routes():
            with self.subTest(route=name):
                self.assert_refusal(
                    self.send(method, path + repeated, body), "invalid_query"
                )

    def test_a_noncanonical_workspace_uid_refuses_on_every_route(self) -> None:
        for spoiled in ("not-a-uuid", NIL_WORKSPACE, self.workspace_uid.upper(), ""):
            for method, name, path, body in self.routes():
                with self.subTest(route=name, spoiled=spoiled):
                    self.assert_refusal(
                        self.send(
                            method,
                            path + "?" + urlencode({"workspace_uid": spoiled}),
                            body,
                        ),
                        "invalid_query",
                    )

    def test_an_unknown_query_key_refuses_on_every_route(self) -> None:
        for method, name, path, body in self.routes():
            with self.subTest(route=name):
                spoiled = path + self.query() + "&template=daily-v1"
                self.assert_refusal(self.send(method, spoiled, body), "invalid_query")

    def test_paging_keys_belong_to_the_list_route_alone(self) -> None:
        created = self.created_report()
        for path in (
            self.read_path(created["uid"], state="all"),
            self.read_path(created["uid"], limit=50),
            self.read_path(created["uid"], cursor="x"),
        ):
            with self.subTest(path=path):
                self.assert_refusal(self.get(path), "invalid_query")

    def test_a_limit_that_is_not_the_page_size_refuses(self) -> None:
        for spoiled in ("49", "0", "0050", "fifty", "50.0", ""):
            with self.subTest(spoiled=spoiled):
                self.assert_refusal(
                    self.get(self.list_path(limit=spoiled)), "invalid_query"
                )
        status, _body = self.get(self.list_path(limit=50))
        self.assertEqual(status, 200)

    def test_an_unknown_state_alias_refuses(self) -> None:
        self.assert_refusal(self.get(self.list_path(state="draft")), "invalid_query")

    def test_a_malformed_cursor_refuses_as_a_cursor(self) -> None:
        for spoiled in ("x", "!" * 80, "a" * 300, "b" * 80):
            with self.subTest(spoiled=spoiled[:8]):
                self.assert_refusal(
                    self.get(self.list_path(cursor=spoiled)),
                    "report_cursor_invalid",
                    field="cursor",
                )

    def test_a_noncanonical_report_uid_in_the_path_refuses_as_a_query(self) -> None:
        for spoiled in (
            UNKNOWN_REPORT.upper(),
            "not-a-uuid",
            UNKNOWN_REPORT.replace("-4", "-1", 1),
            "%61aaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        ):
            with self.subTest(spoiled=spoiled):
                self.assert_refusal(
                    self.get(self.read_path(spoiled)),
                    "invalid_query",
                    field="report_uid",
                )


class ReportWorkspaceAdmissionHttpTest(_ReportHttpCase):
    """A report belongs to one workspace, and the query says which."""

    def test_a_foreign_query_workspace_refuses_on_every_route(self) -> None:
        created = self.created_report()
        uid = created["uid"]
        cases = (
            ("GET", self.list_path(workspace_uid=FOREIGN_WORKSPACE), None),
            ("GET", self.read_path(uid, workspace_uid=FOREIGN_WORKSPACE), None),
            (
                "POST",
                "/api/v1/reports" + self.query(workspace_uid=FOREIGN_WORKSPACE),
                self.create_body(OTHER_DAY, workspace_uid=FOREIGN_WORKSPACE),
            ),
            (
                "POST",
                self.action_path(uid, "finalize", workspace_uid=FOREIGN_WORKSPACE),
                {"workspace_uid": FOREIGN_WORKSPACE, "expected_revision": 1},
            ),
        )
        before = self.reports_bytes()
        for method, path, body in cases:
            with self.subTest(path=path):
                response = (
                    self.get(path)
                    if method == "GET"
                    else self.post(path, body, key=self.next_key())
                )
                self.assert_refusal(response, "workspace_mismatch")
        self.assertEqual(self.reports_bytes(), before)

    def test_a_body_workspace_that_disagrees_with_the_query_refuses(self) -> None:
        created = self.created_report()
        before = self.reports_bytes()
        response = self.post(
            self.action_path(created["uid"], "finalize"),
            {"workspace_uid": FOREIGN_WORKSPACE, "expected_revision": 1},
            key=self.next_key(),
        )
        self.assert_refusal(response, "workspace_mismatch")
        self.assertEqual(self.reports_bytes(), before)

    def test_a_create_body_workspace_that_disagrees_refuses(self) -> None:
        response = self.post(
            self.list_path(),
            self.create_body(workspace_uid=FOREIGN_WORKSPACE),
            key=self.next_key(),
        )
        self.assert_refusal(response, "workspace_mismatch")
        self.assertFalse(json.loads(self.reports_bytes())["reports"])


class ReportSyncAdmissionHttpTest(_ReportHttpCase):
    """§8.1 steps 2 and 3: whose store this is, then whether it may be read."""

    def desync(self) -> bytes:
        """One ordinary external edit, the way the shipped preview suite makes one."""

        path = self.root / "notes.json"
        notes = json.loads(path.read_text(encoding="utf-8"))
        notes["notes"].append(
            {"id": "N-external", "text": "Changed", "links": [], "created": DAY}
        )
        path.write_text(json.dumps(notes), encoding="utf-8")
        return self.reports_bytes()

    def test_an_external_change_refuses_every_route_content_free(self) -> None:
        before = self.desync()
        cases = (
            ("GET", self.list_path(), None),
            ("GET", self.read_path(UNKNOWN_REPORT), None),
            ("POST", self.list_path(), self.create_body()),
            (
                "POST",
                self.action_path(UNKNOWN_REPORT, "finalize"),
                {"workspace_uid": self.workspace_uid, "expected_revision": 1},
            ),
        )
        for method, path, body in cases:
            with self.subTest(path=path):
                response = (
                    self.get(path)
                    if method == "GET"
                    else self.post(path, body, key=self.next_key())
                )
                error = self.assert_refusal(response, "store_sync_required")
                # The shipped preview names the changed file; a report refusal
                # must not, so the detail bag stays empty.
                self.assertEqual(error["details"], {})
        self.assertEqual(self.reports_bytes(), before)

    def test_the_owner_is_admitted_before_the_sync_guard(self) -> None:
        self.desync()
        self.assert_refusal(
            self.get(self.list_path(workspace_uid=FOREIGN_WORKSPACE)),
            "workspace_mismatch",
        )
        self.assert_refusal(
            self.post(
                "/api/v1/reports" + self.query(workspace_uid=FOREIGN_WORKSPACE),
                self.create_body(workspace_uid=FOREIGN_WORKSPACE),
                key=self.next_key(),
            ),
            "workspace_mismatch",
        )

    def test_the_sync_guard_precedes_the_body(self) -> None:
        before = self.desync()
        response = self.post(
            self.list_path(),
            {**self.create_body(), "surprise": 1},
            key=self.next_key(),
        )
        self.assert_refusal(response, "store_sync_required")
        self.assertEqual(self.reports_bytes(), before)


class ReportIdempotencyHttpTest(_ReportHttpCase):
    """The service's own receipts, reached through the shipped header rule."""

    def mutation_routes(self) -> tuple[tuple[str, str, dict[str, Any]], ...]:
        return (
            ("create", self.list_path(), self.create_body(OTHER_DAY)),
            (
                "revisions",
                self.action_path(UNKNOWN_REPORT, "revisions"),
                {
                    "workspace_uid": self.workspace_uid,
                    "expected_revision": 1,
                    "markdown": "# x",
                    "note": None,
                },
            ),
            (
                "finalize",
                self.action_path(UNKNOWN_REPORT, "finalize"),
                {"workspace_uid": self.workspace_uid, "expected_revision": 1},
            ),
            (
                "archive",
                self.action_path(UNKNOWN_REPORT, "archive"),
                {
                    "workspace_uid": self.workspace_uid,
                    "expected_revision": 1,
                    "note": None,
                },
            ),
            (
                "restore",
                self.action_path(UNKNOWN_REPORT, "restore"),
                {"workspace_uid": self.workspace_uid, "expected_revision": 1},
            ),
        )

    def test_a_missing_key_refuses_on_every_mutation_route(self) -> None:
        for name, path, body in self.mutation_routes():
            with self.subTest(route=name):
                self.assert_refusal(
                    self.post(path, body), "idempotency_key_required"
                )

    def test_an_invalid_key_refuses_on_every_mutation_route(self) -> None:
        """An empty header is present and malformed, not absent (`_header_once`)."""

        for spoiled in ("short", "!" * 16, "a" * 129, ""):
            for name, path, body in self.mutation_routes():
                with self.subTest(route=name, spoiled=spoiled[:6]):
                    self.assert_refusal(
                        self.post(path, body, key=spoiled), "invalid_idempotency_key"
                    )

    def test_the_shipped_key_rule_runs_before_the_query_is_decoded(self) -> None:
        """§13 reuses `server.py::_idempotency_key`, which the dispatcher runs first.

        The semantic admission order the contract fixes — owner, sync, key,
        body — is the service's, and it is asserted elsewhere. This pins the
        one place the transport differs: a request with neither a usable key
        nor a usable query is answered about its key, because the shipped
        header rule every other writer gets is applied before any route
        handler sees the request.
        """

        self.assert_refusal(
            self.post("/api/v1/reports", self.create_body(), key="short"),
            "invalid_idempotency_key",
        )

    def test_a_repeated_key_header_refuses_before_any_write(self) -> None:
        before = self.reports_bytes()
        raw = json.dumps(self.create_body()).encode("utf-8")
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.putrequest("POST", self.list_path())
        connection.putheader("Host", self.host)
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Origin", "http://" + self.host)
        connection.putheader("X-WorkStack-CSRF", self.csrf)
        connection.putheader("Idempotency-Key", "report-http-key-aaaa")
        connection.putheader("Idempotency-Key", "report-http-key-bbbb")
        connection.putheader("Content-Length", str(len(raw)))
        connection.endheaders()
        connection.send(raw)
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        status = response.status
        connection.close()
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_header")
        self.assertEqual(self.reports_bytes(), before)

    def test_a_replay_returns_the_recorded_answer_after_later_edits(self) -> None:
        """§9.2: the receipt is immutable, and later history does not re-project it."""

        key = self.next_key()
        body = self.create_body()
        status, first = self.post(self.list_path(), body, key=key)
        self.assertEqual(status, 201)
        uid = first["data"]["uid"]

        # The source day moves, and the document moves twice more.
        self.change_source()
        self.post(
            self.action_path(uid, "revisions"),
            {
                "workspace_uid": self.workspace_uid,
                "expected_revision": 1,
                "markdown": "# Later",
                "note": None,
            },
            key=self.next_key(),
        )
        self.post(
            self.action_path(uid, "finalize"),
            {"workspace_uid": self.workspace_uid, "expected_revision": 2},
            key=self.next_key(),
        )
        before = self.reports_bytes()

        status, replayed = self.post(self.list_path(), body, key=key)
        self.assertEqual(status, 201)
        self.assertEqual(replayed["meta"], {"replayed": True})
        self.assertEqual(replayed["data"], first["data"])
        self.assertEqual(replayed["data"]["state"], "draft")
        self.assertEqual(replayed["data"]["revision"], 1)
        self.assertEqual(replayed["data"]["source_stale"], False)
        # A replay saves nothing, so no second content revision was authored.
        self.assertEqual(self.reports_bytes(), before)
        current = self.get(self.read_path(uid))[1]["data"]
        self.assertEqual(current["state"], "finalized")
        self.assertEqual(len(current["revisions"]), 2)

    def test_the_same_key_with_a_different_body_conflicts(self) -> None:
        key = self.next_key()
        self.post(self.list_path(), self.create_body(), key=key)
        before = self.reports_bytes()
        response = self.post(
            self.list_path(),
            self.create_body(markdown="# Different"),
            key=key,
        )
        self.assert_refusal(response, "idempotency_conflict")
        self.assertEqual(self.reports_bytes(), before)

    def test_one_key_is_bound_to_the_route_it_was_used_on(self) -> None:
        created = self.created_report()
        key = self.next_key()
        self.post(
            self.action_path(created["uid"], "finalize"),
            {"workspace_uid": self.workspace_uid, "expected_revision": 1},
            key=key,
        )
        response = self.post(
            self.action_path(created["uid"], "archive"),
            {
                "workspace_uid": self.workspace_uid,
                "expected_revision": 2,
                "note": None,
            },
            key=key,
        )
        self.assert_refusal(response, "idempotency_conflict")


class ReportBrowserGateHttpTest(_ReportHttpCase):
    """Loopback host, same-origin Origin and the CSRF token still gate writes."""

    def test_a_missing_origin_refuses_without_writing(self) -> None:
        before = self.reports_bytes()
        status, body = self.post(
            self.list_path(), self.create_body(), key=self.next_key(), omit_origin=True
        )
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "origin_required")
        self.assertEqual(self.reports_bytes(), before)

    def test_a_foreign_origin_refuses_without_writing(self) -> None:
        before = self.reports_bytes()
        status, body = self.post(
            self.list_path(),
            self.create_body(),
            key=self.next_key(),
            headers={"Origin": "http://evil.example"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "invalid_origin")
        self.assertEqual(self.reports_bytes(), before)

    def test_a_missing_or_wrong_csrf_token_refuses_without_writing(self) -> None:
        before = self.reports_bytes()
        status, body = self.post(
            self.list_path(), self.create_body(), key=self.next_key(), omit_csrf=True
        )
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "invalid_csrf")
        status, body = self.post(
            self.list_path(),
            self.create_body(),
            key=self.next_key(),
            headers={"X-WorkStack-CSRF": "not-the-token"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "invalid_csrf")
        self.assertEqual(self.reports_bytes(), before)

    def test_an_agent_bearer_does_not_bypass_the_browser_gate(self) -> None:
        before = self.reports_bytes()
        status, body = self.post(
            self.list_path(),
            self.create_body(),
            key=self.next_key(),
            omit_origin=True,
            omit_csrf=True,
            headers={"Authorization": "Bearer " + self.server.capture_token},
        )
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "origin_required")
        self.assertEqual(self.reports_bytes(), before)

    def test_a_foreign_host_header_refuses_on_read_and_write(self) -> None:
        status, body = self.get(self.list_path(), headers={"Host": "evil.example"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_host")
        status, body = self.post(
            self.list_path(),
            self.create_body(),
            key=self.next_key(),
            headers={"Host": "evil.example"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_host")


class ReportContentBoundsHttpTest(_ReportHttpCase):
    """The shipped body bounds apply, and the model's own caps sit behind them."""

    def test_a_body_over_the_shipped_limit_is_refused_before_the_handler(self) -> None:
        """The declared length is the bound, exactly as every other writer sees it."""

        before = self.reports_bytes()
        status, body, _headers = self.call(
            "POST",
            self.list_path(),
            raw=b"",
            headers={
                "Content-Type": "application/json",
                "Origin": "http://" + self.host,
                "X-WorkStack-CSRF": self.csrf,
                "Idempotency-Key": self.next_key(),
                "Content-Length": str(DEFAULT_BODY_LIMIT + 1),
            },
        )
        self.assertEqual(status, 413)
        self.assertEqual(body["error"]["code"], "body_too_large")
        self.assertEqual(self.reports_bytes(), before)

    def test_markdown_over_the_model_cap_is_a_body_defect(self) -> None:
        before = self.reports_bytes()
        response = self.post(
            self.list_path(),
            self.create_body(markdown="a" * (MAX_MARKDOWN_CHARS + 1)),
            key=self.next_key(),
        )
        self.assert_refusal(response, "report_body_invalid", field="markdown")
        self.assertEqual(self.reports_bytes(), before)

    def test_markdown_at_the_model_cap_is_accepted(self) -> None:
        status, body = self.post(
            self.list_path(),
            self.create_body(markdown="a" * MAX_MARKDOWN_CHARS),
            key=self.next_key(),
        )
        self.assertEqual(status, 201)
        self.assertEqual(body["meta"], {"replayed": False})

    def test_a_non_json_content_type_refuses(self) -> None:
        status, body = self.post(
            self.list_path(),
            self.create_body(),
            key=self.next_key(),
            headers={"Content-Type": "text/plain"},
        )
        self.assertEqual(status, 415)
        self.assertEqual(body["error"]["code"], "unsupported_media_type")

    def test_an_unknown_body_key_is_a_body_defect(self) -> None:
        response = self.post(
            self.list_path(),
            {**self.create_body(), "surprise": 1},
            key=self.next_key(),
        )
        self.assert_refusal(response, "report_body_invalid", field="request")

    def test_markdown_is_forbidden_where_the_contract_forbids_it(self) -> None:
        created = self.created_report()
        response = self.post(
            self.action_path(created["uid"], "finalize"),
            {
                "workspace_uid": self.workspace_uid,
                "expected_revision": 1,
                "markdown": "# nope",
            },
            key=self.next_key(),
        )
        self.assert_refusal(response, "report_body_invalid", field="request")

    def test_an_unsupported_template_refuses_as_declared(self) -> None:
        response = self.post(
            self.list_path(),
            self.create_body(template="weekly-v1"),
            key=self.next_key(),
        )
        self.assert_refusal(
            response, "report_template_unsupported", field="template"
        )

    def test_markdown_is_stored_and_returned_verbatim(self) -> None:
        authored = "# 한글 <script>alert(1)</script>\n\n| a | b |\n"
        status, created = self.post(
            self.list_path(),
            self.create_body(markdown=authored),
            key=self.next_key(),
        )
        self.assertEqual(status, 201)
        read = self.get(self.read_path(created["data"]["uid"]))[1]["data"]
        self.assertEqual(read["revisions"][0]["markdown"], authored)


class ReportSourceHttpTest(_ReportHttpCase):
    """The source is read, compared and never written by a report route."""

    def test_a_create_against_a_moved_source_conflicts(self) -> None:
        stale = self.create_body()
        self.change_source()
        before = self.reports_bytes()
        response = self.post(self.list_path(), stale, key=self.next_key())
        self.assert_refusal(response, "report_source_changed", field="source_digest")
        self.assertEqual(self.reports_bytes(), before)

    def test_a_read_reports_staleness_without_rewriting_the_digest(self) -> None:
        created = self.created_report()
        recorded = created["source_digest"]
        fresh = self.get(self.read_path(created["uid"]))[1]["data"]
        self.assertEqual(fresh["source_stale"], False)
        self.change_source()
        stale = self.get(self.read_path(created["uid"]))[1]["data"]
        self.assertEqual(stale["source_stale"], True)
        self.assertEqual(stale["source_digest"], recorded)

    def test_a_revision_over_a_moved_source_is_accepted_and_reported(self) -> None:
        created = self.created_report()
        self.change_source()
        status, body = self.post(
            self.action_path(created["uid"], "revisions"),
            {
                "workspace_uid": self.workspace_uid,
                "expected_revision": 1,
                "markdown": "# Edited anyway",
                "note": None,
            },
            key=self.next_key(),
        )
        self.assertEqual(status, 200)
        data = self.assert_mutation_envelope(body, replayed=False)
        self.assertEqual(data["source_stale"], True)
        self.assertEqual(data["source_digest"], created["source_digest"])

    def test_the_list_carries_no_staleness_field_at_all(self) -> None:
        self.created_report()
        self.change_source()
        rows = self.get(self.list_path())[1]["data"]["reports"]
        self.assertEqual(set(rows[0]), LIST_ITEM_KEYS)

    def test_a_full_lifecycle_leaves_every_source_document_untouched(self) -> None:
        before = self.source_bytes()
        created = self.created_report()
        uid = created["uid"]
        self.post(
            self.action_path(uid, "revisions"),
            {
                "workspace_uid": self.workspace_uid,
                "expected_revision": 1,
                "markdown": "# Second",
                "note": "why",
            },
            key=self.next_key(),
        )
        self.post(
            self.action_path(uid, "finalize"),
            {"workspace_uid": self.workspace_uid, "expected_revision": 2},
            key=self.next_key(),
        )
        self.post(
            self.action_path(uid, "archive"),
            {
                "workspace_uid": self.workspace_uid,
                "expected_revision": 3,
                "note": None,
            },
            key=self.next_key(),
        )
        self.post(
            self.action_path(uid, "restore"),
            {"workspace_uid": self.workspace_uid, "expected_revision": 4},
            key=self.next_key(),
        )
        self.get(self.list_path(state="all"))
        self.get(self.read_path(uid))
        self.assertEqual(self.source_bytes(), before)
        self.assertTrue(json.loads(self.reports_bytes())["reports"])

    def test_the_daily_preview_answers_identically_across_report_writes(self) -> None:
        path = "/api/v1/reports/daily-preview?" + urlencode(
            {
                "date": DAY,
                "template": TEMPLATE_DAILY_V1,
                "workspace_uid": self.workspace_uid,
            }
        )
        with mock.patch(
            "workstack.reporting_http._utc_now", return_value=FROZEN_PREVIEW_NOW
        ):
            before = self.get(path)
            created = self.created_report()
            self.post(
                self.action_path(created["uid"], "finalize"),
                {"workspace_uid": self.workspace_uid, "expected_revision": 1},
                key=self.next_key(),
            )
            after = self.get(path)
        self.assertEqual(before[0], 200)
        self.assertEqual(before, after)


class ReportCursorHttpTest(_ReportHttpCase):
    """One real page boundary, walked over the wire."""

    def test_a_full_page_pages_and_the_cursor_is_filter_bound(self) -> None:
        made = 51
        for index in range(made):
            date = "2026-{:02d}-{:02d}".format(1 + index // 28, 1 + index % 28)
            status, _body = self.post(
                self.list_path(), self.create_body(date), key=self.next_key()
            )
            self.assertEqual(status, 201)
        first = self.get(self.list_path())[1]["data"]
        self.assertEqual(len(first["reports"]), 50)
        self.assertEqual(first["omitted_count"], made - 50)
        self.assertIsInstance(first["cursor"], str)

        second = self.get(self.list_path(cursor=first["cursor"]))[1]["data"]
        self.assertEqual(len(second["reports"]), made - 50)
        self.assertEqual(second["omitted_count"], 0)
        self.assertIsNone(second["cursor"])
        seen = [item["uid"] for item in first["reports"] + second["reports"]]
        self.assertEqual(len(set(seen)), made)

        # The same cursor under a different filter is refused, not restarted.
        self.assert_refusal(
            self.get(self.list_path(state="all", cursor=first["cursor"])),
            "report_cursor_invalid",
            field="cursor",
        )
        # And it is bound to its workspace, so a foreign owner never reaches it.
        self.assert_refusal(
            self.get(
                self.list_path(
                    workspace_uid=FOREIGN_WORKSPACE, cursor=first["cursor"]
                )
            ),
            "workspace_mismatch",
        )


class ReportRefusalTaxonomyTest(unittest.TestCase):
    """The status table is exhaustive, and it is the contract's table."""

    def test_the_table_is_exactly_the_declared_taxonomy(self) -> None:
        self.assertEqual(REPORT_REFUSAL_STATUS, DECLARED_STATUS)

    def test_every_raisable_code_has_a_status(self) -> None:
        raisable = (
            set(report_json._MESSAGES)
            | set(report_queries._QUERY_MESSAGES)
            | set(report_repository_service._MESSAGES)
        )
        self.assertEqual(raisable - set(REPORT_REFUSAL_STATUS), set())

    def test_a_query_refusal_is_the_query_leafs_own_type(self) -> None:
        """One code, one class, one sentence — decoded here or one layer down."""

        with self.assertRaises(report_queries.ReportQueryError) as caught:
            transport.parse_report_query("workspace_uid=super-secret-value")
        self.assertEqual(caught.exception.code, "invalid_query")
        self.assertEqual(caught.exception.field, "workspace_uid")
        self.assertNotIn("super-secret-value", str(caught.exception))
        self.assertEqual(
            str(caught.exception),
            str(report_queries.ReportQueryError("invalid_query")),
        )

    def test_an_unknown_query_key_is_never_named_back(self) -> None:
        with self.assertRaises(report_queries.ReportQueryError) as caught:
            transport.parse_report_query(
                "workspace_uid={}&secret_key=1".format(UNKNOWN_REPORT)
            )
        self.assertEqual(caught.exception.code, "invalid_query")
        self.assertIsNone(caught.exception.field)

    def test_details_carry_the_field_and_the_retry_budget_only(self) -> None:
        error = report_json.ReportDocumentError(
            "report_idempotency_capacity", "idempotency", 600
        )
        code, message, status, details = transport._refusal(error)
        self.assertEqual(code, "report_idempotency_capacity")
        self.assertEqual(status, 429)
        self.assertEqual(message, str(error))
        self.assertEqual(
            details, {"field": "idempotency", "retry_after_seconds": 600}
        )

    def test_an_unclassified_code_raises_rather_than_being_guessed(self) -> None:
        class _Unknown(Exception):
            code = "report_something_new"

        with self.assertRaises(KeyError):
            transport._refusal(_Unknown())

    def test_the_action_map_covers_exactly_the_declared_verbs(self) -> None:
        self.assertEqual(
            transport.REPORT_ACTION_OPERATIONS,
            {
                "revisions": "revise",
                "finalize": "finalize",
                "archive": "archive",
                "restore": "restore",
            },
        )


if __name__ == "__main__":
    unittest.main()
