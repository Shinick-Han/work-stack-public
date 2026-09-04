import json
import tempfile
import unittest
from pathlib import Path

import fixture_support


def evidence_terminal(findings, digest=None):
    return {
        "schema_version": 1,
        "result": "evidence",
        "packet_id": "agent-p0-selftest-x1",
        "attempt_id": "attempt-1",
        "findings": findings,
        "finding_sha256": digest or fixture_support.sha256_hex(fixture_support.canonical_bytes(findings)),
    }


CANDIDATE = {
    "schema_version": 1,
    "result": "candidate",
    "packet_id": "agent-p0-selftest-b1",
    "attempt_id": "attempt-1",
    "base_sha": "a" * 40,
    "head_sha": "b" * 40,
}


class TerminalSchemaTest(unittest.TestCase):
    def setUp(self):
        self.runner = fixture_support.runner_module()

    def test_candidate_variant_is_valid(self):
        self.assertEqual(self.runner.terminal_errors(fixture_support.ORACLE_ROOT, dict(CANDIDATE)), [])

    def test_blocked_variants_are_valid(self):
        base = {
            "schema_version": 1,
            "result": "blocked",
            "packet_id": "agent-p0-selftest-b1",
            "attempt_id": "attempt-1",
        }
        contract_gap = dict(base, reason="contract_gap", contract_sha256="c" * 64, requested_symbol="admit_authority", failing_fixture="G10")
        ownership_gap = dict(base, reason="ownership_gap", required_path="workstack/agent_authority.py")
        dependency_gap = dict(base, reason="dependency_gap", receipt="agent-p0-g10")
        for terminal in (contract_gap, ownership_gap, dependency_gap):
            self.assertEqual(self.runner.terminal_errors(fixture_support.ORACLE_ROOT, terminal), [])

    def test_failed_variant_is_valid(self):
        terminal = {
            "schema_version": 1,
            "result": "failed",
            "packet_id": "agent-p0-selftest-b1",
            "attempt_id": "attempt-1",
            "reason": "implementation_incomplete",
        }
        self.assertEqual(self.runner.terminal_errors(fixture_support.ORACLE_ROOT, terminal), [])

    def test_evidence_variant_with_matching_digest_is_valid(self):
        findings = [{"id": "X1-FACT-01", "observation": "server POST returns 201", "source": "workstack/cli.py:apply_agent", "reproduction": ["python", "-m", "unittest", "tests.test_agent_apply"]}]
        terminal = {
            "schema_version": 1,
            "result": "evidence",
            "packet_id": "agent-p0-selftest-x1",
            "attempt_id": "attempt-1",
            "findings": findings,
            "finding_sha256": fixture_support.sha256_hex(fixture_support.canonical_bytes(findings)),
        }
        self.assertEqual(self.runner.terminal_errors(fixture_support.ORACLE_ROOT, terminal), [])
        self.assertTrue(self.runner.evidence_digest_ok(terminal))

    def test_evidence_with_wrong_digest_is_invalid(self):
        findings = [{"id": "X1-FACT-01", "observation": "o", "source": "s", "reproduction": ["argv"]}]
        terminal = {
            "schema_version": 1,
            "result": "evidence",
            "packet_id": "agent-p0-selftest-x1",
            "attempt_id": "attempt-1",
            "findings": findings,
            "finding_sha256": "0" * 64,
        }
        self.assertFalse(self.runner.evidence_digest_ok(terminal))

    def test_unknown_field_is_rejected(self):
        terminal = dict(CANDIDATE)
        terminal["head_note"] = "extra"
        self.assertTrue(self.runner.terminal_errors(fixture_support.ORACLE_ROOT, terminal))

    def test_parse_rejects_trailing_prose(self):
        raw = (json.dumps(CANDIDATE) + "\nand my explanation").encode("utf-8")
        with self.assertRaises(self.runner.TerminalError):
            self.runner.parse_terminal_bytes(raw)

    def test_parse_rejects_markdown_fences(self):
        raw = ("```json\n" + json.dumps(CANDIDATE) + "\n```").encode("utf-8")
        with self.assertRaises(self.runner.TerminalError):
            self.runner.parse_terminal_bytes(raw)

    def test_parse_rejects_multiple_objects(self):
        raw = (json.dumps(CANDIDATE) + "\n" + json.dumps(CANDIDATE)).encode("utf-8")
        with self.assertRaises(self.runner.TerminalError):
            self.runner.parse_terminal_bytes(raw)

    def test_parse_rejects_duplicate_keys(self):
        raw = '{"schema_version":1,"schema_version":1}'.encode("utf-8")
        with self.assertRaises(self.runner.TerminalError):
            self.runner.parse_terminal_bytes(raw)

    def test_parse_rejects_malformed_utf8(self):
        with self.assertRaises(self.runner.TerminalError):
            self.runner.parse_terminal_bytes(b'{"schema_version": "\xff\xfe"}')

    def test_parse_accepts_exact_single_object(self):
        terminal = self.runner.parse_terminal_bytes(json.dumps(CANDIDATE).encode("utf-8"))
        self.assertEqual(terminal["result"], "candidate")
        terminal = self.runner.parse_terminal_bytes((json.dumps(CANDIDATE) + "\n").encode("utf-8"))
        self.assertEqual(terminal["result"], "candidate")

    def test_validate_terminal_cli(self):
        with tempfile.TemporaryDirectory(prefix="p0-terminal-") as temporary:
            good = Path(temporary) / "good.json"
            good.write_bytes(json.dumps(CANDIDATE).encode("utf-8") + b"\n")
            result = self.runner.main(
                ["--oracle-root", str(fixture_support.ORACLE_ROOT), "--validate-terminal", str(good)]
            )
            self.assertEqual(result, 0)
            bad = Path(temporary) / "bad.json"
            bad.write_bytes((json.dumps(CANDIDATE) + "\ntrailing").encode("utf-8"))
            result = self.runner.main(
                ["--oracle-root", str(fixture_support.ORACLE_ROOT), "--validate-terminal", str(bad)]
            )
            self.assertEqual(result, 1)


if __name__ == "__main__":
    unittest.main()
