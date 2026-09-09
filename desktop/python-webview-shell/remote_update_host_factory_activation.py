"""The update flow's ``ActivationPort``, over the real connection registry.

``RegistryFlowActivationPort`` is the one place the remote update flow's
activate/confirm/rollback stages meet the desktop's existing activation
machinery.  It builds no registry of its own, writes no receipt of its own and
invents no activation identity: every effect is one call on the already
reviewed :class:`~remote_update_activation_adapter.RegistryActivationAdapter`,
which in turn drives ``ConnectionRegistryMutationService`` and
``ConnectionRegistryActivationRecoveryService``.

What this module adds is the two things the adapter deliberately does not do.

**The candidate registry.**  Activating the prepared application means
selecting the *same* profile pointed at the new application directory.  The
candidate registry is therefore the live registry with exactly that one field
replaced and ``active_profile_id`` set to that profile -- nothing else moves,
and the profile's expected workspace is never rewritten.

**The proof.**  ``ConnectionRegistryMutationService.activate`` refuses without
a short-lived proof produced from one exact successful profile test.  That
proof is minted here from a real read-only identity probe of the *candidate*
profile, so what is proven is the identity the target application actually
serves, not that some digest somewhere is well formed.  A probe that does not
come back ``ready`` for the expected workspace refuses the activation before
anything is written; a preparation receipt is never accepted in its place.

The port answers in the flow's own vocabulary: ``ActivationOutcome`` and
``RollbackOutcome``, ``PortRefusal`` where nothing can have happened, and
``LostResponse`` where the registry may or may not hold the effect.  The
``observe`` family issues nothing at all.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

_SHELL_DIR = str(Path(__file__).resolve(strict=True).parent)
_ROOT = str(Path(_SHELL_DIR).parents[1])
for _path in (_SHELL_DIR, _ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from connection_registry import (  # noqa: E402
    ConnectionRegistry,
    SshConnectionProfile,
)
from connection_registry_mutations import current_registry_snapshot  # noqa: E402
from profile_inspection import (  # noqa: E402
    ProfileInspectionError,
    ProfileTestResult,
    SshProfileTestCandidate,
    inspect_profile,
)
from remote_update_activation_binding import ActivationBindingError  # noqa: E402
from remote_update_activation_adapter import (  # noqa: E402
    ActivationAdapterRefusal,
    ActivationCandidate,
    ActivationObservation,
    ActivationResponseLost,
    RegistryActivationAdapter,
    RollbackObservation,
)
from remote_update_flow_contract import (  # noqa: E402
    ActivationOutcome,
    LostResponse,
    PortRefusal,
    RollbackOutcome,
)
from ssh_profile_metadata import run_remote_profile_metadata_check  # noqa: E402


#: Refusals this port raises before any registry effect is possible.
REFUSED_NO_PROFILE = "activation_profile_not_selected"
REFUSED_NOT_SSH = "activation_profile_not_remote"
REFUSED_TARGET = "activation_target_invalid"
REFUSED_PROBE = "activation_candidate_probe_refused"
REFUSED_IDENTITY = "activation_candidate_identity_refused"
REFUSED_PROOF = "activation_proof_refused"
REFUSED_REGISTRY = "activation_registry_unreadable"


class CandidateProbeRefused(RuntimeError):
    """The candidate application did not prove the expected served identity."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def candidate_profile(
    profile: SshConnectionProfile, target_app_dir: str
) -> SshConnectionProfile:
    """The same profile, pointed at the prepared application directory.

    One field moves.  The expected workspace, the alias, the data directory and
    the forward port are what make this the same authority, so rewriting any of
    them here would make the activation a different connection wearing the same
    profile id.
    """

    if not isinstance(profile, SshConnectionProfile):
        raise CandidateProbeRefused(REFUSED_NOT_SSH)
    if not isinstance(target_app_dir, str) or not target_app_dir.startswith("/"):
        raise CandidateProbeRefused(REFUSED_TARGET)
    if target_app_dir == profile.remote_app_dir:
        raise CandidateProbeRefused(REFUSED_TARGET)
    return replace(profile, remote_app_dir=target_app_dir)


