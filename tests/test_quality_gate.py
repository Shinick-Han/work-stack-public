from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import ast

from scripts.quality_gate import (
    FILE_LINE_LIMIT,
    _discover,
    _python_graph,
    _python_module,
    _resolve_python_import,
    build_baseline,
    evaluate,
    load_config,
    measure,
)
from quality_typescript import index_typescript_functions


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

    def test_new_noncritical_python_ccn_offender_fails(self) -> None:
        temporary = self._repo()
        root = Path(temporary.name)
        config = load_config(root)
        baseline = build_baseline(measure(root, config), measurement_commit="abc123")
        branches = "\n".join(
            f"    if value == {index}:\n        return {index}" for index in range(16)
        )
        (root / "workstack" / "foundation.py").write_text(
            f"VALUE = 1\n\ndef risky(value):\n{branches}\n    return -1\n",
            encoding="utf-8",
        )
        errors = evaluate(measure(root, config), baseline)
        self.assertTrue(any("new function exceeds CCN 15" in error for error in errors))

    def test_existing_python_ccn_debt_increase_fails_and_decrease_passes(self) -> None:
        temporary = self._repo()
        root = Path(temporary.name)
        config = load_config(root)
        path = root / "workstack" / "foundation.py"

        def write_ccn(branches: int) -> None:
            body = "\n".join(
                f"    if value == {index}:\n        return {index}" for index in range(branches)
            )
            path.write_text(
                f"VALUE = 1\n\ndef risky(value):\n{body}\n    return -1\n",
                encoding="utf-8",
            )

        write_ccn(16)
        baseline = build_baseline(measure(root, config), measurement_commit="abc123")
        write_ccn(17)
        self.assertTrue(
            any("complexity increased" in error for error in evaluate(measure(root, config), baseline))
        )
        write_ccn(16)
        self.assertEqual([], evaluate(measure(root, config), baseline))
        write_ccn(14)
        self.assertEqual([], evaluate(measure(root, config), baseline))

    def test_new_stable_typescript_ccn_offender_fails(self) -> None:
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
                "critical": False,
                "stable": True,
            }
        }
        errors = evaluate(candidate, baseline)
        self.assertTrue(any("new TypeScript function exceeds CCN 15" in error for error in errors))

    def test_existing_stable_typescript_ccn_debt_increase_fails_and_decrease_passes(self) -> None:
        temporary = self._repo()
        root = Path(temporary.name)
        config = load_config(root)
        initial = measure(root, config)
        symbol = "frontend/src/app/main.ts::risky"
        item = {
            "path": "frontend/src/app/main.ts",
            "name": "risky",
            "line": 1,
            "ccn": 16,
            "critical": False,
            "stable": True,
        }
        initial["typescript_complexity"] = {symbol: item}
        baseline = build_baseline(initial, measurement_commit="abc123")
        increased = dict(initial)
        increased["typescript_complexity"] = {symbol: {**item, "ccn": 17}}
        self.assertTrue(
            any("TypeScript complexity increased" in error for error in evaluate(increased, baseline))
        )
        same = dict(initial)
        same["typescript_complexity"] = {symbol: item}
        self.assertEqual([], evaluate(same, baseline))
        reduced = dict(initial)
        reduced["typescript_complexity"] = {symbol: {**item, "ccn": 15}}
        self.assertEqual([], evaluate(reduced, baseline))

    def test_anonymous_typescript_cannot_collide_as_a_baseline_symbol(self) -> None:
        temporary = self._repo()
        root = Path(temporary.name)
        config = load_config(root)
        initial = measure(root, config)
        initial["typescript_complexity"] = {}
        baseline = build_baseline(initial, measurement_commit="abc123")
        anonymous = {
            "frontend/src/app/main.ts::<anonymous@1:1>": {
                "path": "frontend/src/app/main.ts",
                "name": "<anonymous@1:1>",
                "line": 1,
                "ccn": 20,
                "critical": True,
                "stable": False,
            }
        }
        candidate = dict(initial)
        candidate["typescript_complexity"] = anonymous
        baseline["typescript_complexity_debt"] = {
            "frontend/src/app/main.ts::<anonymous@1:1>": 20
        }
        self.assertEqual([], evaluate(candidate, baseline))
        mixed = dict(initial)
        mixed["typescript_complexity"] = {
            **anonymous,
            "frontend/src/app/main.ts::risky": {
                "path": "frontend/src/app/main.ts",
                "name": "risky",
                "line": 4,
                "ccn": 16,
                "critical": False,
                "stable": True,
            },
        }
        errors = evaluate(mixed, baseline)
        self.assertTrue(any("new TypeScript function exceeds CCN 15" in error for error in errors))
        self.assertFalse(any("anonymous" in error for error in errors))

    def _function_source(self, name: str, lines: int) -> str:
        inner = max(0, lines - 2)
        body = "\n".join(["    x = 1"] * inner)
        return f"VALUE = 1\n\ndef {name}():\n{body}\n    return x\n"

    def test_new_function_over_100_lines_fails(self) -> None:
        temporary = self._repo()
        root = Path(temporary.name)
        config = load_config(root)
        baseline = build_baseline(measure(root, config), measurement_commit="abc123")
        (root / "workstack" / "foundation.py").write_text(
            self._function_source("long_fn", 101), encoding="utf-8"
        )
        errors = evaluate(measure(root, config), baseline)
        self.assertTrue(any("new function exceeds 100 lines" in error for error in errors))

    def test_existing_long_function_growth_fails_and_reduction_passes(self) -> None:
        temporary = self._repo()
        root = Path(temporary.name)
        config = load_config(root)
        path = root / "workstack" / "foundation.py"
        path.write_text(self._function_source("long_fn", 101), encoding="utf-8")
        baseline = build_baseline(measure(root, config), measurement_commit="abc123")
        path.write_text(self._function_source("long_fn", 102), encoding="utf-8")
        self.assertTrue(
            any("function length increased" in error for error in evaluate(measure(root, config), baseline))
        )
        path.write_text(self._function_source("long_fn", 101), encoding="utf-8")
        self.assertEqual([], evaluate(measure(root, config), baseline))
        path.write_text(self._function_source("long_fn", 100), encoding="utf-8")
        self.assertEqual([], evaluate(measure(root, config), baseline))

    def test_new_typescript_function_over_100_lines_fails(self) -> None:
        temporary = self._repo()
        root = Path(temporary.name)
        config = load_config(root)
        initial = measure(root, config)
        baseline = build_baseline(initial, measurement_commit="abc123")
        candidate = dict(initial)
        candidate["typescript_functions"] = {
            "frontend/src/app/main.ts::longFn": {
                "path": "frontend/src/app/main.ts",
                "name": "longFn",
                "line": 1,
                "length": 101,
                "stable": True,
            }
        }
        errors = evaluate(candidate, baseline)
        self.assertTrue(
            any("new TypeScript function exceeds 100 lines" in error for error in errors)
        )

    def test_existing_typescript_function_length_growth_fails_and_reduction_passes(self) -> None:
        temporary = self._repo()
        root = Path(temporary.name)
        config = load_config(root)
        initial = measure(root, config)
        symbol = "frontend/src/app/main.ts::longFn"
        item = {
            "path": "frontend/src/app/main.ts",
            "name": "longFn",
            "line": 1,
            "length": 101,
            "stable": True,
        }
        initial["typescript_functions"] = {symbol: item}
        baseline = build_baseline(initial, measurement_commit="abc123")
        grown = dict(initial)
        grown["typescript_functions"] = {symbol: {**item, "length": 102}}
        self.assertTrue(
            any(
                "TypeScript function length increased" in error
                for error in evaluate(grown, baseline)
            )
        )
        self.assertEqual([], evaluate(initial, baseline))
        reduced = dict(initial)
        reduced["typescript_functions"] = {symbol: {**item, "length": 100}}
        self.assertEqual([], evaluate(reduced, baseline))

    def _write_counted_file(self, path: Path, lines: int, header: str = "VALUE = 1") -> None:
        path.write_text(
            "\n".join([header, *[f"# pad {index}" for index in range(lines - 1)]]) + "\n",
            encoding="utf-8",
        )

    def test_new_product_file_over_800_lines_fails(self) -> None:
        temporary = self._repo()
        root = Path(temporary.name)
        config_path = root / "quality" / "quality-config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["python_layers"] = [
            {"name": "py_all", "globs": ["workstack/**"], "may_import": []}
        ]
        config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        loaded = load_config(root)
        baseline = build_baseline(measure(root, loaded), measurement_commit="abc123")
        self._write_counted_file(root / "workstack" / "giant.py", 801, header="GIANT = 1")
        errors = evaluate(measure(root, loaded), baseline)
        self.assertTrue(
            any("new production file exceeds 800 lines" in error for error in errors)
        )

    def test_existing_giant_file_growth_fails_and_reduction_passes(self) -> None:
        temporary = self._repo()
        root = Path(temporary.name)
        config = load_config(root)
        path = root / "workstack" / "foundation.py"
        self._write_counted_file(path, 801)
        baseline = build_baseline(measure(root, config), measurement_commit="abc123")
        self._write_counted_file(path, 802)
        self.assertTrue(
            any(
                "production file length increased" in error
                for error in evaluate(measure(root, config), baseline)
            )
        )
        self._write_counted_file(path, 801)
        self.assertEqual([], evaluate(measure(root, config), baseline))
        self._write_counted_file(path, 800)
        self.assertEqual([], evaluate(measure(root, config), baseline))

    def test_provenance_only_edit_cannot_conceal_debt_or_config_mutation(self) -> None:
        temporary = self._repo()
        root = Path(temporary.name)
        config = load_config(root)
        clean = measure(root, config)
        baseline = build_baseline(clean, measurement_commit="abc123")
        branches = "\n".join(
            f"    if value == {index}:\n        return {index}" for index in range(16)
        )
        (root / "workstack" / "foundation.py").write_text(
            f"VALUE = 1\n\ndef risky(value):\n{branches}\n    return -1\n",
            encoding="utf-8",
        )
        dirty = measure(root, config)
        forged = dict(baseline)
        forged["measurement_commit"] = "forged-head"
        forged["measurement_source_digest"] = dirty["candidate_source_digest"]
        errors = evaluate(dirty, forged)
        self.assertTrue(any("new function exceeds CCN 15" in error for error in errors))
        emptied = dict(forged)
        emptied["python_complexity_debt"] = {}
        emptied["critical_complexity_debt"] = {}
        emptied["config_digest"] = dirty["config_digest"]
        self.assertTrue(
            any("new function exceeds CCN 15" in error for error in evaluate(dirty, emptied))
        )

    def _ts_item(
        self,
        name: str,
        containers: list[str | None],
        line: int,
        length: int = 101,
        path: str = "frontend/src/app/main.ts",
        computed: bool = False,
        anonymous: bool = False,
    ) -> dict:
        return {
            "path": path,
            "name": name,
            "containers": containers,
            "line": line,
            "column": 1,
            "length": length,
            "computed": computed,
            "anonymous": anonymous,
        }

    def test_two_class_constructors_are_separately_governed(self) -> None:
        temporary = self._repo()
        root = Path(temporary.name)
        config = load_config(root)
        initial = measure(root, config)
        baseline = build_baseline(initial, measurement_commit="abc123")
        functions, _diagnostic, errors = index_typescript_functions(
            [
                self._ts_item("constructor", ["Alpha"], 2),
                self._ts_item("constructor", ["Beta"], 40),
            ]
        )
        self.assertEqual(errors, [])
        self.assertEqual(
            set(functions),
            {
                "frontend/src/app/main.ts::Alpha.constructor",
                "frontend/src/app/main.ts::Beta.constructor",
            },
        )
        candidate = dict(initial)
        candidate["typescript_functions"] = functions
        findings = evaluate(candidate, baseline)
        self.assertTrue(any("Alpha.constructor" in item for item in findings))
        self.assertTrue(any("Beta.constructor" in item for item in findings))

    def test_same_method_name_in_two_classes_has_distinct_identities(self) -> None:
        functions, _diagnostic, errors = index_typescript_functions(
            [
                self._ts_item("receive", ["Alpha"], 4, length=12),
                self._ts_item("receive", ["Beta"], 30, length=12),
            ]
        )
        self.assertEqual(errors, [])
        self.assertEqual(
            set(functions),
            {
                "frontend/src/app/main.ts::Alpha.receive",
                "frontend/src/app/main.ts::Beta.receive",
            },
        )

    def test_reordering_classes_does_not_change_identities(self) -> None:
        alpha = self._ts_item("constructor", ["Alpha"], 2, length=8)
        beta = self._ts_item("constructor", ["Beta"], 20, length=8)
        first, _, errors_a = index_typescript_functions([alpha, beta])
        second, _, errors_b = index_typescript_functions([beta, alpha])
        self.assertEqual(errors_a, [])
        self.assertEqual(errors_b, [])
        self.assertEqual(set(first), set(second))
        self.assertEqual(set(first), {
            "frontend/src/app/main.ts::Alpha.constructor",
            "frontend/src/app/main.ts::Beta.constructor",
        })

    def test_named_lexical_function_cannot_escape_length_ratchet_by_collision(self) -> None:
        temporary = self._repo()
        root = Path(temporary.name)
        config = load_config(root)
        initial = measure(root, config)
        baseline = build_baseline(initial, measurement_commit="abc123")
        functions, diagnostic, identity_errors = index_typescript_functions(
            [
                self._ts_item("fail", ["Host"], 4, length=101),
                self._ts_item("fail", ["Host"], 120, length=140),
            ]
        )
        self.assertEqual(functions, {})
        self.assertTrue(any(item.get("category") == "unresolved" for item in diagnostic))
        self.assertEqual(
            identity_errors,
            ["unresolved TypeScript named identity: frontend/src/app/main.ts::Host.fail"],
        )
        candidate = dict(initial)
        candidate["typescript_functions"] = functions
        candidate["unresolved_typescript_identities"] = diagnostic
        candidate["config_errors"] = list(initial["config_errors"]) + identity_errors
        findings = evaluate(candidate, baseline)
        self.assertTrue(
            any("unresolved TypeScript named identity" in item for item in findings)
        )
        self.assertFalse(
            any("new TypeScript function exceeds 100 lines" in item for item in findings)
        )

    def test_q0_rollout_introduces_no_new_file_over_800_lines(self) -> None:
        import subprocess

        root = Path(__file__).resolve().parents[1]
        config = load_config(root)
        populations, errors = _discover(root, config)
        self.assertEqual(errors, [])
        crossed: list[str] = []
        for paths in populations.values():
            for relative in paths:
                current = len((root / relative).read_text(encoding="utf-8").splitlines())
                if current <= FILE_LINE_LIMIT:
                    continue
                shown = subprocess.run(
                    ["git", "show", f"HEAD:{relative}"],
                    cwd=root,
                    check=False,
                    capture_output=True,
                )
                sealed = (
                    len(shown.stdout.decode("utf-8", errors="replace").splitlines())
                    if shown.returncode == 0
                    else 0
                )
                if sealed <= FILE_LINE_LIMIT:
                    crossed.append(f"{relative} current={current} sealed={sealed}")
        self.assertEqual(crossed, [])

    def test_baseline_cannot_bless_file_that_crossed_800_from_sealed_head(self) -> None:
        temporary = self._repo()
        root = Path(temporary.name)
        config = load_config(root)
        report = measure(root, config)
        path = "workstack/foundation.py"
        candidate = dict(report)
        candidate["file_lengths"] = {**report["file_lengths"], path: 801}
        candidate["sealed_file_lengths"] = {
            **report.get("sealed_file_lengths", report["file_lengths"]),
            path: 676,
        }
        baseline = build_baseline(candidate, measurement_commit="abc123")
        self.assertNotIn(path, baseline["production_file_length_debt"])
        findings = evaluate(candidate, baseline)
        self.assertTrue(
            any("new production file exceeds 800 lines" in item for item in findings)
        )


