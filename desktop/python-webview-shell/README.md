# Work Stack signed-Python desktop host

This is the Windows desktop entry point for Work Stack. The signed bundled
`pythonw.exe` owns the process, pywebview supplies its pinned WebView2/pythonnet
runtime, and the existing React interface remains the permanent application
surface.

Context Inbox reports only a provider key and a viewport rectangle. The host
places a second native WebView2 in that rectangle for Outlook, Teams, or
OneNote. It does not read Microsoft page content, cookies, tokens, or planning
state. Leaving Context Inbox hides the Microsoft surface.

For a source-tree probe after installing `requirements-windows-desktop.txt`:

```powershell
python .\desktop\python-webview-shell\workstack_desktop.py `
  --install-root . `
  --state-root .\.runtime\python-desktop-state `
  --url http://127.0.0.1:8765/ `
  --probe-provider outlook `
  --probe-result .\.runtime\python-desktop-probe.txt
```

The shipping shortcut uses `runtime\pythonw.exe`, so no console window or new
unsigned application executable is introduced.

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
