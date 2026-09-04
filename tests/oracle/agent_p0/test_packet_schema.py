import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import fixture_support


class PacketSchemaTest(unittest.TestCase):
    def test_good_packet_is_valid(self):
        packet = fixture_support.make_packet(base_sha="a" * 40)
        runner = fixture_support.runner_module()
        self.assertEqual(runner.packet_errors(fixture_support.ORACLE_ROOT, packet), [])

    def test_unknown_field_is_rejected(self):
        packet = fixture_support.make_packet()
        packet["unexpected_field"] = 1
        runner = fixture_support.runner_module()
        errors = runner.packet_errors(fixture_support.ORACLE_ROOT, packet)
        self.assertTrue(any("unexpected_field" in error for error in errors))

    def test_missing_required_field_is_rejected(self):
        packet = fixture_support.make_packet()
        del packet["base_sha"]
        runner = fixture_support.runner_module()
        errors = runner.packet_errors(fixture_support.ORACLE_ROOT, packet)
        self.assertTrue(any("base_sha" in error for error in errors))

    def test_malformed_hashes_are_rejected(self):
        packet = fixture_support.make_packet(base_sha="nothex")
        runner = fixture_support.runner_module()
        errors = runner.packet_errors(fixture_support.ORACLE_ROOT, packet)
        self.assertTrue(any("base_sha" in error for error in errors))
        packet = fixture_support.make_packet(contract_sha256="0" * 63)
        errors = runner.packet_errors(fixture_support.ORACLE_ROOT, packet)
        self.assertTrue(any("contract_sha256" in error for error in errors))

    def test_bad_enum_values_are_rejected(self):
        packet = fixture_support.make_packet(allowed_change_types=["teleport"])
        runner = fixture_support.runner_module()
        errors = runner.packet_errors(fixture_support.ORACLE_ROOT, packet)
        self.assertTrue(any("allowed_change_types" in error for error in errors))
        packet = fixture_support.make_packet(packet_version=2)
        errors = runner.packet_errors(fixture_support.ORACLE_ROOT, packet)
        self.assertTrue(any("packet_version" in error for error in errors))

    def test_non_canonical_owned_paths_are_rejected(self):
        packet = fixture_support.make_packet(owned_paths=["../escape.py"])
        runner = fixture_support.runner_module()
        errors = runner.packet_errors(fixture_support.ORACLE_ROOT, packet)
        self.assertTrue(any("'..'" in error for error in errors))
        packet = fixture_support.make_packet(owned_paths=[r"windows\path.py"])
        errors = runner.packet_errors(fixture_support.ORACLE_ROOT, packet)
        self.assertTrue(any("POSIX" in error for error in errors))

    def test_validate_packet_cli_accepts_and_rejects(self):
        runner = fixture_support.runner_module()
        with tempfile.TemporaryDirectory(prefix="p0-packet-schema-") as temporary:
            work = Path(temporary)
            good = work / "good.json"
            fixture_support.write_packet(good, fixture_support.make_packet())
            result = runner.main(
                ["--oracle-root", str(fixture_support.ORACLE_ROOT), "--validate-packet", str(good)]
            )
            self.assertEqual(result, 0)

            packet = fixture_support.make_packet()
            packet["extra"] = True
            bad = work / "bad.json"
            fixture_support.write_packet(bad, packet)
            result = runner.main(
                ["--oracle-root", str(fixture_support.ORACLE_ROOT), "--validate-packet", str(bad)]
            )
            self.assertEqual(result, 1)

            noncanonical = work / "noncanonical.json"
            noncanonical.write_text(json.dumps(fixture_support.make_packet(), indent=2) + "\n", encoding="utf-8")
            result = runner.main(
                ["--oracle-root", str(fixture_support.ORACLE_ROOT), "--validate-packet", str(noncanonical)]
            )
            self.assertEqual(result, 1)

    def test_schema_file_matches_evaluator_required_fields(self):
        schema = json.loads(
            (fixture_support.ORACLE_DIR / "packet-schema.v1.json").read_text(encoding="utf-8")
        )
        required = set(schema["required"])
        self.assertEqual(required, set(schema["properties"]) - {"lane"})
        self.assertFalse(schema["additionalProperties"])


if __name__ == "__main__":
    unittest.main()
