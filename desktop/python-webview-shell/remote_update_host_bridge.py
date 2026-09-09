"""The narrow host seam for the native remote update page.

Mixed into ``WorkStackDesktopHost``.  This module owns only the WinForms and
WebView2 mechanics the controller cannot own: marshalling to the UI thread,
rendering the page into a dedicated WebView, admitting its navigation,
retiring it, and putting a bounded diagnostic report on the clipboard.  All
of the update semantics stay in ``remote_update_controller`` and, behind it,
in the frozen flow and view modules.

The host supplies four things this mixin deliberately does not invent:

``remote_update_webview``
    The WebView2 control the page is rendered into.  ``None`` means the page
    cannot be mounted, and the mixin refuses instead of rendering elsewhere.
``_remote_update_selection()``
    The selected remote session as a ``RemoteUpdateSelection``, carrying the
    real ``RemoteUpdateFlow`` built with production ports.  ``None`` when no
    remote profile is selected -- there is no simulated flow to fall back to.
``_remote_update_startup_evidence()``
    A ``StartupEvidence`` describing what this start observed after an
    activation restart, or ``None`` when nothing was observed.  Absent
    evidence is unknown and leaves the retained receipt pending.
``_remote_update_restart()``
    Begin the restart that finishes a pending activation.  Returns whether
    the restart was actually begun.

The three independent PC actions route to the host's existing update methods
(``_start_update_check`` and ``_install_downloaded_update``); this mixin adds
no second updater and downloads nothing itself.
"""

from __future__ import annotations

from remote_update_controller import (
    RemoteUpdateController,
    RemoteUpdateHostHooks,
    RemoteUpdateSelection,
)


#: Which independent PC action the page offers for each host update state.
#: States that are already working (``checking``/``downloading``/
#: ``installing``) and the healthy ``current`` state offer nothing, so a PC
#: button never stands in front of the remote flow's own next action.
PC_ACTION_FOR_UPDATE_STATE: dict[str, str] = {
    "available": "download_this_pc",
    "ready": "update_this_pc",
    "idle": "check_this_pc",
    "error": "check_this_pc",
}

MAX_DIAGNOSTIC_CLIPBOARD_CHARACTERS = 4096


