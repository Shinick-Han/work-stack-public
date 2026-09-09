"""The dedicated native view the remote update page paints into.

This is the WinForms/WebView2 half of the host surface, and only that half: it
creates one panel and one ``WebView2`` for the update page, waits for the
control's own asynchronous initialization, presents the page only once there is
something to present, and takes the whole viewport back down when the page is
retired.

Two rules shape it.

**Nothing is shown before there is something to show.**  ``WebView2`` reaches a
usable ``CoreWebView2`` asynchronously.  ``EnsureCoreWebView2Async`` starting is
not that control being ready, so the viewport is created hidden, the host
subscribes to ``CoreWebView2InitializationCompleted`` exactly as it already does
for its other WebViews, and the page is rendered and the panel raised only after
that event reports success.  A click that arrives first is remembered and
answered when initialization completes; a failed initialization takes the panel
away again rather than leaving an opaque rectangle over the application.

**A late answer belongs to the view that asked.**  Initialization completes
asynchronously, so a retired view can answer after a new one has been mounted.
Every subscription carries the exact control it was created for and the mount
generation it belongs to, and an answer that is not the current view's is
traced and dropped -- it can neither mark the new view ready nor dispose it.
The handlers a view was given are released with it, so a disposed control keeps
nothing alive.

**Retirement disposes.**  Blanking the core is not retirement: the panel is
docked to fill the form and was brought to the front, so a cancel, a close or a
profile change that only navigated it to ``about:blank`` would leave an empty
surface covering the app forever.  Retiring hides it, removes it from the form
and disposes both controls, and the next open mounts a fresh one.

Navigation authority stays exactly where it was -- the controller admits every
navigation for this dedicated view, and nothing here widens it.
"""

from __future__ import annotations


