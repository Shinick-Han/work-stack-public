"""Capture Packet v1 validation and sanitized allow-list projection."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit


FORBIDDEN_KEYS = {
    "body", "html", "content", "attachments", "raw", "transcript", "recipients"
}
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
TASK_ID_RE = re.compile(r"^T-\d{4,}$", re.I)
RFC3339_RE = re.compile(
    r"^(?P<date_time>\d{4}-\d{2}-\d{2}T"
    r"(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d)"
    r"(?:\.(?P<fraction>\d+))?"
    r"(?P<offset>Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)$"
)
EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
HTML_RE = re.compile(r"<\/?[A-Za-z][^>]*>")
HEADER_RE = re.compile(r"(?im)^(?:from|to|cc|bcc|subject|sent|date):\s*.+$")
QUOTED_REPLY_RE = re.compile(r"(?im)^on .{1,240} wrote:\s*$")
QUOTE_LINE_RE = re.compile(r"(?m)^\s*>.*$")
CANARY_RE = re.compile(r"(?:RAW|ATTACHMENT)_CANARY_DO_NOT_STORE", re.I)
ALLOWED_MICROSOFT_SUFFIXES = (
    ".office.com",
    ".office365.com",
    ".microsoft.com",
    ".microsoftonline.com",
    ".sharepoint.com",
    ".cloud.microsoft",
)
ALLOWED_MICROSOFT_EXACT_HOSTS = frozenset({
    "login.live.com",
    "onedrive.live.com",
    "outlook.live.com",
    "teams.live.com",
})
VALIDATION_PERCENT_DECODE_ROUNDS = 5
URL_CREDENTIAL_NAMES = {
    "accesstoken",
    "refreshtoken",
    "idtoken",
    "oauthtoken",
    "oauthcode",
    "authorization",
    "authorizationcode",
    "bearer",
    "token",
    "clientsecret",
    "password",
    "passwd",
    "apikey",
    "secret",
    "code",
}
URL_CREDENTIAL_ASSIGNMENT_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:"
    r"(?:access|refresh|id|oauth)[_.-]?token|"
    r"(?:oauth|authorization)[_.-]?code|"
    r"authorization|bearer|token|client[_.-]?secret|"
    r"password|passwd|api[_.-]?key|secret|code"
    r")(?![A-Za-z0-9])[\"']?\s*[:=]"
)
CREDENTIAL_VALUE_RE = re.compile(
    r"(?ix)(?:"
    r"\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{12,}"
    r"|(?<![A-Za-z0-9])(?:"
    r"(?:access|refresh|id|oauth)[_.-]?token|"
    r"client[_.-]?(?:secret|assertion)|authorization|bearer|token|"
    r"password|passwd|api[_.-]?key|secret|saml[_.-]?response"
    r")(?![A-Za-z0-9])[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9._~+/=-]{12,}"
    r"|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
    r"|-----BEGIN\s+[A-Z ]*PRIVATE\s+KEY-----"
    r")"
)
RECIPIENT_ASSIGNMENT_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:to|cc|bcc|recipients?)(?![A-Za-z0-9])\s*[:=]"
)
ALLOWED_TOOL_CLAIMS = {
    "m365.outlook.read",
    "m365.teams.read",
    "m365.sharepoint.read",
    "workstack.capture.write",
}
PROVIDER_TOOL = {
    "microsoft-outlook": "m365.outlook.read",
    "microsoft-teams": "m365.teams.read",
    "microsoft-sharepoint": "m365.sharepoint.read",
}


class CaptureValidationError(ValueError):
    def __init__(self, message: str, code: str = "invalid_capture", details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


class PercentDecodingLimitError(ValueError):
    """Raised when a retained value exceeds the bounded validation decode depth."""


@dataclass(frozen=True, order=True)
class _RFC3339Instant:
    """An exact UTC instant, including fractions beyond datetime microseconds."""

    utc_second: dt.datetime
    fraction: str


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def source_key_for(source: dict[str, Any]) -> str:
    text = "\n".join(
        source[field]
        for field in ("provider", "connection_ref", "container_ref", "object_ref")
    )
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def fingerprint_for(source: dict[str, Any]) -> str:
    text = "\n".join(
        source[field]
        for field in (
            "provider", "connection_ref", "container_ref", "object_ref", "version_ref"
        )
    )
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_rfc3339(value: str, field: str) -> _RFC3339Instant:
    match = RFC3339_RE.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise CaptureValidationError("{} must be strict RFC3339".format(field))
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = dt.datetime.fromisoformat(candidate)
        utc_second = parsed.astimezone(dt.timezone.utc).replace(microsecond=0)
    except (OverflowError, ValueError) as error:
        raise CaptureValidationError(
            "{} must contain a valid RFC3339 date, time, and offset".format(field)
        ) from error
    fraction = (match.group("fraction") or "").rstrip("0")
    return _RFC3339Instant(utc_second, fraction)


def _reject_forbidden_keys(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise CaptureValidationError("JSON object keys must be strings")
            if key.casefold() in FORBIDDEN_KEYS:
                raise CaptureValidationError(
                    "forbidden field at {}.{}".format(path, key),
                    code="forbidden_capture_field",
                    details={"path": "{}.{}".format(path, key)},
                )
            _reject_forbidden_keys(child, "{}.{}".format(path, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_forbidden_keys(child, "{}[{}]".format(path, index))


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CaptureValidationError("{} must be an object".format(field))
    return value


def _string(
    value: Any,
    field: str,
    *,
    maximum: int,
    required: bool = True,
) -> str:
    if not isinstance(value, str):
        raise CaptureValidationError("{} must be a string".format(field))
    if required and not value:
        raise CaptureValidationError("{} is required".format(field))
    if len(value) > maximum:
        raise CaptureValidationError("{} exceeds {} characters".format(field, maximum))
    return value


def _decoded_text(value: str, field: str) -> str:
    try:
        return decoded_for_validation(value)
    except PercentDecodingLimitError as error:
        raise CaptureValidationError(
            "{} exceeds the percent-encoding validation depth".format(field),
            code="encoded_content_too_deep",
            details={"field": field},
        ) from error


def _validate_safe_text_view(decoded: str, field: str) -> None:
    if credential_material_in_decoded_text(decoded):
        raise CaptureValidationError(
            "{} appears to contain credential material".format(field),
            code="credential_material_suspected",
            details={"field": field},
        )
    if RECIPIENT_ASSIGNMENT_RE.search(decoded):
        raise CaptureValidationError(
            "{} appears to contain recipient material".format(field),
            code="raw_content_suspected",
            details={"field": field},
        )
    header_matches = HEADER_RE.findall(decoded)
    quote_lines = QUOTE_LINE_RE.findall(decoded)
    suspected = (
        EMAIL_RE.search(decoded)
        or HTML_RE.search(decoded)
        or QUOTED_REPLY_RE.search(decoded)
        or CANARY_RE.search(decoded)
        or len(header_matches) >= 2
        or len(quote_lines) >= 4
        or re.search(r"[\"“][^\"”\n]{600,}[\"”]", decoded)
    )
    if suspected:
        raise CaptureValidationError(
            "{} appears to contain raw source content".format(field),
            code="raw_content_suspected",
            details={"field": field},
        )


def _safe_text(value: str, field: str) -> str:
    decoded = _decoded_text(value, field)
    _validate_safe_text_view(decoded, field)
    return value


def _safe_required(value: Any, field: str, maximum: int) -> str:
    return _safe_text(_string(value, field, maximum=maximum), field)


def _safe_metadata(value: Any, field: str, maximum: int) -> str:
    """Validate retained metadata with the same raw-content gate as display text."""

    return _safe_required(value, field, maximum)


def decoded_for_validation(value: str) -> str:
    """Decode URL escaping for validation, rejecting values beyond the bound."""

    decoded = value
    for _ in range(VALIDATION_PERCENT_DECODE_ROUNDS):
        candidate = unquote(decoded)
        if candidate == decoded:
            return decoded
        decoded = candidate
    if unquote(decoded) != decoded:
        raise PercentDecodingLimitError(
            "percent encoding exceeds {} layers".format(
                VALIDATION_PERCENT_DECODE_ROUNDS
            )
        )
    return decoded


def credential_material_in_decoded_text(value: str) -> bool:
    """Inspect a caller-supplied canonical decoded text view."""

    return bool(CREDENTIAL_VALUE_RE.search(value))


def text_contains_credential_material(value: str) -> bool:
    """Detect explicit/token-shaped secrets without rejecting ordinary opaque IDs."""

    return credential_material_in_decoded_text(decoded_for_validation(value))


def credential_material_in_decoded_url(decoded: str) -> bool:
    """Inspect a caller-supplied canonical decoded URL view."""

    if (
        URL_CREDENTIAL_ASSIGNMENT_RE.search(decoded)
        or credential_material_in_decoded_text(decoded)
    ):
        return True
    try:
        parsed = urlsplit(decoded)
    except ValueError:
        return False
    for component in (parsed.query, parsed.fragment):
        for key, _ in parse_qsl(component, keep_blank_values=True):
            normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
            if normalized in URL_CREDENTIAL_NAMES:
                return True
    return False


def url_contains_credential_material(value: str) -> bool:
    """Detect credential-bearing query/fragment data, including nested encoding."""

    decoded = decoded_for_validation(value)
    return credential_material_in_decoded_url(decoded)


def is_allowed_microsoft_hostname(hostname: str) -> bool:
    normalized = hostname.casefold()
    return (
        normalized in ALLOWED_MICROSOFT_EXACT_HOSTS
        or any(normalized.endswith(suffix) for suffix in ALLOWED_MICROSOFT_SUFFIXES)
    )


def _microsoft_url(value: Any, provider: str) -> str | None:
    if value is None and provider == "manual":
        return None
    url = _string(value, "source.web_url", maximum=4096)
    try:
        parsed = urlsplit(url)
        hostname = (parsed.hostname or "").casefold()
        invalid = (
            parsed.scheme.casefold() != "https"
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
            or (parsed.port is not None and parsed.port != 443)
            or not is_allowed_microsoft_hostname(hostname)
        )
    except ValueError:
        invalid = True
    if invalid:
        raise CaptureValidationError(
            "source.web_url must be an HTTPS URL on an allowed Microsoft host",
            details={"field": "source.web_url"},
        )
    decoded = _decoded_text(url, "source.web_url")
    _validate_safe_text_view(decoded, "source.web_url")
    if credential_material_in_decoded_url(decoded):
        raise CaptureValidationError(
            "source.web_url must not contain credential material",
            code="credential_material_suspected",
            details={"field": "source.web_url"},
        )
    return url


def _action_id(source_key: str, action: dict[str, Any], occurrence: int) -> str:
    material = canonical_digest(action) + "\n" + str(occurrence)
    digest = hashlib.sha256((source_key + "\n" + material).encode("utf-8")).hexdigest()
    return "A-" + digest[:16]


def _project_provenance(value: Any, provider: str) -> dict[str, Any]:
    source = _object(value, "provenance")
    mode = _string(source.get("capture_mode"), "provenance.capture_mode", maximum=32)
    common = {
        "capture_mode": mode,
        "adapter": _safe_metadata(
            source.get("adapter"), "provenance.adapter", maximum=200
        ),
        "adapter_version": _safe_metadata(
            source.get("adapter_version"), "provenance.adapter_version", maximum=100
        ),
        "redaction_policy_version": _safe_metadata(
            source.get("redaction_policy_version"),
            "provenance.redaction_policy_version",
            maximum=100,
        ),
        "raw_retained": source.get("raw_retained"),
        "created_at": _safe_metadata(
            source.get("created_at"), "provenance.created_at", maximum=64
        ),
    }
    if common["raw_retained"] is not False:
        raise CaptureValidationError("provenance.raw_retained must be false")
    parse_rfc3339(common["created_at"], "provenance.created_at")
    if mode == "manual":
        if provider != "manual":
            raise CaptureValidationError("manual provenance requires the manual provider")
        forbidden_claims = {"model", "prompt_version", "tool_trace_digest", "allowed_tools"}
        if any(field in source for field in forbidden_claims):
            raise CaptureValidationError("manual provenance must omit automated-tool claims")
        return common
    if mode != "oob_verified":
        raise CaptureValidationError("unsupported provenance.capture_mode")
    if provider not in PROVIDER_TOOL:
        raise CaptureValidationError("oob_verified provenance requires a supported Microsoft provider")
    digest = _string(
        source.get("tool_trace_digest"), "provenance.tool_trace_digest", maximum=71
    )
    if not SHA256_RE.fullmatch(digest):
        raise CaptureValidationError("provenance.tool_trace_digest must be canonical SHA-256")
    allowed_tools = source.get("allowed_tools")
    if not isinstance(allowed_tools, list) or not allowed_tools or len(allowed_tools) > 10:
        raise CaptureValidationError("provenance.allowed_tools must be a non-empty array")
    if any(not isinstance(tool, str) for tool in allowed_tools):
        raise CaptureValidationError("provenance.allowed_tools entries must be strings")
    normalized_tools = list(dict.fromkeys(allowed_tools))
    required_tools = {PROVIDER_TOOL[provider], "workstack.capture.write"}
    if set(normalized_tools) != required_tools or not set(normalized_tools) <= ALLOWED_TOOL_CLAIMS:
        raise CaptureValidationError("provenance.allowed_tools does not prove least authority")
    return {
        **common,
        "model": _safe_metadata(
            source.get("model"), "provenance.model", maximum=200
        ),
        "prompt_version": _safe_metadata(
            source.get("prompt_version"), "provenance.prompt_version", maximum=100
        ),
        "tool_trace_digest": digest,
        "allowed_tools": normalized_tools,
    }


def validate_capture_packet(packet: Any) -> dict[str, Any]:
    """Reject unsafe values, then return only fields in Capture Packet v1."""

    _reject_forbidden_keys(packet)
    root = _object(packet, "packet")
    if root.get("schema_version") != "1.0":
        raise CaptureValidationError("schema_version must be 1.0")

    incoming_source = _object(root.get("source"), "source")
    source: dict[str, Any] = {}
    for field in (
        "provider", "resource_type", "connection_ref", "container_ref", "object_ref",
        "version_ref",
    ):
        source[field] = _safe_metadata(
            incoming_source.get(field), "source." + field, maximum=1024
        )
    provider = source["provider"]
    if provider not in {"manual", *PROVIDER_TOOL.keys()}:
        raise CaptureValidationError("unsupported source.provider")
    source["display_title"] = _safe_required(
        incoming_source.get("display_title"), "source.display_title", 500
    )
    source["web_url"] = _microsoft_url(incoming_source.get("web_url"), provider)
    source["retrieved_at"] = _safe_metadata(
        incoming_source.get("retrieved_at"), "source.retrieved_at", maximum=64
    )
    parse_rfc3339(source["retrieved_at"], "source.retrieved_at")
    source["fingerprint"] = _string(
        incoming_source.get("fingerprint"), "source.fingerprint", maximum=71
    )

    expected_key = source_key_for(source)
    received_key = root.get("source_key")
    if not isinstance(received_key, str) or not SHA256_RE.fullmatch(received_key):
        raise CaptureValidationError("source_key must be canonical SHA-256")
    if received_key != expected_key:
        raise CaptureValidationError("source_key does not match the source locator")
    expected_fingerprint = fingerprint_for(source)
    if source["fingerprint"] != expected_fingerprint:
        raise CaptureValidationError("source.fingerprint does not match source version")

    incoming_normalized = _object(root.get("normalized"), "normalized")
    summary = _safe_text(
        _string(incoming_normalized.get("summary"), "normalized.summary", maximum=2000),
        "normalized.summary",
    )
    context = _safe_text(
        _string(incoming_normalized.get("context"), "normalized.context", maximum=4000),
        "normalized.context",
    )
    incoming_actions = incoming_normalized.get("action_items")
    if not isinstance(incoming_actions, list) or len(incoming_actions) > 20:
        raise CaptureValidationError("normalized.action_items must contain at most 20 items")
    actions: list[dict[str, Any]] = []
    occurrences: dict[str, int] = {}
    for index, item in enumerate(incoming_actions):
        raw_action = _object(item, "normalized.action_items[{}]".format(index))
        priority = raw_action.get("priority", "P2")
        if priority not in ("P0", "P1", "P2", "P3"):
            raise CaptureValidationError("action priority is invalid")
        due = raw_action.get("due")
        if due is not None:
            due = _string(due, "action.due", maximum=10)
            try:
                dt.date.fromisoformat(due)
            except ValueError as error:
                raise CaptureValidationError("action due date is invalid") from error
        action = {
            "title": _safe_required(raw_action.get("title"), "action.title", 500),
            "detail": _safe_text(
                _string(raw_action.get("detail", ""), "action.detail", maximum=4000, required=False),
                "action.detail",
            ),
            "priority": priority,
            "due": due,
        }
        signature = canonical_digest(action)
        occurrence = occurrences.get(signature, 0)
        occurrences[signature] = occurrence + 1
        action["id"] = _action_id(expected_key, action, occurrence)
        actions.append(action)

    incoming_tags = incoming_normalized.get("tags", [])
    if not isinstance(incoming_tags, list) or len(incoming_tags) > 50:
        raise CaptureValidationError("normalized.tags must contain at most 50 items")
    tags: list[str] = []
    for index, tag in enumerate(incoming_tags):
        safe = _safe_text(_string(tag, "normalized.tags[{}]".format(index), maximum=100), "normalized.tags")
        if safe not in tags:
            tags.append(safe)

    incoming_hints = root.get("task_hints", [])
    if not isinstance(incoming_hints, list) or len(incoming_hints) > 20:
        raise CaptureValidationError("task_hints must contain at most 20 items")
    hints: list[str] = []
    for hint in incoming_hints:
        if not isinstance(hint, str) or not TASK_ID_RE.fullmatch(hint):
            raise CaptureValidationError("task_hints contains an invalid task ID")
        normalized_hint = hint.upper()
        if normalized_hint not in hints:
            hints.append(normalized_hint)

    provenance = _project_provenance(root.get("provenance"), provider)
    return {
        "schema_version": "1.0",
        "source_key": expected_key,
        "source": source,
        "normalized": {
            "summary": summary,
            "context": context,
            "action_items": actions,
            "tags": tags,
        },
        "task_hints": hints,
        "provenance": provenance,
    }
