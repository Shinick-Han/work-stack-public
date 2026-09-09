"""Honest desktop-versus-remote version wording for the update status card.

The Windows updater replaces the desktop shell only. When Work Stack runs
against an SSH remote the interface is served from the remote installation, so
a current desktop proves nothing about the remote build. These helpers turn the
two observed versions into one bounded English sentence that never claims more
than was actually verified: they separate the desktop verdict from the remote
one, keep an unverified remote unverified, and describe a satisfied protocol
floor as compatibility rather than build equality.

Pure text composition. No network, no filesystem, no host state.
"""

from __future__ import annotations

import re


MAX_VERSION_CHARACTERS = 32
_CANONICAL_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")

DESKTOP_ONLY_SCOPE = (
    "This updater replaces the Windows desktop only; update the remote "
    "Work Stack files separately."
)


def canonical_version(value: str | None) -> str:
    """Accept a remote-reported version only if the whole value is canonical.

    The value crosses a trust boundary, so the entire string must be one
    bounded three-part version, the same contract the updater already requires
    of a release. Matching a prefix would be worse than rejecting: it would
    turn `1.0.13<script>` into a credible `1.0.13` and let malformed metadata
    enter the equal-version branch. Anything else returns "", which reads as an
    unverified remote rather than a match.
    """

    if not isinstance(value, str) or len(value) > MAX_VERSION_CHARACTERS:
        return ""
    return value if _CANONICAL_VERSION.fullmatch(value) is not None else ""


def protocol_clause(remote_protocol: int | None, minimum_protocol: int | None) -> str:
    """Describe the protocol gate as compatibility, never as build equality."""

    if not isinstance(remote_protocol, int) or isinstance(remote_protocol, bool):
        return ""
    if not isinstance(minimum_protocol, int) or isinstance(minimum_protocol, bool):
        return ""
    if remote_protocol < minimum_protocol:
        return (
            f"Remote protocol {remote_protocol} is below the required "
            f"{minimum_protocol}."
        )
    return (
        f"Remote protocol {remote_protocol} meets the required {minimum_protocol}, "
        "which is compatibility only, not the same build."
    )


def remote_clause(
    *,
    desktop_version: str,
    remote_version: str | None,
    remote_protocol: int | None = None,
    minimum_protocol: int | None = None,
) -> str:
    """State what is known about the connected remote server, and nothing more."""

    reported = canonical_version(remote_version)
    if not reported:
        return (
            "The connected remote server version could not be verified, so its "
            f"interface build is unknown. {DESKTOP_ONLY_SCOPE}"
        )
    if reported == desktop_version:
        sentences = [
            f"The connected remote server reports {reported}.",
            "Equal version numbers do not prove the remote interface files were "
            "rebuilt from that release.",
        ]
    else:
        sentences = [
            f"The connected remote server reports {reported}, which does not "
            f"match desktop {desktop_version}.",
            DESKTOP_ONLY_SCOPE,
        ]
    clause = protocol_clause(remote_protocol, minimum_protocol)
    if clause:
        sentences.append(clause)
    return " ".join(sentences)


def current_version_message(
    *,
    desktop_version: str,
    remote_connected: bool,
    remote_version: str | None = None,
    remote_protocol: int | None = None,
    minimum_protocol: int | None = None,
) -> str:
    """Wording for "the stable channel has nothing newer for this desktop"."""

    if not remote_connected:
        return "Work Stack is up to date"
    clause = remote_clause(
        desktop_version=desktop_version,
        remote_version=remote_version,
        remote_protocol=remote_protocol,
        minimum_protocol=minimum_protocol,
    )
    return f"Work Stack desktop {desktop_version} is up to date. {clause}"


def newer_than_channel_message(
    *,
    installed_version: str,
    channel_version: str,
    remote_connected: bool,
    remote_version: str | None = None,
    remote_protocol: int | None = None,
    minimum_protocol: int | None = None,
) -> str:
    """Wording for an installed desktop that is ahead of the stable channel."""

    headline = (
        f"Installed Work Stack {installed_version} is newer than "
        f"the stable channel {channel_version}"
    )
    if not remote_connected:
        return headline
    clause = remote_clause(
        desktop_version=installed_version,
        remote_version=remote_version,
        remote_protocol=remote_protocol,
        minimum_protocol=minimum_protocol,
    )
    return f"{headline}. {clause}"
