from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


MATRIX_MODULES = (
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
)


def main() -> int:
    suite = unittest.defaultTestLoader.loadTestsFromNames(MATRIX_MODULES)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
