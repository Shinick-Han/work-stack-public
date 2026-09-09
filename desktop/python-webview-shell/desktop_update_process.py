"""Hand off a verified update to a ready, independent Windows applicator."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import time
import uuid


def launch_update_process(install_root: Path, state_root: Path, downloaded, *, timeout: float = 10):
    install = install_root.resolve()
    state = state_root.resolve()
    if state == install or state.is_relative_to(install):
        raise RuntimeError("Update state directory must be outside the installation")
    script = install / "scripts/windows/Apply-WorkStackUpdate.ps1"
    if not script.is_file():
        raise RuntimeError("Installed update applicator is missing")
    launch_id = uuid.uuid4().hex
    ready = state / "updates" / f".launch-{launch_id}.json"
    logs = state / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    environment = {key: value for key, value in os.environ.items() if key.casefold() != "psmodulepath"}
    # Only inbox PowerShell modules are used. Do not inherit PowerShell 7's
    # module paths into the Windows PowerShell 5.1 applicator.
    command = [
        "powershell.exe", "-NoProfile", "-WindowStyle", "Hidden",
        "-ExecutionPolicy", "Bypass", "-File", str(script),
        "-SetupPath", str(downloaded.setup_path), "-ChecksumPath", str(downloaded.checksum_path),
        "-InstallRoot", str(install), "-StateRoot", str(state),
        "-ParentProcessId", str(os.getpid()), "-TargetVersion", downloaded.version,
        "-LaunchId", launch_id, "-NoShortcut",
    ]
    # Existing shortcuts already point to this installation. Updating their
    # targets is unnecessary and wrongly rejects custom install directories.
    with (logs / "desktop-update-launch.log").open("ab", buffering=0) as output:
        output.write((json.dumps({"event": "launch", "version": downloaded.version, "id": launch_id}) + "\n").encode())
        process = subprocess.Popen(
            command, cwd=state, env=environment, stdin=subprocess.DEVNULL,
            stdout=output, stderr=subprocess.STDOUT, close_fds=True,
            # DETACHED_PROCESS can make Windows PowerShell exit without running
            # -File from the native GUI host. NO_WINDOW hides the console while
            # preserving normal initialization; children outlive the host.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"Update applicator exited before readiness ({process.returncode}); see logs/desktop-update-launch.log")
            if ready.is_file():
                with ready.open("rb") as stream:
                    raw = stream.read(4097)
                if len(raw) > 4096:
                    raise RuntimeError("Invalid update readiness receipt")
                record = json.loads(raw)
                if record != {
                    "status": "waiting-for-parent", "launch_id": launch_id,
                    "version": downloaded.version, "pid": process.pid, "parent_pid": os.getpid(),
                }:
                    raise RuntimeError("Update readiness receipt does not match this launch")
                return process
            time.sleep(0.05)
        raise RuntimeError("Update applicator did not become ready; see logs/desktop-update-launch.log")
    except BaseException:
        # The host is still alive, so the applicator is still before its
        # Wait-Process barrier and cannot have started replacing this app.
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        raise
    finally:
        try:
            ready.unlink(missing_ok=True)
        except OSError:
            pass  # A unique, stale readiness receipt cannot admit another launch.
