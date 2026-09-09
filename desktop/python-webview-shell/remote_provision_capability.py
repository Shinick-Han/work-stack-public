"""Publication capability vocabulary projected from one scratch measurement.

The collector measures; this module decides what a measurement is allowed to
claim. ``unknown`` is a legitimate result: without an opt-in scratch test the
publication capability, the commit mechanism and the NFS publish outcome all
stay unknown, and an NFS filesystem is never blanket-labelled unsupported.
A measured refusal is projected into the narrowest true fact: an ownership or
noexec refusal says publication is unavailable without guessing at NFS, an
ENOSYS/EINVAL class refusal additionally says the commit mechanism itself is
unavailable, and only a genuine NFS rename failure reports ``nfs_publish``
failed. The scratch directory is admitted only when it is exactly the selected
application parent and never the data root or anything beneath it.
"""

from __future__ import annotations


NFS_TYPES = frozenset({"nfs", "nfs2", "nfs3", "nfs4"})
SCRATCH_RESULTS = frozenset({"ok", "ownership", "unavailable", "nfs_failed", "unknown"})


def _nfs_publish_default(fstype: str) -> str:
    if fstype == "other":
        return "not_applicable"
    return "unknown"


def unmeasured_capability(fstype: str, noexec: bool | None) -> dict[str, object]:
    """Project the measured filesystem facts with no publication claim at all."""

    return {
        "publication": "unknown",
        "method": "unknown",
        "commit": "unknown",
        "scratch": "not_requested",
        "filesystem": fstype,
        "noexec": noexec,
        "nfs_publish": _nfs_publish_default(fstype),
    }


def scratch_parent_admitted(
    scratch: str,
    install_parent: str,
    canonical_install: str,
    canonical_data: str,
) -> bool:
    """Admit only the selected application parent, never a data/SSOT location."""

    return (
        scratch == install_parent
        and scratch != canonical_install
        and scratch != canonical_data
        and not scratch.startswith(canonical_data + "/")
    )


def measured_capability(
    fstype: str, noexec: bool | None, measured: str
) -> dict[str, object]:
    """Project one opt-in scratch result into the bounded capability vocabulary."""

    capability = unmeasured_capability(fstype, noexec)
    capability["scratch"] = "measured"
    nfs_default = _nfs_publish_default(fstype)
    if measured == "ok":
        capability["publication"] = "available"
        capability["method"] = "transactional"
        capability["commit"] = "renameat2_noreplace"
        capability["nfs_publish"] = "ok" if fstype == "nfs" else nfs_default
        return capability
    if measured == "ownership":
        capability["publication"] = "unavailable"
        return capability
    if measured == "unavailable":
        capability["publication"] = "unavailable"
        capability["commit"] = "unavailable"
        return capability
    if measured == "nfs_failed" and fstype == "nfs":
        capability["publication"] = "unavailable"
        capability["nfs_publish"] = "failed"
        return capability
    if measured == "nfs_failed":
        capability["publication"] = "unavailable"
        capability["commit"] = "unavailable"
        return capability
    return capability
