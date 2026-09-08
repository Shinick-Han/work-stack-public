"""Native-window bridge for the desktop local-knowledge host.

This mixin owns initialization, trusted-origin dispatch after the host
origin check, the GUI-thread vault picker, worker callbacks, and
bounded-worker start/stop. Request decoding and registry work stay in
``knowledge_host``.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SHELL_DIRECTORY = Path(__file__).resolve().parent
_APPLICATION_ROOT = _SHELL_DIRECTORY.parents[1]
for _import_root in (_SHELL_DIRECTORY, _APPLICATION_ROOT):
    if str(_import_root) not in sys.path:
        sys.path.insert(0, str(_import_root))

from bounded_request_worker import BoundedRequestWorker
from knowledge_host import (
    KnowledgeHostService,
    correlate_knowledge_request,
    encode_knowledge_error_response,
    is_knowledge_host_message,
)


def post_workstack_web_message(host: object, response: str) -> None:
    core = (
        host.workstack_webview.CoreWebView2
        if host.workstack_webview is not None
        else None
    )
    if core is not None:
        core.PostWebMessageAsJson(response)


class KnowledgeDesktopMixin:
    """Native-window lifecycle for the knowledge host worker."""

    def _init_knowledge_desktop(self) -> None:
        self.knowledge_host = KnowledgeHostService(
            self.state_root,
            self._choose_knowledge_vault_directory,
            self._current_knowledge_workspace_uid,
        )
        self.knowledge_worker = BoundedRequestWorker(
            self._execute_knowledge_request,
            self._deliver_knowledge_response,
            maximum_pending=16,
            thread_name="workstack-knowledge-worker",
        )

    def _start_knowledge_desktop(self) -> None:
        self._init_knowledge_desktop()
        self.knowledge_worker.start()

    def _stop_knowledge_desktop(self, timeout: float) -> None:
        knowledge_worker = getattr(self, "knowledge_worker", None)
        if knowledge_worker is not None:
            knowledge_worker.stop(timeout=timeout)

    def _dispatch_knowledge_host_message(self, message: str) -> bool:
        if not is_knowledge_host_message(message):
            return False
        self._handle_knowledge_message(message)
        return True

    def _choose_knowledge_vault_directory(self) -> str | None:
        form = self.form
        if form is None or bool(getattr(form, "IsDisposed", False)):
            return None
        from System import Action
        from System.Windows.Forms import DialogResult, FolderBrowserDialog

        selected: list[str | None] = [None]

        def choose() -> None:
            dialog = FolderBrowserDialog()
            try:
                dialog.Description = "Choose a local Markdown vault"
                dialog.ShowNewFolderButton = False
                if dialog.ShowDialog(form) == DialogResult.OK:
                    selected[0] = str(dialog.SelectedPath)
            finally:
                dialog.Dispose()

        try:
            form.Invoke(Action(choose))
        except Exception as error:
            raise RuntimeError("The vault folder picker could not be opened") from error
        return selected[0]

    def _current_knowledge_workspace_uid(self) -> str | None:
        selection = getattr(self, "local_startup_selection", None)
        if selection is not None:
            uid = getattr(selection, "expected_workspace_id", None)
            if isinstance(uid, str) and uid:
                return uid
        profile = getattr(self, "remote_profile", None)
        if profile is not None:
            uid = getattr(profile, "workspace_id", None)
            if isinstance(uid, str) and uid:
                return uid
        return None

    def _handle_knowledge_message(self, message: str) -> None:
        worker = getattr(self, "knowledge_worker", None)
        if worker is not None and worker.submit(message):
            return
        host = getattr(self, "knowledge_host", None)
        if host is not None:
            response = host.overflow_response(message)
        else:
            correlation = correlate_knowledge_request(message)
            request_id, operation = correlation if correlation is not None else (None, None)
            response = encode_knowledge_error_response(
                request_id,
                operation,
                "busy" if correlation is not None else "invalid_request",
                "Knowledge host is busy. Try again shortly."
                if correlation is not None
                else "Knowledge request is invalid.",
            )
        self._post_knowledge_response(response)

    def _execute_knowledge_request(self, message: str) -> str:
        try:
            return self.knowledge_host.handle_json(message)
        except Exception:
            correlation = correlate_knowledge_request(message)
            request_id, operation = correlation if correlation is not None else (None, None)
            return encode_knowledge_error_response(
                request_id,
                operation,
                "internal_error",
                "Knowledge operation could not be completed.",
            )

    def _deliver_knowledge_response(self, response: str) -> None:
        form = self.form
        if form is None or bool(getattr(form, "IsDisposed", False)):
            return
        from System import Action

        def deliver_on_ui() -> None:
            current = self.form
            if current is not form or bool(getattr(current, "IsDisposed", False)):
                return
            self._post_knowledge_response(response)

        try:
            form.BeginInvoke(Action(deliver_on_ui))
        except Exception:
            self._trace("knowledge response could not be marshalled to the UI thread")

    def _post_knowledge_response(self, response: str) -> None:
        post_workstack_web_message(self, response)
