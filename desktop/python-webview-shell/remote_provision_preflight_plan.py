"""Preflight projection of canonical targets and publication capability.

Resolution and capability facts are optional: a collector that never reported
them projects as unknown rather than as a refusal. When they are present this
module says only what was actually observed -- a configured alias that resolved
to a different canonical target is reported as a resolution, not as a silent
retarget; an unstable or mismatched resolution is drift; a measured unavailable
publication refuses the long install before it starts; and an unmeasured NFS
target stays unknown instead of being called unsupported. Resolution alone
never authorizes a workspace: identity is checked separately by the planner.
"""

from __future__ import annotations

from remote_provision_facts_model import (
    Diagnostic,
    Facts,
    Target,
    _note,
)


def _preflight_notes(target: Target, facts: Facts) -> list[Diagnostic]:
    notes: list[Diagnostic] = []
    resolution = facts.resolution
    if resolution is not None:
        if (
            resolution.install.configured != target.install_root
            or resolution.data.configured != target.data_root
            or not resolution.install.stable
            or not resolution.data.stable
        ):
            notes.append(_note(
                "TARGET_RESOLUTION_DRIFT",
                "error",
                "configured path resolution changed or does not match the selected target",
            ))
        elif (
            resolution.install.configured != resolution.install.canonical
            or resolution.data.configured != resolution.data.canonical
        ):
            notes.append(_note(
                "PATH_RESOLVED",
                "info",
                "configured alias resolves to a canonical target; workspace identity is still required",
            ))
    capability = facts.capability
    if capability is None:
        return notes
    if capability.noexec is True:
        notes.append(_note("TARGET_NOEXEC", "error", "selected application parent is mounted noexec"))
    if capability.nfs_publish == "failed":
        notes.append(_note(
            "NFS_PUBLISH_FAILED",
            "error",
            "measured NFS publication failed; this is not an unmeasured NFS guess",
        ))
    elif capability.publication == "unavailable":
        notes.append(_note(
            "PUBLICATION_UNAVAILABLE",
            "error",
            "atomic publication is unavailable; refuse the long install",
        ))
    elif capability.commit == "unknown":
        notes.append(_note(
            "COMMIT_SUPPORT_UNKNOWN",
            "info",
            "commit support was not measured and is unknown",
        ))
        if capability.filesystem == "nfs":
            notes.append(_note(
                "NFS_COMMIT_UNMEASURED",
                "info",
                "NFS is present; atomic operations stay unknown until measured",
            ))
    return notes


def _preflight_view(target: Target, facts: Facts) -> dict[str, object]:
    resolution = facts.resolution
    if resolution is None:
        install = {"configured": target.install_root, "canonical": target.install_root, "stable": None}
        data = {"configured": target.data_root, "canonical": target.data_root, "stable": None}
    else:
        install = {
            "configured": resolution.install.configured,
            "canonical": resolution.install.canonical,
            "stable": resolution.install.stable,
        }
        data = {
            "configured": resolution.data.configured,
            "canonical": resolution.data.canonical,
            "stable": resolution.data.stable,
        }
    host = facts.host
    capability = facts.capability
    return {
        "install": install,
        "data": data,
        "host": {
            "runtime": None if host is None else host.runtime,
            "machine": None if host is None else host.machine,
            "binding": "unknown" if host is None else host.binding,
        },
        "capability": {
            "publication": "unknown" if capability is None else capability.publication,
            "method": "unknown" if capability is None else capability.method,
            "commit": "unknown" if capability is None else capability.commit,
            "scratch": "not_requested" if capability is None else capability.scratch,
            "filesystem": "unknown" if capability is None else capability.filesystem,
            "noexec": None if capability is None else capability.noexec,
            "nfs_publish": "unknown" if capability is None else capability.nfs_publish,
        },
    }


def compare_provision_resolution(previous: object, current: object) -> str | None:
    """Return TARGET_RESOLUTION_DRIFT when the live resolution cannot authorize apply.

    Two independent conditions refuse. The canonical targets must still equal the
    inspected pair, and the *current* observation must itself be stable. A live
    re-probe that reports the same canonical strings while either ``stable`` flag
    is anything other than ``True`` is the collector's own signal that a
    configured alias retargeted or a bound path changed during that collection,
    so equal strings alone never authorize apply.

    Missing resolution on both sides stays unknown and does not authorize a
    different workspace.
    """

    previous_pair = _canonical_pair(previous)
    current_pair = _canonical_pair(current)
    if previous_pair is None and current_pair is None:
        return None
    if previous_pair != current_pair:
        return "TARGET_RESOLUTION_DRIFT"
    if not _stable_resolution(current):
        return "TARGET_RESOLUTION_DRIFT"
    return None


def _stable_resolution(facts: object) -> bool:
    """Both observed resolution roots must report ``stable`` as exactly True."""

    if type(facts) is not dict:
        return False
    resolution = facts.get("resolution")
    if type(resolution) is not dict:
        return False
    for name in ("install", "data"):
        root = resolution.get(name)
        if type(root) is not dict or root.get("stable") is not True:
            return False
    return True


def _canonical_pair(facts: object) -> tuple[str, str] | None:
    if type(facts) is not dict:
        return None
    resolution = facts.get("resolution")
    if type(resolution) is not dict:
        return None
    install = resolution.get("install")
    data = resolution.get("data")
    if type(install) is not dict or type(data) is not dict:
        return None
    install_canonical = install.get("canonical")
    data_canonical = data.get("canonical")
    if type(install_canonical) is not str or type(data_canonical) is not str:
        return None
    return install_canonical, data_canonical