class RemoteUpdateHostBridgeMixin:
    """Open, render, admit, retire and resume the native update page."""

    def _init_remote_update_bridge(self) -> None:
        """Build the controller once, over this host's own hooks."""

        self.remote_update_webview = getattr(self, "remote_update_webview", None)
        self.remote_update_controller = RemoteUpdateController(
            self._remote_update_hooks(),
            trace=self._trace,
        )

    def _remote_update_hooks(self) -> RemoteUpdateHostHooks:
        return RemoteUpdateHostHooks(
            selected_session=self._remote_update_selection,
            render=self._render_remote_update_page,
            dispatch_ui=self._dispatch_remote_update_ui,
            retire_view=self._retire_remote_update_view,
            restart_desktop=self._remote_update_restart,
            restart_evidence=self._remote_update_startup_evidence,
            copy_diagnostics=self._copy_remote_update_diagnostics,
            pc_check=self._remote_update_pc_check,
            pc_download=self._remote_update_pc_download,
            pc_install=self._remote_update_pc_install,
            pc_actions=self._remote_update_pc_actions,
            theme=self._remote_update_theme,
        )

    # -- lifetime ---------------------------------------------------------

    def _open_remote_update_page(self) -> bool:
        """Mount the page for the selected session, or refuse to mount it."""

        controller = getattr(self, "remote_update_controller", None)
        if controller is None or self._remote_update_core() is None:
            self._trace("remote update page refused: no native view is available")
            return False
        return controller.open()

    def _retire_remote_update_page(self) -> None:
        controller = getattr(self, "remote_update_controller", None)
        if controller is not None:
            controller.retire()

    def _close_remote_update_bridge(self) -> None:
        controller = getattr(self, "remote_update_controller", None)
        if controller is not None:
            controller.close()

    def _resume_remote_update_after_restart(self) -> bool:
        """Continue a pending activation receipt against this start's evidence."""

        controller = getattr(self, "remote_update_controller", None)
        return bool(controller is not None and controller.resume_after_restart())

    # -- WebView seam -----------------------------------------------------

    def _render_remote_update_page(self, page: str) -> None:
        """Paint the page.  Raising here retires the view session."""

        core = self._remote_update_core()
        if core is None:
            raise RuntimeError("The remote update view is not available")
        core.NavigateToString(page)

    def _handle_remote_update_message(self, message: str) -> bool:
        controller = getattr(self, "remote_update_controller", None)
        if controller is None:
            return False
        return controller.handle_web_message(message)

    def _remote_update_admits_navigation(self, target: str) -> bool:
        controller = getattr(self, "remote_update_controller", None)
        return bool(controller is not None and controller.admits_navigation(target))

    def _retire_remote_update_view(self) -> None:
        """Blank the native view so no retired page keeps its capability."""

        core = self._remote_update_core()
        if core is None:
            return
        try:
            core.Navigate("about:blank")
        except Exception as error:
            self._trace(f"remote update view could not be blanked: {type(error).__name__}")

    def _remote_update_core(self) -> object | None:
        view = getattr(self, "remote_update_webview", None)
        return getattr(view, "CoreWebView2", None) if view is not None else None

    def _dispatch_remote_update_ui(self, action: object) -> bool:
        """Marshal one paint onto the UI thread the WebView belongs to."""

        form = getattr(self, "form", None)
        if form is None or bool(getattr(form, "IsDisposed", False)):
            return False
        from System import Action

        def run_on_ui() -> None:
            current = getattr(self, "form", None)
            if current is not form or bool(getattr(current, "IsDisposed", False)):
                return
            action()

        try:
            form.BeginInvoke(Action(run_on_ui))
        except Exception as error:
            self._trace(f"remote update paint dispatch failed: {type(error).__name__}")
            return False
        return True

    def _copy_remote_update_diagnostics(self, report: str) -> bool:
        """Put the bounded, allowlisted diagnostic report on the clipboard."""

        form = getattr(self, "form", None)
        if not isinstance(report, str) or not report or form is None:
            return False
        if len(report) > MAX_DIAGNOSTIC_CLIPBOARD_CHARACTERS:
            return False
        from System import Action
        from System.Windows.Forms import Clipboard

        copied: list[bool] = [False]

        def copy_on_ui() -> None:
            Clipboard.SetText(report)
            copied[0] = True

        try:
            form.Invoke(Action(copy_on_ui))
        except Exception as error:
            self._trace(f"remote update diagnostics copy failed: {type(error).__name__}")
            return False
        return copied[0]

    # -- independent PC update -------------------------------------------

    def _remote_update_pc_actions(self) -> tuple[str, ...]:
        state = getattr(self, "update_status", {})
        offered = PC_ACTION_FOR_UPDATE_STATE.get(str(state.get("state", "")))
        return () if offered is None else (offered,)

    def _remote_update_pc_check(self) -> None:
        self._start_update_check()

    def _remote_update_pc_download(self) -> None:
        self._start_update_check(force_download=True)

    def _remote_update_pc_install(self) -> None:
        self._install_downloaded_update()

    def _remote_update_theme(self) -> str:
        return "light" if str(getattr(self, "current_theme", "dark")) == "light" else "dark"

    # -- host-owned seams -------------------------------------------------

    def _remote_update_selection(self) -> RemoteUpdateSelection | None:
        """The selected remote session, or ``None``.  The host overrides this.

        There is no default flow: a host that has not composed production
        ports has no remote session, and the page refuses to mount.
        """

        return None

    def _remote_update_startup_evidence(self) -> object | None:
        """What this start observed after an activation restart, if anything."""

        return None

    def _remote_update_restart(self) -> bool:
        """Begin the restart a pending activation requires.  Host-owned."""

        self._trace("remote update restart is not wired in this host")
        return False


__all__ = [
    "MAX_DIAGNOSTIC_CLIPBOARD_CHARACTERS",
    "PC_ACTION_FOR_UPDATE_STATE",
    "RemoteUpdateHostBridgeMixin",
]
