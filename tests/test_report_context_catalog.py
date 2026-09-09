"""Pure catalogue helper: related links, refusals, truncation, and closed fields."""

from __future__ import annotations

import unittest

from workstack.report_context_catalog import (
    CONTEXT_CATALOG_ITEM_LIMIT,
    build_context_catalog,
)


CAPTURED_AT = "2026-09-06T01:02:03Z"
CANARY_URL = "https://outlook.office.com/mail/deeplink/read/secret"
CANARY_PATH = "C:\\secret\\payroll.xlsx"


def _capture(
    capture_id: str,
    *,
    title: str = "Synthetic context",
    linked: list[str] | None = None,
    converted: list[str] | None = None,
    status: str = "linked",
    revision: int = 1,
) -> dict:
    return {
        "id": capture_id,
        "revision": revision,
        "status": status,
        "linked_task_ids": list(linked or []),
        "converted_task_ids": list(converted or []),
        "source": {
            "display_title": title,
            "web_url": CANARY_URL,
            "object_ref": CANARY_PATH,
        },
    }


def _catalog(captures: list[dict], task_ids: list[str]) -> dict:
    return build_context_catalog(
        captures=captures,
        provenance_task_ids=task_ids,
        captured_at=CAPTURED_AT,
    )


class ReportContextCatalogTest(unittest.TestCase):
    def test_related_explicit_link_is_included_with_closed_fields(self) -> None:
        catalog = _catalog(
            [_capture("C-0001", linked=["T-0002", "T-0001", "T-0001"])],
            ["T-0002", "T-0001"],
        )
        self.assertEqual(list(catalog), ["captured_at", "items", "omitted_count"])
        self.assertEqual(catalog["captured_at"], CAPTURED_AT)
        self.assertEqual(catalog["omitted_count"], 0)
        self.assertEqual(len(catalog["items"]), 1)
        item = catalog["items"][0]
        self.assertEqual(
            list(item),
            ["capture_id", "capture_revision", "title", "linked_task_ids", "status"],
        )
        self.assertEqual(item["capture_id"], "C-0001")
        self.assertEqual(item["capture_revision"], 1)
        self.assertEqual(item["title"], "Synthetic context")
        self.assertEqual(item["linked_task_ids"], ["T-0001", "T-0002"])
        self.assertEqual(item["status"], "linked")
        encoded = repr(catalog)
        self.assertNotIn(CANARY_URL, encoded)
        self.assertNotIn(CANARY_PATH, encoded)
        self.assertNotIn("converted_task_ids", item)

    def test_converted_provenance_alone_is_not_a_link(self) -> None:
        catalog = _catalog(
            [_capture("C-0001", linked=[], converted=["T-0001"], status="converted")],
            ["T-0001"],
        )
        self.assertEqual(catalog["items"], [])
        self.assertEqual(catalog["omitted_count"], 0)

    def test_unrelated_and_unlinked_rows_are_omitted(self) -> None:
        catalog = _catalog(
            [
                _capture("C-0001", linked=["T-0009"]),
                _capture("C-0002", linked=[]),
            ],
            ["T-0001"],
        )
        self.assertEqual(catalog["items"], [])

    def test_empty_provenance_yields_empty_items(self) -> None:
        catalog = _catalog([_capture("C-0001", linked=["T-0001"])], [])
        self.assertEqual(catalog["items"], [])
        self.assertEqual(catalog["omitted_count"], 0)

    def test_dismissed_status_is_retained_with_explicit_links(self) -> None:
        catalog = _catalog(
            [_capture("C-0001", linked=["T-0001"], status="dismissed", revision=4)],
            ["T-0001"],
        )
        self.assertEqual(catalog["items"][0]["status"], "dismissed")
        self.assertEqual(catalog["items"][0]["capture_revision"], 4)
        self.assertEqual(catalog["items"][0]["linked_task_ids"], ["T-0001"])

    def test_natural_capture_id_order_is_numeric_not_lexicographic(self) -> None:
        catalog = _catalog(
            [
                _capture("C-10", title="Ten", linked=["T-0001"]),
                _capture("C-2", title="Two", linked=["T-0001"]),
                _capture("C-9", title="Nine", linked=["T-0001"]),
            ],
            ["T-0001"],
        )
        self.assertEqual(
            [item["capture_id"] for item in catalog["items"]],
            ["C-2", "C-9", "C-10"],
        )

    def test_truncation_keeps_thirty_two_and_counts_the_rest(self) -> None:
        captures = [
            _capture("C-{0:04d}".format(index), linked=["T-0001"])
            for index in range(1, CONTEXT_CATALOG_ITEM_LIMIT + 4)
        ]
        catalog = _catalog(list(reversed(captures)), ["T-0001"])
        self.assertEqual(len(catalog["items"]), CONTEXT_CATALOG_ITEM_LIMIT)
        self.assertEqual(catalog["omitted_count"], 3)
        self.assertEqual(catalog["items"][0]["capture_id"], "C-0001")
        self.assertEqual(catalog["items"][-1]["capture_id"], "C-0032")

    def test_title_is_the_admitted_display_title_string(self) -> None:
        catalog = _catalog(
            [_capture("C-0001", title="café — 한글", linked=["T-0001"])],
            ["T-0001"],
        )
        self.assertEqual(catalog["items"][0]["title"], "café — 한글")


if __name__ == "__main__":
    unittest.main()
