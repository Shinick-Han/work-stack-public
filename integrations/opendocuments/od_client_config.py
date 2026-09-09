"""Trusted config, origin policy, and closed error shape for chat transport.

Internal helper for ``od_client.py``. Not a public Adapter API.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlparse

CHAT_PATH = "/api/v1/chat"
CORPUS_ONLY_PROFILE = "fast"

MAX_QUERY_CHARS = 1000
MAX_ORIGIN_CHARS = 512
MAX_API_KEY_CHARS = 256
MAX_WORKSPACE_ID_CHARS = 128
MAX_RESPONSE_BYTES = 1_048_576
MAX_SOURCES = 100
MAX_JSON_DEPTH = 32
MIN_TIMEOUT_SECONDS = 0.05
MAX_TIMEOUT_SECONDS = 120.0

CLOSED_CONFIG_KEYS = frozenset(
    {"origin", "api_key", "workspace_id", "profile", "timeout_seconds"}
)
ERROR_CODES = frozenset(
    {
        "invalid_query",
        "invalid_config",
        "origin_refused",
        "origin_unreachable",
        "redirect_refused",
        "response_too_large",
        "malformed_response",
        "auth_refused",
        "upstream_refused",
        "outcome_unknown",
    }
)

_CONTROL_RE = re.compile(r"[\0-\x1f\x7f]")
_API_KEY_RE = re.compile(r"\A[\x21-\x7E]{1,256}\Z")
_WORKSPACE_RE = re.compile(r"\A[A-Za-z0-9._:-]{1,128}\Z")
_DNS_HOST_RE = re.compile(r"\A[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\Z")
_IPV4_RE = re.compile(r"\A(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])(?:\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])){3}\Z")

_LOOPBACK_HTTP_HOSTS = frozenset({"127.0.0.1", "::1"})


@dataclass(frozen=True)
class TrustedBackendConfig:
    """Operator-pinned origin, key, workspace, and corpus-only profile.

    Key-to-workspace binding and profile behaviour are operator
    configuration. A chat response cannot prove them.
    """

    origin: str
    api_key: str
    workspace_id: str
    profile: str
    timeout_seconds: float

    def __repr__(self) -> str:
        return (
            "TrustedBackendConfig("
            f"origin={self.origin!r}, api_key=<redacted>, "
            f"workspace_id={self.workspace_id!r}, profile={self.profile!r}, "
            f"timeout_seconds={self.timeout_seconds!r})"
        )

    __str__ = __repr__


def _error(code: str, message: str) -> dict[str, Any]:
    if code not in ERROR_CODES:
        code = "outcome_unknown"
    return {"ok": False, "error": {"code": code, "message": message}}


def _closed_error(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    error = payload.get("error")
    return payload.get("ok") is False and isinstance(error, dict)


def _validate_query(query: object) -> dict[str, Any] | None:
    if not isinstance(query, str):
        return _error("invalid_query", "query is not a bounded nonempty string")
    if not query or query != query.strip() or len(query) > MAX_QUERY_CHARS:
        return _error("invalid_query", "query is not a bounded nonempty string")
    if _CONTROL_RE.search(query):
        return _error("invalid_query", "query is not a bounded nonempty string")
    return None


def _config_mapping(config: object) -> Mapping[str, Any] | dict[str, Any]:
    if isinstance(config, TrustedBackendConfig):
        return {
            "origin": config.origin,
            "api_key": config.api_key,
            "workspace_id": config.workspace_id,
            "profile": config.profile,
            "timeout_seconds": config.timeout_seconds,
        }
    if isinstance(config, dict):
        extra = set(config) - CLOSED_CONFIG_KEYS
        if extra:
            return _error("invalid_config", "unknown config fields are refused")
        missing = CLOSED_CONFIG_KEYS - set(config)
        if missing:
            return _error("invalid_config", "trusted config is incomplete")
        return config
    return _error("invalid_config", "trusted config is invalid")


def _origin_invalid(origin: object) -> bool:
    if not isinstance(origin, str) or not origin or len(origin) > MAX_ORIGIN_CHARS:
        return True
    if origin != origin.strip() or _CONTROL_RE.search(origin):
        return True
    return False


def _secrets_invalid(api_key: object, workspace_id: object, profile: object) -> bool:
    if not isinstance(api_key, str) or _API_KEY_RE.match(api_key) is None:
        return True
    if not isinstance(workspace_id, str) or _WORKSPACE_RE.match(workspace_id) is None:
        return True
    if profile != CORPUS_ONLY_PROFILE:
        return True
    return False


def _timeout_value(timeout_seconds: object) -> float | dict[str, Any]:
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
        return _error("invalid_config", "trusted config is invalid")
    timeout_value = float(timeout_seconds)
    if timeout_value != timeout_value or timeout_value in (float("inf"), float("-inf")):
        return _error("invalid_config", "trusted config is invalid")
    if timeout_value < MIN_TIMEOUT_SECONDS or timeout_value > MAX_TIMEOUT_SECONDS:
        return _error("invalid_config", "trusted config is invalid")
    return timeout_value


def _bind_config(config: object) -> TrustedBackendConfig | dict[str, Any]:
    values = _config_mapping(config)
    if _closed_error(values):
        return values
    if _origin_invalid(values.get("origin")):
        return _error("invalid_config", "trusted config is invalid")
    if _secrets_invalid(values.get("api_key"), values.get("workspace_id"), values.get("profile")):
        return _error("invalid_config", "trusted config is invalid")
    timeout_seconds = _timeout_value(values.get("timeout_seconds"))
    if not isinstance(timeout_seconds, float):
        return timeout_seconds
    return TrustedBackendConfig(
        origin=values["origin"],
        api_key=values["api_key"],
        workspace_id=values["workspace_id"],
        profile=values["profile"],
        timeout_seconds=timeout_seconds,
    )


def _url_parts_refused(parts: Any) -> bool:
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return True
    if parts.query or parts.fragment or parts.params:
        return True
    if parts.path not in {"", "/"}:
        return True
    return False


def _origin_port(parts: Any, scheme: str) -> int | None:
    try:
        port = parts.port
    except ValueError:
        return None
    if port is None:
        if scheme == "https":
            return 443
        return 80
    if port < 1 or port > 65535:
        return None
    return port


def _https_host_allowed(host: str) -> bool:
    if host in _LOOPBACK_HTTP_HOSTS:
        return True
    if _IPV4_RE.match(host):
        return True
    if ":" in host:
        return _is_ipv6_literal(host)
    return _DNS_HOST_RE.match(host) is not None and len(host) <= 253


def _is_ipv6_literal(host: str) -> bool:
    try:
        ipaddress.IPv6Address(host)
    except ValueError:
        return False
    return True


def _parse_origin(origin: str) -> tuple[str, str, int] | dict[str, Any]:
    refused = _error("origin_refused", "origin is not allowed")
    try:
        parts = urlparse(origin)
    except ValueError:
        return refused
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        return refused
    if _url_parts_refused(parts):
        return refused
    host = parts.hostname
    if not isinstance(host, str) or not host:
        return refused
    host = host.lower()
    port = _origin_port(parts, scheme)
    if port is None:
        return refused
    if scheme == "http":
        if host not in _LOOPBACK_HTTP_HOSTS:
            return refused
        return (scheme, host, port)
    if not _https_host_allowed(host):
        return refused
    return (scheme, host, port)
