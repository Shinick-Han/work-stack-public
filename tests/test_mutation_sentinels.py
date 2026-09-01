from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class MutationSentinelContractTests(unittest.TestCase):
    def test_critical_sentinel_anchors_are_exact_unique_and_unmutated(self) -> None:
        path = ROOT / "scripts" / "run_mutation_sentinels.py"
        spec = importlib.util.spec_from_file_location("run_mutation_sentinels", path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader if spec else None)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        module.validate_sentinels(ROOT)
        self.assertEqual(
            [sentinel["id"] for sentinel in module.SENTINELS],
            [
                "revision-safe-integer-bound",
                "capture-microsoft-url-bound",
                "snapshot-byte-envelope",
            ],
        )


if __name__ == "__main__":
    unittest.main()