if __name__ == "__main__":
    unittest.main()


class ResolverMeasurementTests(unittest.TestCase):
    """The dependency resolver must describe the real graph, not a guess."""

    def _measure(self, build) -> dict:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "workstack").mkdir()
        (root / "workstack" / "__init__.py").write_text("", encoding="utf-8")
        (root / "frontend" / "src" / "app").mkdir(parents=True)
        (root / "quality").mkdir()
        build(root)
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
                    "name": "frontend_app",
                    "roots": ["frontend/src"],
                    "extensions": [".ts", ".tsx"],
                    "exclude_globs": [],
                },
            ],
            "layers": [],
            "config_inputs": [],
        }
        return measure(root, config)

    # -- frontend ---------------------------------------------------------
    def _frontend(self, root: Path, name: str, body: str) -> None:
        (root / "frontend" / "src" / "app" / name).write_text(body, encoding="utf-8")

    def test_an_explicit_css_sibling_is_an_asset_not_a_self_cycle(self) -> None:
        def build(root: Path) -> None:
            for stem in ("DateInput", "GraphContextPopover"):
                self._frontend(
                    root, stem + ".tsx",
                    "import './%s.css'\nexport const %s = 1\n" % (stem, stem),
                )
                (root / "frontend" / "src" / "app" / (stem + ".css")).write_text(
                    ".x { color: red }\n", encoding="utf-8"
                )

        report = self._measure(build)
        self.assertEqual(report["dependency_cycles"]["frontend"], [], "no self cycle")
        self.assertEqual(
            [e for e in report["config_errors"] if "frontend" in e], [], "no error"
        )

    def test_an_existing_svg_asset_resolves_without_an_edge(self) -> None:
        def build(root: Path) -> None:
            (root / "frontend" / "src" / "assets").mkdir(parents=True)
            (root / "frontend" / "src" / "assets" / "mark.svg").write_text(
                "<svg/>\n", encoding="utf-8"
            )
            self._frontend(root, "brand.tsx", "import '../assets/mark.svg'\nexport const b = 1\n")

        report = self._measure(build)
        self.assertEqual(
            [e for e in report["config_errors"] if "frontend" in e], [], "svg resolves"
        )

    def test_a_missing_asset_is_still_reported(self) -> None:
        for label, specifier in (("svg", "../assets/absent.svg"), ("css", "./absent.css")):
            with self.subTest(asset=label):
                def build(root: Path, specifier=specifier) -> None:
                    self._frontend(
                        root, "widget.tsx",
                        "import '%s'\nexport const w = 1\n" % specifier,
                    )

                report = self._measure(build)
                self.assertTrue(
                    [e for e in report["config_errors"] if "frontend" in e],
                    "a missing asset must be reported",
                )

    def test_an_asset_above_the_root_that_does_not_exist_is_refused(self) -> None:
        def build(root: Path) -> None:
            self._frontend(
                root, "escape.tsx", "import '../../../outside.css'\nexport const e = 1\n"
            )

        report = self._measure(build)
        self.assertTrue(
            [e for e in report["config_errors"] if "frontend" in e],
            "a path escape must be refused",
        )

    def test_an_unsupported_extension_never_becomes_a_code_edge(self) -> None:
        def build(root: Path) -> None:
            self._frontend(root, "theme.ts", "export const theme = 1\n")
            self._frontend(root, "uses.tsx", "import './theme.scss'\nexport const u = 1\n")

        report = self._measure(build)
        self.assertTrue(
            [e for e in report["config_errors"] if "frontend" in e],
            "an unsupported extension stays unresolved",
        )

    def test_missing_and_healthy_code_specifiers_keep_their_behaviour(self) -> None:
        def build(root: Path) -> None:
            self._frontend(root, "model.ts", "export const value = 1\n")
            (root / "frontend" / "src" / "app" / "widget").mkdir()
            (root / "frontend" / "src" / "app" / "widget" / "index.tsx").write_text(
                "export const w = 1\n", encoding="utf-8"
            )
            self._frontend(
                root, "main.tsx",
                "import { value } from './model'\nimport { w } from './widget'\n"
                "import './model.ts'\nexport { value, w }\n",
            )

        report = self._measure(build)
        self.assertEqual(
            [e for e in report["config_errors"] if "frontend" in e], [], "all resolve"
        )

    def test_a_missing_code_module_is_still_reported(self) -> None:
        def build(root: Path) -> None:
            self._frontend(root, "main.tsx", "import { x } from './absent'\nexport { x }\n")

        report = self._measure(build)
        self.assertTrue([e for e in report["config_errors"] if "frontend" in e])

    # -- python -----------------------------------------------------------
    def _python(self, root: Path, name: str, body: str) -> None:
        (root / "workstack" / name).write_text(body, encoding="utf-8")

    def _python_edges(self, report: dict) -> dict:
        # The cycles view is not enough; re-derive edges from the same report by
        # asserting on cycles and errors only where the graph is not exposed.
        return report

    def test_named_submodules_resolve_relatively_and_absolutely(self) -> None:
        def build(root: Path) -> None:
            self._python(root, "alpha.py", "ALPHA = 1\n")
            self._python(root, "beta.py", "BETA = 2\n")
            self._python(
                root, "relative_user.py",
                "from . import alpha, beta\n\ndef use():\n    return alpha.ALPHA + beta.BETA\n",
            )
            self._python(
                root, "absolute_user.py",
                "from workstack import alpha, beta\n\ndef use():\n"
                "    return alpha.ALPHA + beta.BETA\n",
            )
            self._python(
                root, "aliased_user.py",
                "from . import alpha as a\n\ndef use():\n    return a.ALPHA\n",
            )

        report = self._measure(build)
        self.assertEqual(
            [e for e in report["config_errors"] if "Python" in e], [], "all resolve"
        )
        # A named submodule import must not be recorded as a cycle through the
        # package: alpha and beta import nothing.
        self.assertEqual(report["dependency_cycles"]["python"], [])

    def test_an_ordinary_symbol_import_keeps_the_module_edge(self) -> None:
        def build(root: Path) -> None:
            self._python(root, "alpha.py", "def helper():\n    return 1\n")
            self._python(
                root, "user.py",
                "from .alpha import helper\n\ndef use():\n    return helper()\n",
            )

        report = self._measure(build)
        self.assertEqual([e for e in report["config_errors"] if "Python" in e], [])

    def test_a_package_export_falls_back_to_the_package(self) -> None:
        def build(root: Path) -> None:
            (root / "workstack" / "__init__.py").write_text(
                "EXPORTED = 1\n", encoding="utf-8"
            )
            self._python(
                root, "user.py",
                "from workstack import EXPORTED\n\ndef use():\n    return EXPORTED\n",
            )

        report = self._measure(build)
        self.assertEqual([e for e in report["config_errors"] if "Python" in e], [])

    # -- the python resolver, observed directly ---------------------------
    MODULES = {
        "workstack",
        "workstack.alpha",
        "workstack.beta",
        "workstack.user",
    }

    def _resolved(self, current: str, source: str) -> set:
        node = ast.parse(source).body[0]
        return _resolve_python_import(current, node, self.MODULES)

    def test_the_resolver_names_relative_submodule_aliases(self) -> None:
        self.assertEqual(
            self._resolved("workstack.user", "from . import alpha, beta"),
            {"workstack.alpha", "workstack.beta"},
        )

    def test_the_resolver_names_absolute_submodule_aliases(self) -> None:
        self.assertEqual(
            self._resolved("workstack.user", "from workstack import alpha, beta"),
            {"workstack.alpha", "workstack.beta"},
        )

    def test_the_resolver_follows_an_as_alias_to_its_submodule(self) -> None:
        self.assertEqual(
            self._resolved("workstack.user", "from . import alpha as a"),
            {"workstack.alpha"},
        )

    def test_the_resolver_keeps_the_module_edge_for_a_symbol(self) -> None:
        self.assertEqual(
            self._resolved("workstack.user", "from .alpha import helper"),
            {"workstack.alpha"},
        )

    def test_the_resolver_falls_back_to_the_package_for_an_export(self) -> None:
        self.assertEqual(
            self._resolved("workstack.user", "from workstack import EXPORTED"),
            {"workstack"},
        )

    def test_the_resolver_keeps_plain_imports(self) -> None:
        self.assertEqual(
            self._resolved("workstack.user", "import workstack.alpha"),
            {"workstack.alpha"},
        )


