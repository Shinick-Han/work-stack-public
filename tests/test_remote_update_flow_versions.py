"""Actual flow version observations across preview, probe and verification."""
import unittest
from dataclasses import replace

from tests.test_remote_update_flow import Harness, PREVIEW_OK, PROBE_OK, VERIFY_OK, ProbeOutcome
from remote_update_flow_contract import measured_version, version
from remote_update_view import normalize_remote_update_snapshot
from remote_update_diagnostics import render_report


class ObservedFlowVersionsTest(unittest.TestCase):
    def test_actual_digest_survives_flow_and_view(self):
        digest = "sha256:" + "a" * 64
        harness = Harness(preview=(replace(PREVIEW_OK, served_ui_version=digest),))
        document = harness.flow.advance().to_document()
        self.assertEqual(document["versions"]["served_ui"], digest)
        view = normalize_remote_update_snapshot(document)
        self.assertEqual(view.versions.served_ui, digest)
        self.assertIn(digest, render_report(document))

    def test_explicit_unknown_probe_clears_old_ui_and_protocol_only(self):
        harness = Harness(probe=(ProbeOutcome(status="unknown"),))
        harness.run(4)
        snapshot = harness.flow.advance()
        self.assertIsNone(snapshot.versions["served_ui"])
        self.assertIsNone(snapshot.versions["protocol"])
        self.assertEqual(snapshot.versions["desktop"], PREVIEW_OK.desktop_version)
        self.assertEqual(snapshot.versions["remote"], PREVIEW_OK.remote_version)

    def test_verification_unknowns_do_not_reuse_pre_restart_versions(self):
        harness = Harness(verify=(replace(VERIFY_OK, status="unknown", remote_version=None,
                                          served_ui_version=None, protocol_version=None,
                                          schema_version=None),))
        harness.run(6)
        harness.resume()
        snapshot = harness.flow.advance()
        for field in ("remote", "served_ui", "protocol", "schema"):
            self.assertIsNone(snapshot.versions[field], field)
        self.assertEqual(snapshot.versions["desktop"], PREVIEW_OK.desktop_version)

    def test_new_digest_replaces_previous_entrypoint(self):
        first, second = "sha256:" + "a" * 64, "sha256:" + "b" * 64
        harness = Harness(preview=(replace(PREVIEW_OK, served_ui_version=first),),
                          probe=(replace(PROBE_OK, served_ui_version=second),))
        harness.run(4)
        self.assertEqual(harness.flow.advance().versions["served_ui"], second)

    def test_generic_version_bounds_unchanged_and_digest_is_exact(self):
        digest = "sha256:" + "a" * 64
        self.assertIsNone(version(digest))
        for field in ("desktop", "remote", "protocol", "schema"):
            self.assertIsNone(measured_version(field, digest))
        for invalid in ("sha256:a", "sha256:" + "A" * 64, digest + "\n", digest + "a"):
            with self.subTest(invalid=invalid):
                self.assertIsNone(measured_version("served_ui", invalid))


if __name__ == "__main__":
    unittest.main()
