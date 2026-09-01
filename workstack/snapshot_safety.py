"""Frozen snapshot-v1 high-confidence credential tripwire."""

from __future__ import annotations

import base64
import binascii
import ipaddress
import re
from typing import Callable


_FIXED_PLACEHOLDERS = {
    "<redacted>",
    "[redacted]",
    "redacted",
    "***",
    "xxxxx",
    "example",
    "placeholder",
    "not-a-secret",
}
_IDENTIFIER_PLACEHOLDER = re.compile(
    r"^(?:\$[A-Za-z_][A-Za-z0-9_]{0,63}|"
    r"\$\{[A-Za-z_][A-Za-z0-9_]{0,63}\}|"
    r"%[A-Za-z_][A-Za-z0-9_]{0,63}%)$"
)
_CREDENTIAL_KEY = re.compile(
    r"(?i)(?<![^ \t\"'\{\[,])"
    r"(password|passwd|pwd|api[_-]key|access[_-]token|refresh[_-]token|"
    r"id[_-]token|oauth[_-]token|client[_-]secret|private[_-]key)"
    r"[ \t]*[:=][ \t]*"
)
_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_])(?:ghp_[A-Za-z0-9]{20,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,})"
    r"(?![A-Za-z0-9_])"
)
_URL = re.compile(r"(?i)(?<![A-Za-z0-9+.-])https?://")
_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)
_USERINFO_ASCII = frozenset(_UNRESERVED | set("!$&'()*+,;=:%-"))
_REG_NAME_ASCII = frozenset(_UNRESERVED | set("!$&'()*+,;=%"))
_PATH_SUFFIXES = (
    "/.ssh/id_rsa",
    "/.ssh/id_ed25519",
    "/.aws/credentials",
    "/gcloud/application_default_credentials.json",
)
_PATH_DELIMITERS = frozenset(" \t\n\"')]} ,;".replace(" ", "") + " \t\n")


def _result(
    decision: str, *, code: str | None = None, rule: str | None = None
) -> dict[str, str]:
    value = {"decision": decision}
    if code is not None:
        value["code"] = code
    if rule is not None:
        value["rule"] = rule
    return value


def _placeholder(value: str) -> bool:
    candidate = value.strip(" \t")
    return (
        candidate.casefold() in _FIXED_PLACEHOLDERS
        or _IDENTIFIER_PLACEHOLDER.fullmatch(candidate) is not None
    )


def _controls_valid(value: str, field: str) -> bool:
    for character in value:
        codepoint = ord(character)
        if 0xD800 <= codepoint <= 0xDFFF or codepoint == 0x7F or 0x80 <= codepoint <= 0x9F:
            return False
        if codepoint <= 0x1F:
            if field == "detail" and character in "\n\t":
                continue
            return False
    return True


