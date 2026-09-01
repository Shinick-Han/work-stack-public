"""Small serialized worker for bounded native WebView host operations."""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable


RequestHandler = Callable[[str], str]
ResponseDelivery = Callable[[str], None]


class BoundedRequestWorker:
    """Serialize blocking host work outside the WebView callback thread.

    The queue is deliberately bounded. Callers own the overflow response
    because only the versioned host contract can safely correlate it.
    """

    def __init__(
        self,
        handler: RequestHandler,
        deliver: ResponseDelivery,
        *,
        maximum_pending: int = 16,
        thread_name: str = "workstack-bounded-host-worker",
    ) -> None:
        if (
            isinstance(maximum_pending, bool)
            or not isinstance(maximum_pending, int)
            or not 1 <= maximum_pending <= 64
        ):
            raise ValueError("maximum_pending must be an integer from 1 to 64")
        if not thread_name or len(thread_name) > 100:
            raise ValueError("thread_name must be bounded non-empty text")
        self._handler = handler
        self._deliver = deliver
        self._requests: queue.Queue[str | object] = queue.Queue(maximum_pending)
        self._stop_marker = object()
        self._stop_event = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._thread_name = thread_name
        self._started_once = False

    @property
    def is_running(self) -> bool:
        with self._lifecycle_lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        with self._lifecycle_lock:
            if self._started_once:
                return False
            self._stop_event.clear()
            thread = threading.Thread(
                target=self._run_and_release,
                name=self._thread_name,
                daemon=True,
            )
            self._thread = thread
            self._started_once = True
            thread.start()
        return True

    def submit(self, request: str) -> bool:
        if not isinstance(request, str):
            raise TypeError("request must be text")
        with self._lifecycle_lock:
            running = (
                self._thread is not None
                and self._thread.is_alive()
                and not self._stop_event.is_set()
            )
        if not running:
            return False
        try:
            self._requests.put_nowait(request)
        except queue.Full:
            return False
        return True

    def stop(self, timeout: float | None = None) -> None:
        with self._lifecycle_lock:
            thread = self._thread
        if thread is None:
            return
        self._stop_event.set()
        try:
            self._requests.put_nowait(self._stop_marker)
        except queue.Full:
            # A daemon worker may finish its bounded queue after UI shutdown;
            # no new work is accepted once the thread reference is cleared.
            pass
        if thread is not threading.current_thread():
            thread.join(timeout)
        with self._lifecycle_lock:
            if self._thread is thread and not thread.is_alive():
                self._thread = None

    def _run_and_release(self) -> None:
        try:
            while True:
                request = self._requests.get()
                try:
                    if request is self._stop_marker:
                        return
                    try:
                        response = self._handler(str(request))
                    except Exception:
                        return
                    self._deliver(response)
                    if self._stop_event.is_set():
                        return
                finally:
                    self._requests.task_done()
        finally:
            with self._lifecycle_lock:
                if self._thread is threading.current_thread():
                    self._thread = None
