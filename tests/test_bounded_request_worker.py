from __future__ import annotations

import importlib.util
import sys
import threading
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "desktop" / "python-webview-shell" / "bounded_request_worker.py"
SPEC = importlib.util.spec_from_file_location("bounded_request_worker_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class BoundedRequestWorkerTest(unittest.TestCase):
    def test_serializes_requests_off_the_calling_thread(self) -> None:
        delivered: list[str] = []
        handled_threads: list[int] = []
        complete = threading.Event()

        def handle(request: str) -> str:
            handled_threads.append(threading.get_ident())
            return request.upper()

        def deliver(response: str) -> None:
            delivered.append(response)
            if len(delivered) == 2:
                complete.set()

        caller_thread = threading.get_ident()
        worker = MODULE.BoundedRequestWorker(handle, deliver, maximum_pending=2)
        self.assertTrue(worker.start())
        self.assertFalse(worker.start())
        self.assertTrue(worker.submit("one"))
        self.assertTrue(worker.submit("two"))
        self.assertTrue(complete.wait(2))
        worker.stop(2)

        self.assertEqual(delivered, ["ONE", "TWO"])
        self.assertTrue(all(value != caller_thread for value in handled_threads))
        self.assertFalse(worker.is_running)

    def test_rejects_submission_when_stopped_and_bounds_options(self) -> None:
        worker = MODULE.BoundedRequestWorker(lambda value: value, lambda _value: None)
        self.assertFalse(worker.submit("request"))
        with self.assertRaises(TypeError):
            worker.submit(1)
        for invalid in (0, 65, True):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                MODULE.BoundedRequestWorker(
                    lambda value: value,
                    lambda _value: None,
                    maximum_pending=invalid,
                )

    def test_worker_cannot_restart_and_replay_queued_requests_after_stop(self) -> None:
        worker = MODULE.BoundedRequestWorker(lambda value: value, lambda _value: None)
        self.assertTrue(worker.start())
        worker.stop(1)
        self.assertFalse(worker.start())
        self.assertFalse(worker.submit("stale"))

    def test_handler_failure_does_not_run_later_delivery_out_of_order(self) -> None:
        delivered: list[str] = []
        complete = threading.Event()

        def handle(request: str) -> str:
            if request == "bad":
                raise RuntimeError("expected test failure")
            return request

        worker = MODULE.BoundedRequestWorker(handle, delivered.append)
        worker.start()
        worker.submit("bad")
        worker.submit("later")
        # The worker fails closed rather than delivering an uncorrelated later
        # response after its serialized handler contract was violated.
        complete.wait(0.05)
        worker.stop(1)
        self.assertEqual(delivered, [])

    def test_stop_timeout_does_not_hide_a_still_running_handler(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def handle(request: str) -> str:
            entered.set()
            release.wait(2)
            return request

        worker = MODULE.BoundedRequestWorker(handle, lambda _value: None)
        worker.start()
        worker.submit("request")
        self.assertTrue(entered.wait(1))
        worker.stop(0.01)
        self.assertTrue(worker.is_running)
        release.set()
        for _attempt in range(100):
            if not worker.is_running:
                break
            threading.Event().wait(0.01)
        self.assertFalse(worker.is_running)


if __name__ == "__main__":
    unittest.main()
