"""Thin OpenDocuments chat transport for the separate Adapter process.

This module is not Work Stack core. It issues exactly one
``POST /api/v1/chat`` using operator-pinned trusted configuration. The
parsed upstream JSON is returned only as an ephemeral in-process object
for a trusted mapper. See ``CLIENT.md``.
"""

from __future__ import annotations

import http.client
import importlib.util
import json
import sys
import time
from pathlib import Path
from typing import Any, Mapping


def _load_by_path(stem: str) -> Any:
    module_name = f"opendocuments_{stem}"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    path = Path(__file__).resolve().parent / f"{stem}.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


try:
    from . import od_client_config as _config
    from . import od_client_transport as _transport
except ImportError:
    _config = _load_by_path("od_client_config")
    _transport = _load_by_path("od_client_transport")

CHAT_PATH = _config.CHAT_PATH
CORPUS_ONLY_PROFILE = _config.CORPUS_ONLY_PROFILE
MAX_QUERY_CHARS = _config.MAX_QUERY_CHARS
MAX_ORIGIN_CHARS = _config.MAX_ORIGIN_CHARS
MAX_API_KEY_CHARS = _config.MAX_API_KEY_CHARS
MAX_WORKSPACE_ID_CHARS = _config.MAX_WORKSPACE_ID_CHARS
MAX_RESPONSE_BYTES = _config.MAX_RESPONSE_BYTES
MAX_SOURCES = _config.MAX_SOURCES
MAX_JSON_DEPTH = _config.MAX_JSON_DEPTH
MIN_TIMEOUT_SECONDS = _config.MIN_TIMEOUT_SECONDS
MAX_TIMEOUT_SECONDS = _config.MAX_TIMEOUT_SECONDS
CLOSED_CONFIG_KEYS = _config.CLOSED_CONFIG_KEYS
ERROR_CODES = _config.ERROR_CODES
TrustedBackendConfig = _config.TrustedBackendConfig
EphemeralChatResponse = _transport.EphemeralChatResponse


def post_opendocuments_chat(
    query: str, config: TrustedBackendConfig | Mapping[str, Any]
) -> dict[str, Any]:
    """POST ``/api/v1/chat`` using trusted config. Query is the only caller input.

    The function accepts no URL, route, workspace, profile, conversation,
    or collection override besides the trusted config object. It never
    retries a POST after timeout or connection loss.
    """

    error = _config._validate_query(query)
    if error is not None:
        return error
    bound = _config._bind_config(config)
    if not isinstance(bound, TrustedBackendConfig):
        return bound
    parsed = _config._parse_origin(bound.origin)
    if not isinstance(parsed, tuple):
        return parsed
    scheme, host, port = parsed
    deadline = time.monotonic() + float(bound.timeout_seconds)
    body = json.dumps(
        {
            "query": query,
            "profile": bound.profile,
            "workspaceId": bound.workspace_id,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Connection": "close",
        "X-API-Key": bound.api_key,
    }
    exchanged = _transport._exchange_chat(scheme, host, port, deadline, body, headers)
    if not isinstance(exchanged, tuple):
        return exchanged
    status, raw = exchanged
    return _transport._result_from_status(status, raw)
