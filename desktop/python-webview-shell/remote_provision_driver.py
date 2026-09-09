"""Bounded local orchestration for the existing remote provisioning parts.

This module owns no protocol of its own.  It assembles four checked-in
contracts that already exist and are owned elsewhere: the bounded read-only
SSH facts probe (``remote_provision_probe``), the pure planner
(``remote_provision_plan``), the stdin installer program
(``remote_provision_payload``), and the fixed-shape OpenSSH argv
(``remote_provision_command``).

Rules this module enforces and never relaxes:

* Facts are always collected live from the exact selected profile.  A
  caller-supplied plan or facts document is never accepted as proof of state,
  and neither is a *retained* one: an inspection carries deep-frozen evidence
  plus a digest over it, and ``apply_remote_install`` re-probes the same
  profile and re-plans before it will build an install command.  What
  authorises the install is the second reading, not the first.
* Inspect/plan is the default.  ``apply_remote_install`` refuses unless the
  caller passes ``apply=True``, hands back evidence this process issued
  unedited, and the freshly recomputed plan decides ``install_needed``.
* Install targets a fresh application directory only, for the expected
  workspace only.  There is no force, steal, overwrite, or delete path.
* An unknown install outcome stays unknown.  Nothing is retried, nothing is
  reconciled automatically, and no receipt is rewritten.  Cleanup that could
  not be confirmed is reported as such and never softens the outcome.
* Installing an application is not activating a connection.  This module
  never reads or writes the SSOT, the connection registry, or an owner
  receipt, and never starts a server.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

_SHELL_DIR = str(Path(__file__).resolve(strict=True).parent)
if _SHELL_DIR not in sys.path:
    sys.path.insert(0, _SHELL_DIR)

from bounded_process_exchange import (  # noqa: E402
    CLEANUP_SETTLED,
    CLEANUP_UNSETTLED,
    ExchangeError,
    ExchangeOutput,
    start_exchange,
)
from remote_provision_artifact import (  # noqa: E402
    ArtifactError,
    ArtifactSelection,
    admit_artifact,
    load_json_object,
)
from remote_provision_evidence import (  # noqa: E402
    DIGEST_PREFIX,
    EvidenceError,
    compute_evidence,
    issue,
    plain,
    require_issued,
)
from remote_command_contract import (  # noqa: E402
    RemoteCommandError,
    require_posix_owner,
    require_workspace_uid,
    validated_posix_path,
)
from remote_provision_command import (  # noqa: E402
    build_ssh_provision_install_command,
)
from remote_provision_installer import (  # noqa: E402
    MAX_STDERR,
    MAX_STDOUT,
    REFUSE_KEYS,
    SUCCESS_KEYS,
)
from remote_provision_payload import (  # noqa: E402
    PayloadError,
    build_installer_payload_source,
)
from remote_provision_plan import (  # noqa: E402
    PlanError,
    plan_remote_provision,
)
from remote_provision_probe import (  # noqa: E402
    PROBE_TIMEOUT_SECONDS,
    ProbeError,
    run_remote_provision_probe,
)


SCHEMA_VERSION = 1
INSTALL_TIMEOUT_SECONDS = 300.0
MAX_DETAIL_LENGTH = 256
MAX_CODE_LENGTH = 64

#: Outcomes this driver reports.  ``unknown`` is terminal for this process and
#: requires an operator to reconcile from the install receipt.
OUTCOME_INSTALLED = "installed"
OUTCOME_REFUSED = "refused"
OUTCOME_UNKNOWN = "unknown"

#: Installer refusal codes whose remote effect is genuinely ambiguous.  These
#: are reported as ``unknown``, never as a clean refusal.
AMBIGUOUS_INSTALL_CODES = frozenset({"REMOTE_INSTALL_COMMIT_UNKNOWN"})

RECONCILE_GUIDANCE = (
    "The remote install outcome was not established. Do not re-run this "
    "driver against the same install root. Read the install receipt "
    ".workstack-install.json under the install root out of band, decide from "
    "it, and choose a different install root if a retry is warranted."
)

#: Appended when the local SSH client process could not be confirmed stopped.
#: This never softens the outcome: the commit is still unknown, and there is
#: now a second thing to check by hand.
CLEANUP_GUIDANCE = (
    "The local SSH client process for this attempt could not be confirmed "
    "stopped. Check for a stray client process for this install root before "
    "doing anything else."
)

#: Installer stderr codes admitted verbatim into a refusal report.  Anything
#: else collapses to ``REMOTE_INSTALL_FAILED`` so remote text never leaks.
STABLE_INSTALL_CODES = frozenset(
    {
        "REMOTE_ARTIFACT_INVALID",
        "REMOTE_ATOMIC_COMMIT_UNAVAILABLE",
        "REMOTE_INSTALL_COMMIT_UNKNOWN",
        "REMOTE_INSTALL_FAILED",
        "REMOTE_INSTALLER_UNSUPPORTED",
        "REMOTE_PROTOCOL_INVALID",
        "REMOTE_PYTHON_REQUIRED",
        "REMOTE_SMOKE_FAILED",
        "REMOTE_WORKSPACE_MISMATCH",
        "INSTALL_ROOT_EXISTS",
        "TARGET_OWNERSHIP_MISMATCH",
        "TARGET_REPARSE_AMBIGUITY",
    }
)


def _clip_text(value: str, maximum: int) -> str:
    if len(value) <= maximum:
        return value
    return value[: maximum - 1] + "…"


class DriverError(RuntimeError):
    """Bounded driver failure.

    ``detail`` is always a fixed sentence written here.  Remote output, host
    names, credentials, and file contents never reach it.  ``cleanup`` says
    whether any process this driver created was confirmed stopped.
    """

    def __init__(
        self, code: str, detail: str = "", *, cleanup: str = CLEANUP_SETTLED
    ) -> None:
        self.code = _clip_text(code, MAX_CODE_LENGTH)
        self.detail = _clip_text(detail, MAX_DETAIL_LENGTH)
        self.cleanup = cleanup
        super().__init__(self.code if not self.detail else f"{self.code}: {self.detail}")


@dataclass(frozen=True)
class DriverProfile:
    """The exact remote target the facts and the install are bound to.

    Field names match what ``remote_provision_probe`` and
    ``remote_provision_command`` read off a profile object, so the same
    instance drives both without a translation layer.  Nothing here is
    discovered: every value is supplied by the caller.
    """

    ssh_host_alias: str
    remote_python: str
    remote_app_dir: str
    remote_data_dir: str
    expected_workspace_id: str


@dataclass(frozen=True)
class DriverInspection:
    """Live facts for one profile plus the plan computed from exactly those.

    ``facts`` and ``plan`` are deep-frozen read-only mappings, not the
    caller-editable dictionaries the probe and planner returned, and
    ``evidence`` digests both of them together with ``binding``.  A retained
    inspection is therefore a record of what was read, not a licence: apply
    revalidates the digest, requires this process to have issued it, and then
    re-reads the target before it will build an install command.
    """

    binding: str
    profile: DriverProfile
    owner: str
    facts: Mapping[str, object]
    plan: Mapping[str, object]
    evidence: str

    @property
    def decision(self) -> str:
        plan = self.plan
        value = plan.get("decision") if isinstance(plan, Mapping) else None
        return value if isinstance(value, str) else "refused"

    @property
    def install_allowed(self) -> bool:
        return self.decision == "install_needed"


@dataclass(frozen=True)
class InstallOutcome:
    """Terminal report for one install attempt. Never a retry instruction.

    ``cleanup`` is ``settled`` only when every process and thread this driver
    created for the attempt was confirmed stopped.  ``unsettled`` never turns
    an unknown commit into a refusal or a safe retry; it adds a second thing
    the operator must check.
    """

    outcome: str
    code: str | None
    receipt: dict[str, object] | None
    guidance: str | None
    cleanup: str = CLEANUP_SETTLED


def _require_profile(profile: object) -> DriverProfile:
    if not isinstance(profile, DriverProfile):
        raise DriverError("DRIVER_PROFILE_INVALID", "a DriverProfile is required")
    try:
        alias = profile.ssh_host_alias
        if not isinstance(alias, str) or not alias or alias.startswith("-"):
            raise RemoteCommandError("REMOTE_PROTOCOL_INVALID", "alias")
        install = validated_posix_path(profile.remote_app_dir, "remote_app_dir")
        data = validated_posix_path(profile.remote_data_dir, "remote_data_dir")
        require_workspace_uid(profile.expected_workspace_id)
    except RemoteCommandError:
        raise DriverError(
            "DRIVER_PROFILE_INVALID", "profile fields are not a valid remote target"
        ) from None
    if install == data or install.startswith(data + "/") or data.startswith(install + "/"):
        raise DriverError(
            "DRIVER_PROFILE_INVALID", "install and data roots must be separate"
        )
    if not isinstance(profile.remote_python, str) or not profile.remote_python:
        raise DriverError("DRIVER_PROFILE_INVALID", "remote_python is required")
    return profile


def _require_owner(owner: object) -> str:
    try:
        return require_posix_owner(owner)
    except RemoteCommandError:
        raise DriverError("DRIVER_PROFILE_INVALID", "owner is not a POSIX user name") from None


def _require_ssh_executable(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise DriverError("DRIVER_PROFILE_INVALID", "an explicit ssh executable is required")
    if any(ord(character) < 32 or character == "\x7f" for character in value):
        raise DriverError("DRIVER_PROFILE_INVALID", "an explicit ssh executable is required")
    return value


def _require_timeout(timeout: object, maximum: float) -> float:
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
        raise DriverError("DRIVER_PROFILE_INVALID", "timeout must be a bounded number")
    value = float(timeout)
    if not 0.1 <= value <= maximum:
        raise DriverError("DRIVER_PROFILE_INVALID", "timeout is outside the supported bound")
    return value


def select_artifact(archive_bytes: object, sidecar_bytes: object) -> ArtifactSelection:
    """Admit one archive and its sidecar, locally, before any SSH happens."""

    try:
        return admit_artifact(archive_bytes, sidecar_bytes)
    except ArtifactError as error:
        raise DriverError("DRIVER_ARTIFACT_INVALID", error.detail) from None


def compute_binding(
    profile: DriverProfile, owner: str, artifact: ArtifactSelection
) -> str:
    """Digest the whole decision: this target, this operator, this artifact.

    An inspection carries the binding it was produced under, and an install
    recomputes it.  A profile edited after inspect/plan therefore cannot ride
    a stale plan into an install.
    """

    parts = (
        profile.ssh_host_alias,
        profile.remote_python,
        profile.remote_app_dir,
        profile.remote_data_dir,
        profile.expected_workspace_id,
        owner,
        artifact.digest,
        artifact.manifest_digest,
        artifact.product_version,
        str(artifact.protocol_version),
    )
    joined = "\n".join(parts).encode("utf-8")
    return DIGEST_PREFIX + hashlib.sha256(joined).hexdigest()


def _driver_error(error: EvidenceError) -> DriverError:
    return DriverError(error.code, error.detail)


def _issue_inspection(
    binding: str, profile: DriverProfile, owner: str, facts: object, plan: object
) -> DriverInspection:
    """Freeze what was read and record that this process issued exactly it."""

    try:
        frozen_facts, frozen_plan, evidence = issue(binding, facts, plan)
    except EvidenceError as error:
        raise _driver_error(error) from None
    return DriverInspection(
        binding, profile, owner, frozen_facts, frozen_plan, evidence
    )


def _require_issued_evidence(inspection: DriverInspection) -> None:
    try:
        require_issued(
            inspection.binding, inspection.facts, inspection.plan, inspection.evidence
        )
    except EvidenceError as error:
        raise _driver_error(error) from None


def build_plan_request(
    profile: DriverProfile,
    owner: str,
    artifact: ArtifactSelection,
    facts: Mapping[str, object],
) -> dict[str, object]:
    """Shape the planner's request from the selected target and live facts.

    The target block is derived from the profile, never from the caller, so
    the facts and the plan describe the same roots.
    """

    return {
        "schema_version": 1,
        "target": {
            "os": "linux",
            "install_root": profile.remote_app_dir,
            "data_root": profile.remote_data_dir,
            "owner": owner,
            "expected_workspace_id": profile.expected_workspace_id,
        },
        "artifact": {
            "product_version": artifact.product_version,
            "protocol_version": artifact.protocol_version,
            "digest": artifact.digest,
        },
        "facts": plain(facts),
    }


def _probe_and_plan(
    profile: DriverProfile,
    owner: str,
    artifact: ArtifactSelection,
    *,
    ssh_executable: str,
    timeout: float,
    process_factory: Callable[..., object],
) -> tuple[dict[str, object], dict[str, object]]:
    """Read live facts for this exact profile and plan from exactly those.

    The only path in this module that produces facts.  Both the inspection and
    the pre-install revalidation go through here, so an install can never be
    authorised by anything but a probe run against the selected profile.
    """

    facts = run_remote_provision_probe(
        profile,
        owner,
        ssh_executable=ssh_executable,
        timeout=timeout,
        process_factory=process_factory,
    )
    request = build_plan_request(profile, owner, artifact, facts)
    plan = plan_remote_provision(json.dumps(request, ensure_ascii=True).encode("utf-8"))
    return facts, plan


def inspect_remote_target(
    profile: DriverProfile,
    owner: str,
    artifact: ArtifactSelection,
    *,
    ssh_executable: str,
    timeout: float = PROBE_TIMEOUT_SECONDS,
    process_factory: Callable[..., object] = subprocess.Popen,
) -> DriverInspection:
    """Collect live facts for this exact profile and plan against them.

    Read-only.  This never installs and never mutates anything local or
    remote.  The returned plan is only ever computed from facts this call
    collected; a plan document handed in from outside is not accepted
    anywhere in this module.
    """

    target = _require_profile(profile)
    user = _require_owner(owner)
    if not isinstance(artifact, ArtifactSelection):
        raise DriverError("DRIVER_ARTIFACT_INVALID", "an ArtifactSelection is required")
    executable = _require_ssh_executable(ssh_executable)
    bound_timeout = _require_timeout(timeout, 60.0)

    try:
        facts, plan = _probe_and_plan(
            target,
            user,
            artifact,
            ssh_executable=executable,
            timeout=bound_timeout,
            process_factory=process_factory,
        )
    except PlanError as error:
        raise DriverError(
            "DRIVER_PLAN_REFUSED", f"plan rejected the request: {error.code}"
        ) from None
    return _issue_inspection(
        compute_binding(target, user, artifact), target, user, facts, plan
    )


def _require_fresh_target(facts: Mapping[str, object], expected_workspace_id: str) -> None:
    """Admit only a fresh application directory for the expected workspace.

    The planner already refuses a conflicting install root, but it tolerates a
    data root that does not exist yet.  The installer engine does not: it
    requires an existing data root carrying the expected workspace identity.
    Refusing that here keeps the failure local instead of spending an SSH
    round trip to learn it.
    """

    install = facts.get("install")
    data = facts.get("data")
    if not isinstance(install, Mapping) or not isinstance(data, Mapping):
        raise DriverError("DRIVER_TARGET_NOT_FRESH", "live facts did not describe both roots")
    if install.get("exists") is not False or install.get("symlink") is not False:
        raise DriverError(
            "DRIVER_TARGET_NOT_FRESH",
            "install requires an application directory that does not exist yet",
        )
    if data.get("exists") is not True or data.get("symlink") is not False:
        raise DriverError(
            "DRIVER_WORKSPACE_UNCONFIRMED",
            "install requires an existing non-symlink data root",
        )
    observed = data.get("workspace_id")
    if type(observed) is not str or observed != expected_workspace_id:
        raise DriverError(
            "DRIVER_WORKSPACE_UNCONFIRMED",
            "live data root identity is not the expected workspace",
        )


#: Exchange codes whose remote effect is ambiguous, because the payload had
#: already left this process when they happened.  ``NOT_STARTED`` and
#: ``NO_STDIN`` are the only two that cannot have reached the remote host.
_AMBIGUOUS_EXCHANGE_CODES = {
    "TIMEOUT": "DRIVER_TIMEOUT",
    "OVERSIZE": "DRIVER_OVERSIZE",
    "NO_OUTPUT": "DRIVER_TRANSPORT_FAILED",
}


def _unknown_outcome(code: str, cleanup: str) -> InstallOutcome:
    """An ambiguous commit, plus whatever cleanup actually managed to do.

    A cleanup that could not be confirmed adds guidance; it never downgrades
    the commit to a refusal and never suggests a retry.
    """

    guidance = RECONCILE_GUIDANCE
    if cleanup != CLEANUP_SETTLED:
        guidance = f"{RECONCILE_GUIDANCE} {CLEANUP_GUIDANCE}"
    return InstallOutcome(OUTCOME_UNKNOWN, code, None, guidance, cleanup)


def _exchange_failure(error: ExchangeError) -> InstallOutcome:
    """Report a channel failure without inventing a clean stop or a retry."""

    if error.code not in _AMBIGUOUS_EXCHANGE_CODES:
        raise DriverError(
            "DRIVER_TRANSPORT_FAILED",
            "the install channel could not be started or used",
            cleanup=error.cleanup,
        )
    return _unknown_outcome(_AMBIGUOUS_EXCHANGE_CODES[error.code], error.cleanup)


def _one_line(payload: bytes, limit: int) -> bytes | None:
    if not payload or len(payload) > limit:
        return None
    if b"\r" in payload or payload.count(b"\n") != 1 or not payload.endswith(b"\n"):
        return None
    return payload[:-1]


def _refusal_code(stderr_payload: bytes) -> str:
    line = _one_line(stderr_payload, MAX_STDERR)
    if line is None:
        return "REMOTE_INSTALL_FAILED"
    try:
        document = load_json_object(line)
    except (UnicodeError, ValueError, RecursionError, MemoryError):
        return "REMOTE_INSTALL_FAILED"
    if set(document) != set(REFUSE_KEYS) or document.get("outcome") != "refused":
        return "REMOTE_INSTALL_FAILED"
    code = document.get("code")
    if type(code) is not str or code not in STABLE_INSTALL_CODES:
        return "REMOTE_INSTALL_FAILED"
    return code


def _exact_receipt(
    stdout_payload: bytes,
    profile: DriverProfile,
    artifact: ArtifactSelection,
) -> dict[str, object] | None:
    """Accept a success line only if it is exactly what we asked to install."""

    line = _one_line(stdout_payload, MAX_STDOUT)
    if line is None:
        return None
    try:
        document = load_json_object(line)
    except (UnicodeError, ValueError, RecursionError, MemoryError):
        return None
    if set(document) != set(SUCCESS_KEYS):
        return None
    expected = {
        "outcome": "installed",
        "schema_version": 1,
        "workspace_uid": profile.expected_workspace_id,
        "artifact_digest": artifact.digest,
        "artifact_manifest_sha256": artifact.manifest_digest,
        "product_version": artifact.product_version,
        "remote_protocol_version": artifact.protocol_version,
    }
    if any(document.get(key) != value for key, value in expected.items()):
        return None
    return document


def _authorise_install(
    inspection: object, artifact: object, apply: object
) -> tuple[DriverProfile, str]:
    """Decide whether this install may even be considered, before any process runs.

    Every refusal here costs zero remote work.  Passing this is not approval:
    it only establishes that the caller handed back an inspection this process
    issued, unedited, for this exact target and artifact.  What the remote
    state *is* is settled afterwards, by reading it again.
    """

    if not isinstance(inspection, DriverInspection):
        raise DriverError("DRIVER_BINDING_MISMATCH", "a DriverInspection is required")
    if not isinstance(artifact, ArtifactSelection):
        raise DriverError("DRIVER_ARTIFACT_INVALID", "an ArtifactSelection is required")
    if apply is not True:
        raise DriverError(
            "DRIVER_APPLY_NOT_REQUESTED",
            "install requires an explicit apply; inspect/plan is the default",
        )
    target = _require_profile(inspection.profile)
    user = _require_owner(inspection.owner)
    if compute_binding(target, user, artifact) != inspection.binding:
        raise DriverError(
            "DRIVER_BINDING_MISMATCH",
            "the plan was produced for a different target or artifact",
        )
    _require_issued_evidence(inspection)
    if not inspection.install_allowed:
        raise DriverError(
            "DRIVER_PLAN_REFUSED",
            f"plan decision does not authorise an install: {inspection.decision}",
        )
    _require_fresh_target(inspection.facts, target.expected_workspace_id)
    return target, user


def _revalidate_install(
    inspection: DriverInspection,
    target: DriverProfile,
    user: str,
    artifact: ArtifactSelection,
    *,
    ssh_executable: str,
    timeout: float,
    process_factory: Callable[..., object],
) -> None:
    """Read the target again and re-plan, immediately before the install spawn.

    The inspection says what was true when it was taken; this says what is
    true now, and the install is authorised by this second reading.  Failing
    to read the current facts refuses too: not being able to check is never
    approval.
    """

    try:
        facts, plan = _probe_and_plan(
            target,
            user,
            artifact,
            ssh_executable=ssh_executable,
            timeout=timeout,
            process_factory=process_factory,
        )
    except (ProbeError, PlanError) as error:
        raise DriverError(
            "DRIVER_REVALIDATION_FAILED",
            f"the current remote state could not be established: {error.code}",
        ) from None
    decision = plan.get("decision")
    if decision != "install_needed":
        raise DriverError(
            "DRIVER_PLAN_REFUSED", f"the current facts do not authorise an install: {decision}"
        )
    _require_fresh_target(facts, target.expected_workspace_id)
    if compute_evidence(inspection.binding, facts, plan) != inspection.evidence:
        raise DriverError(
            "DRIVER_TARGET_CHANGED",
            "the target changed since it was inspected; inspect it again",
        )


def _install_payload(artifact: ArtifactSelection) -> bytes:
    try:
        return build_installer_payload_source(
            archive_bytes=artifact.archive_bytes, sidecar_bytes=artifact.sidecar_bytes
        )
    except PayloadError as error:
        raise DriverError("DRIVER_ARTIFACT_INVALID", f"payload refused: {error.code}") from None


def _install_command(target: DriverProfile, user: str, executable: str) -> list[str]:
    try:
        return build_ssh_provision_install_command(target, user, executable)
    except RemoteCommandError:
        raise DriverError(
            "DRIVER_PROFILE_INVALID", "the install command could not be built for this target"
        ) from None


def _interpret(
    result: ExchangeOutput,
    target: DriverProfile,
    artifact: ArtifactSelection,
    cleanup: str,
) -> InstallOutcome:
    """Turn one completed exchange into exactly one terminal outcome."""

    if result.returncode != 0:
        code = _refusal_code(result.stderr)
        if code in AMBIGUOUS_INSTALL_CODES:
            return _unknown_outcome(code, cleanup)
        return InstallOutcome(OUTCOME_REFUSED, code, None, None, cleanup)
    receipt = _exact_receipt(result.stdout, target, artifact)
    if receipt is None:
        return _unknown_outcome("DRIVER_RECEIPT_UNRECOGNISED", cleanup)
    return InstallOutcome(OUTCOME_INSTALLED, None, receipt, None, cleanup)


def apply_remote_install(
    inspection: DriverInspection,
    artifact: ArtifactSelection,
    *,
    apply: bool = False,
    ssh_executable: str,
    timeout: float = INSTALL_TIMEOUT_SECONDS,
    probe_timeout: float = PROBE_TIMEOUT_SECONDS,
    process_factory: Callable[..., object] = subprocess.Popen,
) -> InstallOutcome:
    """Install only what the *current* remote state authorises, or refuse.

    Nothing here trusts the retained inspection as proof of remote state.  It
    is checked for integrity and issuance, and then the target is read again
    through the same read-only probe and re-planned; an install command is
    built only if that second reading still says ``install_needed`` for a
    fresh application directory carrying the expected workspace.  Every
    refusal below, including a refusal to re-read, happens before the install
    process is created and costs zero install work.

    The returned outcome is terminal.  ``unknown`` is never converted into a
    retry, and this function never deletes, overwrites, or steals anything.
    Nothing here activates a connection: a successful install leaves the
    profile, the registry, and the running server exactly as they were.
    """

    target, user = _authorise_install(inspection, artifact, apply)
    executable = _require_ssh_executable(ssh_executable)
    bound_timeout = _require_timeout(timeout, 900.0)
    payload = _install_payload(artifact)
    _revalidate_install(
        inspection,
        target,
        user,
        artifact,
        ssh_executable=executable,
        timeout=_require_timeout(probe_timeout, 60.0),
        process_factory=process_factory,
    )
    try:
        exchange = start_exchange(process_factory, _install_command(target, user, executable))
        exchange.write(payload, bound_timeout)
        result = exchange.drain(MAX_STDOUT, MAX_STDERR, bound_timeout)
    except ExchangeError as error:
        return _exchange_failure(error)
    return _interpret(result, target, artifact, exchange.cleanup)


def _target_document(inspection: DriverInspection) -> dict[str, object]:
    return {
        "ssh_host_alias": inspection.profile.ssh_host_alias,
        "install_root": inspection.profile.remote_app_dir,
        "data_root": inspection.profile.remote_data_dir,
        "owner": inspection.owner,
        "expected_workspace_id": inspection.profile.expected_workspace_id,
    }


def render_inspection(inspection: DriverInspection) -> dict[str, object]:
    """Shape one inspect/plan report. Read-only, no activation claim."""

    return {
        "schema_version": SCHEMA_VERSION,
        "stage": "inspect",
        "binding": inspection.binding,
        "evidence": inspection.evidence,
        "target": _target_document(inspection),
        "facts": plain(inspection.facts),
        "plan": plain(inspection.plan),
        "activation": "not_activated",
    }


def render_outcome(
    inspection: DriverInspection, outcome: InstallOutcome
) -> dict[str, object]:
    """Shape one install report. An install is never an activation."""

    document: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "stage": "apply",
        "binding": inspection.binding,
        "target": _target_document(inspection),
        "outcome": outcome.outcome,
        "code": outcome.code,
        "receipt": outcome.receipt,
        "cleanup": outcome.cleanup,
        "activation": "not_activated",
    }
    if outcome.guidance is not None:
        document["guidance"] = outcome.guidance
    return document


def encode_document(document: Mapping[str, object]) -> bytes:
    encoded = json.dumps(
        document, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return encoded + b"\n"


__all__ = [
    "AMBIGUOUS_INSTALL_CODES", "ArtifactSelection", "CLEANUP_GUIDANCE",
    "CLEANUP_SETTLED", "CLEANUP_UNSETTLED", "DriverError", "DriverInspection",
    "DriverProfile", "INSTALL_TIMEOUT_SECONDS", "InstallOutcome",
    "OUTCOME_INSTALLED", "OUTCOME_REFUSED", "OUTCOME_UNKNOWN",
    "PROBE_TIMEOUT_SECONDS", "ProbeError", "RECONCILE_GUIDANCE",
    "apply_remote_install", "build_plan_request", "compute_binding",
    "compute_evidence", "encode_document", "inspect_remote_target",
    "render_inspection", "render_outcome", "select_artifact",
]
