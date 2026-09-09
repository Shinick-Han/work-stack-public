"""Bounded request/facts vocabulary shared by the remote provisioning planner.

The planner never trusts a caller document: every accepted key set, every
bounded enumerated value and every frozen shape a parsed request may take is
declared here once, next to the refusal type and the clipped diagnostic note
the planner and its preflight projection both emit. Nothing in this module
parses, decides or contacts anything; it is the shared shape only.
"""

from __future__ import annotations

from dataclasses import dataclass


MAX_DETAIL_LENGTH = 256
MAX_CODE_LENGTH = 64


DOCUMENT_KEYS = frozenset({"schema_version", "target", "artifact", "facts"})
TARGET_KEYS = frozenset(
    {"os", "install_root", "data_root", "owner", "expected_workspace_id"}
)
ARTIFACT_KEYS = frozenset({"product_version", "protocol_version", "digest"})
FACTS_KEYS = frozenset({"os", "python", "install", "data"})
OPTIONAL_FACTS_KEYS = frozenset({"resolution", "host", "capability"})
PYTHON_KEYS = frozenset({"path", "version"})
INSTALL_KEYS = frozenset(
    {"exists", "owner", "symlink", "product_version", "protocol_version", "digest"}
)
DATA_KEYS = frozenset({"exists", "owner", "symlink", "workspace_id"})
RESOLUTION_ROOT_KEYS = frozenset({"install", "data"})
PATH_RESOLUTION_KEYS = frozenset({"configured", "canonical", "stable"})
HOST_KEYS = frozenset({"runtime", "machine", "binding"})
CAPABILITY_KEYS = frozenset(
    {"publication", "method", "commit", "scratch", "filesystem", "noexec", "nfs_publish"}
)
PUBLICATION_VALUES = frozenset({"available", "unavailable", "unknown"})
METHOD_VALUES = frozenset({"transactional", "verified_unpack", "unknown"})
COMMIT_VALUES = frozenset({"renameat2_noreplace", "unavailable", "unknown"})
SCRATCH_VALUES = frozenset({"not_requested", "measured", "refused"})
FILESYSTEM_VALUES = frozenset({"nfs", "other", "unknown"})
NFS_PUBLISH_VALUES = frozenset({"ok", "failed", "unknown", "not_applicable"})
HOST_BINDING_VALUES = frozenset({"selected_profile", "unknown"})
SCHEMA_LOCATIONS = frozenset(
    {
        "request",
        "target",
        "artifact",
        "facts",
        "python",
        "install",
        "data",
        "resolution",
        "host",
        "capability",
    }
)


class PlanError(ValueError):
    """Caller document is not a valid inspect/plan request."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = _clip_text(code, MAX_CODE_LENGTH)
        self.detail = _clip_text(detail, MAX_DETAIL_LENGTH)
        super().__init__(self.code if not self.detail else f"{self.code}: {self.detail}")


@dataclass(frozen=True)
class PythonFacts:
    path: str
    version: str
    series: tuple[int, int]


@dataclass(frozen=True)
class InstallFacts:
    exists: bool
    owner: str | None
    symlink: bool
    product_version: str | None
    protocol_version: int | None
    digest: str | None


@dataclass(frozen=True)
class DataFacts:
    exists: bool
    owner: str | None
    symlink: bool
    workspace_id: str | None


@dataclass(frozen=True)
class Target:
    os: str
    install_root: str
    data_root: str
    owner: str
    expected_workspace_id: str


@dataclass(frozen=True)
class Artifact:
    product_version: str
    protocol_version: int
    digest: str


@dataclass(frozen=True)
class PathResolution:
    configured: str
    canonical: str
    stable: bool


@dataclass(frozen=True)
class ResolutionFacts:
    install: PathResolution
    data: PathResolution


@dataclass(frozen=True)
class HostFacts:
    runtime: str
    machine: str | None
    binding: str


@dataclass(frozen=True)
class CapabilityFacts:
    publication: str
    method: str
    commit: str
    scratch: str
    filesystem: str
    noexec: bool | None
    nfs_publish: str


@dataclass(frozen=True)
class Facts:
    os: str
    python: PythonFacts | None
    install: InstallFacts
    data: DataFacts
    resolution: ResolutionFacts | None = None
    host: HostFacts | None = None
    capability: CapabilityFacts | None = None


@dataclass(frozen=True)
class Diagnostic:
    code: str
    severity: str
    detail: str


def _clip_text(value: str, maximum: int) -> str:
    if len(value) <= maximum:
        return value
    return value[: maximum - 1] + "…"


def _note(code: str, severity: str, detail: str) -> Diagnostic:
    return Diagnostic(
        _clip_text(code, MAX_CODE_LENGTH),
        severity,
        _clip_text(detail, MAX_DETAIL_LENGTH),
    )
