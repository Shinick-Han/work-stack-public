# Knowledge driver exchange v1 contract

Status: `workstack/knowledge_driver_exchange.py` implements this document exactly. It is a
**trusted internal transport**, not a wire contract and not a request parser. It is a library
seam: **nothing in Work Stack calls `run_knowledge_driver` yet.** No route, no CLI command and
no desktop host reaches it at this revision, and this document does not describe a shipped user
flow. The owner route that will call it — current ledger/policy/Task/expiry/digest checks, the
per-instance execution guard, then this exchange outside the owner transaction — is the next
lane's, and it is contracted separately when it lands. Integrating this library is not an
adapter, a connector, or an execution feature.

The bounded child primitive it uses is `workstack/bounded_process_exchange.py`. That module
moved out of `desktop/python-webview-shell/` in this change so a backend process can use it
without putting the desktop shell directory on `sys.path`. The desktop path still exists and
still works: it binds `sys.modules["bounded_process_exchange"]` to the package module, so both
spellings are **the same module object**. There is exactly one `SETTLE_GRACE_SECONDS`, one set
of exchange codes and one `BoundedExchange` class, and a caller or test that assigns
`bounded_process_exchange.SETTLE_GRACE_SECONDS` still changes the bound the code reads.

## The call

```python
run_knowledge_driver(
    command,                 # operator-pinned argv, list or tuple
    payload,                 # request document, bytes, stdin only
    *,
    environment,             # explicit mapping, required
    timeout_seconds=75.0,    # one budget for the whole exchange
    process_factory=subprocess.Popen,
) -> DriverExchangeResult
```

`command`, `payload` and `environment` are all produced by Work Stack code that has already
decided what may run. **None of them is a public request surface.** This module does not read a
configuration file, resolve or stat an executable, follow a symlink, or build a command; pinning
the executable and its configuration is the caller's job, and stays the caller's job. What this
module admits is types and sizes only, so a shape mistake in the calling code is refused before
anything is spawned.

`process_factory` is a trusted injected seam so a test can stand in for the launcher. It is
called **at most once per call**, and never called at all when the request is refused.

## Admission

Every rule below is checked before any child exists. A single violation refuses the whole call
with `invalid_driver_input` and **zero spawn attempts**.

| Argument | Admitted |
| --- | --- |
| `command` | a `list` or `tuple` of 1–16 parts; every part a non-empty `str` of at most 512 characters; `command[0]` an absolute path |
| `payload` | `bytes`, non-empty, at most 16 KiB (`bytearray` and `str` are refused) |
| `environment` | a `Mapping` whose every key and value is a `str`; the empty mapping is admitted |
| `timeout_seconds` | a finite `int` or `float` with `0 < value <= 120`; `bool` is refused, so `True` is not a one-second timeout |

There is no default environment. The ambient `os.environ` is never read and never inherited: the
child receives `dict(environment)` and nothing else.

## The exchange

One spawn through the shared `start_exchange`, which owns the three pipes and the
hidden-window creation flag on Windows. This transport binds two further decisions onto that
one spawn: `shell=False`, and `env=dict(environment)` captured before the launch.

The payload is written to stdin and stdin is closed; then stdout and stderr are drained and the
child is reaped. **The write and the drain share one monotonic deadline** — a driver that is
slow to accept the request has already spent part of the budget the answer must fit in. stdout
is bounded at 64 KiB and stderr at 8 KiB. stderr is read so a chatty driver cannot block on a
full pipe and is then **discarded**: child diagnostics never reach a caller, a response or a
log. The payload is never placed on the argv, and neither the payload, the command nor the
environment is ever logged.

### What the deadline governs

`timeout_seconds` is an **operation budget: a bound on what may be accepted**, and it is
deliberately *not* a promise about when the call returns. The shared bounded primitive keeps its
own legacy floors — a drain waits at least 0.1 seconds for stderr and at least 0.1 seconds for
the reap, whatever remainder it was handed — and after a kill it settles handles under a further
`SETTLE_GRACE_SECONDS` bound. Thread scheduling adds its own slack. So the wall clock can and
does run past `timeout_seconds` before `run_knowledge_driver` returns, and this contract claims
no hard wall-clock return at exactly the timeout. Cleanup stays bounded, and it is reported
honestly as `settled` or `unsettled` either way.

What the budget does govern absolutely is the verdict:

* the remaining budget is checked **before each phase**. If it is spent, that phase does not
  begin — an exhausted budget after the write means the drain never starts at all — the one
  child is stopped, and the result is `driver_outcome_unknown` carrying the cleanup that was
  actually achieved;
* the budget is checked **once more after the drain**, before any success is returned. A result
  that is clean in every other respect — stdout within bounds, a zero exit, a `settled` cleanup —
  but that arrived at or after the deadline is **not a success**. Its bytes are discarded, the
  code is `driver_outcome_unknown`, and the real cleanup fact is preserved. By then the child has
  often already exited on its own; stopping it again only settles the handles and says so.

**Late completion can never be reported as success.** A zero exit and stdout on the pipe are not
enough on their own: the answer has to have arrived inside the budget. No branch resets the
deadline, extends it, retries a phase, or takes out a fresh permit.

Nothing is retried, on any branch. An unknown outcome is not evidence that nothing happened.

## The result

```python
DriverExchangeResult(stdout: bytes | None, error_code: str | None, cleanup: str)
```

`cleanup` is always `"settled"` or `"unsettled"`, on success and on failure alike.

Success requires a zero child exit, a `settled` cleanup **and** an answer that arrived inside the
deadline; only when all three hold is `stdout` populated and `error_code` `None`. Those bytes are still completely untrusted: parsing
them, validating them and deciding what they authorise belongs to the caller.

| `error_code` | Raised when | `stdout` |
| --- | --- | --- |
| `invalid_driver_input` | any admission rule above failed; nothing was spawned | `None` |
| `driver_not_started` | the shared exchange proved the payload never left this process: `NOT_STARTED` (the command could not be started) or `NO_STDIN` (the channel exposed no stdin), or the launcher refused the arguments with `ValueError` | `None` |
| `driver_outcome_unknown` | every branch where the driver may already have acted: timeout, oversize stdout, `NO_OUTPUT`, a nonzero exit, a cleanup that could not be confirmed, an exhausted budget before a phase, or an otherwise clean answer that arrived at or after the deadline | `None` |

An error carries the code and the cleanup fact and nothing else — no child stderr, no exception
text, no command, path, query, payload or environment value. A partial answer from a driver that
then failed is dropped rather than returned.

Programming errors and `KeyboardInterrupt` are **not** converted into a code. The child is
stopped first, and then the exception is allowed to propagate, so a bug in the calling code stays
visible instead of being reported as a driver failure.

## What this is not

* **Not an OS sandbox.** The driver runs as the same user with that user's rights. An explicit
  environment controls what the child is *told*, not what it is *allowed*. Credentials remain in
  the operating configuration of whoever pinned the driver.
* **Not a guarantee about descendants.** Cleanup describes this exchange's own child and its own
  handles. Killing the direct child does not prove that processes that child started are gone,
  and `settled` never claims it does.
* **Not an execution authority.** This module checks no ledger state, no Task revision, no
  expiry, no digest and no owner authority, and it consumes no guard. A caller that runs a driver
  without those checks is not made correct by this contract.
