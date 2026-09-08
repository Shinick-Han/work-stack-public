"""Text and reference admission for everything that leaves a remote system.

One place decides whether a caller-supplied string may be stored: control
characters, embedded markup, quoted mail, credential material and non-Microsoft
URLs are refused here rather than in each command. The reply body, the opaque
target references and the Microsoft web URL all share the same decoded view, so
a percent-encoded payload cannot pass one check and fail another.
"""

from __future__ import annotations

import unicodedata
from typing import Any
from urllib.parse import urlsplit

from .capture import (
    EMAIL_RE,
    PercentDecodingLimitError,
    RECIPIENT_ASSIGNMENT_RE,
    credential_material_in_decoded_text,
    credential_material_in_decoded_url,
    decoded_for_validation,
    is_allowed_microsoft_hostname,
)
from .service_domain import (
    HTML_TAG_RE,
    MAIL_HEADER_RE,
    MICROSOFT_WEB_URL_MAX,
    QUOTED_REPLY_RE,
    QUOTE_LINE_RE,
    RAW_CANARY_RE,
    REMOTE_HEADER_PREFIX_RE,
    REMOTE_MESSAGE_REF_MAX,
    REMOTE_MESSAGE_REF_RE,
    REPLY_BODY_MAX,
    SECRET_TEXT_RE,
)
from .service_errors import DomainError


def _reject_controls(value: str, field: str, *, multiline: bool) -> str:
    allowed = {"\n", "\r", "\t"} if multiline else set()
    if any(
        character not in allowed and unicodedata.category(character).startswith("C")
        for character in value
    ):
        raise DomainError("{} contains control characters".format(field), {"field": field})
    if not multiline and ("\n" in value or "\r" in value):
        raise DomainError("{} must be a single line".format(field), {"field": field})
    return value


def _approved_plain_text(value: Any) -> str:
    if not isinstance(value, str):
        raise DomainError("body must be a string", {"field": "body"})
    if not value.strip():
        raise DomainError("body is required", {"field": "body"})
    if len(value) > REPLY_BODY_MAX:
        raise DomainError(
            "body exceeds {} characters".format(REPLY_BODY_MAX),
            {"field": "body", "maximum": REPLY_BODY_MAX},
        )
    _reject_controls(value, "body", multiline=True)
    if (
        HTML_TAG_RE.search(value)
        or RAW_CANARY_RE.search(value)
        or QUOTED_REPLY_RE.search(value)
        or len(MAIL_HEADER_RE.findall(value)) >= 2
        or len(QUOTE_LINE_RE.findall(value)) >= 4
    ):
        raise DomainError("body contains HTML or unsafe raw content", {"field": "body"})
    try:
        decoded = decoded_for_validation(value)
    except PercentDecodingLimitError as error:
        raise DomainError(
            "body exceeds the percent-encoding validation depth", {"field": "body"}
        ) from error
    if SECRET_TEXT_RE.search(decoded) or credential_material_in_decoded_text(decoded):
        raise DomainError("body appears to contain an authentication token", {"field": "body"})
    return value


def _reference_views(value: Any, field: str, maximum: int) -> tuple[str, str]:
    if not isinstance(value, str) or not value:
        raise DomainError("{} must be a non-empty string".format(field), {"field": field})
    if len(value) > maximum:
        raise DomainError(
            "{} exceeds {} characters".format(field, maximum),
            {"field": field, "maximum": maximum},
        )
    try:
        decoded = decoded_for_validation(value)
    except PercentDecodingLimitError as error:
        raise DomainError(
            "{} exceeds the percent-encoding validation depth".format(field),
            {"field": field},
        ) from error
    _reject_controls(decoded, field, multiline=False)
    if (
        HTML_TAG_RE.search(decoded)
        or RAW_CANARY_RE.search(decoded)
        or RECIPIENT_ASSIGNMENT_RE.search(decoded)
    ):
        raise DomainError("{} contains unsafe content".format(field), {"field": field})
    if SECRET_TEXT_RE.search(decoded) or credential_material_in_decoded_text(decoded):
        raise DomainError("{} appears to contain an authentication token".format(field), {"field": field})
    return value, decoded


def _opaque_reference(value: Any, field: str, maximum: int) -> str:
    reference, _ = _reference_views(value, field, maximum)
    return reference


def _remote_message_reference(value: Any) -> str:
    reference, decoded = _reference_views(
        value, "remote_message_ref", REMOTE_MESSAGE_REF_MAX
    )
    if (
        not REMOTE_MESSAGE_REF_RE.fullmatch(reference)
        or not REMOTE_MESSAGE_REF_RE.fullmatch(decoded)
        or "://" in decoded
        or EMAIL_RE.search(decoded)
        or REMOTE_HEADER_PREFIX_RE.search(decoded)
        or RECIPIENT_ASSIGNMENT_RE.search(decoded)
        or HTML_TAG_RE.search(decoded)
        or RAW_CANARY_RE.search(decoded)
    ):
        raise DomainError(
            "remote_message_ref must be an opaque Microsoft message identifier",
            {"field": "remote_message_ref"},
        )
    return reference


def _microsoft_web_url(value: Any) -> str:
    url, decoded = _reference_views(value, "web_url", MICROSOFT_WEB_URL_MAX)
    if EMAIL_RE.search(decoded) or RECIPIENT_ASSIGNMENT_RE.search(decoded):
        raise DomainError(
            "web_url must not contain recipient material", {"field": "web_url"}
        )
    if credential_material_in_decoded_url(decoded):
        raise DomainError(
            "web_url appears to contain an authentication token", {"field": "web_url"}
        )
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
        raise DomainError(
            "web_url must be a token-free HTTPS URL on an allowed Microsoft host",
            {"field": "web_url"},
        )
    return url
