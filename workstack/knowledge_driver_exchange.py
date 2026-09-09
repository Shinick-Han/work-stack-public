"""One bounded exchange with one operator-pinned knowledge driver.

This module is a *trusted internal transport*, not a request parser.  Every
argument is supplied by Work Stack code that already decided what may run:
``command`` is the argv an operator pinned in configuration, ``payload`` is a
document this process composed, and ``environment`` is the exact mapping the
caller chose to hand over.  None of the three is ever taken from a browser
request, a stored document, or a remote answer, and this module deliberately
does no admission work that belongs to the caller: it does not read a config
file, resolve an executable, follow a symlink, or build a command.  It admits
only *types and sizes*, so a shape mistake in the calling code is refused
before anything is spawned instead of reaching ``Popen``.

What it does own is the bounded child exchange: one process, one write, one
drain, one overall deadline, and one closed outcome.  It reuses the shared
``workstack.bounded_process_exchange`` primitive rather than repeating it, so
the kill/reap/settle contract -- and its ``settled``/``unsettled`` honesty --
is the same one the desktop remote-provisioning driver already relies on.

The outcome is deliberately narrow:

* success requires **both** a zero child exit and a ``settled`` cleanup, and
  carries the child's stdout as bytes that are still completely untrusted --
  parsing and verifying them belongs to the caller, not here;
* ``invalid_driver_input`` means nothing was spawned at all;
* ``driver_not_started`` is only used where the shared exchange proves the
  payload never left this process (``NOT_STARTED``, ``NO_STDIN``);
* ``driver_outcome_unknown`` covers every branch where the driver may already
  have acted -- timeout, oversize output, missing output, nonzero exit, a
  cleanup that could not be confirmed, or an otherwise clean answer that only
  arrived once the deadline had passed.  Nothing here retries, on any branch,
  because an unknown outcome is not evidence that nothing happened.

The deadline is a bound on what may be *accepted*, and not a promise about
when this function returns.  The shared primitive keeps legacy floors and a
post-kill settle grace, and the operating system schedules threads when it
likes, so the wall clock can run past ``timeout_seconds`` before the call
comes back.  What is guaranteed is the judgement: once the budget is spent,
no phase begins and no result is a success, however clean it looks.

An error carries a fixed code and the cleanup fact and nothing else: no child
stderr, no exception text, no command, path, payload, or environment value.

Two things this module does **not** claim.  Killing the direct child does not
prove that processes the child itself started are gone; the reported cleanup
describes this exchange's own handles.  And an explicit environment is not an
operating-system sandbox: the driver runs as the same user with that user's
rights, and its credentials remain the operating concern of whoever pinned it.
"""

from __future__ import annotations

import math
import os
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from workstack.bounded_process_exchange import (
    CLEANUP_SETTLED,
    NOT_STARTED,
    NO_STDIN,
    BoundedExchange,
    ExchangeError,
    start_exchange,
)

#: Shape bounds on the argv an operator pinned.  The caller separately proves
#: that ``argv[0]`` is the executable it means to run.
MAX_COMMAND_PARTS = 16
MAX_COMMAND_PART_CHARS = 512

#: One request document, written to stdin and never placed on the argv.
MAX_PAYLOAD_BYTES = 16 * 1024

#: Output bounds, matching the existing bounded provider transport.
MAX_STDOUT_BYTES = 64 * 1024
MAX_STDERR_BYTES = 8 * 1024

#: One finite budget covers the write and the drain together.
DEFAULT_TIMEOUT_SECONDS = 75.0
MAX_TIMEOUT_SECONDS = 120.0

#: Closed outcome codes.  These are the only strings that leave this module.
INVALID_DRIVER_INPUT = "invalid_driver_input"
DRIVER_NOT_STARTED = "driver_not_started"
DRIVER_OUTCOME_UNKNOWN = "driver_outcome_unknown"


@dataclass(frozen=True)
class DriverExchangeResult:
    """The whole outcome of one exchange: bytes, a closed code, or neither."""

    #: The child's untrusted stdout on success, and ``None`` on every failure.
    stdout: bytes | None
    #: ``None`` on success, otherwise one of the three closed codes above.
    error_code: str | None
    #: ``settled`` only when every handle of this exchange was accounted for.
    cleanup: str


def _refused(code: str, cleanup: str = CLEANUP_SETTLED) -> DriverExchangeResult:
    return DriverExchangeResult(None, code, cleanup)


def _admissible_command(command: object) -> bool:
    """One to sixteen non-empty short parts, the first one absolute."""

    if not isinstance(command, (list, tuple)):
        return False
    if not 1 <= len(command) <= MAX_COMMAND_PARTS:
        return False
    for part in command:
        if not isinstance(part, str) or not part:
            return False
        if len(part) > MAX_COMMAND_PART_CHARS:
            return False
    return os.path.isabs(command[0])


def _admissible_payload(payload: object) -> bool:
    return isinstance(payload, bytes) and 0 < len(payload) <= MAX_PAYLOAD_BYTES


def _admissible_environment(environment: object) -> bool:
    """An explicit string-to-string mapping -- never an inherited default."""

    if not isinstance(environment, Mapping):
        return False
    return all(
        isinstance(name, str) and isinstance(value, str)
        for name, value in environment.items()
    )


