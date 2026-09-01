from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "desktop" / "python-webview-shell" / "workstack_desktop.py"
SPEC = importlib.util.spec_from_file_location("workstack_desktop_source_bridge_refactor", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
with mock.patch.dict(sys.modules, {"webview": types.SimpleNamespace(Window=object)}):
    SPEC.loader.exec_module(MODULE)


class DesktopSourceBridgeRefactorTest(unittest.TestCase):
    def test_show_message_parser_accepts_only_known_provider_and_safe_bounds(self) -> None:
        self.assertEqual(
            MODULE.parse_source_show_message(
                "workstack-source-host|show|outlook|-10000|10000|160|120"
            ),
            ("outlook", -10000, 10000, 160, 120),
        )
        rejected = (
            "workstack-source-host|show|unknown|0|0|500|500",
            "workstack-source-host|show|teams|0|0|159|500",
            "workstack-source-host|show|teams|0|0|500|119",
            "workstack-source-host|show|teams|10001|0|500|500",
            "workstack-source-host|show|teams|0|0|not-a-number|500",
            "workstack-source-host|hide",
        )
        for message in rejected:
            with self.subTest(message=message):
                self.assertIsNone(MODULE.parse_source_show_message(message))

    def test_capture_request_parser_keeps_the_existing_bounded_contract(self) -> None:
        self.assertEqual(
            MODULE.parse_source_capture_request("workstack-source-host|capture|teams|request-1"),
            ("teams", "request-1"),
        )
        for message in (
            "workstack-source-host|capture|unknown|request-1",
            "workstack-source-host|capture|teams|",
            "workstack-source-host|capture|teams|" + "x" * 129,
            "workstack-source-host|other|teams|request-1",
        ):
            with self.subTest(message=message[:80]):
                self.assertIsNone(MODULE.parse_source_capture_request(message))

    def test_non_outlook_capture_posts_the_seed_once(self) -> None:
        host = object.__new__(MODULE.WorkStackDesktopHost)
        host.workstack_webview = types.SimpleNamespace(CoreWebView2=object())
        host._source_capture_seed = mock.Mock(return_value={"url": "", "title": "T", "text": "B"})
        host._begin_outlook_visible_capture = mock.Mock()
        host._post_source_draft = mock.Mock()

        host._send_source_capture("workstack-source-host|capture|teams|request-1")

        host._source_capture_seed.assert_called_once_with("teams")
        host._begin_outlook_visible_capture.assert_not_called()
        host._post_source_draft.assert_called_once_with(
            "teams", "request-1", {"url": "", "title": "T", "text": "B"}
        )

    def test_outlook_visible_capture_defers_only_when_script_started(self) -> None:
        host = object.__new__(MODULE.WorkStackDesktopHost)
        host.workstack_webview = types.SimpleNamespace(CoreWebView2=object())
        capture = {"url": "", "title": "T", "text": "B"}
        host._source_capture_seed = mock.Mock(return_value=capture)
        host._begin_outlook_visible_capture = mock.Mock(return_value=True)
        host._post_source_draft = mock.Mock()

        host._send_source_capture("workstack-source-host|capture|outlook|request-1")
        host._post_source_draft.assert_not_called()

        host._begin_outlook_visible_capture.return_value = False
        host._send_source_capture("workstack-source-host|capture|outlook|request-2")
        host._post_source_draft.assert_called_once_with("outlook", "request-2", capture)


if __name__ == "__main__":
    unittest.main()
