"""Exclusive-local CLI reads match WorkStack; owner-held reads refuse."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from workstack import cli, cli_reads
from workstack.service import WorkStack
from workstack.store import LOCK_NAME, Store, _FileLease


class ReadParity(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.root = self.home / "data"
        self.runtime = self.home / "runtime"
        self.scratch = self.home / "tmp"
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.scratch.mkdir(parents=True, exist_ok=True)
        self._saved = {
            name: os.environ.get(name)
            for name in ("WORK_STACK_RUNTIME", "TEMP", "TMP", "TMPDIR")
        }
        os.environ["WORK_STACK_RUNTIME"] = str(self.runtime)
        os.environ["TEMP"] = str(self.scratch)
        os.environ["TMP"] = str(self.scratch)
        os.environ["TMPDIR"] = str(self.scratch)
        self.addCleanup(self._restore)
        self.store = Store(self.root)
        self.stack = WorkStack(self.store)
        created = self.stack.add_task("Parity task", "", "P2", None, [], [], None, [])
        self.stack.add_objective("Parity objective", "2026-Q3")
        self.stack.checkin("09:00", "2026-09-05")
        self.stack.add_worklog(
            created["id"], ["done item"], ["next item"], [], "2026-09-05"
        )

    def _restore(self) -> None:
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def run_cli(self, *arguments: str) -> tuple[int, object, str]:
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--data-dir", str(self.root), *arguments])
        payload = json.loads(out.getvalue()) if out.getvalue().strip() else None
        return code, payload, err.getvalue()

    def test_backlog_list_and_show_match_the_workstack_read_api(self) -> None:
        code, listed, err = self.run_cli("backlog", "list", "--status", "all")
        self.assertEqual(code, 0, msg=err)
        self.assertEqual(listed, self.stack.list_tasks("all"))
        task_id = listed[0]["id"]
        code, shown, err = self.run_cli("backlog", "show", task_id)
        self.assertEqual(code, 0, msg=err)
        self.assertEqual(shown, self.stack.get_task(task_id))

    def test_okr_worklog_and_weekly_match_where_supported(self) -> None:
        code, objectives, err = self.run_cli("okr", "list", "--status", "all")
        self.assertEqual(code, 0, msg=err)
        self.assertEqual(objectives, self.stack.list_objectives("all"))
        code, rollup, err = self.run_cli("okr", "rollup")
        self.assertEqual(code, 0, msg=err)
        self.assertEqual(rollup, self.stack.objective_rollup())
        code, worklog, err = self.run_cli("worklog", "list", "--date", "2026-09-05")
        self.assertEqual(code, 0, msg=err)
        self.assertEqual(worklog, self.stack.list_worklog("2026-09-05"))
        code, weekly, err = self.run_cli(
            "weekly", "--end", "2026-09-05", "--days", "7"
        )
        self.assertEqual(code, 0, msg=err)
        self.assertEqual(weekly, self.stack.weekly_report("2026-09-05", 7))

    def test_owner_http_read_parity_is_empty_and_list_refuses_under_owner(self) -> None:
        self.assertEqual(cli_reads.OWNER_HTTP_READ_PARITY, frozenset())
        self.assertTrue(cli_reads.is_parity_read("backlog.list"))
        self.store.write_server_info("127.0.0.1", 9)
        blocker = _FileLease(self.root / LOCK_NAME)
        blocker.acquire()
        self.addCleanup(blocker.release)
        code, payload, err = self.run_cli("okr", "list")
        self.assertEqual(code, 2)
        self.assertIsNone(payload)
        self.assertIn(cli_reads.OWNER_READ_REFUSAL, err)


if __name__ == "__main__":
    unittest.main()
