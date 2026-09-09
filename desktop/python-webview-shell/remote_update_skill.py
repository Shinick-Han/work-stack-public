"""The optional agent-Skill port over the application this update prepared.

This is the effect half of the Skill offer.  It executes the helper that the
verified application already carries -- ``remote_skill_install.py``, by path,
inside the directory ``preparation_verified`` blessed -- over the same bounded
one-shot ssh exchange every other remote step uses, and turns its one JSON
document into the typed fact ``remote_update_skill_port`` defines.

It ships no program over stdin, builds no second command vocabulary and owns no
subprocess management: the argv comes from ``remote_provision_command``'s
``build_ssh_skill_command`` and the child is owned by ``bounded_process_exchange``
exactly as the unpack exchange owns its own.

Three properties are this module's own responsibility.

*   No ssh is started at all until the update has a verified prepared
    application in this process.  Without one the answer is the local refusal
    that names why, and nothing reaches the network.
*   The target is not a parameter.  Both calls read the profile and the
    artifact the update itself prepared, so the Skill written is the Skill of
    the build the operator just verified and of no other.
*   A run whose answer did not arrive is ``unknown`` and stays ``unknown``.
    This module never re-sends ``--apply`` to find out what happened; the
    caller's only next step is another read-only inspect, which measures the
    receipt and the bytes that are actually there.

``build_ssh_skill_command`` is resolved at import and may legitimately be
absent -- it is a separately reviewed transport seam.  When it is, this module
builds no port and the update screen simply never makes the offer, which is why
``RemoteUpdatePorts.skill`` is optional and defaults to nothing.
"""

from __future__ import annotations

from bounded_process_exchange import ExchangeError, start_exchange
from remote_command_contract import RemoteCommandError
from remote_update_install_ports import RemoteUpdateInstallPorts
from remote_update_install_ports_transport import (
    MAX_STDERR,
    MAX_STDOUT,
    PRE_EFFECT_EXCHANGE,
    parse_unpack_document as parse_json_line_document,
)
from remote_update_skill_port import (
    CHANNEL_LOST,
    CHANNEL_UNAVAILABLE,
    CHANNEL_UNBUILDABLE,
    DOCUMENT_MISMATCHED,
    DOCUMENT_UNRECOGNISED,
    SkillOutcome,
    admit_skill_outcome,
    unknown_outcome,
)


try:  # pragma: no cover - exercised by whichever half of the seam is present
    from remote_provision_command import build_ssh_skill_command as _BUILD_SKILL_COMMAND
except ImportError:  # pragma: no cover - the transport seam is not merged here
    _BUILD_SKILL_COMMAND = None

#: The refusal code the helper itself would answer with, used for the local
#: pre-flight so the screen names one reason rather than two.
APP_NOT_VERIFIED = "APP_NOT_VERIFIED"


def skill_transport_available() -> bool:
    """Whether the reviewed skill command builder is present in this build."""

    return callable(_BUILD_SKILL_COMMAND)


class RemoteUpdateSkillPort:
    """One ``SkillPort`` bound to one prepared application.

    ``build_command`` is the reviewed argv builder, injectable so a test can
    drive the whole port against the frozen signature without the transport
    module being merged.  It is called exactly as that seam exports it:
    ``build_command(profile, ssh_executable, apply=...)``.
    """

    def __init__(
        self,
        ports: RemoteUpdateInstallPorts,
        *,
        build_command: object = None,
    ) -> None:
        builder = build_command if build_command is not None else _BUILD_SKILL_COMMAND
        if not callable(builder):
            raise ValueError("a skill command builder is required")
        self._ports = ports
        self._build_command = builder

    def inspect_skill(self) -> SkillOutcome:
        """Measure the operator's Skill against this build.  Writes nothing."""

        return self._run(apply=False)

    def install_skill(self) -> SkillOutcome:
        """Write the Skill into ``$HOME``.  Only an explicit click reaches here."""

        return self._run(apply=True)

    def _run(self, *, apply: bool) -> SkillOutcome:
        if not self._ports.preparation_is_verified():
            # The one gate that precedes the network: without a verified
            # prepared application there is no build to install the Skill of.
            return SkillOutcome(status="refused", code=APP_NOT_VERIFIED, mutating=apply)
        command = self._command(apply=apply)
        if command is None:
            return unknown_outcome(CHANNEL_UNBUILDABLE, mutating=False)
        output, failure = self._exchange(command)
        if output is None:
            return unknown_outcome(failure, mutating=apply)
        document = parse_json_line_document(output.stdout)
        if document is None:
            return unknown_outcome(DOCUMENT_UNRECOGNISED, mutating=apply)
        admitted = admit_skill_outcome(
            document,
            output.returncode,
            expected_version=self._ports.inputs.artifact.product_version,
            mutating=apply,
        )
        if admitted is None:
            # Well-formed JSON that is not this helper's document for this
            # build: a different fact from stdout that was not a document.
            return unknown_outcome(DOCUMENT_MISMATCHED, mutating=apply)
        return admitted

    def _command(self, *, apply: bool) -> list[str] | None:
        try:
            command = self._build_command(
                self._ports.candidate_profile(),
                self._ports.inputs.ssh_executable,
                apply=apply,
            )
        except (RemoteCommandError, TypeError, ValueError):
            return None
        return command if isinstance(command, list) and command else None

    def _exchange(self, command: list[str]) -> tuple[object | None, str]:
        """One bounded run.  Never retried, and the argv is never reported."""

        timeout = self._ports.inputs.install_timeout
        try:
            exchange = start_exchange(self._ports.inputs.process_factory, command)
            exchange.write(b"", timeout)
            output = exchange.drain(MAX_STDOUT, MAX_STDERR, timeout)
        except ExchangeError as error:
            if error.code in PRE_EFFECT_EXCHANGE:
                return None, CHANNEL_UNAVAILABLE
            return None, CHANNEL_LOST
        return output, ""


def build_skill_port(
    ports: RemoteUpdateInstallPorts, *, build_command: object = None
) -> RemoteUpdateSkillPort | None:
    """The port, or nothing where this build carries no skill command builder."""

    if build_command is None and not skill_transport_available():
        return None
    try:
        return RemoteUpdateSkillPort(ports, build_command=build_command)
    except ValueError:
        return None


__all__ = [
    "APP_NOT_VERIFIED",
    "RemoteUpdateSkillPort",
    "build_skill_port",
    "skill_transport_available",
]
