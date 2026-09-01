from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.quality_gate import build_baseline, evaluate, load_config, measure


class QualityGateTests(unittest.TestCase):
    def _repo(self) -> tempfile.TemporaryDirectory[str]:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        (root / "workstack").mkdir()
        (root / "workstack" / "__init__.py").write_text("", encoding="utf-8")
        (root / "workstack" / "foundation.py").write_text("VALUE = 1\n", encoding="utf-8")
        (root / "workstack" / "service.py").write_text(
            "from workstack.foundation import VALUE\n\ndef use_value():\n    return VALUE\n",
            encoding="utf-8",
        )
        (root / "frontend" / "src" / "domain").mkdir(parents=True)
        (root / "frontend" / "src" / "app").mkdir(parents=True)
        (root / "frontend" / "src" / "domain" / "model.ts").write_text(
            "export const value = 1\n", encoding="utf-8"
        )
        (root / "frontend" / "src" / "app" / "main.ts").write_text(
            "import { value } from '../domain/model'\nexport { value }\n", encoding="utf-8"
        )
        (root / "quality").mkdir()
        config = {
            "schema_version": 1,
            "source_sets": [
                {
                    "name": "python_core",
                    "roots": ["workstack"],
                    "extensions": [".py"],
                    "exclude_globs": [],
                },
                {
                    "name": "frontend",
                    "roots": ["frontend/src"],
                    "extensions": [".ts", ".tsx"],
                    "exclude_globs": ["**/*.test.ts", "**/*.test.tsx"],
                },
            ],
            "config_inputs": ["quality/quality-config.json"],
            "critical_python_globs": ["workstack/service.py"],
            "python_layers": [
                {
                    "name": "foundation",
                    "globs": ["workstack/__init__.py", "workstack/foundation.py"],
                    "may_import": [],
                },
                {
                    "name": "application",
                    "globs": ["workstack/service.py"],
                    "may_import": ["foundation"],
                },
            ],
            "frontend_layers": [
                {"name": "domain", "globs": ["frontend/src/domain/**"], "may_import": []},
                {
                    "name": "app",
                    "globs": ["frontend/src/app/**"],
                    "may_import": ["domain"],
                },
            ],
            "architecture_exceptions": [],
        }
        (root / "quality" / "quality-config.json").write_text(
            json.dumps(config, indent=2) + "\n", encoding="utf-8"
        )
        self.addCleanup(temporary.cleanup)
        return temporary

    def test_source_changes_do_not_require_a_new_baseline(self) -> None:
        temporary = self._repo()
        root = Path(temporary.name)
        config = load_config(root)
        initial = measure(root, config)
        baseline = build_baseline(initial, measurement_commit="abc123")

        service = root / "workstack" / "service.py"
        service.write_text(service.read_text(encoding="utf-8") + "\n# harmless change\n", encoding="utf-8")
        candidate = measure(root, config)

        self.assertNotEqual(initial["candidate_source_digest"], candidate["candidate_source_digest"])
        self.assertEqual([], evaluate(candidate, baseline))

    def test_config_digest_mismatch_fails(self) -> None:
        temporary = self._repo()
        root = Path(temporary.name)
        config = load_config(root)
        report = measure(root, config)
        baseline = build_baseline(report, measurement_commit="abc123")
        baseline["config_digest"] = "0" * 64

        errors = evaluate(report, baseline)

        self.assertTrue(any("config_digest" in error for error in errors))

    def test_config_digest_is_independent_of_crlf_checkout_policy(self) -> None:
        temporary = self._repo()
        root = Path(temporary.name)
        config_path = root / "quality" / "quality-config.json"
        payload = config_path.read_bytes().replace(b"\r\n", b"\n")
        config_path.write_bytes(payload)
        lf_report = measure(root, load_config(root))

        config_path.write_bytes(payload.replace(b"\n", b"\r\n"))
        crlf_report = measure(root, load_config(root))

        self.assertEqual(lf_report["config_digest"], crlf_report["config_digest"])

    def test_line_shift_does_not_rename_critical_function(self) -> None:
        temporary = self._repo()
        root = Path(temporary.name)
        config = load_config(root)
        service = root / "workstack" / "service.py"
        branches = "\n".join(
            f"    if value == {index}:\n        return {index}" for index in range(16)
        )
        body = f"def risky(value):\n{branches}\n    return -1\n"
        service.write_text(body, encoding="utf-8")
        baseline = build_baseline(measure(root, config), measurement_commit="abc123")

        service.write_text("# module comment\n\n" + body, encoding="utf-8")
        report = measure(root, config)

        self.assertEqual([], evaluate(report, baseline))

    def test_unclassified_source_fails(self) -> None:
        temporary = self._repo()
        root = Path(temporary.name)
        (root / "frontend" / "src" / "new-area").mkdir()
        (root / "frontend" / "src" / "new-area" / "orphan.ts").write_text(
            "export const orphan = true\n", encoding="utf-8"
        )

        report = measure(root, load_config(root))

        self.assertIn("frontend/src/new-area/orphan.ts", report["unclassified_files"])

    def test_dependency_cycle_fails(self) -> None:
        temporary = self._repo()
        root = Path(temporary.name)
        (root / "workstack" / "foundation.py").write_text(
            "from workstack.service import use_value\nVALUE = use_value\n", encoding="utf-8"
        )
        report = measure(root, load_config(root))
        baseline = build_baseline(report, measurement_commit="abc123")

        errors = evaluate(report, baseline)

        self.assertTrue(any("cycle" in error.lower() for error in errors))

    def test_reverse_layer_import_fails(self) -> None:
        temporary = self._repo()
        root = Path(temporary.name)
        (root / "workstack" / "foundation.py").write_text(
            "from workstack.service import use_value\nVALUE = use_value\n", encoding="utf-8"
        )

        report = measure(root, load_config(root))

        self.assertTrue(report["architecture_violations"])

    def test_unresolved_relative_frontend_import_fails(self) -> None:
        temporary = self._repo()
        root = Path(temporary.name)
        (root / "frontend" / "src" / "app" / "main.ts").write_text(
            "import { missing } from '../domain/missing'\nexport { missing }\n",
            encoding="utf-8",
        )

        report = measure(root, load_config(root))

        self.assertTrue(any("unresolved frontend import" in error for error in report["config_errors"]))

    def test_critical_complexity_above_fifteen_fails(self) -> None:
        temporary = self._repo()
        root = Path(temporary.name)
        config = load_config(root)
        baseline = build_baseline(measure(root, config), measurement_commit="abc123")
        branches = "\n".join(
            f"    if value == {index}:\n        return {index}" for index in range(16)
        )
        (root / "workstack" / "service.py").write_text(
            f"def risky(value):\n{branches}\n    return -1\n", encoding="utf-8"
        )
        report = measure(root, config)

        errors = evaluate(report, baseline)

        self.assertTrue(any("CCN" in error for error in errors))

    def test_critical_frontend_complexity_above_fifteen_fails(self) -> None:
        temporary = self._repo()
        root = Path(temporary.name)
        config = load_config(root)
        initial = measure(root, config)
        initial["typescript_complexity"] = {}
        baseline = build_baseline(initial, measurement_commit="abc123")
        candidate = dict(initial)
        candidate["typescript_complexity"] = {
            "frontend/src/app/main.ts::risky": {
                "path": "frontend/src/app/main.ts",
                "name": "risky",
                "line": 1,
                "ccn": 16,
                "critical": True,
                "stable": True,
            }
        }

        errors = evaluate(candidate, baseline)

        self.assertTrue(any("TypeScript" in error and "CCN 16" in error for error in errors))

    def test_existing_critical_frontend_complexity_cannot_increase(self) -> None:
        temporary = self._repo()
        root = Path(temporary.name)
        config = load_config(root)
        initial = measure(root, config)
        symbol = "frontend/src/app/main.ts::risky"
        initial["typescript_complexity"] = {
            symbol: {
                "path": "frontend/src/app/main.ts",
                "name": "risky",
                "line": 1,
                "ccn": 16,
                "critical": True,
                "stable": True,
            }
        }
        baseline = build_baseline(initial, measurement_commit="abc123")
        candidate = dict(initial)
        candidate["typescript_complexity"] = {
            symbol: {**initial["typescript_complexity"][symbol], "ccn": 17}
        }

        errors = evaluate(candidate, baseline)

        self.assertTrue(any("TypeScript complexity increased" in error for error in errors))

    def test_anonymous_frontend_complexity_remains_diagnostic(self) -> None:
        temporary = self._repo()
        root = Path(temporary.name)
        config = load_config(root)
        initial = measure(root, config)
        initial["typescript_complexity"] = {}
        baseline = build_baseline(initial, measurement_commit="abc123")
        candidate = dict(initial)
        candidate["typescript_complexity"] = {
            "frontend/src/app/main.ts::<anonymous@1:1>": {
                "path": "frontend/src/app/main.ts",
                "name": "<anonymous@1:1>",
                "line": 1,
                "ccn": 20,
                "critical": True,
                "stable": False,
            }
        }

        self.assertEqual([], evaluate(candidate, baseline))


if __name__ == "__main__":
    unittest.main()
