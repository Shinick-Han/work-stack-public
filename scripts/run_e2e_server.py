#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

READY_NAME = "ready.json"
STOP_NAME = "stop.request"
COMPLETION_NAME = "completion.json"


class AcceptanceRefusal(RuntimeError):
    """The acceptance run may not start; the caller keeps custody of the root."""


def acceptance_paths(control_root: Path) -> dict[str, Path]:
    """Every location this run may write, all beneath its own control root."""
    return {
        "appdata": control_root / "appdata",
        "cache": control_root / "cache",
        "completion": control_root / COMPLETION_NAME,
        "localappdata": control_root / "localappdata",
        "ready": control_root / READY_NAME,
        "runtime": control_root / "runtime",
        "stop": control_root / STOP_NAME,
        "temp": control_root / "temp",
    }


def refuse_prefilled_root(control_root: Path, paths: dict[str, Path]) -> None:
    """A fresh run adopts no existing record, stale stop request or runtime subtree."""
    if not control_root.is_absolute():
        raise AcceptanceRefusal("The acceptance control root must be an absolute path.")
    for name in ("ready", "completion", "stop", "appdata", "cache", "localappdata", "runtime", "temp"):
        if paths[name].exists():
            raise AcceptanceRefusal(f"The acceptance control root already holds {name}.")


def refuse_occupied_port(port: int) -> None:
    """
    Refuse a loopback port that already answers. The product server sets
    SO_REUSEADDR, and on Windows that lets a second listener bind a port a
    stranger is already serving without any error, so the bind itself cannot
    be the refusal; a connect probe is.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    try:
        probe.connect(("127.0.0.1", port))
    except OSError:
        return
    finally:
        probe.close()
    raise AcceptanceRefusal(f"Port {port} is already occupied; refusing to share it.")


def refuse_nonpositive_budget(budget_seconds: float) -> None:
    """The child self-deadline must be finite: an unbounded fixture is refused."""
    if not budget_seconds > 0:
        raise AcceptanceRefusal("Acceptance mode requires a positive --budget-seconds.")


def dist_manifest(project_root: Path) -> dict[str, object]:
    """
    The built React entry this mode refuses to serve without. Without it the
    server would fall back to the legacy page and a healthy session probe would
    prove nothing about the reviewed build.
    """
    index = project_root / "frontend" / "dist" / "index.html"
    if not index.is_file():
        raise AcceptanceRefusal("No built frontend/dist/index.html to serve.")
    payload = index.read_bytes()
    assets = sorted(
        path.relative_to(project_root).as_posix()
        for path in (project_root / "frontend" / "dist" / "assets").glob("*")
        if path.is_file()
    )
    if not assets:
        raise AcceptanceRefusal("The built dist carries no assets; refusing to serve it.")
    return {
        "asset_count": len(assets),
        "assets": assets,
        "index_bytes": len(payload),
        "index_sha256": hashlib.sha256(payload).hexdigest(),
    }


def contain_child_environment(paths: dict[str, Path]) -> None:
    """
    Runtime writes land beneath the owned root, set BEFORE the Store import and
    construction. HOME and USERPROFILE are deliberately left untouched.
    """
    for key in ("appdata", "cache", "localappdata", "runtime", "temp"):
        paths[key].mkdir(parents=True, exist_ok=True)
    os.environ["TEMP"] = str(paths["temp"])
    os.environ["TMP"] = str(paths["temp"])
    os.environ["TMPDIR"] = str(paths["temp"])
    os.environ["APPDATA"] = str(paths["appdata"])
    os.environ["LOCALAPPDATA"] = str(paths["localappdata"])
    os.environ["XDG_CACHE_HOME"] = str(paths["cache"])
    os.environ["WORK_STACK_HOME"] = str(paths["runtime"])
    os.environ["WORK_STACK_RUNTIME"] = str(paths["runtime"])


def stop_requested(paths: dict[str, Path], run_id: str) -> bool:
    """Only THIS run's stop request counts; a foreign or stale one is ignored."""
    request = paths["stop"]
    if not request.is_file():
        return False
    try:
        return request.read_text(encoding="utf-8").strip() == run_id
    except OSError:
        return False


