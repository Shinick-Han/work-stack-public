"""The UI asset identity survives diagnostics without widening other fields."""
from __future__ import annotations

import unittest

from tests.test_remote_update_diagnostics import DIAG


class ServedBuildIdentity(unittest.TestCase):
    def test_observed_asset_identity_survives_both_projection_and_copy(self):
        digest = "sha256:" + "1234567890abcdef" * 4
        view = DIAG.normalize_view({"versions": {"served_ui": digest}})
        self.assertEqual(view["versions"]["served_ui"], digest)
        report = DIAG.format_report(view)
        self.assertIn(digest, report)
        self.assertLessEqual(len(report), DIAG.MAX_REPORT_CHARACTERS)
        self.assertIsNone(view["versions"]["remote"])

    def test_digest_does_not_become_a_generic_version_or_extra_field(self):
        digest = "sha256:" + "a" * 64
        payload = {"versions": {key: digest for key in DIAG.VERSION_FIELDS},
                   "session_token_hash": "b" * 64}
        view = DIAG.normalize_view(payload)
        for field in DIAG.VERSION_FIELDS:
            if field != "served_ui":
                self.assertIsNone(view["versions"][field])
        self.assertNotIn("b" * 64, DIAG.format_report(view))

    def test_bad_digest_and_bare_hash_are_unknown(self):
        values = ["a" * 64, "sha256:" + "a" * 63, "sha256:" + "a" * 65,
                  "sha256:" + "A" * 64, "sha256:" + "a" * 64 + "\n",
                  "sha256:C:/private/file", "sha256:<script>", True, {}]
        for value in values:
            with self.subTest(value=value):
                view = DIAG.normalize_view({"versions": {"served_ui": value}})
                self.assertIsNone(view["versions"]["served_ui"])

    def test_product_version_does_not_fill_missing_served_identity(self):
        view = DIAG.normalize_view({"versions": {"desktop": "1.0.13", "remote": "1.0.13"}})
        self.assertIsNone(view["versions"]["served_ui"])


if __name__ == "__main__":
    unittest.main()
