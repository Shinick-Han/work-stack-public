"""Recognizing, correlating and answering connection-registry host requests.

Mixed into `WorkStackDesktopHost`, which supplies `connection_registry_worker`,
`connection_registry_host`, `form` and `_trace`.
"""

from __future__ import annotations

import re
import uuid

from connection_registry_host_contract import (
    MAX_HOST_REQUEST_BYTES as CONNECTION_REGISTRY_MAX_REQUEST_BYTES,
    RegistryHostErrorResponse,
    encode_registry_host_response,
)
from knowledge_desktop import post_workstack_web_message


_REGISTRY_TYPE_PATTERN = re.compile(
    r'"type"\s*:\s*"workstack-connection-registry-request"'
)
_REGISTRY_REQUEST_ID_PATTERN = re.compile(
    r'"request_id"\s*:\s*"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"'
)
_REGISTRY_OPERATION_PATTERN = re.compile(
    r'"operation"\s*:\s*"(get-registry|save-registry|discover-ssh-aliases|choose-local-directory|test-profile|activate-profile)"'
)


def is_connection_registry_host_message(message: str) -> bool:
    if not isinstance(message, str) or not message.lstrip().startswith("{"):
        return False
    try:
        encoded = message.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return bool(_REGISTRY_TYPE_PATTERN.search(message[:4096]))


class ConnectionRegistryBridgeMixin:
    """Registry request handling for the desktop host that mixes it in."""

    def _handle_connection_registry_message(self, message: str) -> None:
        if self.connection_registry_worker.submit(message):
            return
        response = self._connection_registry_busy_response(message)
        self._post_connection_registry_response(response)

    def _execute_connection_registry_request(self, message: str) -> str:
        try:
            return self.connection_registry_host.handle_json(message)
        except Exception:
            correlation = self._connection_registry_correlation(message)
            request_id, operation = correlation if correlation is not None else (None, None)
            return encode_registry_host_response(
                RegistryHostErrorResponse(
                    request_id=request_id,
                    operation=operation,
                    code="internal_error",
                    message="Connection registry operation could not be completed.",
                )
            )

    @staticmethod
    def _connection_registry_busy_response(message: str) -> str:
        correlation = ConnectionRegistryBridgeMixin._connection_registry_correlation(message)
        if correlation is None:
            return encode_registry_host_response(
                RegistryHostErrorResponse(
                    request_id=None,
                    operation=None,
                    code="invalid_request",
                    message="Connection registry request is invalid.",
                )
            )
        request_id, operation = correlation
        return encode_registry_host_response(
            RegistryHostErrorResponse(
                request_id=request_id,
                operation=operation,
                code="busy",
                message="Connection registry is busy. Try again shortly.",
            )
        )

    @staticmethod
    def _connection_registry_correlation(message: str) -> tuple[str, str] | None:
        if not isinstance(message, str) or len(message) > CONNECTION_REGISTRY_MAX_REQUEST_BYTES:
            return None
        prefix = message[:4096]
        if len(_REGISTRY_TYPE_PATTERN.findall(prefix)) != 1:
            return None
        request_ids = _REGISTRY_REQUEST_ID_PATTERN.findall(prefix)
        operations = _REGISTRY_OPERATION_PATTERN.findall(prefix)
        if len(request_ids) != 1 or len(operations) != 1:
            return None
        try:
            request_id = str(uuid.UUID(request_ids[0]))
        except ValueError:
            return None
        if request_id != request_ids[0] or uuid.UUID(request_id).int == 0:
            return None
        return request_id, operations[0]

    def _deliver_connection_registry_response(self, response: str) -> None:
        form = self.form
        if form is None or bool(getattr(form, "IsDisposed", False)):
            return
        from System import Action

        def deliver_on_ui() -> None:
            current = self.form
            if current is not form or bool(getattr(current, "IsDisposed", False)):
                return
            self._post_connection_registry_response(response)

        try:
            form.BeginInvoke(Action(deliver_on_ui))
        except Exception:
            self._trace("connection registry response could not be marshalled to the UI thread")

    def _post_connection_registry_response(self, response: str) -> None:
        post_workstack_web_message(self, response)

    def _choose_local_ssot_directory(self) -> str | None:
        form = self.form
        if form is None or bool(getattr(form, "IsDisposed", False)):
            return None
        from System import Action
        from System.Windows.Forms import DialogResult, FolderBrowserDialog

        selected: list[str | None] = [None]

        def choose() -> None:
            dialog = FolderBrowserDialog()
            try:
                dialog.Description = "Choose a Work Stack SSOT directory"
                dialog.ShowNewFolderButton = True
                if dialog.ShowDialog(form) == DialogResult.OK:
                    selected[0] = str(dialog.SelectedPath)
            finally:
                dialog.Dispose()

        try:
            form.Invoke(Action(choose))
        except Exception as error:
            raise RuntimeError("The local SSOT directory picker could not be opened") from error
        return selected[0]

    @staticmethod
    def _save_connection_registry_from_host(_state_root, _registry):
        raise RuntimeError(
            "Connection registry mutations are disabled until activation safety is released"
        )
