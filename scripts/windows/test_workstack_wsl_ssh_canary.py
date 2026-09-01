#!/usr/bin/env python3
"""Exercise the real Work Stack SSH path against an isolated WSL sshd.

The canary never uses the configured Work Stack workspace.  It creates a
throw-away Linux authority under /tmp, a throw-away SSH key, and an ephemeral
Windows OpenSSH host alias.  The user's SSH config is restored byte-for-byte in
all exit paths.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
SHELL = ROOT / "desktop" / "python-webview-shell"
sys.path.insert(0, str(SHELL))

from remote_connection_monitor import RemoteConnectionMonitor  # noqa: E402
from ssot_connection import (  # noqa: E402
    LOOPBACK_HOST,
    RemoteConnectionProfile,
    build_ssh_tunnel_command,
    find_ssh_executable,
    run_remote_connection_check,
)


class CanaryError(RuntimeError):
    pass


def run(
    arguments: list[str],
    *,
    check: bool = True,
    timeout: float = 120,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        arguments,
        check=False,
        timeout=timeout,
        capture_output=capture,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-2000:]
        raise CanaryError(
            f"Command failed with exit {result.returncode}: {arguments!r}\n{detail}"
        )
    return result


def wsl(
    distro: str,
    arguments: list[str],
    *,
    root: bool = False,
    check: bool = True,
    timeout: float = 120,
) -> subprocess.CompletedProcess[str]:
    command = ["wsl.exe", "-d", distro]
    if root:
        command.extend(("-u", "root"))
    command.extend(("--", *arguments))
    return run(command, check=check, timeout=timeout)


def available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind((LOOPBACK_HOST, 0))
        return int(listener.getsockname()[1])


def wsl_path(distro: str, path: Path) -> str:
    del distro  # WSL's default automount is the supported Windows layout here.
    resolved = path.resolve()
    drive = resolved.drive.rstrip(":").casefold()
    if not drive or len(drive) != 1:
        raise CanaryError(f"Cannot translate Windows path for WSL: {resolved}")
    tail = resolved.as_posix().split(":", 1)[1].lstrip("/")
    return f"/mnt/{drive}/{tail}"


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8", newline="\n")


def wait_for_json(url: str, *, timeout: float = 25) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as response:
                return json.loads(response.read(128 * 1024).decode("utf-8"))
        except Exception as error:  # the readiness loop is deliberately bounded
            last_error = error
            time.sleep(0.25)
    raise CanaryError(f"Timed out waiting for {url}: {last_error}")


def api_request(
    port: int,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    outgoing = None
    actual_headers = dict(headers or {})
    if body is not None:
        outgoing = json.dumps(body, separators=(",", ":")).encode("utf-8")
        actual_headers.setdefault("Content-Type", "application/json")
    connection = http.client.HTTPConnection(LOOPBACK_HOST, port, timeout=5)
    try:
        connection.request(method, path, body=outgoing, headers=actual_headers)
        response = connection.getresponse()
        raw = response.read()
        return response.status, json.loads(raw.decode("utf-8"))
    finally:
        connection.close()


def stop_process(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def hash_remote_authority(ssh: str, alias: str, data_root: str) -> str:
    script = (
        "set -eu; "
        f"find {shlex.quote(data_root)} -type f -print0 | "
        "sort -z | xargs -0 sha256sum"
    )
    result = run([ssh, "-T", "--", alias, "sh", "-lc", shlex.quote(script)])
    return hashlib.sha256(result.stdout.encode("utf-8")).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--distro", default="Ubuntu")
    parser.add_argument("--keep-wsl-artifacts", action="store_true")
    args = parser.parse_args()

    canary_id = uuid.uuid4().hex
    artifact_root = ROOT / ".artifacts" / "wsl-ssh-canary" / canary_id
    artifact_root.mkdir(parents=True)
    linux_root = f"/tmp/workstack-wsl-ssh-canary-{canary_id}"
    if not linux_root.startswith("/tmp/workstack-wsl-ssh-canary-"):
        raise CanaryError("Refusing an unsafe WSL canary root")

    alias = f"workstack-wsl-canary-{canary_id}"
    ssh_port = available_port()
    forward_port = available_port()
    remote_port = available_port()
    second_remote_port = available_port()
    ssh = find_ssh_executable()
    client_key = artifact_root / "id_ed25519"
    known_hosts = artifact_root / "known_hosts"
    sshd_config_windows = artifact_root / "sshd_config"
    wrapper_windows = artifact_root / "run_work_stack.py"
    ssh_log = artifact_root / "remote-ssh.log"
    evidence_path = artifact_root / "evidence.json"

    user_ssh = Path.home() / ".ssh"
    user_config = user_ssh / "config"
    config_existed = user_config.is_file()
    original_config = user_config.read_bytes() if config_existed else None
    created_ssh_directory = not user_ssh.exists()
    current_process: list[subprocess.Popen[str] | None] = [None]
    monitor: RemoteConnectionMonitor | None = None
    sshd_started = False
    evidence: dict[str, Any] = {
        "canary_id": canary_id,
        "distro": args.distro,
        "linux_root": linux_root,
        "ssh_port": ssh_port,
        "local_forward_port": forward_port,
        "remote_port": remote_port,
        "checks": {},
    }

    try:
        packages = wsl(
            args.distro,
            ["sh", "-lc", "command -v sshd >/dev/null && python3 -m venv --help >/dev/null"],
            root=True,
        )
        evidence["checks"]["wsl_prerequisites"] = packages.returncode == 0

        repo_wsl = wsl_path(args.distro, ROOT)
        artifact_wsl = wsl_path(args.distro, artifact_root)
        wsl(args.distro, ["mkdir", "-p", f"{linux_root}/product", f"{linux_root}/app", f"{linux_root}/data"], root=True)
        for name in ("workstack", "contracts", "web"):
            wsl(args.distro, ["cp", "-a", f"{repo_wsl}/{name}", f"{linux_root}/product/"], root=True)
        for name in ("run_work_stack.py", "requirements.txt"):
            wsl(args.distro, ["cp", f"{repo_wsl}/{name}", f"{linux_root}/product/{name}"], root=True)

        wrapper = (
            "from pathlib import Path\n"
            "import os, sys\n"
            "root = Path(__file__).resolve().parent.parent\n"
            "python = root / 'venv' / 'bin' / 'python'\n"
            "runner = root / 'product' / 'run_work_stack.py'\n"
            "os.execv(str(python), [str(python), str(runner), *sys.argv[1:]])\n"
        )
        write_text(wrapper_windows, wrapper)
        wsl(args.distro, ["cp", f"{artifact_wsl}/run_work_stack.py", f"{linux_root}/app/run_work_stack.py"], root=True)
        wsl(args.distro, ["python3", "-m", "venv", f"{linux_root}/venv"], root=True, timeout=180)
        wsl(
            args.distro,
            [f"{linux_root}/venv/bin/pip", "install", "--disable-pip-version-check", "-r", f"{linux_root}/product/requirements.txt"],
            root=True,
            timeout=300,
        )
        wsl(
            args.distro,
            [f"{linux_root}/venv/bin/python", f"{linux_root}/product/run_work_stack.py", "--data-dir", f"{linux_root}/data", "backlog", "list"],
            root=True,
        )
        workspace_payload = json.loads(
            wsl(args.distro, ["cat", f"{linux_root}/data/workspace.json"], root=True).stdout
        )
        workspace_id = workspace_payload["id"]
        evidence["workspace_id"] = workspace_id
        evidence["checks"]["isolated_ssot_initialized"] = True

        run(["ssh-keygen.exe", "-q", "-t", "ed25519", "-N", "", "-f", str(client_key)])
        client_public_wsl = wsl_path(args.distro, client_key.with_suffix(".pub"))
        wsl(args.distro, ["cp", client_public_wsl, f"{linux_root}/authorized_keys"], root=True)
        wsl(args.distro, ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", f"{linux_root}/host_key"], root=True)

        sshd_config = "\n".join(
            (
                f"Port {ssh_port}",
                "ListenAddress 127.0.0.1",
                f"HostKey {linux_root}/host_key",
                f"AuthorizedKeysFile {linux_root}/authorized_keys",
                "PubkeyAuthentication yes",
                "PasswordAuthentication no",
                "KbdInteractiveAuthentication no",
                "PermitRootLogin prohibit-password",
                "UsePAM no",
                "StrictModes no",
                f"PidFile {linux_root}/sshd.pid",
                "LogLevel VERBOSE",
                "AllowUsers root",
                "",
            )
        )
        write_text(sshd_config_windows, sshd_config)
        wsl(args.distro, ["cp", f"{artifact_wsl}/sshd_config", f"{linux_root}/sshd_config"], root=True)
        wsl(args.distro, ["mkdir", "-p", "/run/sshd"], root=True)
        wsl(
            args.distro,
            ["/usr/sbin/sshd", "-f", f"{linux_root}/sshd_config", "-E", f"{linux_root}/sshd.log"],
            root=True,
        )
        sshd_started = True

        host_public = wsl(
            args.distro, ["cat", f"{linux_root}/host_key.pub"], root=True
        ).stdout.strip().split()
        if len(host_public) < 2 or host_public[0] != "ssh-ed25519":
            raise CanaryError("The isolated WSL sshd host key is invalid")
        write_text(
            known_hosts,
            f"[127.0.0.1]:{ssh_port} {host_public[0]} {host_public[1]}\n",
        )

        config_block = "\n".join(
            (
                f"Host {alias}",
                "  HostName 127.0.0.1",
                f"  Port {ssh_port}",
                "  User root",
                f"  IdentityFile {client_key.as_posix()}",
                "  IdentitiesOnly yes",
                f"  UserKnownHostsFile {known_hosts.as_posix()}",
                "  BatchMode yes",
                "  ConnectTimeout 5",
                "  ServerAliveInterval 2",
                "  ServerAliveCountMax 2",
                "  TCPKeepAlive yes",
                "",
            )
        )
        user_ssh.mkdir(parents=True, exist_ok=True)
        prefix = b"" if original_config is None else original_config.rstrip(b"\r\n") + b"\n\n"
        user_config.write_bytes(prefix + config_block.encode("utf-8"))

        profile = RemoteConnectionProfile(
            ssh_host_alias=alias,
            remote_app_dir=f"{linux_root}/app",
            remote_data_dir=f"{linux_root}/data",
            local_forward_port=forward_port,
            workspace_id=workspace_id,
            remote_port=remote_port,
        )
        run_remote_connection_check(profile)
        evidence["checks"]["product_profile_check"] = True

        command = build_ssh_tunnel_command(profile, ssh)

        def start_tunnel() -> bool:
            stop_process(current_process[0])
            log_handle = ssh_log.open("a", encoding="utf-8")
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                text=True,
            )
            setattr(process, "_workstack_log_handle", log_handle)
            current_process[0] = process
            try:
                health = wait_for_json(
                    f"http://{LOOPBACK_HOST}:{forward_port}/api/v1/health", timeout=25
                )
                return health == {"data": {"api_version": "v1", "status": "ready"}}
            except CanaryError:
                return False

        if not start_tunnel():
            raise CanaryError("The real Work Stack SSH tunnel command did not become ready")
        evidence["checks"]["tunnel_health"] = True

        storage = wait_for_json(f"http://{LOOPBACK_HOST}:{forward_port}/api/v1/storage")
        if storage["data"]["workspace_id"] != workspace_id:
            raise CanaryError("The forwarded server reported the wrong workspace identity")
        evidence["checks"]["workspace_identity"] = True

        status, session = api_request(forward_port, "GET", "/api/v1/session")
        if status != 200:
            raise CanaryError("Could not establish the forwarded browser session")
        headers = {
            "Origin": f"http://{LOOPBACK_HOST}:{forward_port}",
            "X-WorkStack-CSRF": session["data"]["csrf_token"],
            "Idempotency-Key": f"wsl.ssh.canary.{canary_id}",
            "Content-Type": "application/json",
        }
        status, created = api_request(
            forward_port,
            "POST",
            "/api/v1/tasks",
            {"title": "WSL SSH reconnect canary", "priority": "P2"},
            headers,
        )
        if status != 201:
            raise CanaryError(f"Forwarded task creation failed: {status} {created}")
        task_id = created["data"]["id"]
        evidence["task_id"] = task_id
        evidence["checks"]["remote_write"] = True
        authority_hash_before = hash_remote_authority(ssh, alias, f"{linux_root}/data")

        states: list[str] = []
        reloads: list[float] = []

        def process_alive() -> bool:
            process = current_process[0]
            return process is not None and process.poll() is None

        def healthy() -> bool:
            try:
                payload = wait_for_json(
                    f"http://{LOOPBACK_HOST}:{forward_port}/api/v1/health", timeout=1.6
                )
                return payload == {"data": {"api_version": "v1", "status": "ready"}}
            except CanaryError:
                return False

        monitor = RemoteConnectionMonitor(
            is_healthy=healthy,
            is_process_alive=process_alive,
            reconnect_once=start_tunnel,
            publish_state=states.append,
            reload_view=lambda: reloads.append(time.monotonic()),
            initial_grace=0.2,
            poll_interval=0.5,
            failure_threshold=2,
            reconnect_backoff=(0.5, 1.0),
            reconnect_grace=0.2,
        )
        monitor.start()
        deadline = time.monotonic() + 5
        while "ready" not in states and time.monotonic() < deadline:
            time.sleep(0.1)
        original_process = current_process[0]
        stop_process(original_process)
        if original_process is not None:
            handle = getattr(original_process, "_workstack_log_handle", None)
            if handle is not None:
                handle.close()
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            if "reconnecting" in states and states[-1:] == ["ready"] and reloads:
                break
            time.sleep(0.1)
        if "reconnecting" not in states or states[-1:] != ["ready"] or not reloads:
            raise CanaryError(f"Automatic reconnect did not recover: states={states!r}")
        evidence["monitor_states"] = states
        evidence["checks"]["automatic_reconnect"] = True

        status, task = api_request(forward_port, "GET", f"/api/v1/tasks/{task_id}")
        if status != 200 or task["data"]["task"]["id"] != task_id:
            raise CanaryError("The remote task was not readable after reconnect")
        evidence["checks"]["post_reconnect_read"] = True

        authority_hash_after = hash_remote_authority(ssh, alias, f"{linux_root}/data")
        if authority_hash_after != authority_hash_before:
            raise CanaryError("Reconnect changed authoritative SSOT bytes")
        evidence["authority_digest"] = authority_hash_after
        evidence["checks"]["byte_exact_reconnect"] = True

        second = RemoteConnectionProfile(
            ssh_host_alias=alias,
            remote_app_dir=profile.remote_app_dir,
            remote_data_dir=profile.remote_data_dir,
            local_forward_port=available_port(),
            workspace_id=workspace_id,
            remote_port=second_remote_port,
        )
        contender = run(
            build_ssh_tunnel_command(second, ssh),
            check=False,
            timeout=10,
        )
        contender_output = (contender.stdout + contender.stderr).strip()
        evidence["contender_exit"] = contender.returncode
        evidence["contender_output"] = contender_output[-1000:]
        if contender.returncode == 0:
            raise CanaryError("A second remote server unexpectedly exited successfully")
        if "lock" not in contender_output.casefold() and "another writer" not in contender_output.casefold():
            raise CanaryError(
                "The second remote writer failed, but did not report the expected lock refusal: "
                + contender_output[-500:]
            )
        evidence["checks"]["single_writer_refusal"] = True

        evidence["status"] = "passed"
        write_text(evidence_path, json.dumps(evidence, indent=2, sort_keys=True) + "\n")
        print(json.dumps(evidence, indent=2, sort_keys=True))
        print(f"Evidence: {evidence_path}")
        return 0
    except Exception as error:
        evidence["status"] = "failed"
        evidence["error"] = f"{type(error).__name__}: {error}"
        write_text(evidence_path, json.dumps(evidence, indent=2, sort_keys=True) + "\n")
        raise
    finally:
        if monitor is not None:
            monitor.stop(timeout=5)
        process = current_process[0]
        stop_process(process)
        if process is not None:
            handle = getattr(process, "_workstack_log_handle", None)
            if handle is not None and not handle.closed:
                handle.close()
        if sshd_started:
            cleanup = (
                f"if test -f {shlex.quote(linux_root + '/sshd.pid')}; then "
                f"kill \"$(cat {shlex.quote(linux_root + '/sshd.pid')})\" 2>/dev/null || true; fi"
            )
            wsl(args.distro, ["sh", "-lc", cleanup], root=True, check=False)
        if not args.keep_wsl_artifacts:
            wsl(args.distro, ["rm", "-rf", "--", linux_root], root=True, check=False)
        if original_config is None:
            user_config.unlink(missing_ok=True)
            if created_ssh_directory and user_ssh.is_dir() and not any(user_ssh.iterdir()):
                user_ssh.rmdir()
        else:
            user_config.write_bytes(original_config)
        client_key.unlink(missing_ok=True)
        client_key.with_suffix(".pub").unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