def _decode_base64(payload: str, field: str) -> str:
    if not payload or len(payload) % 4 or re.fullmatch(r"[A-Za-z0-9+/]*={0,2}", payload) is None:
        raise ValueError
    try:
        decoded = base64.b64decode(payload, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ValueError from error
    if base64.b64encode(decoded).decode("ascii") != payload or len(decoded) > 4096:
        raise ValueError
    if decoded.startswith(b"\xef\xbb\xbf"):
        raise ValueError
    try:
        text = decoded.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ValueError from error
    if not _controls_valid(text, field):
        raise ValueError
    return text


def _decode_percent(payload: str, field: str) -> str:
    if not payload:
        raise ValueError
    output = bytearray()
    index = 0
    while index < len(payload):
        character = payload[index]
        if character in _UNRESERVED or character == "+":
            output.append(ord(character))
            index += 1
            continue
        if (
            character != "%"
            or index + 2 >= len(payload)
            or re.fullmatch(r"[0-9A-F]{2}", payload[index + 1:index + 3]) is None
        ):
            raise ValueError
        output.append(int(payload[index + 1:index + 3], 16))
        index += 3
    if len(output) > 4096 or output.startswith(b"\xef\xbb\xbf"):
        raise ValueError
    try:
        text = bytes(output).decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ValueError from error
    if not _controls_valid(text, field):
        raise ValueError
    return text


def _views(value: str, field: str) -> tuple[list[str], bool]:
    views = [value]
    invalid = False
    for line in value.split("\n"):
        candidate = line.strip(" \t")
        decoder: Callable[[str, str], str] | None = None
        prefix = ""
        if candidate.startswith("conduit-base64:"):
            decoder = _decode_base64
            prefix = "conduit-base64:"
        elif candidate.startswith("conduit-percent:"):
            decoder = _decode_percent
            prefix = "conduit-percent:"
        if decoder is None:
            continue
        try:
            views.append(decoder(candidate[len(prefix):], field))
        except ValueError:
            invalid = True
            break
    return views, invalid


def _s001(value: str) -> bool:
    return any(
        line.strip(" \t").startswith("-----BEGIN ")
        and line.strip(" \t").endswith(" PRIVATE KEY-----")
        for line in value.split("\n")
    )


def _s002(value: str) -> bool:
    pattern = re.compile(
        r"(?i)^(?:authorization|proxy-authorization)[ \t]*:[ \t]*"
        r"(?:basic|bearer)[ \t]+([!-~]+)"
    )
    for line in value.split("\n"):
        match = pattern.match(line.lstrip(" \t"))
        if match and len(match.group(1)) >= 12 and not _placeholder(match.group(1)):
            return True
    return False


def _s003(value: str) -> bool:
    for line in value.split("\n"):
        for match in _CREDENTIAL_KEY.finditer(line):
            start = match.end()
            if start >= len(line):
                continue
            if line[start] in "\"'":
                quote = line[start]
                end = line.find(quote, start + 1)
                if end < 0:
                    candidate = line[start:]
                else:
                    candidate = line[start + 1:end]
            else:
                end = start
                while end < len(line) and line[end] not in " \t":
                    end += 1
                candidate = line[start:end]
            if len(candidate) >= 8 and not _placeholder(candidate):
                return True
    return False


def _userinfo_octets(value: str) -> bytes | None:
    output = bytearray()
    index = 0
    while index < len(value):
        character = value[index]
        if ord(character) > 0x7F or character not in _USERINFO_ASCII:
            return None
        if character == "%":
            if index + 2 >= len(value) or re.fullmatch(
                r"[0-9A-Fa-f]{2}", value[index + 1:index + 3]
            ) is None:
                return None
            output.append(int(value[index + 1:index + 3], 16))
            index += 3
        else:
            output.append(ord(character))
            index += 1
    return bytes(output)


def _decode_userinfo(value: str) -> str | None:
    octets = _userinfo_octets(value)
    if octets is None:
        return None
    try:
        return octets.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None


def _ip_literal_valid(literal: str) -> bool:
    if re.fullmatch(
        r"[vV][0-9A-Fa-f]+\.[A-Za-z0-9._~!$&'()*+,;=:-]+", literal
    ) is not None:
        return True
    try:
        return ipaddress.ip_address(literal).version == 6
    except ValueError:
        return False


def _port_suffix_valid(value: str) -> bool:
    if not value:
        return True
    if not value.startswith(":"):
        return False
    port = value[1:]
    return not port or port.isdecimal()


def _ip_literal_host_port_valid(host_port: str) -> bool:
    close = host_port.find("]")
    if close < 0:
        return False
    literal = host_port[1:close]
    remainder = host_port[close + 1:]
    return _ip_literal_valid(literal) and _port_suffix_valid(remainder)


def _reg_name_valid(host: str) -> bool:
    index = 0
    while index < len(host):
        character = host[index]
        if character != "%":
            if character not in _REG_NAME_ASCII:
                return False
            index += 1
            continue
        if index + 2 >= len(host) or re.fullmatch(
            r"[0-9A-Fa-f]{2}", host[index + 1:index + 3]
        ) is None:
            return False
        index += 3
    return True


def _reg_name_host_port_valid(host_port: str) -> bool:
    if host_port.count(":") > 1:
        return False
    host, separator, port = host_port.rpartition(":")
    if not separator:
        host = host_port
    elif not host or (port and not port.isdecimal()):
        return False
    return _reg_name_valid(host)


def _authority_valid(authority: str) -> bool:
    if any(ord(character) > 0x7F for character in authority) or "@" not in authority:
        return False
    userinfo, host_port = authority.rsplit("@", 1)
    if not userinfo or _userinfo_octets(userinfo) is None:
        return False
    if host_port.startswith("["):
        return _ip_literal_host_port_valid(host_port)
    return _reg_name_host_port_valid(host_port)


def _s005(value: str) -> bool:
    for line in value.split("\n"):
        for match in _URL.finditer(line):
            start = match.end()
            end = start
            while end < len(line) and line[end] not in "/?# \t":
                end += 1
            authority = line[start:end]
            if not _authority_valid(authority):
                continue
            userinfo, host = authority.rsplit("@", 1)
            if ":" not in userinfo:
                continue
            username, password = userinfo.split(":", 1)
            if not username:
                continue
            decoded = _decode_userinfo(password)
            if decoded is not None and len(decoded) >= 8 and not _placeholder(decoded):
                return True
    return False


def _s006(value: str) -> bool:
    view = value.replace("\\", "/").translate(
        str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
    )
    for suffix in _PATH_SUFFIXES:
        offset = 0
        while True:
            index = view.find(suffix, offset)
            if index < 0:
                break
            following = index + len(suffix)
            if following == len(view) or view[following] in _PATH_DELIMITERS:
                return True
            offset = index + 1
    return False


def evaluate_safety(value: str, field: str) -> dict[str, str]:
    """Evaluate one valid snapshot text field without returning candidate content."""

    if field not in {"title", "detail"} or not isinstance(value, str):
        raise ValueError("snapshot safety requires a title or detail string")
    views, invalid = _views(value, field)
    if invalid:
        return _result(
            "REFUSE", code="SNAPSHOT_SAFETY_ENCODING_INVALID", rule="S000"
        )
    rules = (
        ("S001", _s001, "SNAPSHOT_CREDENTIAL_SUSPECTED"),
        ("S002", _s002, "SNAPSHOT_CREDENTIAL_SUSPECTED"),
        ("S003", _s003, "SNAPSHOT_CREDENTIAL_SUSPECTED"),
        ("S004", lambda item: _TOKEN.search(item) is not None, "SNAPSHOT_CREDENTIAL_SUSPECTED"),
        ("S005", _s005, "SNAPSHOT_CREDENTIAL_SUSPECTED"),
        ("S006", _s006, "SNAPSHOT_SENSITIVE_PATH_SUSPECTED"),
    )
    for rule, predicate, code in rules:
        if any(predicate(view) for view in views):
            return _result("REFUSE", code=code, rule=rule)
    return _result("ALLOW")
