"""Failed ESLint command diagnostics stay bounded and actionable."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import quality_typescript  # noqa: E402

SOURCE_CANARY = "HUGE_SOURCE_BODY_CANARY_TOKEN_SHOULD_NEVER_APPEAR"
FILE_CANARY = "SUCCESSFUL_FILE_CANARY_TOKEN_SHOULD_NEVER_APPEAR"


def _config() -> dict[str, object]:
    return {"frontend_complexity": {"command": ["eslint", "--format", "json"]}}


def _completed(
    returncode: int, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["eslint", "--format", "json"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _result(
    root: Path,
    relative: str,
    *,
    messages: list[dict[str, object]] | None = None,
    source: str = "",
    extra: str | None = None,
) -> dict[str, object]:
    path = extra if extra is not None else str(root / relative)
    return {
        "filePath": path,
        "messages": list(messages or []),
        "suppressedMessages": [],
        "errorCount": sum(1 for item in (messages or []) if item.get("severity") != 1),
        "fatalErrorCount": 0,
        "warningCount": sum(1 for item in (messages or []) if item.get("severity") == 1),
        "fixableErrorCount": 0,
        "fixableWarningCount": 0,
        "usedDeprecatedRules": [],
        "source": source,
        "unrelatedField": {"nested": SOURCE_CANARY},
    }


class EslintCommandDiagnosticTests(unittest.TestCase):
    def _root(self) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Path(temporary.name).resolve()

    def _run(
        self,
        root: Path,
        completed: subprocess.CompletedProcess[str],
    ) -> tuple[list[object] | None, list[str]]:
        with patch.object(quality_typescript.subprocess, "run", return_value=completed) as mocked:
            payload, errors = quality_typescript._run_eslint_complexity(root, _config())
        mocked.assert_called_once()
        kwargs = mocked.call_args.kwargs
        self.assertEqual(kwargs.get("cwd"), root)
        self.assertFalse(kwargs.get("check"))
        return payload, errors

    def test_successful_valid_array_is_unchanged(self) -> None:
        root = self._root()
        payload = [
            _result(
                root,
                "frontend/src/ok.ts",
                source="export const ok = 1\n",
            ),
            _result(
                root,
                "frontend/src/app.ts",
                messages=[
                    {
                        "ruleId": "complexity",
                        "severity": 1,
                        "line": 4,
                        "column": 1,
                        "message": "Function 'App' has a complexity of 3.",
                    }
                ],
                source="export function App() { return 1 }\n",
            ),
        ]
        stdout = json.dumps(payload, ensure_ascii=False)
        got, errors = self._run(root, _completed(0, stdout))
        self.assertEqual(errors, [])
        self.assertEqual(got, payload)
        self.assertIsNot(got, payload)
        self.assertIn("source", got[0])
        self.assertEqual(got[0]["source"], "export const ok = 1\n")

    def test_nonzero_missing_rule_error_is_concise_and_drops_canaries(self) -> None:
        root = self._root()
        huge = SOURCE_CANARY + ("x" * 12000)
        payload = [
            _result(
                root,
                "frontend/src/api/client.ts",
                source=huge,
                extra=str(root / "frontend" / "src" / "api" / FILE_CANARY),
            ),
            _result(
                root,
                "frontend/src/features/inbox/useSourceCheckHistory.ts",
                messages=[
                    {
                        "ruleId": "react-hooks/exhaustive-deps",
                        "severity": 2,
                        "line": 201,
                        "column": 5,
                        "message": "Definition for rule 'react-hooks/exhaustive-deps' was not found.",
                    }
                ],
                source=huge,
            ),
        ]
        for index in range(40):
            payload.insert(
                0,
                _result(
                    root,
                    f"frontend/src/ok{index}.ts",
                    source=huge,
                    extra=str(root / "frontend" / "src" / f"{FILE_CANARY}-{index}.ts"),
                ),
            )
        stdout = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        self.assertGreater(len(stdout), 20_000)
        got, errors = self._run(root, _completed(1, stdout))
        self.assertIsNone(got)
        self.assertEqual(len(errors), 1)
        text = errors[0]
        self.assertTrue(text.startswith("frontend complexity command failed:"))
        self.assertIn("exit 1", text)
        self.assertIn("frontend/src/features/inbox/useSourceCheckHistory.ts", text)
        self.assertIn(":201 ", text)
        self.assertIn("react-hooks/exhaustive-deps", text)
        self.assertIn("Definition for rule 'react-hooks/exhaustive-deps' was not found.", text)
        self.assertNotIn(SOURCE_CANARY, text)
        self.assertNotIn(FILE_CANARY, text)
        self.assertNotIn('"source"', text)
        self.assertNotIn("filePath", text)
        self.assertLessEqual(len(text), 4096)

    def test_many_errors_cap_with_omission_count(self) -> None:
        root = self._root()
        messages_payload = []
        for index in range(27):
            messages_payload.append(
                _result(
                    root,
                    f"frontend/src/too-complex-{index:02d}.ts",
                    messages=[
                        {
                            "ruleId": f"custom/rule-{index:02d}",
                            "severity": 2,
                            "line": index + 1,
                            "column": 1,
                            "message": f"error {index:02d}",
                        }
                    ],
                )
            )
        got, errors = self._run(root, _completed(1, json.dumps(messages_payload)))
        self.assertIsNone(got)
        text = errors[0]
        self.assertIn("27 errors", text)
        self.assertIn("omitted 7 more diagnostic(s)", text)
        self.assertIn("frontend/src/too-complex-00.ts:1 custom/rule-00: error 00", text)
        self.assertIn("frontend/src/too-complex-19.ts:20 custom/rule-19: error 19", text)
        self.assertNotIn("too-complex-20.ts", text)
        self.assertNotIn("custom/rule-20", text)
        self.assertLessEqual(len(text), 4096)

    def test_warning_only_failure_stays_failed(self) -> None:
        root = self._root()
        payload = [
            _result(
                root,
                "frontend/src/app/App.tsx",
                messages=[
                    {
                        "ruleId": "max-lines-per-function",
                        "severity": 1,
                        "line": 166,
                        "column": 8,
                        "message": "Function 'App' has too many lines (462). Maximum allowed is 100.",
                    }
                ],
                source=SOURCE_CANARY,
            )
        ]
        got, errors = self._run(root, _completed(1, json.dumps(payload)))
        self.assertIsNone(got)
        self.assertTrue(errors)
        text = errors[0]
        self.assertTrue(text.startswith("frontend complexity command failed:"))
        self.assertIn("exit 1", text)
        self.assertIn("1 warning", text)
        self.assertIn("frontend/src/app/App.tsx", text)
        self.assertIn(":166 ", text)
        self.assertIn("max-lines-per-function", text)
        self.assertNotIn(SOURCE_CANARY, text)

    def test_errors_are_preferred_over_warnings(self) -> None:
        root = self._root()
        payload = [
            _result(
                root,
                "frontend/src/warn.ts",
                messages=[
                    {
                        "ruleId": "max-lines-per-function",
                        "severity": 1,
                        "line": 10,
                        "column": 1,
                        "message": "too long",
                    }
                ],
            ),
            _result(
                root,
                "frontend/src/fail.ts",
                messages=[
                    {
                        "ruleId": "no-undef",
                        "severity": 2,
                        "line": 3,
                        "column": 1,
                        "message": "missing",
                    }
                ],
            ),
        ]
        _, errors = self._run(root, _completed(1, json.dumps(payload)))
        text = errors[0]
        self.assertIn("1 error", text)
        self.assertIn("frontend/src/fail.ts:3 no-undef: missing", text)
        self.assertNotIn("max-lines-per-function", text)
        self.assertNotIn("warn.ts", text)

    def test_malformed_output_and_long_stderr_are_bounded(self) -> None:
        root = self._root()
        stderr = ("ESLINT_STDERR_CANARY " * 400) + ("Z" * 8000)
        got, errors = self._run(root, _completed(2, stdout="{not-json", stderr=stderr))
        self.assertIsNone(got)
        text = errors[0]
        self.assertTrue(text.startswith("frontend complexity command failed:"))
        self.assertIn("exit 2", text)
        self.assertIn("output is not JSON", text)
        self.assertIn("ESLINT_STDERR_CANARY", text)
        self.assertLessEqual(len(text), 4096)
        self.assertIn("truncated; failure text limited to 4096 characters", text)
        self.assertLess(text.count("Z"), 8000)

    def test_non_array_json_and_empty_diagnostics_remain_failed(self) -> None:
        root = self._root()
        dump = json.dumps({"filePath": FILE_CANARY, "source": SOURCE_CANARY, "ok": True})
        got, errors = self._run(
            root, _completed(1, stdout=dump, stderr="npx eslint failed to load plugin")
        )
        self.assertIsNone(got)
        text = errors[0]
        self.assertIn("exit 1", text)
        self.assertIn("output is not a JSON array", text)
        self.assertIn("npx eslint failed to load plugin", text)
        self.assertNotIn(FILE_CANARY, text)
        self.assertNotIn(SOURCE_CANARY, text)

        empty = [
            _result(root, "frontend/src/ok.ts", source=SOURCE_CANARY, extra=str(root / FILE_CANARY))
        ]
        got, errors = self._run(
            root,
            _completed(1, stdout=json.dumps(empty), stderr="max warnings exceeded"),
        )
        self.assertIsNone(got)
        text = errors[0]
        self.assertIn("exit 1", text)
        self.assertIn("no diagnostic messages", text)
        self.assertIn("max warnings exceeded", text)
        self.assertNotIn(SOURCE_CANARY, text)
        self.assertNotIn(FILE_CANARY, text)

    def test_utf8_messages_are_retained(self) -> None:
        root = self._root()
        payload = [
            _result(
                root,
                "frontend/src/hooks.ts",
                messages=[
                    {
                        "ruleId": "react-hooks/exhaustive-deps",
                        "severity": 2,
                        "line": 12,
                        "column": 5,
                        "message": "규칙 'react-hooks/exhaustive-deps' 정의를 찾을 수 없습니다.",
                    }
                ],
                source=SOURCE_CANARY,
            )
        ]
        stdout = json.dumps(payload, ensure_ascii=False)
        got, errors = self._run(root, _completed(1, stdout))
        self.assertIsNone(got)
        text = errors[0]
        self.assertIn("규칙 'react-hooks/exhaustive-deps' 정의를 찾을 수 없습니다.", text)
        self.assertIn("frontend/src/hooks.ts:12", text)
        self.assertNotIn(SOURCE_CANARY, text)

    def test_long_messages_are_bounded_and_total_stays_within_limit(self) -> None:
        root = self._root()
        payload = [
            _result(
                root,
                f"frontend/src/file-{index:02d}.ts",
                messages=[
                    {
                        "ruleId": "complexity",
                        "severity": 2,
                        "line": 4,
                        "column": 1,
                        "message": "M" * 800,
                    }
                ],
                source=SOURCE_CANARY,
            )
            for index in range(20)
        ]
        got, errors = self._run(root, _completed(1, json.dumps(payload)))
        self.assertIsNone(got)
        text = errors[0]
        self.assertLessEqual(len(text), 4096)
        self.assertIn("truncated; failure text limited to 4096 characters", text)
        self.assertNotIn("M" * 301, text)
        self.assertNotIn(SOURCE_CANARY, text)


if __name__ == "__main__":
    unittest.main()