class ResolverCompletionTests(unittest.TestCase):
    """Per-alias fallback, package context and a true escape."""

    # Reused deliberately by reference rather than by inheritance, so the suite
    # does not re-collect the earlier class's cases under a second name.
    MODULES = ResolverMeasurementTests.MODULES
    _measure = ResolverMeasurementTests._measure
    _frontend = ResolverMeasurementTests._frontend
    _resolved = ResolverMeasurementTests._resolved

    def _measure_with_outside(self, build, outside_name: str = "outside.css") -> dict:
        """Like _measure, but also creates a real file ABOVE the measured root.

        The file lives in this fixture's own temporary tree, one level outside
        the directory the gate measures, so an escaping specifier really does
        point at something that exists.
        """

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        outer = Path(temporary.name)
        (outer / outside_name).write_text(".x { color: red }\n", encoding="utf-8")
        root = outer / "repo"
        (root / "workstack").mkdir(parents=True)
        (root / "workstack" / "__init__.py").write_text("", encoding="utf-8")
        (root / "frontend" / "src" / "app").mkdir(parents=True)
        (root / "quality").mkdir()
        build(root)
        config = {
            "schema_version": 1,
            "source_sets": [
                {"name": "python_core", "roots": ["workstack"], "extensions": [".py"],
                 "exclude_globs": []},
                {"name": "frontend_app", "roots": ["frontend/src"],
                 "extensions": [".ts", ".tsx"], "exclude_globs": []},
            ],
            "layers": [],
            "config_inputs": [],
        }
        self.outer = outer
        self.measured_root = root
        return measure(root, config)

    def test_an_existing_asset_outside_the_root_is_refused(self) -> None:
        def build(root: Path) -> None:
            self._frontend(
                root, "escape.tsx",
                "import '../../../../outside.css'\nexport const e = 1\n",
            )
            (root / "frontend" / "src" / "app" / "inside.css").write_text(
                ".y { color: blue }\n", encoding="utf-8"
            )
            self._frontend(
                root, "healthy.tsx", "import './inside.css'\nexport const h = 1\n"
            )

        report = self._measure_with_outside(build)
        # The escaping specifier really does resolve to an existing file.
        target = (self.measured_root / "frontend" / "src" / "app"
                  / ".." / ".." / ".." / ".." / "outside.css").resolve()
        self.assertTrue(target.is_file(), "the outside file exists")
        self.assertEqual(target, (self.outer / "outside.css").resolve())
        self.assertFalse(
            str(target).startswith(str(self.measured_root.resolve())),
            "and it really is outside the measured root",
        )
        errors = [e for e in report["config_errors"] if "escape.tsx" in e]
        self.assertTrue(errors, "an existing file outside the root is still refused")
        self.assertEqual(
            [e for e in report["config_errors"] if "healthy.tsx" in e],
            [],
            "the inside asset still resolves",
        )

    # -- QR-F1: every alias keeps its own resolution ----------------------
    def _architecture(self, build) -> dict:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "workstack").mkdir()
        (root / "quality").mkdir()
        build(root)
        config = {
            "schema_version": 1,
            "source_sets": [
                {"name": "python_core", "roots": ["workstack"], "extensions": [".py"],
                 "exclude_globs": []},
            ],
            "python_layers": [
                {"name": "package", "globs": ["workstack/__init__.py"], "may_import": []},
                {"name": "child", "globs": ["workstack/alpha.py"], "may_import": []},
                {"name": "consumer", "globs": ["workstack/consumer.py"],
                 "may_import": ["child"]},
            ],
            "layers": [],
            "config_inputs": [],
        }
        return measure(root, config)

    def test_a_mixed_import_keeps_both_the_submodule_and_package_edges(self) -> None:
        def build(root: Path) -> None:
            (root / "workstack" / "__init__.py").write_text(
                "EXPORTED = 1\n", encoding="utf-8"
            )
            (root / "workstack" / "alpha.py").write_text("ALPHA = 1\n", encoding="utf-8")
            (root / "workstack" / "consumer.py").write_text(
                "from workstack import alpha, EXPORTED\n\n"
                "def use():\n    return alpha.ALPHA + EXPORTED\n",
                encoding="utf-8",
            )

        report = self._architecture(build)
        # consumer may import child but NOT package, so the package edge that
        # EXPORTED creates must surface as an architecture violation.
        violations = [v for v in report["architecture_violations"] if "consumer" in v]
        self.assertTrue(
            violations,
            "the package edge for a non-module alias must not be dropped",
        )

    def test_a_named_only_import_reports_no_package_edge(self) -> None:
        def build(root: Path) -> None:
            (root / "workstack" / "__init__.py").write_text("", encoding="utf-8")
            (root / "workstack" / "alpha.py").write_text("ALPHA = 1\n", encoding="utf-8")
            (root / "workstack" / "consumer.py").write_text(
                "from workstack import alpha\n\ndef use():\n    return alpha.ALPHA\n",
                encoding="utf-8",
            )

        report = self._architecture(build)
        self.assertEqual(
            [v for v in report["architecture_violations"] if "consumer" in v],
            [],
            "a named-only import creates no package edge",
        )

    def test_the_resolver_keeps_each_alias_resolution(self) -> None:
        self.assertEqual(
            self._resolved("workstack.user", "from workstack import alpha, EXPORTED"),
            {"workstack.alpha", "workstack"},
        )
        self.assertEqual(
            self._resolved("workstack.user", "from . import alpha, EXPORTED"),
            {"workstack.alpha", "workstack"},
        )
        self.assertEqual(
            self._resolved("workstack.user", "from . import alpha as a, EXPORTED as e"),
            {"workstack.alpha", "workstack"},
        )

    # -- QR-F2: a package initializer resolves from its own package -------
    def test_a_package_initializer_resolves_its_own_submodules(self) -> None:
        def build(root: Path) -> None:
            (root / "workstack" / "pkg").mkdir()
            (root / "workstack" / "pkg" / "alpha.py").write_text(
                "ALPHA = 1\n", encoding="utf-8"
            )
            (root / "workstack" / "pkg" / "__init__.py").write_text(
                "from . import alpha\n\ndef use():\n    return alpha.ALPHA\n",
                encoding="utf-8",
            )
            (root / "workstack" / "alpha.py").write_text(
                "ALPHA = 'the outer one'\n", encoding="utf-8"
            )

        config_layers = {
            "schema_version": 1,
            "source_sets": [
                {"name": "python_core", "roots": ["workstack"], "extensions": [".py"],
                 "exclude_globs": []},
            ],
            "python_layers": [
                {"name": "inner", "globs": ["workstack/pkg/*.py"], "may_import": ["inner"]},
                {"name": "outer", "globs": ["workstack/alpha.py"], "may_import": []},
            ],
            "layers": [],
            "config_inputs": [],
        }
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "workstack").mkdir()
        (root / "workstack" / "__init__.py").write_text("", encoding="utf-8")
        (root / "quality").mkdir()
        build(root)
        report = measure(root, config_layers)
        # The initializer imports its OWN sibling, which is inner->inner and
        # allowed. Resolving to the outer workstack.alpha would be inner->outer
        # and would surface as a violation.
        self.assertEqual(
            [v for v in report["architecture_violations"] if "pkg/__init__" in v],
            [],
            "a package initializer must resolve its own submodule",
        )

    def test_an_ordinary_module_keeps_its_relative_behaviour(self) -> None:
        def build(root: Path) -> None:
            (root / "workstack" / "pkg").mkdir()
            (root / "workstack" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
            (root / "workstack" / "pkg" / "alpha.py").write_text(
                "ALPHA = 1\n", encoding="utf-8"
            )
            (root / "workstack" / "pkg" / "user.py").write_text(
                "from . import alpha\n\ndef use():\n    return alpha.ALPHA\n",
                encoding="utf-8",
            )

        report = self._measure(build)
        self.assertEqual(
            [e for e in report["config_errors"] if "Python" in e], [], "resolves"
        )


DESKTOP = "desktop/python-webview-shell"


class DesktopPackageIdentityTests(unittest.TestCase):
    """The desktop shell is a script root, but everything below it is a package.

    Its immediate files are imported by bare name, so their identity drops the two
    leading directory components. Nested files keep their relative components as a
    dotted identity, and __init__.py identifies its containing package. Returning
    the bare stem for every descendant collapsed each initializer to "__init__" and
    each same-named child to its basename.
    """

    CONFIG = {
        "schema_version": 1,
        "source_sets": [
            {"name": "desktop", "roots": [DESKTOP], "extensions": [".py"],
             "exclude_globs": []},
            {"name": "python_core", "roots": ["workstack"], "extensions": [".py"],
             "exclude_globs": []},
        ],
        "layers": [],
        "config_inputs": [],
    }

    def _build(self, files: dict) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for relative, body in files.items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
        (root / "quality").mkdir(exist_ok=True)
        for relative in files:
            # Assert the resolved path first, so a fixture that was never written
            # cannot masquerade as a missing edge.
            self.assertTrue((root / relative).is_file(), relative)
        return root

    def _graph(self, root: Path, files: dict) -> dict:
        graph, _complexity, errors = _python_graph(root, sorted(files), [])
        self.assertEqual(errors, [], "no unresolved internal imports")
        for relative in files:
            self.assertIn(relative, graph, "every file is a graph node")
        return graph

    def _cycles(self, root: Path) -> list:
        return measure(root, self.CONFIG)["dependency_cycles"]["python"]

    def test_a_nested_desktop_package_imports_its_child_not_itself(self):
        files = {
            DESKTOP + "/generated/__init__.py":
                "from .theme_tokens import THEME_TOKENS\n\n"
                "__all__ = [\"THEME_TOKENS\"]\n",
            DESKTOP + "/generated/theme_tokens.py": "THEME_TOKENS: dict = {}\n",
            DESKTOP + "/workstack_desktop.py": "VALUE = 1\n",
            "workstack/__init__.py": "",
        }
        root = self._build(files)
        initializer = DESKTOP + "/generated/__init__.py"
        child = DESKTOP + "/generated/theme_tokens.py"
        self.assertEqual(_python_module(initializer), "generated")
        self.assertEqual(_python_module(child), "generated.theme_tokens")
        graph = self._graph(root, files)
        self.assertEqual(graph[initializer], {child}, "the genuine child edge")
        self.assertNotIn(initializer, graph[initializer], "and no self edge")
        self.assertEqual(self._cycles(root), [])

    def test_an_immediate_desktop_module_keeps_its_bare_name(self):
        files = {
            DESKTOP + "/connection_registry_mutations.py": "def mutate():\n    return 1\n",
            DESKTOP + "/connection_registry_activation_recovery.py":
                "from connection_registry_mutations import mutate\n\n"
                "def use():\n    return mutate()\n",
            "workstack/__init__.py": "",
        }
        root = self._build(files)
        importer = DESKTOP + "/connection_registry_activation_recovery.py"
        target = DESKTOP + "/connection_registry_mutations.py"
        self.assertEqual(_python_module(target), "connection_registry_mutations")
        self.assertEqual(self._graph(root, files)[importer], {target})

    def test_an_ordinary_package_initializer_is_unaffected(self):
        files = {
            "workstack/__init__.py": "",
            "workstack/pkg/__init__.py": "from . import alpha\n",
            "workstack/pkg/alpha.py": "ALPHA = 1\n",
            "workstack/alpha.py": "ALPHA = 'outer'\n",
        }
        root = self._build(files)
        self.assertEqual(_python_module("workstack/pkg/__init__.py"), "workstack.pkg")
        graph = self._graph(root, files)
        self.assertEqual(graph["workstack/pkg/__init__.py"], {"workstack/pkg/alpha.py"})
        self.assertEqual(graph["workstack/alpha.py"], set())

    def test_two_nested_packages_with_the_same_child_stay_distinct(self):
        files = {
            DESKTOP + "/one/__init__.py": "from .child import VALUE\n",
            DESKTOP + "/one/child.py": "VALUE = 1\n",
            DESKTOP + "/two/__init__.py": "from .child import VALUE\n",
            DESKTOP + "/two/child.py": "VALUE = 2\n",
            "workstack/__init__.py": "",
        }
        root = self._build(files)
        self.assertEqual(
            {_python_module(name) for name in files if name.startswith(DESKTOP)},
            {"one", "one.child", "two", "two.child"},
        )
        graph = self._graph(root, files)
        self.assertEqual(graph[DESKTOP + "/one/__init__.py"], {DESKTOP + "/one/child.py"})
        self.assertEqual(graph[DESKTOP + "/two/__init__.py"], {DESKTOP + "/two/child.py"})
        self.assertEqual(self._cycles(root), [])

    def test_a_genuine_cycle_between_nested_children_is_reported(self):
        files = {
            DESKTOP + "/one/__init__.py": "",
            DESKTOP + "/one/child.py": "from two.child import VALUE\n\nOWN = 1\n",
            DESKTOP + "/two/__init__.py": "",
            DESKTOP + "/two/child.py": "from one.child import OWN\n\nVALUE = 2\n",
            "workstack/__init__.py": "",
        }
        root = self._build(files)
        graph = self._graph(root, files)
        self.assertEqual(graph[DESKTOP + "/one/child.py"], {DESKTOP + "/two/child.py"})
        self.assertEqual(graph[DESKTOP + "/two/child.py"], {DESKTOP + "/one/child.py"})
        cycles = self._cycles(root)
        self.assertEqual(len(cycles), 1, cycles)
        self.assertEqual(
            set(cycles[0]), {DESKTOP + "/one/child.py", DESKTOP + "/two/child.py"}
        )

    def test_the_real_generated_package_resolves_to_its_child(self):
        repository = Path(__file__).resolve().parents[1]
        initializer = DESKTOP + "/generated/__init__.py"
        child = DESKTOP + "/generated/theme_tokens.py"
        self.assertTrue((repository / initializer).is_file())
        self.assertTrue((repository / child).is_file())
        self.assertEqual(_python_module(initializer), "generated")
        self.assertEqual(_python_module(child), "generated.theme_tokens")
        # Parsed only; the generated module is never imported or executed.
        graph, _complexity, errors = _python_graph(
            repository,
            sorted(
                path.relative_to(repository).as_posix()
                for path in (repository / DESKTOP).rglob("*.py")
            ),
            [],
        )
        # Only the desktop files are measured here, so this file set cannot
        # resolve their cross-root workstack imports; those errors are expected
        # and unrelated. Nothing in the generated package may be unresolved.
        self.assertEqual(
            [item for item in errors if "/generated/" in item], []
        )
        self.assertEqual(graph[initializer], {child})
        self.assertNotIn(initializer, graph[initializer])