def candidate_registry(
    registry: ConnectionRegistry, prepared: SshConnectionProfile
) -> ConnectionRegistry:
    """The live registry with exactly this profile replaced and selected."""

    matches = [
        profile
        for profile in registry.profiles
        if profile.profile_id == prepared.profile_id
    ]
    if len(matches) != 1 or not matches[0].enabled:
        raise CandidateProbeRefused(REFUSED_NO_PROFILE)
    profiles = tuple(
        prepared if profile.profile_id == prepared.profile_id else profile
        for profile in registry.profiles
    )
    return replace(registry, active_profile_id=prepared.profile_id, profiles=profiles)


def require_served_identity(
    result: object, expected_workspace_id: str
) -> ProfileTestResult:
    """Admit only a probe that says this target serves the expected workspace.

    ``ready`` alone is not enough and neither is a well-formed answer: the
    workspace the candidate actually reported has to be the one being updated.
    """

    if not isinstance(result, ProfileTestResult):
        raise CandidateProbeRefused(REFUSED_PROBE)
    if result.status != "ready":
        raise CandidateProbeRefused(REFUSED_IDENTITY)
    if result.actual_workspace_id != expected_workspace_id:
        raise CandidateProbeRefused(REFUSED_IDENTITY)
    return result


def activation_outcome(observed: ActivationObservation) -> ActivationOutcome:
    """Project one adapter observation into the flow's own answer.

    The adapter's ``selection``, ``code`` and recovery fields are diagnostics
    for the host, not part of what the flow decides on, so they are not carried
    across.  The versions stay ``None``: this port reads a registry, and a
    registry does not measure what the remote serves.
    """

    return ActivationOutcome(
        status=observed.status,
        committed=observed.committed,
        state=observed.state,
        restart_required=observed.restart_required,
    )


def rollback_outcome(observed: RollbackObservation) -> RollbackOutcome:
    return RollbackOutcome(
        status=observed.status,
        previous_activation_selected=observed.previous_activation_selected,
    )


def _refusal_code(error: BaseException, fallback: str) -> str:
    argument = error.args[0] if error.args else ""
    return argument if isinstance(argument, str) and argument else fallback


