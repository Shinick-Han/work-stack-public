from __future__ import annotations

import unittest

from scripts.check_coverage import evaluate, evaluate_changed


def metric(percent: float, covered: int = 1) -> dict:
    return {
        "percent_covered": percent,
        "percent_branches_covered": percent,
        "covered_lines": covered,
    }


def frontend_metric(percent: float, covered: int = 1) -> dict:
    return {
        "lines": {"pct": percent, "covered": covered},
        "branches": {"pct": percent, "covered": covered},
        "functions": {"pct": percent, "covered": covered},
    }


class CoverageGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.floors = {
            "schema_version": 1,
            "python": {
                "global": {"lines": 60, "branches": 50},
                "critical": {"workstack/core.py": {"lines": 70, "branches": 60}},
            },
            "frontend": {
                "global": {"lines": 70, "branches": 60, "functions": 50},
                "critical": {"src/api/client.ts": {"lines": 75, "branches": 65}},
            },
        }
        self.python = {
            "totals": metric(80),
            "files": {"workstack\\core.py": {"summary": metric(80)}},
        }
        self.frontend = {
            "total": frontend_metric(80),
            "C:\\repo\\frontend\\src\\api\\client.ts": frontend_metric(80),
        }

    def test_current_floors_pass(self) -> None:
        self.assertEqual(([], []), evaluate(self.python, self.frontend, self.floors))

    def test_global_regression_fails(self) -> None:
        self.python["totals"] = metric(49)
        errors, _ = evaluate(self.python, self.frontend, self.floors)
        self.assertTrue(any("Python global" in error for error in errors))

    def test_missing_critical_report_fails_closed(self) -> None:
        self.frontend.pop("C:\\repo\\frontend\\src\\api\\client.ts")
        errors, _ = evaluate(self.python, self.frontend, self.floors)
        self.assertTrue(any("critical file missing" in error for error in errors))

    def test_zero_coverage_noncritical_file_warns_without_blocking(self) -> None:
        self.python["files"]["workstack\\optional.py"] = {"summary": metric(0, covered=0)}
        errors, warnings = evaluate(self.python, self.frontend, self.floors)
        self.assertEqual([], errors)
        self.assertTrue(any("zero covered lines" in warning for warning in warnings))

    def test_changed_critical_file_blocks_but_noncritical_only_warns(self) -> None:
        python = {
            "files": {
                "workstack\\core.py": {
                    "executed_lines": [1],
                    "missing_lines": [2],
                    "executed_branches": [[1, 2]],
                    "missing_branches": [[1, 3]],
                },
                "workstack\\optional.py": {
                    "executed_lines": [1],
                    "missing_lines": [2],
                    "executed_branches": [],
                    "missing_branches": [],
                },
            },
        }
        errors, warnings = evaluate_changed(
            {"workstack/core.py": {1, 2}, "workstack/optional.py": {1, 2}},
            python,
            {},
            self.floors,
        )
        self.assertTrue(any("workstack/core.py" in error for error in errors))
        self.assertTrue(any("workstack/optional.py" in warning for warning in warnings))


if __name__ == "__main__":
    unittest.main()
