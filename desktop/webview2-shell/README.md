# Work Stack WebView2 feasibility shell

This is a bounded Windows-only feasibility shell, not the shipping desktop product.

It reuses the installed Work Stack Python server and React UI, opens them in the installed Evergreen WebView2 Runtime, and keeps the Work Stack interface as the permanent application surface. A second WebView2 is visible only inside the rectangle owned by Source Inbox, where the existing Outlook, Teams, and OneNote tabs control it. There is no shell-level provider toolbar.

The bridge accepts only a provider key and layout coordinates. It does not inspect Microsoft page content, scrape cookies or tokens, inject OAuth credentials, call Conduit, or alter planning serialization.

## Build

```powershell
powershell -ExecutionPolicy Bypass -File .\desktop\webview2-shell\Build-WorkStackShell.ps1
```

The builder uses the Windows .NET Framework 4.8 compiler already present on the machine and a pinned Microsoft WebView2 SDK package. It does not require Visual Studio, the .NET SDK, Rust, Electron, or Node.js.

## Run

```powershell
.\.artifacts\webview2-shell\WorkStackShell.exe
```

The shell checks `/api/v1/health`. If Work Stack is stopped, it delegates to the installed backup-aware launcher with `-NoBrowser`. It stops the server on exit only when this shell started that server.

## Feasibility gates

1. Work Stack renders and remains usable in WebView2.
2. The shell starts a stopped server and does not stop a server it did not start.
3. Source Inbox controls an embedded Outlook, Teams, or OneNote surface without navigating Work Stack away.
4. The company tenant permits interactive sign-in in the isolated WebView2 profile.
5. No token, cookie, hidden DOM, recipient, or page-content access is added during the spike.

Microsoft recommends system-browser or brokered OAuth rather than scraping embedded sign-in flows. This spike only observes whether the Microsoft web applications themselves can operate in a persistent top-level WebView2 profile.
