# Windows knowledge-driver config (owner launch only)

Optional local operator `config.json` field `knowledge_drivers_config` is an
absolute Windows path string to a driver registry file. The Windows desktop
host and `Start-WorkStack.ps1` may append `--knowledge-drivers-config` plus
that unmodified path to the bundled `runtime\python.exe` → `run_work_stack.py
graph serve` argv. They do not read the registry, keys, command, or env.

## When the flag is added

- **Absent field:** existing owner argv and defaults are unchanged.
- **Present and valid:** the flag and the given path are appended as two argv
  tokens. Spaces, non-ASCII, and quotes are one argv; PowerShell quoting uses
  `ConvertTo-WindowsCommandLineArgument`. Environment variables are not
  expanded and no default path is searched.
- **Present and invalid** (null, empty, non-string, control characters,
  relative, or drive-relative): refused with a content-free error **before**
  backup, mkdir, or process start.

This is trusted operator state, not browser input. There is no webview setter
and no command/env on this path.

## Existing owner reuse

Settings apply only to a **newly started** owner. A healthy owner already
serving the configured workspace is reused as today: the host does not
force-stop or reconfigure that process. Changing `config.json` does not
hot-reload a running owner. Restart the owner to apply a new path.

Adapter executables are installed and named in the registry by the operator.
This document does not claim installed acceptance or that the generic CLI
flag is already present in this tree.
