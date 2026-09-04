# Work Stack desktop host

This is the Windows desktop entry point for Work Stack. The installed shortcuts
target `WorkStack.exe` at the installation root: a branded x64 host, compiled
from `WorkStackHost.cs` during packaging, that owns the process and runs this
`workstack_desktop.py` entry in-process through one `Py_Main` call against the
bundled `runtime\python312.dll`. pywebview supplies its pinned WebView2/pythonnet
runtime, and the existing React interface remains the permanent application
surface.

Context Inbox reports only a provider key and a viewport rectangle. The host
places a second native WebView2 in that rectangle for Outlook, Teams, or
OneNote. It does not read Microsoft page content, cookies, tokens, or planning
state. Leaving Context Inbox hides the Microsoft surface.

The three Microsoft surfaces share a dedicated persistent WebView2 environment
under `%LOCALAPPDATA%\WorkStack\desktop-microsoft-profile`. Windows primary-account
SSO is enabled for that environment. Authentication windows retain WebView2
parent/child popup semantics instead of replacing the provider page, including
enterprise identity-provider redirects. Host-only diagnostics are written to
`%LOCALAPPDATA%\WorkStack\logs\microsoft-webview.log`; the log never records URL
paths, queries, fragments, page titles, selected text, tokens, or cookies.

For a source-tree probe after installing `requirements-windows-desktop.txt`:

```powershell
python .\desktop\python-webview-shell\workstack_desktop.py `
  --install-root . `
  --state-root .\.runtime\python-desktop-state `
  --url http://127.0.0.1:8765/ `
  --probe-provider outlook `
  --probe-result .\.runtime\python-desktop-probe.txt
```

The shipping shortcut targets `WorkStack.exe`, a windowless (`/target:winexe`)
host, so no console window appears. The host is a new compiled executable and
the build applies no code signature to it: it is unsigned, like the setup
artifact. It is not a launcher. It never spawns `pythonw.exe`, never falls back
to another interpreter, and forwards only the GUI options listed in
`WorkStackHost.cs`. The bundled `runtime\pythonw.exe` stays in the payload only
for links written by earlier installations; `Stop-WorkStack.ps1` and
`Uninstall-WorkStack.ps1` still recognise that legacy invocation, but a newly
written link never targets it.

## SSH-owned remote SSOT mode

Local mode remains the default when `%LOCALAPPDATA%\WorkStack\remote-connection.json`
does not exist. To make the desktop process own a temporary SSH tunnel and a
loopback-only Work Stack server on Linux, use this exact profile shape:

```json
{
  "storage_mode": "ssh-remote",
  "ssh_host_alias": "work-linux",
  "remote_app_dir": "/srv/workstack/app",
  "remote_data_dir": "/srv/workstack/ssot",
  "local_forward_port": 18765,
  "workspace_id": "11111111-1111-4111-8111-111111111111",
  "remote_port": 8765
}
```

`remote_port` is optional and defaults to `8765`; every other field is
required. The host alias must already work through the user's OpenSSH config
and agent. Work Stack never stores a password or private-key path. Host-key
checking remains strict, both ends of the forward bind to loopback, and the
desktop process stops its SSH child when the window closes. Unknown fields,
relative or root Linux paths, unsafe aliases, and invalid ports fail closed.
`local_forward_port` is a preferred port, not a promise. If another process is
already listening there, the current run selects an OS-assigned loopback port
without probing, replacing, or terminating the occupant; the saved preference
is left unchanged. The SSH forward still uses `ExitOnForwardFailure=yes` so a
bind race fails closed.

While the desktop application is open, an in-process monitor checks both the
owned SSH process and the loopback health endpoint. After two consecutive
health failures (or an exited SSH child), it makes at most three reconnect
attempts with bounded backoff. The Work Stack view reloads only after the
replacement tunnel and workspace identity are verified. A terminal failure is
shown as `Disconnected`; closing the application cancels waits and prevents a
late reconnect from resurrecting the tunnel. This monitor is part of the
desktop process, not a background daemon.

The sidebar's **SSOT connection** entry opens the Connection Center. It can
validate a Local or Remote SSH draft without changing the active connection,
then save the profile atomically. Applying a saved profile still requires an
application restart so the active planning-state authority changes at a clear
boundary.
The remote server must expose the exact configured workspace UUID before the
desktop UI opens; the remote command also refuses a directory without an
existing Work Stack identity store.

Before opening the application, run the read-only prerequisite check:

```powershell
python .\desktop\python-webview-shell\workstack_desktop.py `
  --state-root "$env:LOCALAPPDATA\WorkStack" `
  --check-remote-connection
```

The check verifies the known host/authentication path, both remote directories,
`python3`, and that the remote Work Stack entry point can load. It does not
start a server or mutate the SSOT.
