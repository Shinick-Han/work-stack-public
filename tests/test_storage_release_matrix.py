from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts import run_storage_regression


ROOT = Path(__file__).resolve().parents[1]


class StorageReleaseMatrixTests(unittest.TestCase):
    def test_matrix_names_every_wave_zero_through_seven_contract_suite(self) -> None:
        self.assertEqual(
            run_storage_regression.MATRIX_MODULES,
            (
                "tests.test_storage_v4_schema_artifacts",
                "tests.test_storage_canonical",
                "tests.test_store_v3_contract_inventory",
                "tests.test_storage_path_validation",
                "tests.test_storage_cross_invariants",
                "tests.test_storage_contracts_runtime",
                "tests.test_storage_v4_reader",
                "tests.test_storage_v4_manifest",
                "tests.test_storage_repository_admission",
                "tests.test_storage_semantic_parity",
                "tests.test_storage_migration_paths",
                "tests.test_storage_migration_source",
                "tests.test_storage_migration_conversion",
                "tests.test_storage_migration_idempotency",
                "tests.test_storage_migration",
                "tests.test_storage_runtime",
                "tests.test_storage_v4_journal",
                "tests.test_storage_v4_manifest_store",
                "tests.test_storage_idempotency",
                "tests.test_storage_v4_record_staging",
                "tests.test_storage_v4_stream_staging",
                "tests.test_storage_v4_write_session",
                "tests.test_storage_v4_write_session_faults",
                "tests.test_storage_v4_mutation_admission",
                "tests.test_storage_v4_domain_composition",
                "tests.test_storage_command_backend_support",
                "tests.test_storage_document_repository",
                "tests.test_experimental_v4_http_canary",
                "tests.test_storage_read_repository",
                "tests.test_storage_capture_reply_contract",
                "tests.test_service_capture_reply_backend",
                "tests.test_service_wave5_command_backends",
                "tests.test_service_wave5_slice56_backends",
                "tests.test_storage_intent_dual_backend",
                "tests.test_storage_task_contract",
                "tests.test_storage_task_relationship_repository",
                "tests.test_storage_projection",
                "tests.test_storage_query_repository",
                "tests.test_storage_v4_backup",
                "tests.test_cli_v4_backup_characterization",
                "tests.test_profile_inspection_v4",
                "tests.test_v4_activation_binding",
                "tests.test_storage_released_rollout_guard",
                "tests.test_cli_characterization",
            ),
        )

    def test_main_returns_runner_status(self) -> None:
        suite = Mock()
        with (
            patch.object(
                run_storage_regression.unittest.defaultTestLoader,
                "loadTestsFromNames",
                return_value=suite,
            ) as load,
            patch.object(run_storage_regression.unittest, "TextTestRunner") as runner_type,
        ):
            runner_type.return_value.run.return_value = SimpleNamespace(wasSuccessful=lambda: True)
            self.assertEqual(run_storage_regression.main(), 0)
            load.assert_called_once_with(run_storage_regression.MATRIX_MODULES)
            runner_type.assert_called_once_with(verbosity=2)
            runner_type.return_value.run.assert_called_once_with(suite)

            runner_type.return_value.run.return_value = SimpleNamespace(wasSuccessful=lambda: False)
            self.assertEqual(run_storage_regression.main(), 1)

    def test_reusable_quality_gate_runs_the_focused_matrix(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "quality-reusable.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("Run storage contract regression matrix", workflow)
        self.assertIn("python scripts/run_storage_regression.py", workflow)

        policy = (ROOT / "quality" / "release-path-policy.json").read_text(encoding="utf-8")
        self.assertIn('"quality"', policy)
        self.assertIn('"always_gates"', policy)


if __name__ == "__main__":
    unittest.main()