def _admissible_timeout(timeout_seconds: object) -> bool:
    """A finite positive budget.  ``True`` is not a one-second timeout."""

    if isinstance(timeout_seconds, bool):
        return False
    if not isinstance(timeout_seconds, (int, float)):
        return False
    if not math.isfinite(timeout_seconds):
        return False
    return 0.0 < timeout_seconds <= MAX_TIMEOUT_SECONDS


def _bound_factory(
    process_factory: Callable[..., object], environment: Mapping[str, str]
) -> Callable[..., object]:
    """Bind this exchange's explicit environment to its single spawn.

    The shared ``start_exchange`` owns the pipes and the hidden-window flag;
    binding here adds the two decisions that belong to this transport without
    copying that primitive: no shell, and exactly the given environment.
    """

    prepared = dict(environment)

    def spawn(command: list[str], **keywords: object) -> object:
        return process_factory(command, shell=False, env=prepared, **keywords)

    return spawn


def _closed_code(code: str) -> str:
    """Map a shared exchange code to what the caller may conclude from it."""

    if code in (NOT_STARTED, NO_STDIN):
        return DRIVER_NOT_STARTED
    return DRIVER_OUTCOME_UNKNOWN


def _expired(deadline: float) -> bool:
    """Whether the one operation budget has been reached or passed."""

    return time.monotonic() >= deadline


def _exchange_once(
    exchange: BoundedExchange, payload: bytes, timeout_seconds: float
) -> DriverExchangeResult:
    """Write, drain and judge against one monotonic deadline.

    The deadline governs *acceptance*, not only the arguments handed to the
    shared primitive.  That primitive keeps legacy floors -- a drain never
    waits less than a tenth of a second for stderr or for the reap -- and its
    post-kill settle grace is a separate bound again, so the budget passed in
    is not by itself proof that the answer arrived in time.  The budget is
    therefore checked before each phase and once more after the drain: a
    child that answered late is stopped and reported unknown, and its late
    output is discarded rather than returned as a success.  Nothing here
    resets the deadline, asks for a fresh one, or runs a phase twice.
    """

    deadline = time.monotonic() + timeout_seconds
    try:
        if _expired(deadline):
            return _refused(DRIVER_OUTCOME_UNKNOWN, exchange.stop())
        exchange.write(payload, deadline - time.monotonic())
        if _expired(deadline):
            # The write spent the whole budget: the drain never begins, and
            # the payload may already have reached the driver.
            return _refused(DRIVER_OUTCOME_UNKNOWN, exchange.stop())
        output = exchange.drain(
            MAX_STDOUT_BYTES,
            MAX_STDERR_BYTES,
            deadline - time.monotonic(),
        )
    except ExchangeError as error:
        return _refused(_closed_code(error.code), error.cleanup)
    except (OSError, ValueError, subprocess.SubprocessError):
        return _refused(DRIVER_OUTCOME_UNKNOWN, exchange.stop())
    except BaseException:
        # A programming error or an interrupt is not this module's to answer.
        # The child is still stopped before it is allowed to propagate.
        exchange.stop()
        raise
    if output.returncode != 0 or exchange.cleanup != CLEANUP_SETTLED:
        return _refused(DRIVER_OUTCOME_UNKNOWN, exchange.cleanup)
    if _expired(deadline):
        # A nominally clean result that arrived past the deadline is not a
        # success: the bytes are dropped and the real cleanup is preserved.
        # The child may well have exited already; stopping it again only
        # settles the handles and answers honestly if it could not.
        return _refused(DRIVER_OUTCOME_UNKNOWN, exchange.stop())
    return DriverExchangeResult(output.stdout, None, CLEANUP_SETTLED)


def run_knowledge_driver(
    command: Sequence[str],
    payload: bytes,
    *,
    environment: Mapping[str, str],
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    process_factory: Callable[..., object] = subprocess.Popen,
) -> DriverExchangeResult:
    """Run one pinned driver once and return its bounded, closed outcome.

    ``process_factory`` is a trusted injected seam so a test can stand in for
    the real launcher; it is called at most once, and never called at all when
    the arguments are refused.  The payload travels on stdin only -- it is
    never placed on the argv, and nothing here writes it, the command or the
    environment to a log.
    """

    if not (
        _admissible_command(command)
        and _admissible_payload(payload)
        and _admissible_environment(environment)
        and _admissible_timeout(timeout_seconds)
    ):
        return _refused(INVALID_DRIVER_INPUT)
    factory = _bound_factory(process_factory, environment)
    try:
        exchange = start_exchange(factory, list(command))
    except ExchangeError as error:
        return _refused(_closed_code(error.code), error.cleanup)
    except ValueError:
        # The launcher refused the arguments before any child existed, which
        # the existing bounded provider transport treats the same as an OSError.
        return _refused(DRIVER_NOT_STARTED)
    return _exchange_once(exchange, payload, float(timeout_seconds))


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "DRIVER_NOT_STARTED",
    "DRIVER_OUTCOME_UNKNOWN",
    "DriverExchangeResult",
    "INVALID_DRIVER_INPUT",
    "MAX_COMMAND_PARTS",
    "MAX_COMMAND_PART_CHARS",
    "MAX_PAYLOAD_BYTES",
    "MAX_STDERR_BYTES",
    "MAX_STDOUT_BYTES",
    "MAX_TIMEOUT_SECONDS",
    "run_knowledge_driver",
]
