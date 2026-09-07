"""Fail-closed oracles for scripts/receipt_runner.py (Wave 1 G1).

Platform-neutral: no Git Bash. Commands are argv lists executed with shell=False.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.receipt_runner import (
    EMPTY_SHA256,
    FAIL_CLOSED_EXIT,
    REQUIRED_RECEIPT_FIELDS,
    REQUIRED_SPEC_FIELDS,
    Command,
    FailClosedError,
    aggregate_exit,
    combine_streams,
    contract_matches_builtin,
    detect_d3a_swallow,
    execute_argv,
    load_contract,
    missing_schema_fields,
    parse_commands,
    resolve_under_root,
    run_commands,
    run_spec,
    sha256_hex,
    verify_receipt,
    write_receipt,
)

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "quality" / "receipt-contract-v1.json"


def _spec(**overrides: object) -> dict[str, object]:
    spec: dict[str, object] = {
        "step_id": "unit-fixture",
        "description": "argv aggregation fixture",
        "worktree": str(ROOT / "does-not-exist-worktree"),
        "base_sha": "abc123def456",
        "cwd": str(ROOT),
        "changed_paths": "none",
        "kind": "mocked",
        "oracle": "§8.5 unit fixture expected vs observed in assertions",
        "output_dir": str(ROOT / "does-not-exist-output"),
        "commands": [{"argv": [sys.executable, "-c", "raise SystemExit(0)"]}],
        "expected_red": False,
    }
    spec.update(overrides)
    return spec


def _minimal_fields(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "step_id": "unit-fixture",
        "description": "schema fixture",
        "host": "testhost",
        "user": "testuser",
        "cwd": "C:/tmp",
        "started_utc": "2026-09-04T00:00:00.000000Z",
        "finished_utc": "2026-09-04T00:00:01.000000Z",
        "base_sha": "abc123",
        "worktree": "C:/tmp/worktree",
        "changed_paths": "none",
        "commands": json.dumps([{"argv": [sys.executable, "-c", "print(1)"]}]),
        "command_exit_codes": "[0]",
        "exit_code": "0",
        "stdout_bytes": "0",
        "stderr_bytes": "0",
        "stdout_sha256": EMPTY_SHA256,
        "stderr_sha256": EMPTY_SHA256,
        "kind": "mocked",
        "oracle": "§8.5 fixture",
    }
    fields.update(overrides)
    return fields


def _write_pair(directory: Path, step_id: str, fields: dict[str, object], stdout: bytes, stderr: bytes) -> Path:
    raw_dir = directory / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / f"{step_id}.stdout").write_bytes(stdout)
    (raw_dir / f"{step_id}.stderr").write_bytes(stderr)
    fields = dict(fields)
    fields["step_id"] = step_id
    fields["stdout_bytes"] = str(len(stdout))
    fields["stderr_bytes"] = str(len(stderr))
    fields["stdout_sha256"] = sha256_hex(stdout)
    fields["stderr_sha256"] = sha256_hex(stderr)
    fields["raw_stdout"] = f"raw/{step_id}.stdout"
    fields["raw_stderr"] = f"raw/{step_id}.stderr"
    lines = ["# Receipt " + step_id, "", "| field | value |", "|---|---|"]
    for key, value in fields.items():
        lines.append(f"| {key} | {value} |")
    lines.extend(
        [
            "",
            "## stdout",
            "",
            "```text",
            stdout.decode("utf-8", errors="replace"),
            "```",
            "",
            "## stderr",
            "",
            "```text",
            stderr.decode("utf-8", errors="replace"),
            "```",
            "",
        ]
    )
    dest = directory / f"{step_id}.md"
    dest.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    return dest


class ParseAndAggregateTests(unittest.TestCase):
    def test_aggregate_first_nonzero(self) -> None:
        self.assertEqual(aggregate_exit([0, 2, 1]), 2)
        self.assertEqual(aggregate_exit([0, 0]), 0)
        self.assertEqual(aggregate_exit([]), FAIL_CLOSED_EXIT)

    def test_rejects_empty_commands(self) -> None:
        with self.assertRaises(FailClosedError):
            parse_commands([])

    def test_rejects_shell_strings(self) -> None:
        with self.assertRaises(FailClosedError):
            parse_commands([{"shell": "echo hi"}])

    def test_rejects_argv_and_shell_together(self) -> None:
        with self.assertRaises(FailClosedError):
            parse_commands([{"argv": ["true"], "shell": "true"}])

    def test_execute_argv_sets_shell_false(self) -> None:
        with patch("scripts.receipt_runner.subprocess.run") as mocked:
            mocked.return_value = type("P", (), {"returncode": 0, "stdout": b"", "stderr": b""})()
            execute_argv([sys.executable, "-c", "raise SystemExit(0)"], str(ROOT), os.environ.copy())
            kwargs = mocked.call_args.kwargs
            self.assertIs(kwargs.get("shell"), False)

    def test_missing_field_fail_closed(self) -> None:
        record = _minimal_fields()
        del record["base_sha"]
        self.assertIn("base_sha", missing_schema_fields(record))

    def test_blank_field_fail_closed(self) -> None:
        record = _minimal_fields(oracle="  ")
        self.assertIn("oracle", missing_schema_fields(record))


class PathAndAtomicTests(unittest.TestCase):
    def test_path_traversal_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(FailClosedError):
                resolve_under_root(root, Path("..") / "outside.txt")

    def test_absolute_escape_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(FailClosedError):
                resolve_under_root(root, Path(tmp).resolve().parent / "secret")

    def test_atomic_replace_failure_leaves_no_accepted_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            spec = _spec(
                output_dir=str(output),
                commands=[{"argv": [sys.executable, "-c", "print('ok')"]}],
            )
            result = run_spec(spec, clock=lambda: "2026-09-04T00:00:00.000000Z")
            dest = output / "unit-fixture.md"

            def boom(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
                raise OSError("simulated interruption")

            with patch("scripts.receipt_runner.os.replace", side_effect=boom):
                with self.assertRaises(OSError):
                    write_receipt(result, output)
            self.assertFalse(dest.exists())


class RunCommandsTests(unittest.TestCase):
    def test_success_then_failure_aggregates(self) -> None:
        commands = [
            Command(argv=(sys.executable, "-c", "raise SystemExit(0)")),
            Command(argv=(sys.executable, "-c", "raise SystemExit(2)")),
        ]
        results = run_commands(commands, cwd=str(ROOT))
        self.assertEqual([item.exit_code for item in results], [0, 2])
        self.assertEqual(aggregate_exit([item.exit_code for item in results]), 2)

    def test_index_boundaries_and_empty_stderr_hash(self) -> None:
        commands = [Command(argv=(sys.executable, "-c", "print('a')"))]
        results = run_commands(commands, cwd=str(ROOT))
        stdout, stderr = combine_streams(results)
        self.assertIn(b"=== command[0] exit=0", stdout)
        self.assertIn(b"a", stdout)
        self.assertEqual(stderr, b"")
        self.assertEqual(sha256_hex(stderr), EMPTY_SHA256)

    def test_unicode_bytes_exact(self) -> None:
        payload = "한글✓"
        commands = [
            Command(
                argv=(
                    sys.executable,
                    "-c",
                    "import sys; sys.stdout.buffer.write('한글✓'.encode('utf-8'))",
                )
            )
        ]
        results = run_commands(commands, cwd=str(ROOT))
        self.assertEqual(results[0].exit_code, 0)
        self.assertIn(payload.encode("utf-8"), results[0].stdout)


class VerifyRoundTripTests(unittest.TestCase):
    def test_write_and_verify_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            spec = _spec(
                output_dir=str(output),
                commands=[
                    {"argv": [sys.executable, "-c", "raise SystemExit(0)"]},
                    {"argv": [sys.executable, "-c", "raise SystemExit(2)"]},
                ],
            )
            result = run_spec(spec, clock=lambda: "2026-09-04T00:00:00.000000Z")
            self.assertEqual(result.exit_code, 2)
            dest = write_receipt(result, output)
            verified = verify_receipt(dest, output_root=output)
            self.assertEqual(verified.errors, [])
            self.assertTrue(verified.ok)

    def test_hash_tamper_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = _write_pair(root, "tamper", _minimal_fields(), b"abc\n", b"")
            (root / "raw" / "tamper.stdout").write_bytes(b"mutated\n")
            result = verify_receipt(path, output_root=root)
            self.assertFalse(result.ok)
            self.assertTrue(any("stdout_sha256 mismatch" in err for err in result.errors))

    def test_command_count_mismatch_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fields = _minimal_fields(
                commands=json.dumps([{"argv": ["a"]}, {"argv": ["b"]}]),
                command_exit_codes="[0]",
                exit_code="0",
            )
            path = _write_pair(root, "count-mismatch", fields, b"", b"")
            result = verify_receipt(path, output_root=root)
            self.assertFalse(result.ok)
            self.assertTrue(any("commands count" in err for err in result.errors))

    def test_expected_red_nonzero_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fields = _minimal_fields(expected_red="true", exit_code="1", command_exit_codes="[1]")
            path = _write_pair(root, "red-nonzero", fields, b"fail\n", b"")
            result = verify_receipt(path, output_root=root)
            self.assertEqual(result.errors, [])
            self.assertTrue(result.ok)
            self.assertEqual(result.fields.get("exit_code"), "1")

    def test_expected_red_zero_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fields = _minimal_fields(expected_red="true", exit_code="0", command_exit_codes="[0]")
            path = _write_pair(root, "red-zero", fields, b"", b"")
            result = verify_receipt(path, output_root=root)
            self.assertFalse(result.ok)
            self.assertTrue(any("expected_red" in err for err in result.errors))


class D3aAndMutantTests(unittest.TestCase):
    def test_d3a_header_zero_body_classify_exit_rejected(self) -> None:
        reason = detect_d3a_swallow(
            {"exit_code": "0", "command_exit_codes": "[0]"},
            "classify_exit=2\n",
        )
        self.assertIsNotNone(reason)
        self.assertIn("classify_exit", reason or "")

    def test_d3a_fixture_receipt_verify_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fields = _minimal_fields(exit_code="0", command_exit_codes="[0]")
            path = _write_pair(root, "d3a-fixture", fields, b"classify_exit=2\n", b"")
            result = verify_receipt(path, output_root=root)
            self.assertFalse(result.ok)
            self.assertTrue(any("D3a" in err or "classify_exit" in err for err in result.errors))

    def test_nonzero_codes_with_header_zero_rejected(self) -> None:
        reason = detect_d3a_swallow(
            {"exit_code": "0", "command_exit_codes": "[2, 0]"},
            "",
        )
        self.assertIsNotNone(reason)

    def test_raw_path_escape_in_receipt_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fields = _minimal_fields(raw_stdout="../../secret.stdout")
            path = _write_pair(root, "escape", fields, b"x\n", b"")
            # overwrite declared raw path after write_pair set a safe one
            text = path.read_text(encoding="utf-8")
            text = text.replace("raw/escape.stdout", "../secret.stdout")
            path.write_text(text, encoding="utf-8")
            result = verify_receipt(path, output_root=root)
            self.assertFalse(result.ok)
            self.assertTrue(any("escapes" in err for err in result.errors))

    def test_contract_json_matches_builtin(self) -> None:
        payload = load_contract(CONTRACT_PATH)
        self.assertEqual(contract_matches_builtin(payload), [])
        self.assertEqual(tuple(payload["required_receipt_fields"]), REQUIRED_RECEIPT_FIELDS)
        self.assertEqual(tuple(payload["required_spec_fields"]), REQUIRED_SPEC_FIELDS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
