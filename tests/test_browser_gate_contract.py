from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"


class BrowserGateContractTest(unittest.TestCase):
    def test_axe_surfaces_are_split_into_independent_timeout_budgets(self) -> None:
        spec = (FRONTEND / "e2e" / "workstack.spec.ts").read_text(encoding="utf-8")

        self.assertIn("axeSurfaceCases", spec)
        self.assertIn("for (const { name, path } of axeSurfaceCases)", spec)
        self.assertNotIn("for (const path of surfaces)", spec)

    def test_compatibility_config_is_bounded_to_firefox_and_webkit(self) -> None:
        config = (FRONTEND / "playwright.compat.config.ts").read_text(encoding="utf-8")

        self.assertIn("compatibility.spec.ts", config)
        self.assertIn("Desktop Firefox", config)
        self.assertIn("Desktop Safari", config)
        self.assertNotIn("Desktop Chrome", config)

    def test_accessibility_regressions_cover_forced_colors_and_reflow(self) -> None:
        spec = (FRONTEND / "e2e" / "workstack.spec.ts").read_text(encoding="utf-8")

        self.assertIn("forcedColors: 'active'", spec)
        self.assertIn("width: 640, height: 480", spec)

    def test_ci_installs_and_runs_the_bounded_compatibility_gate(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "quality-reusable.yml").read_text(
            encoding="utf-8"
        )
        package = (FRONTEND / "package.json").read_text(encoding="utf-8")

        self.assertIn("playwright install chromium firefox webkit", workflow)
        self.assertIn("npm --prefix frontend run test:e2e:compat", workflow)
        self.assertIn('"test:e2e:compat"', package)

    def test_ci_cannot_mask_an_earlier_command_failure_with_a_later_success(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "quality-reusable.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("name: Install locked Python dependencies\n", workflow)
        self.assertIn("name: Install locked frontend dependencies\n", workflow)
        self.assertIn("name: Run frontend tests\n", workflow)
        self.assertIn("name: Build frontend\n", workflow)
        self.assertIn("name: Run Chromium product and accessibility gates\n", workflow)
        self.assertIn("name: Run Firefox and WebKit compatibility gates\n", workflow)


if __name__ == "__main__":
    unittest.main()
