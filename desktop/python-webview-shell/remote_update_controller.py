"""Drives the native remote update page against one real flow.

This module is the seam between the host window and the two frozen modules
that already own the update semantics.  ``RemoteUpdateFlow`` (D) owns the
operations and the receipts; ``RemoteUpdateViewSession`` and
``remote_update_projection`` (E) own the page, the capability and the single
admissible next action.  Nothing here re-implements either one: the flow is
injected whole, the projection is recomputed from that same flow object, and
every click is resolved by ``resolve_remote_update_action``.

Four properties are this module's own responsibility.

* Every flow call is blocking -- SSH, unpack, probe -- so it runs on a
  ``BoundedRequestWorker`` thread, never on the WebView callback that
  delivered the click.  The repaint is marshalled back through the host's UI
  dispatcher.
* A completion may not paint over a screen that is gone.  Each mount takes a
  generation, and a paint that arrives for an older generation, for a retired
  page, or for a selection the host no longer has selected is dropped and the
  page is retired instead.
* A click is resolved against the flow as it is *now*, not as it was when the
  page was painted, so a repeated or stale click can never issue a second
  mutation against an identity that already moved.
* Mounting a page is a new question.  The host reuses one composed flow for an
  unchanged binding, so the ephemeral answer the previous page held about the
  operator's agent Skill is withdrawn before the new page is built, and only a
  fresh read can offer a write or claim one succeeded.  A Skill click is bound
  to that page's epoch at submission; a queued call whose page has since been
  replaced is discarded before the port runs.  A call already armed is not
  cancelled; its later reply is detached from the new page.

There is no fallback port, no simulated flow and no confirmation dialog: a
missing host hook or an unselected session refuses to open the page.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from typing import Callable

import remote_update_diagnostics as DIAGNOSTICS
import remote_update_projection as PROJECT
import remote_update_view as VIEW
from bounded_request_worker import BoundedRequestWorker
from remote_update_flow_contract import RemoteUpdateRefused, StartupEvidence
from remote_update_presentation_actions import THIS_PC_ACTIONS
from remote_update_view import RemoteUpdateViewSession


#: Host kinds this controller answers by calling the injected flow.  ``verify``
#: is the last stage of the run order, so it advances like the others.  The two
#: skill kinds are their own calls: they are offered only once the run order is
#: finished, which is exactly the condition ``advance`` refuses, so they are
#: dispatched by name rather than by relaxing that refusal.  A flow that does
#: not have them -- an older bundle, or a build with no skill transport -- is
#: simply not dispatched, because ``_submit_flow_call`` requires the attribute.
FLOW_CALLS: dict[str, str] = {
    "advance": "advance",
    "verify": "advance",
    "retry": "retry",
    "reconcile": "reconcile",
    "rollback": "rollback",
    "restore": "restore",
    "cancel": "cancel",
    "skill_inspect": "inspect_skill",
    "skill_install": "install_skill",
}

#: The finite external callbacks behind the three independent PC actions.
PC_CALLBACKS: dict[str, str] = {
    "check_this_pc": "pc_check",
    "download_this_pc": "pc_download",
    "update_this_pc": "pc_install",
}

REQUIRED_HOOKS: tuple[str, ...] = (
    "selected_session",
    "render",
    "dispatch_ui",
    "retire_view",
    "restart_desktop",
    "restart_evidence",
    "copy_diagnostics",
    "pc_check",
    "pc_download",
    "pc_install",
    "pc_actions",
    "theme",
)

MAX_PENDING_JOBS = 8
WORKER_THREAD_NAME = "workstack-remote-update"


@dataclass(frozen=True)
class RemoteUpdateSelection:
    """The one remote session this page is allowed to speak for.

    ``flow`` is a real ``RemoteUpdateFlow`` built by the host with production
    ports.  A different selected profile is a different value here, and the
    controller treats that as a reason to retire the page rather than paint
    another session's outcome onto it.
    """

    session_id: str
    workspace_id: str
    flow: object


@dataclass(frozen=True)
class RemoteUpdateHostHooks:
    """The finite set of host effects this controller is allowed to cause."""

    selected_session: Callable[[], object]
    render: Callable[[str], None]
    dispatch_ui: Callable[[Callable[[], None]], bool]
    retire_view: Callable[[], None]
    restart_desktop: Callable[[], bool]
    restart_evidence: Callable[[], object]
    copy_diagnostics: Callable[[str], bool]
    pc_check: Callable[[], None]
    pc_download: Callable[[], None]
    pc_install: Callable[[], None]
    pc_actions: Callable[[], tuple[str, ...]]
    theme: Callable[[], str]


class RemoteUpdateController:
    """One native page, one injected flow, one admissible next action."""

    def __init__(
        self,
        hooks: RemoteUpdateHostHooks,
        *,
        view_factory: Callable[[], RemoteUpdateViewSession] = RemoteUpdateViewSession,
        worker_factory: Callable[..., object] | None = None,
        trace: Callable[[str], None] | None = None,
    ) -> None:
        missing = [name for name in REQUIRED_HOOKS if not callable(getattr(hooks, name, None))]
        if missing:
            raise ValueError(f"remote update host hooks are incomplete: {', '.join(missing)}")
        self._hooks = hooks
        self._view_factory = view_factory
        self._trace = trace if callable(trace) else _silent
        self._lock = threading.Lock()
        self._generation = 0
        self._page_epoch = 0
        self._sequence = 0
        self._bound: RemoteUpdateSelection | None = None
        self._projection: PROJECT.RemoteUpdateProjection | None = None
        self._view: RemoteUpdateViewSession | None = None
        self._jobs: dict[str, Callable[[], object]] = {}
        self._job_flows: dict[str, object] = {}
        build = worker_factory if worker_factory is not None else BoundedRequestWorker
        self._worker = build(
            self._run_job,
            self._deliver_job,
            maximum_pending=MAX_PENDING_JOBS,
            thread_name=WORKER_THREAD_NAME,
        )
        self._worker.start()

    # -- mount and retire -------------------------------------------------

    def open(self) -> bool:
        """Mount the page for the currently selected session, or refuse."""

        selection = self._admit_selection()
        if selection is None:
            self._trace("remote update page refused: no selected remote session")
            return False
        view = self._view_factory()
        with self._lock:
            self._reopen_skill(selection)
            self._generation += 1
            self._page_epoch = _skill_page_epoch(selection.flow)
            self._drop_queued_jobs_locked()
            generation = self._generation
            self._bound = selection
            self._projection = None
            self._view = view
        return self._show(selection, generation)

    def _reopen_skill(self, selection: RemoteUpdateSelection) -> None:
        """Drop the previous page's ephemeral agent-Skill answer, if any.

        Mounting is the only place this happens.  A repaint of the page on
        screen keeps what that page has already published -- including an
        outcome that has only just arrived.  A Skill call still in flight is
        left running, but the new page does not inherit its later write offer
        or success sentence.

        A flow without the call is an older bundle or a build with no Skill
        transport; there is no ephemeral state to withdraw and nothing to do.
        """

        reopen = getattr(selection.flow, "reopen_skill_view", None)
        if not callable(reopen):
            return
        try:
            reopen()
        except Exception as error:
            self._trace(f"remote update skill reopen failed: {type(error).__name__}")

    def retire(self, *, closed: bool = False) -> None:
        """Take the page down.  Any outcome still in flight paints nothing."""

        with self._lock:
            self._generation += 1
            self._page_epoch = 0
            self._drop_queued_jobs_locked()
            view = self._view
            self._bound = None
            self._projection = None
            self._view = None
        if view is not None:
            view.retire(closed=closed)
        self._safely(self._hooks.retire_view, "retire")

    def close(self) -> None:
        """Retire the page and stop the worker.  Called at host shutdown."""

        self.retire(closed=True)
        stop = getattr(self._worker, "stop", None)
        if callable(stop):
            stop(1.0)

    @property
    def is_open(self) -> bool:
        with self._lock:
            return self._view is not None and self._bound is not None

    @property
    def projection(self) -> PROJECT.RemoteUpdateProjection | None:
        """The projection the page on screen was painted from."""

        with self._lock:
            return self._projection

    # -- host callbacks ---------------------------------------------------

    def admits_navigation(self, target: str) -> bool:
        """Only the document this controller just rendered may be entered."""

        with self._lock:
            view = self._view
        return view is not None and view.admits_navigation(target)

    def handle_web_message(self, message: str) -> bool:
        """Admit and resolve one click.  ``False`` means "not this page"."""

        if VIEW.parse_remote_update_request(message) is None:
            return False
        with self._lock:
            view = self._view
            projection = self._projection
            bound = self._bound
            generation = self._generation
            origin_epoch = self._page_epoch
        if view is None or projection is None or bound is None:
            return True
        admission = view.admit(message)
        if admission.outcome == "unbound":
            return True
        if not _same_selection(self._hooks.selected_session(), bound):
            self._trace("remote update click ignored: the selected session changed")
            self.retire()
            return True
        resolved = PROJECT.resolve_remote_update_action(
            admission,
            projection,
            current=bound.flow,
            session_id=bound.session_id,
            workspace_id=bound.workspace_id,
        )
        self._apply(
            resolved, bound, generation, origin_epoch, admitted=admission.outcome == "action"
        )
        return True

    def resume_after_restart(self) -> bool:
        """Continue the retained activation after the restart it required.

        The evidence comes from the host; absent evidence is unknown and the
        receipt stays pending.  This never issues a fresh activation.
        """

        selection = self._admit_selection()
        if selection is None:
            return False
        evidence = self._hooks.restart_evidence()
        if not isinstance(evidence, StartupEvidence):
            self._trace("remote update restart evidence is unavailable; the receipt stays pending")
            return False
        resume = getattr(selection.flow, "resume_after_restart", None)
        if not callable(resume):
            return False
        with self._lock:
            generation = self._generation
        return self._submit(lambda: resume(evidence), generation)

    # -- resolution -------------------------------------------------------

    def _apply(
        self,
        resolved: PROJECT.ResolvedRemoteUpdateOperation,
        bound: RemoteUpdateSelection,
        generation: int,
        origin_epoch: int,
        *,
        admitted: bool,
    ) -> None:
        if resolved.outcome == "retire":
            self.retire(closed=True)
            return
        if resolved.outcome != "dispatch":
            self._trace(f"remote update click not dispatched: {resolved.reason}")
            if admitted:
                # The view armed itself for this click; repaint so the page
                # shows the condition that actually holds now.
                self._request_paint(generation)
            return
        kind = resolved.host_kind
        if kind == "pc_independent":
            self._run_pc_action(resolved.e_action)
            self._request_paint(generation)
            return
        if kind == "review":
            self._copy_diagnostics()
            self._request_paint(generation)
            return
        if kind == "restart":
            # The host owns the restart.  The receipt stays pending until
            # ``resume_after_restart`` confirms it against real evidence.
            self._safely(self._hooks.restart_desktop, "restart")
            return
        if not self._submit_flow_call(kind, bound, generation, origin_epoch):
            self._trace(f"remote update host kind could not be dispatched: {kind}")
            self._request_paint(generation)

    def _submit_flow_call(
        self,
        kind: str | None,
        bound: RemoteUpdateSelection,
        generation: int,
        origin_epoch: int,
    ) -> bool:
        name = FLOW_CALLS.get(kind or "")
        if name is None:
            return False
        call = getattr(bound.flow, name, None)
        if not callable(call):
            return False
        return self._submit(_bound_skill_call(call, name, origin_epoch), generation)

    def _run_pc_action(self, action: str | None) -> bool:
        """Route an independent PC action to its own external callback."""

        name = PC_CALLBACKS.get(action or "")
        if name is None:
            return False
        return self._safely(getattr(self._hooks, name), f"pc action {action}")

    def _copy_diagnostics(self) -> bool:
        with self._lock:
            projection = self._projection
        if projection is None:
            return False
        try:
            report = DIAGNOSTICS.format_report(projection.diagnostics)
        except DIAGNOSTICS.DiagnosticsError:
            self._trace("remote update diagnostics could not be formatted")
            return False
        try:
            return bool(self._hooks.copy_diagnostics(report))
        except Exception as error:
            self._trace(f"remote update diagnostics copy failed: {type(error).__name__}")
            return False

    # -- worker -----------------------------------------------------------

    def _drop_queued_jobs_locked(self) -> None:
        """Forget clicks that were accepted but whose worker has not begun.

        Tokens already popped by ``_run_job`` stay with ``_job_flows`` so a
        call that has started is not cancelled.  The worker may still deliver
        the leftover token; without a job it is a no-op.
        """

        for token in list(self._jobs):
            self._jobs.pop(token, None)
            self._job_flows.pop(token, None)

    def _submit(self, job: Callable[[], object], generation: int) -> bool:
        with self._lock:
            if generation != self._generation:
                return False
            self._sequence += 1
            token = f"job-{self._sequence}-{generation}"
            self._jobs[token] = job
            self._job_flows[token] = None if self._bound is None else self._bound.flow
        if self._worker.submit(token):
            return True
        with self._lock:
            self._jobs.pop(token, None)
            self._job_flows.pop(token, None)
        self._trace("remote update worker refused the request")
        return False

    def _run_job(self, token: str) -> str:
        """Run one blocking flow call off the UI thread.  Never raises."""

        with self._lock:
            job = self._jobs.pop(token, None)
        if job is None:
            return token
        try:
            job()
        except RemoteUpdateRefused as refusal:
            self._trace(f"remote update refused: {refusal.code}")
        except Exception as error:
            self._trace(f"remote update call failed: {type(error).__name__}")
        return token

    def _deliver_job(self, token: str) -> None:
        """Refresh the page that still speaks for this job's flow, if any.

        A reopen of the same flow must see the settled state -- never an
        Install the closed page earned -- and a different selection must not
        be painted from this completion.
        """

        generation = _token_generation(token)
        with self._lock:
            current = self._generation
            bound = self._bound
            job_flow = self._job_flows.pop(token, None)
        if bound is not None and job_flow is bound.flow:
            self._request_paint(current)
            return
        if generation is not None:
            self._request_paint(generation)

    # -- painting ---------------------------------------------------------

    def _request_paint(self, generation: int) -> None:
        def paint() -> None:
            self._paint(generation)

        try:
            delivered = bool(self._hooks.dispatch_ui(paint))
        except Exception as error:
            self._trace(f"remote update paint dispatch failed: {type(error).__name__}")
            return
        if not delivered:
            self._trace("remote update paint could not reach the UI thread")

    def _paint(self, generation: int) -> None:
        """Repaint, but only if this outcome still belongs on the screen."""

        with self._lock:
            bound = self._bound
            view = self._view
            current = generation == self._generation
        if not current or bound is None or view is None or view.closed:
            return
        if not _same_selection(self._hooks.selected_session(), bound):
            self._trace("remote update page retired: the selected session changed")
            self.retire()
            return
        self._show(bound, generation)

    def _show(self, selection: RemoteUpdateSelection, generation: int) -> bool:
        projection = self._project(selection)
        if projection is None:
            return False
        with self._lock:
            if generation != self._generation:
                return False
            view = self._view
            self._projection = projection
        if view is None:
            return False
        return view.show(projection.snapshot, self._hooks.render, theme=self._theme())

    def _project(
        self, selection: RemoteUpdateSelection
    ) -> PROJECT.RemoteUpdateProjection | None:
        try:
            return PROJECT.project_remote_update(
                selection.flow,
                session_id=selection.session_id,
                workspace_id=selection.workspace_id,
                pc_actions=self._pc_actions(),
            )
        except Exception as error:
            self._trace(f"remote update projection failed: {type(error).__name__}")
            return None

    # -- host reads -------------------------------------------------------

    def _admit_selection(self) -> RemoteUpdateSelection | None:
        try:
            selection = self._hooks.selected_session()
        except Exception as error:
            self._trace(f"remote update session lookup failed: {type(error).__name__}")
            return None
        if not isinstance(selection, RemoteUpdateSelection):
            return None
        if _canonical_uuid(selection.session_id) is None:
            return None
        if _canonical_uuid(selection.workspace_id) is None:
            return None
        if not callable(getattr(selection.flow, "snapshot", None)):
            return None
        return selection

    def _pc_actions(self) -> tuple[str, ...]:
        try:
            offered = self._hooks.pc_actions()
        except Exception as error:
            self._trace(f"remote update PC actions failed: {type(error).__name__}")
            return ()
        if not isinstance(offered, (tuple, list)):
            return ()
        admitted: list[str] = []
        for action in offered:
            if action in THIS_PC_ACTIONS and action not in admitted:
                admitted.append(action)
        return tuple(admitted)

    def _theme(self) -> str:
        try:
            theme = self._hooks.theme()
        except Exception:
            return "dark"
        return theme if theme in ("dark", "light") else "dark"

    def _safely(self, call: Callable[[], object], what: str) -> bool:
        try:
            call()
        except Exception as error:
            self._trace(f"remote update {what} failed: {type(error).__name__}")
            return False
        return True


def _same_selection(candidate: object, bound: RemoteUpdateSelection) -> bool:
    """The same session, workspace and the same flow object -- not a copy."""

    return (
        isinstance(candidate, RemoteUpdateSelection)
        and candidate.session_id == bound.session_id
        and candidate.workspace_id == bound.workspace_id
        and candidate.flow is bound.flow
    )


def _bound_skill_call(
    call: Callable[..., object], name: str, origin_epoch: int
) -> Callable[[], object]:
    """Bind a Skill click to the page epoch captured with its generation.

    The originating epoch is closed over here and compared later under
    ``SkillOffer``'s arming guard.  This function must not read the live
    offer: a paused callback can reach submit after a newer page exists.
    """

    if name not in ("inspect_skill", "install_skill"):
        return call

    def bound(job: Callable[..., object] = call, epoch: int = origin_epoch) -> object:
        return job(origin_epoch=epoch)

    return bound


def _skill_page_epoch(flow: object) -> int:
    offer = getattr(flow, "_skill", None)
    epoch = getattr(offer, "epoch", None)
    return epoch if isinstance(epoch, int) else 0


def _token_generation(token: object) -> int | None:
    if not isinstance(token, str):
        return None
    try:
        return int(token.rsplit("-", 1)[-1])
    except ValueError:
        return None


def _canonical_uuid(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return None
    if parsed.int == 0 or str(parsed) != value:
        return None
    return value


def _silent(_note: str) -> None:
    return None


__all__ = [
    "FLOW_CALLS",
    "MAX_PENDING_JOBS",
    "PC_CALLBACKS",
    "REQUIRED_HOOKS",
    "WORKER_THREAD_NAME",
    "RemoteUpdateController",
    "RemoteUpdateHostHooks",
    "RemoteUpdateSelection",
]
