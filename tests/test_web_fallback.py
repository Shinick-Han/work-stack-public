from __future__ import annotations

import unittest
from pathlib import Path


WEB_INDEX = Path(__file__).resolve().parents[1] / "web" / "index.html"


class LegacyWebFallbackTest(unittest.TestCase):
    def test_task_submit_retains_one_key_through_transport_and_user_retry(self) -> None:
        source = WEB_INDEX.read_text(encoding="utf-8")

        self.assertIn('form data-path="/api/v1/tasks"', source)
        self.assertIn("form.dataset.pendingKey||createKey()", source)
        self.assertIn("form.dataset.pendingKey=key", source)
        self.assertIn("if(!idempotencyKey)throw firstError", source)
        self.assertIn("button.disabled=true", source)
        self.assertIn("delete form.dataset.pendingKey", source)
        self.assertIn('mutate(`/api/v1/tasks/${id}`,"PATCH",{status,revision:task.revision})', source)


if __name__ == "__main__":
    unittest.main()