def deadline_expired(started: float, budget_seconds: float, now: float) -> bool:
    """
    A finite self-deadline, never extended by a retry or by shutdown. A
    non-positive budget never reaches this predicate: refuse_nonpositive_budget
    rejects it before the run starts.
    """
    return budget_seconds > 0 and (now - started) >= budget_seconds


def write_record(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def ready_record(run_id: str, port: int, data_dir: Path, paths: dict[str, Path],
                 manifest: dict[str, object], workspace_uid: object,
                 runtime_dir: Path | None = None) -> dict[str, object]:
    """The identity a parent must be able to cross-check before it drives a page."""
    return {
        "data_dir": str(data_dir),
        "dist": manifest,
        "pid": os.getpid(),
        "port": port,
        "run_id": run_id,
        "runtime_dir": str(runtime_dir if runtime_dir is not None else paths["runtime"]),
        "workspace_uid": workspace_uid,
    }


def run_ordinary(port: int) -> int:
    """The unchanged behaviour every existing caller already relies on."""
    from workstack.server import create_server
    from workstack.service import WorkStack
    from workstack.store import Store

    with tempfile.TemporaryDirectory(prefix="workstack-e2e-") as directory:
        stack = WorkStack(Store(Path(directory)))
        stack.store.seed_demo(PROJECT_ROOT / "data")
        server = create_server(stack, "127.0.0.1", port)
        print("work-stack e2e: http://127.0.0.1:{}/".format(server.actual_port), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    return 0


def run_acceptance(port: int, control_root: Path, run_id: str, budget_seconds: float) -> int:
    """
    Acceptance mode: an owned fresh control root, a required built dist, a
    recorded identity, one run-bound stop request or a finite self-deadline, and
    an ordinary close followed by the temporary-directory cleanup.
    """
    paths = acceptance_paths(control_root)
    # Validate before creating anything: a refused root leaves no directory behind.
    refuse_prefilled_root(control_root, paths)
    refuse_nonpositive_budget(budget_seconds)
    manifest = dist_manifest(PROJECT_ROOT)
    refuse_occupied_port(port)
    control_root.mkdir(parents=True, exist_ok=True)
    contain_child_environment(paths)

    from workstack.server import create_server
    from workstack.service import WorkStack
    from workstack.store import Store

    started = time.monotonic()
    reason = "stop-request"
    interrupted = False
    with tempfile.TemporaryDirectory(prefix="acceptance-", dir=str(paths["temp"])) as data:
        stack = WorkStack(Store(Path(data)))
        stack.store.seed_demo(PROJECT_ROOT / "data")
        # The occupied-port refusal already happened in refuse_occupied_port:
        # on Windows this bind would not raise for a port a stranger serves.
        server = create_server(stack, "127.0.0.1", port)
        readiness = getattr(stack.store, "readiness", None)
        write_record(paths["ready"], ready_record(
            run_id, server.actual_port, Path(data), paths, manifest,
            getattr(readiness, "workspace_uid", None),
            runtime_dir=getattr(stack.store, "runtime_root", None),
        ))
        loop = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2})
        loop.start()
        try:
            while True:
                if stop_requested(paths, run_id):
                    break
                if deadline_expired(started, budget_seconds, time.monotonic()):
                    reason = "self-deadline"
                    break
                time.sleep(0.1)
        except KeyboardInterrupt:
            # A console interrupt still closes the server and cleans up, but it
            # is not an ordinary end: the parent refuses this reason on purpose.
            reason = "interrupt"
            interrupted = True
        finally:
            server.shutdown()
            loop.join()
            server.server_close()
    write_record(paths["completion"], {"reason": reason, "run_id": run_id})
    return 130 if interrupted else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Work Stack with a disposable demo store.")
    parser.add_argument("--port", type=int, default=8781)
    parser.add_argument("--acceptance-root", default=None)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--budget-seconds", type=float, default=600.0)
    arguments = parser.parse_args(argv)

    if arguments.acceptance_root is None:
        return run_ordinary(arguments.port)
    if not arguments.run_id:
        raise AcceptanceRefusal("Acceptance mode requires an explicit --run-id.")
    refuse_nonpositive_budget(arguments.budget_seconds)
    return run_acceptance(
        arguments.port,
        Path(arguments.acceptance_root),
        arguments.run_id,
        arguments.budget_seconds,
    )


if __name__ == "__main__":
    raise SystemExit(main())