class RegistryFlowActivationPort:
    """One selected profile's activation, issued and observed through the registry.

    ``probe`` is the read-only SSH identity check the proof is minted from.  It
    is injectable so a deterministic stand-in can replace the transport, never
    the decision: the admission in :func:`require_served_identity` runs on
    whatever it returns.
    """

    def __init__(
        self,
        state_root: Path,
        *,
        profile_id: str,
        target_app_dir: str,
        expected_workspace_id: str,
        adapter: RegistryActivationAdapter,
        mutation_service: object,
        probe: Callable[[SshConnectionProfile], object] = run_remote_profile_metadata_check,
        registry_reader: Callable[[Path], tuple[ConnectionRegistry, str]] = current_registry_snapshot,
    ) -> None:
        self._state_root = Path(state_root)
        self._profile_id = profile_id
        self._target_app_dir = target_app_dir
        self._expected_workspace_id = expected_workspace_id
        self._adapter = adapter
        self._mutations = mutation_service
        self._probe = probe
        self._registry_reader = registry_reader
        self._last_probe: ProfileTestResult | None = None

    @property
    def last_candidate_probe(self) -> ProfileTestResult | None:
        """The identity probe the last activation attempt was proven by."""

        return self._last_probe

    def observe_receipt(self, operation_id: str) -> ActivationObservation:
        """The adapter's own full observation, for the host's startup evidence.

        Not part of ``ActivationPort``: the flow decides on ``ActivationOutcome``
        and has no use for ``selection``, but the host does -- whether the live
        registry currently selects this operation's receipt is exactly what a
        restart has to establish, and it is a different question from whether
        the receipt was written.  Read-only, like every ``observe``.
        """

        return self._adapter.observe(operation_id)

    # -- ActivationPort ---------------------------------------------------

    def activate(self, operation_id: str) -> ActivationOutcome:
        """Bind this operation to its exact candidate, then issue it once."""

        try:
            self._bind(operation_id)
        except CandidateProbeRefused as error:
            raise PortRefusal(error.code) from None
        return self._observed(operation_id, self._adapter.activate)

    def observe(self, operation_id: str) -> ActivationOutcome:
        return self._observed(operation_id, self._adapter.observe)

    def confirm(self, operation_id: str) -> ActivationOutcome:
        return self._observed(operation_id, self._adapter.confirm)

    def observe_confirm(self, operation_id: str) -> ActivationOutcome:
        return self._observed(operation_id, self._adapter.observe_confirm)

    def rollback(self, operation_id: str) -> RollbackOutcome:
        return self._rolled_back(operation_id, self._adapter.rollback)

    def observe_rollback(self, operation_id: str) -> RollbackOutcome:
        return self._rolled_back(operation_id, self._adapter.observe_rollback)

    # -- one adapter call -------------------------------------------------

    def _observed(
        self, operation_id: str, call: Callable[[str], ActivationObservation]
    ) -> ActivationOutcome:
        try:
            return activation_outcome(call(operation_id))
        except ActivationResponseLost:
            raise LostResponse(operation_id) from None
        except ActivationAdapterRefusal as error:
            raise PortRefusal(_refusal_code(error, "activation_refused")) from None
        except ActivationBindingError as error:
            # No binding means this operation never reached the registry, so
            # nothing can have been written under it.  A refusal, not an
            # unknown: an unknown would leave the flow an identity to
            # reconcile forever against a receipt that does not exist.
            raise PortRefusal(error.code) from None

    def _rolled_back(
        self, operation_id: str, call: Callable[[str], RollbackObservation]
    ) -> RollbackOutcome:
        try:
            return rollback_outcome(call(operation_id))
        except ActivationResponseLost:
            raise LostResponse(operation_id) from None
        except ActivationAdapterRefusal as error:
            raise PortRefusal(_refusal_code(error, "rollback_refused")) from None
        except ActivationBindingError as error:
            raise PortRefusal(error.code) from None

    # -- candidate and proof ----------------------------------------------

    def _bind(self, operation_id: str) -> None:
        """Prove the candidate and record its fingerprint before any effect.

        Re-entering after a lost response re-probes the same target and rebinds
        the same fingerprint; the adapter's own ``bind`` is idempotent for that
        fingerprint, so nothing here can issue a second activation.
        """

        registry, digest = self._read_registry()
        prepared = candidate_profile(self._selected(registry), self._target_app_dir)
        candidate = candidate_registry(registry, prepared)
        result = require_served_identity(
            self._run_probe(prepared), self._expected_workspace_id
        )
        self._last_probe = result
        try:
            proof = self._mutations.issue_successful_test_proof(
                prepared, result, base_registry_digest=digest
            )
        except Exception:  # noqa: BLE001 - the service's refusal family is open
            raise CandidateProbeRefused(REFUSED_PROOF) from None
        self._adapter.bind(
            ActivationCandidate(
                operation_id=operation_id,
                registry=candidate,
                profile_id=prepared.profile_id,
                proof_id=proof.proof_id,
                expected_registry_digest=digest,
            )
        )

    def _read_registry(self) -> tuple[ConnectionRegistry, str]:
        try:
            registry, digest = self._registry_reader(self._state_root)
        except Exception:  # noqa: BLE001 - any unreadable registry is a refusal
            raise CandidateProbeRefused(REFUSED_REGISTRY) from None
        if not isinstance(registry, ConnectionRegistry) or not isinstance(digest, str):
            raise CandidateProbeRefused(REFUSED_REGISTRY)
        return registry, digest

    def _selected(self, registry: ConnectionRegistry) -> SshConnectionProfile:
        for profile in registry.profiles:
            if profile.profile_id == self._profile_id:
                if not isinstance(profile, SshConnectionProfile):
                    raise CandidateProbeRefused(REFUSED_NOT_SSH)
                if profile.expected_workspace_id != self._expected_workspace_id:
                    raise CandidateProbeRefused(REFUSED_IDENTITY)
                return profile
        raise CandidateProbeRefused(REFUSED_NO_PROFILE)

    def _run_probe(self, prepared: SshConnectionProfile) -> object:
        try:
            return inspect_profile(
                SshProfileTestCandidate(prepared, self._expected_workspace_id),
                ssh_profile_tester=self._probe,
            )
        except ProfileInspectionError:
            raise CandidateProbeRefused(REFUSED_PROBE) from None
        except (OSError, RuntimeError, ValueError, TypeError):
            raise CandidateProbeRefused(REFUSED_PROBE) from None


__all__ = [
    "CandidateProbeRefused",
    "REFUSED_IDENTITY",
    "REFUSED_NOT_SSH",
    "REFUSED_NO_PROFILE",
    "REFUSED_PROBE",
    "REFUSED_PROOF",
    "REFUSED_REGISTRY",
    "REFUSED_TARGET",
    "RegistryFlowActivationPort",
    "activation_outcome",
    "candidate_profile",
    "candidate_registry",
    "require_served_identity",
    "rollback_outcome",
]