class RemoteUpdateHostViewMixin:
    """Mount, present, hide and dispose the update page's own WebView."""

    def _init_remote_update_view(self) -> None:
        self.remote_update_webview = None
        self.remote_update_viewport = None
        self.remote_update_view_ready = False
        self.remote_update_open_pending = False
        self.remote_update_view_generation = 0
        self.remote_update_view_handlers: list[object] = []

    # -- mounting ---------------------------------------------------------

    def _mount_remote_update_view(self) -> bool:
        """Create the dedicated WebView the page paints into, once."""

        if self.remote_update_webview is not None:
            return True
        form = getattr(self, "form", None)
        if form is None or bool(getattr(form, "IsDisposed", False)):
            return False
        try:
            self._create_remote_update_view(form)
        except Exception as error:  # noqa: BLE001 - the WinForms family is open
            self._trace(f"remote update view could not be created: {type(error).__name__}")
            self.remote_update_webview = None
            self.remote_update_viewport = None
            self.remote_update_view_ready = False
            return False
        return self.remote_update_webview is not None

    def _create_remote_update_view(self, form) -> None:
        """The WinForms half.  Only reached on a real host with a real form."""

        import System.Windows.Forms as WinForms
        from Microsoft.Web.WebView2.WinForms import WebView2
        from System.Drawing import Color

        from native_theme import theme_rgb

        viewport = WinForms.Panel()
        viewport.Dock = WinForms.DockStyle.Fill
        viewport.Visible = False
        viewport.BackColor = Color.FromArgb(
            *theme_rgb(self.current_theme, "native.overlay")
        )
        view = WebView2()
        view.Dock = WinForms.DockStyle.Fill
        navigation_starting = lambda _sender, args: setattr(  # noqa: E731
            args, "Cancel", not self._remote_update_admits_navigation(str(args.Uri))
        )
        message_received = lambda _sender, args: self._handle_remote_update_message(  # noqa: E731
            str(args.TryGetWebMessageAsString())
        )
        generation = self.remote_update_view_generation + 1
        view_ready = lambda _sender, args, owned=view, mount=generation: (  # noqa: E731
            self._on_remote_update_view_ready(owned, mount, bool(args.IsSuccess))
        )
        handlers = (navigation_starting, message_received, view_ready)
        self.source_event_handlers.extend(handlers)
        self.remote_update_view_handlers = list(handlers)
        view.NavigationStarting += navigation_starting
        view.WebMessageReceived += message_received
        view.CoreWebView2InitializationCompleted += view_ready
        viewport.Controls.Add(view)
        form.Controls.Add(viewport)
        self.remote_update_viewport = viewport
        self.remote_update_webview = view
        self.remote_update_view_ready = False
        self.remote_update_view_generation = generation
        view.EnsureCoreWebView2Async(None)

    # -- initialization ---------------------------------------------------

    def _on_remote_update_view_ready(
        self, view: object, generation: int, success: bool
    ) -> None:
        """Settle *this* control's initialization, and only then present.

        A view that has already been retired can still answer -- the callback
        was queued before the disposal reached it.  Such an answer belongs to a
        control this host no longer owns, so it neither marks the current view
        ready nor takes it down; it is traced and dropped.
        """

        current = view is self.remote_update_webview
        if not current or generation != self.remote_update_view_generation:
            self._trace("remote update view initialization ignored: a retired view")
            return
        if not success:
            self._trace("remote update view failed to initialize")
            self.remote_update_open_pending = False
            self._dispose_remote_update_view()
            return
        self.remote_update_view_ready = True
        if not self.remote_update_open_pending:
            return
        self.remote_update_open_pending = False
        self._present_remote_update_page()

    def _present_remote_update_page(self) -> bool:
        """Render the page, then raise the panel.  Never the other way round."""

        if not self._open_remote_update_page():
            self._hide_remote_update_viewport()
            return False
        self._show_remote_update_viewport()
        return True

    # -- viewport lifetime -------------------------------------------------

    def _show_remote_update_viewport(self) -> None:
        viewport = self.remote_update_viewport
        if viewport is None:
            return
        try:
            viewport.Visible = True
            viewport.BringToFront()
        except Exception as error:  # noqa: BLE001 - the WinForms family is open
            self._trace(f"remote update view could not be shown: {type(error).__name__}")

    def _hide_remote_update_viewport(self) -> None:
        viewport = self.remote_update_viewport
        if viewport is None:
            return
        try:
            viewport.Visible = False
        except Exception as error:  # noqa: BLE001 - the WinForms family is open
            self._trace(f"remote update view could not be hidden: {type(error).__name__}")

    def _retire_remote_update_view(self) -> None:
        """Blank the page's authority, then take the viewport away for real."""

        self.remote_update_open_pending = False
        core = self._remote_update_core()
        if core is not None:
            try:
                core.Navigate("about:blank")
            except Exception as error:  # noqa: BLE001 - the WinForms family is open
                self._trace(
                    f"remote update view could not be blanked: {type(error).__name__}"
                )
        self._dispose_remote_update_view()

    def _dispose_remote_update_view(self) -> None:
        """Hide, unparent and dispose the owned controls; keep no handle."""

        viewport = self.remote_update_viewport
        view = self.remote_update_webview
        self.remote_update_viewport = None
        self.remote_update_webview = None
        self.remote_update_view_ready = False
        self._release_view_handlers()
        self._hide_owned_control(viewport)
        for control in (view, viewport):
            self._dispose_owned_control(control)

    def _release_view_handlers(self) -> None:
        """Stop retaining the delegates the disposed view was subscribed with.

        The host keeps its event handlers alive deliberately, because the CLR
        would otherwise collect them while the control still holds them.  A
        control that has been disposed no longer holds anything, so keeping its
        delegates would only pin the retired view and this host together.
        """

        owned = self.remote_update_view_handlers
        self.remote_update_view_handlers = []
        if not owned:
            return
        self.source_event_handlers[:] = [
            handler for handler in self.source_event_handlers if handler not in owned
        ]

    def _hide_owned_control(self, control) -> None:
        if control is None:
            return
        try:
            control.Visible = False
            parent = getattr(control, "Parent", None)
            if parent is not None:
                parent.Controls.Remove(control)
        except Exception as error:  # noqa: BLE001 - the WinForms family is open
            self._trace(f"remote update view could not be removed: {type(error).__name__}")

    def _dispose_owned_control(self, control) -> None:
        if control is None:
            return
        dispose = getattr(control, "Dispose", None)
        if dispose is None:
            return
        try:
            dispose()
        except Exception as error:  # noqa: BLE001 - the WinForms family is open
            self._trace(f"remote update view could not be disposed: {type(error).__name__}")


__all__ = ["RemoteUpdateHostViewMixin"]
