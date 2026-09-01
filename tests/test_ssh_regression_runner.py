from __future__ import annotations

import unittest
from unittest import mock

from scripts import run_ssh_regression


class SshRegressionRunnerTests(unittest.TestCase):
    def test_default_gate_is_deterministic_and_does_not_require_wsl(self) -> None:
        with (
            mock.patch.object(run_ssh_regression, "run_unit_matrix", return_value=True) as unit,
            mock.patch.object(run_ssh_regression, "run_wsl_canary") as canary,
        ):
            self.assertEqual(run_ssh_regression.main([]), 0)
        unit.assert_called_once_with()
        canary.assert_not_called()

    def test_wsl_canary_runs_only_after_the_unit_matrix_passes(self) -> None:
        with (
            mock.patch.object(run_ssh_regression, "run_unit_matrix", return_value=True),
            mock.patch.object(run_ssh_regression, "run_wsl_canary", return_value=0) as canary,
        ):
            self.assertEqual(
                run_ssh_regression.main(["--wsl-distro", "Ubuntu"]), 0
            )
        canary.assert_called_once_with("Ubuntu")

    def test_failed_unit_matrix_never_touches_external_wsl_state(self) -> None:
        with (
            mock.patch.object(run_ssh_regression, "run_unit_matrix", return_value=False),
            mock.patch.object(run_ssh_regression, "run_wsl_canary") as canary,
        ):
            self.assertEqual(
                run_ssh_regression.main(["--wsl-distro", "Ubuntu"]), 1
            )
        canary.assert_not_called()


if __name__ == "__main__":
    unittest.main()
