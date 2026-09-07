#!/usr/bin/env python3
"""Repository-owned fail-closed receipt runner (Wave 1 G1).

Ports G0 semantics without machine-specific paths or shell strings.
Parse, execute, render, and verify are separate functions so CCN stays bounded.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
FAIL_CLOSED_EXIT = 2
STEP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
CLASSIFY_EXIT_RE = re.compile(r"classify_exit=(\d+)")
TABLE_ROW_RE = re.compile(r"^\|\s*([^|]+?)\s*\|\s*(.*?)\s*\|$")

REQUIRED_RECEIPT_FIELDS = (
    "step_id",
    "description",
    "host",
    "user",
    "cwd",
    "started_utc",
    "finished_utc",
    "base_sha",
    "worktree",
    "changed_paths",
    "commands",
    "command_exit_codes",
    "exit_code",
    "stdout_bytes",
    "stderr_bytes",
    "stdout_sha256",
    "stderr_sha256",
    "kind",
    "oracle",
)
REQUIRED_SPEC_FIELDS = (
    "step_id",
    "description",
    "worktree",
    "base_sha",
    "cwd",
    "changed_paths",
    "kind",
    "oracle",
    "commands",
    "output_dir",
)

UtcNow = Callable[[], str]


class FailClosedError(Exception):
    """Schema, path, or swallow violation. Runner must not exit 0."""


@dataclass(frozen=True)
class Command:
    argv: tuple[str, ...]

    def label(self) -> str:
        return json.dumps(list(self.argv), ensure_ascii=False)

    def to_public(self) -> dict[str, Any]:
        return {"argv": list(self.argv)}


@dataclass
class CommandResult:
    command: Command
    exit_code: int
    stdout: bytes
    stderr: bytes


@dataclass
class RunResult:
    step_id: str
    description: str
    host: str
    user: str
    cwd: str
    started_utc: str
    finished_utc: str
    base_sha: str
    worktree: str
    changed_paths: str
    kind: str
    oracle: str
    commands: list[Command]
    command_results: list[CommandResult]
    stdout: bytes
    stderr: bytes
    stdout_sha256: str
    stderr_sha256: str
    exit_code: int
    expected_red: bool = False
    executed_command: str = ""
    extra_fields: dict[str, str] = field(default_factory=dict)

    @property
    def command_exit_codes(self) -> list[int]:
        return [item.exit_code for item in self.command_results]


@dataclass
class VerifyResult:
    path: Path
    ok: bool
    errors: list[str]
    fields: dict[str, str]


def system_utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def truthy(raw: Any) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "y"}


def current_user() -> str:
    return os.environ.get("USERNAME") or os.environ.get("USER") or "unknown"


def current_host() -> str:
    return os.environ.get("RECEIPT_HOST") or socket.gethostname()


def aggregate_exit(exit_codes: Sequence[int]) -> int:
    if not exit_codes:
        return FAIL_CLOSED_EXIT
    for code in exit_codes:
        if int(code) != 0:
            return int(code)
    return 0


def missing_schema_fields(
    record: Mapping[str, Any],
    required: Sequence[str] = REQUIRED_RECEIPT_FIELDS,
) -> list[str]:
    missing: list[str] = []
    for name in required:
        if name not in record:
            missing.append(name)
            continue
        value = record[name]
        if value is None or (isinstance(value, str) and value.strip() == ""):
            missing.append(name)
    return missing


def parse_command_exit_codes(raw: Any) -> list[int] | None:
    if raw is None:
        return None
    if isinstance(raw, list):
        try:
            return [int(x) for x in raw]
        except (TypeError, ValueError):
            return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        loaded = None
    if isinstance(loaded, list):
        try:
            return [int(x) for x in loaded]
        except (TypeError, ValueError):
            return None
    parts = [part.strip() for part in text.split(",") if part.strip()]
    if not parts:
        return None
    try:
        return [int(part) for part in parts]
    except ValueError:
        return None


def load_contract(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FailClosedError(f"cannot read contract {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise FailClosedError("receipt contract must be a JSON object")
    return payload


def contract_matches_builtin(payload: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    receipt = payload.get("required_receipt_fields")
    spec = payload.get("required_spec_fields")
    if list(receipt or []) != list(REQUIRED_RECEIPT_FIELDS):
        errors.append("contract required_receipt_fields != runner builtin")
    if list(spec or []) != list(REQUIRED_SPEC_FIELDS):
        errors.append("contract required_spec_fields != runner builtin")
    return errors


def parse_commands(raw: Any) -> list[Command]:
    if not isinstance(raw, list) or not raw:
        raise FailClosedError("spec.commands must be a non-empty list")
    parsed: list[Command] = []
    for index, item in enumerate(raw):
        parsed.append(_parse_one_command(index, item))
    return parsed


def _parse_one_command(index: int, item: Any) -> Command:
    if not isinstance(item, dict):
        raise FailClosedError(f"commands[{index}] must be an object")
    if "shell" in item and item.get("shell") not in (None, ""):
        raise FailClosedError("opaque shell strings are rejected; use argv")
    argv = item.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x != "" for x in argv):
        raise FailClosedError(f"commands[{index}].argv must be a non-empty list of strings")
    return Command(argv=tuple(str(x) for x in argv))


def validate_step_id(step_id: str) -> str:
    if not STEP_ID_RE.match(step_id):
        raise FailClosedError(f"invalid step_id: {step_id!r}")
    return step_id


def resolve_under_root(root: Path, candidate: Path) -> Path:
    root_r = root.resolve()
    raw = Path(candidate)
    joined = raw if raw.is_absolute() else root_r / raw
    resolved = joined.resolve()
    try:
        resolved.relative_to(root_r)
    except ValueError as exc:
        raise FailClosedError(f"path escapes output root: {candidate}") from exc
    return resolved


def load_spec(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FailClosedError(f"cannot read spec {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise FailClosedError("spec must be a JSON object")
    missing = missing_schema_fields(data, REQUIRED_SPEC_FIELDS)
    if missing:
        raise FailClosedError("spec missing fields: " + ",".join(missing))
    validate_step_id(str(data["step_id"]))
    parse_commands(data["commands"])
    return data


def execute_argv(
    argv: Sequence[str],
    cwd: str,
    env: dict[str, str],
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        list(argv),
        cwd=cwd,
        env=env,
        capture_output=True,
        check=False,
        shell=False,
    )


def run_commands(
    commands: Sequence[Command],
    cwd: str,
    env: dict[str, str] | None = None,
) -> list[CommandResult]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    merged["PYTHONUTF8"] = "1"
    results: list[CommandResult] = []
    for command in commands:
        completed = execute_argv(command.argv, cwd, merged)
        results.append(
            CommandResult(
                command=command,
                exit_code=int(completed.returncode),
                stdout=completed.stdout or b"",
                stderr=completed.stderr or b"",
            )
        )
    return results


def combine_streams(results: Sequence[CommandResult]) -> tuple[bytes, bytes]:
    stdout_parts: list[bytes] = []
    stderr_parts: list[bytes] = []
    for index, item in enumerate(results):
        banner = f"=== command[{index}] exit={item.exit_code} {item.command.label()} ===\n"
        stdout_parts.append(banner.encode("utf-8"))
        stdout_parts.append(item.stdout)
        if item.stdout and not item.stdout.endswith(b"\n"):
            stdout_parts.append(b"\n")
        stderr_parts.append(item.stderr)
    return b"".join(stdout_parts), b"".join(stderr_parts)


def detect_d3a_swallow(fields: Mapping[str, Any], stdout_text: str) -> str | None:
    try:
        header = int(str(fields.get("exit_code", "")).strip())
    except (TypeError, ValueError):
        return "exit_code is not an integer"
    codes = parse_command_exit_codes(fields.get("command_exit_codes"))
    if codes is not None and aggregate_exit(codes) != 0 and header == 0:
        return f"command_exit_codes contain a non-zero status but exit_code=0 (codes={codes})"
    match = CLASSIFY_EXIT_RE.search(stdout_text or "")
    if match and int(match.group(1)) != 0 and header == 0:
        return f"stdout classify_exit={match.group(1)} with header exit_code=0 (D3a swallow class)"
    return None


def _fenced_block_after(text: str, heading: str) -> str | None:
    lines = text.splitlines()
    capture = False
    fence: str | None = None
    collected: list[str] = []
    for line in lines:
        if not capture and line.startswith(heading):
            capture = True
            continue
        if capture and fence is None:
            if line.strip().startswith("```"):
                fence = line.strip()
            continue
        if capture and fence is not None:
            if line.strip().startswith("```"):
                return "\n".join(collected)
            collected.append(line)
    return None


def parse_receipt_markdown(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    in_table = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and "field" in stripped.lower() and "value" in stripped.lower():
            in_table = True
            continue
        if in_table and re.match(r"^\|\s*-+", stripped):
            continue
        if in_table and stripped.startswith("|"):
            match = TABLE_ROW_RE.match(stripped)
            if match:
                key = match.group(1).strip()
                if key.lower() != "field":
                    fields[key] = match.group(2).strip()
            continue
        if in_table and not stripped.startswith("|"):
            break
    stdout_text = _fenced_block_after(text, "## stdout")
    stderr_text = _fenced_block_after(text, "## stderr")
    if stdout_text is not None:
        fields["_stdout_body"] = stdout_text
    if stderr_text is not None:
        fields["_stderr_body"] = stderr_text
    return fields


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    fd = os.open(str(tmp), flags, 0o644)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def write_receipt(result: RunResult, output_root: Path) -> Path:
    root = output_root.resolve()
    validate_step_id(result.step_id)
    dest = resolve_under_root(root, Path(f"{result.step_id}.md"))
    raw_stdout = resolve_under_root(root, Path("raw") / f"{result.step_id}.stdout")
    raw_stderr = resolve_under_root(root, Path("raw") / f"{result.step_id}.stderr")
    markdown = render_receipt_markdown(result)
    staged = [
        (raw_stdout, result.stdout),
        (raw_stderr, result.stderr),
        (dest, markdown.encode("utf-8")),
    ]
    _commit_staged(staged)
    return dest


def _commit_staged(pairs: Sequence[tuple[Path, bytes]]) -> None:
    tmps: list[tuple[Path, Path]] = []
    try:
        for dest, data in pairs:
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".tmp")
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            if hasattr(os, "O_BINARY"):
                flags |= os.O_BINARY
            fd = os.open(str(tmp), flags, 0o644)
            try:
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            tmps.append((tmp, dest))
        for tmp, dest in tmps:
            os.replace(tmp, dest)
    except Exception:
        for tmp, dest in tmps:
            if dest.exists():
                continue
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise


def _fence(body: str) -> str:
    ticks = 3
    while "`" * ticks in body:
        ticks += 1
    mark = "`" * ticks
    return f"{mark}text\n{body}\n{mark}"


def render_receipt_markdown(result: RunResult) -> str:
    commands_json = json.dumps([cmd.to_public() for cmd in result.commands], ensure_ascii=False)
    codes_json = json.dumps(result.command_exit_codes)
    rows = [
        ("step_id", result.step_id),
        ("description", result.description),
        ("host", result.host),
        ("user", result.user),
        ("cwd", result.cwd.replace("\\", "/")),
        ("started_utc", result.started_utc),
        ("finished_utc", result.finished_utc),
        ("base_sha", result.base_sha),
        ("worktree", result.worktree.replace("\\", "/")),
        ("changed_paths", result.changed_paths),
        ("commands", commands_json),
        ("command_exit_codes", codes_json),
        ("exit_code", str(result.exit_code)),
        ("stdout_bytes", str(len(result.stdout))),
        ("stderr_bytes", str(len(result.stderr))),
        ("stdout_sha256", result.stdout_sha256),
        ("stderr_sha256", result.stderr_sha256),
        ("raw_stdout", f"raw/{result.step_id}.stdout"),
        ("raw_stderr", f"raw/{result.step_id}.stderr"),
        ("kind", result.kind),
        ("oracle", result.oracle),
        ("expected_red", "true" if result.expected_red else "false"),
    ]
    rows.extend((key, value) for key, value in result.extra_fields.items())
    table = ["| field | value |", "|---|---|"]
    table.extend(f"| {key} | {value} |" for key, value in rows)
    stdout_text = result.stdout.decode("utf-8", errors="replace")
    stderr_text = result.stderr.decode("utf-8", errors="replace")
    command_block = result.executed_command.strip() or "(see commands field)"
    return "\n".join(
        [
            f"# Receipt {result.step_id}",
            "",
            *table,
            "",
            "## command (executed verbatim)",
            "",
            "```text",
            command_block,
            "```",
            "",
            "## stdout",
            "",
            _fence(stdout_text),
            "",
            "## stderr",
            "",
            _fence(stderr_text),
            "",
        ]
    )


def worktree_head(worktree: Path) -> str:
    completed = execute_argv(["git", "rev-parse", "HEAD"], str(worktree), os.environ.copy())
    if completed.returncode != 0:
        raise FailClosedError("git rev-parse HEAD failed")
    return completed.stdout.decode("ascii", errors="replace").strip()


def run_spec(
    spec: Mapping[str, Any],
    *,
    executed_command: str = "",
    clock: UtcNow = system_utc_now,
) -> RunResult:
    missing = missing_schema_fields(spec, REQUIRED_SPEC_FIELDS)
    if missing:
        raise FailClosedError("spec missing fields: " + ",".join(missing))
    step_id = validate_step_id(str(spec["step_id"]))
    output_root = Path(str(spec["output_dir"]))
    resolve_under_root(output_root, Path(f"{step_id}.md"))
    worktree = Path(str(spec["worktree"]))
    base_sha = str(spec["base_sha"]).strip()
    if worktree.is_dir():
        head = worktree_head(worktree)
        if head != base_sha:
            raise FailClosedError(f"worktree HEAD {head} != base_sha {base_sha}")
    commands = parse_commands(spec["commands"])
    started = clock()
    results = run_commands(commands, cwd=str(spec["cwd"]))
    finished = clock()
    stdout, stderr = combine_streams(results)
    codes = [item.exit_code for item in results]
    exit_code = aggregate_exit(codes)
    expected_red = truthy(spec.get("expected_red", ""))
    swallow = detect_d3a_swallow(
        {
            "exit_code": str(exit_code),
            "command_exit_codes": json.dumps(codes),
        },
        stdout.decode("utf-8", errors="replace"),
    )
    if swallow and exit_code == 0:
        exit_code = FAIL_CLOSED_EXIT
    extra = {}
    if spec.get("extra_fields"):
        extra = {str(k): str(v) for k, v in dict(spec["extra_fields"]).items()}
    return RunResult(
        step_id=step_id,
        description=str(spec["description"]),
        host=current_host(),
        user=current_user(),
        cwd=str(spec["cwd"]),
        started_utc=started,
        finished_utc=finished,
        base_sha=base_sha,
        worktree=str(spec["worktree"]),
        changed_paths=str(spec["changed_paths"]),
        kind=str(spec["kind"]),
        oracle=str(spec["oracle"]),
        commands=commands,
        command_results=results,
        stdout=stdout,
        stderr=stderr,
        stdout_sha256=sha256_hex(stdout),
        stderr_sha256=sha256_hex(stderr),
        exit_code=exit_code,
        expected_red=expected_red,
        executed_command=executed_command,
        extra_fields=extra,
    )


def _read_raw(path: Path, errors: list[str], label: str) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError as exc:
        errors.append(f"missing raw {label} {path}: {exc}")
        return None


def _check_hash_and_size(
    fields: Mapping[str, str],
    label: str,
    data: bytes,
    errors: list[str],
) -> None:
    actual_hash = sha256_hex(data)
    actual_size = str(len(data))
    declared_hash = fields.get(f"{label}_sha256", "")
    declared_size = fields.get(f"{label}_bytes", "")
    if declared_hash and declared_hash != actual_hash:
        errors.append(f"{label}_sha256 mismatch declared={declared_hash} actual={actual_hash}")
    if declared_size and declared_size != actual_size:
        errors.append(f"{label}_bytes mismatch declared={declared_size} actual={actual_size}")


def verify_receipt(path: Path, *, output_root: Path | None = None) -> VerifyResult:
    errors: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return VerifyResult(path=path, ok=False, errors=[f"cannot read: {exc}"], fields={})
    fields = parse_receipt_markdown(text)
    missing = missing_schema_fields(fields)
    if missing:
        errors.append("missing schema fields: " + ",".join(missing))
    if "step_id" in fields:
        try:
            validate_step_id(fields["step_id"])
        except FailClosedError as exc:
            errors.append(str(exc))
    swallow = detect_d3a_swallow(fields, fields.get("_stdout_body", ""))
    if swallow:
        errors.append(swallow)
    root = (output_root or path.parent).resolve()
    raw_stdout = _raw_path(root, path, fields.get("raw_stdout"), f"{path.stem}.stdout", errors)
    raw_stderr = _raw_path(root, path, fields.get("raw_stderr"), f"{path.stem}.stderr", errors)
    stdout_bytes = _read_raw(raw_stdout, errors, "stdout") if raw_stdout else None
    stderr_bytes = _read_raw(raw_stderr, errors, "stderr") if raw_stderr else None
    if stdout_bytes is not None:
        _check_hash_and_size(fields, "stdout", stdout_bytes, errors)
    if stderr_bytes is not None:
        _check_hash_and_size(fields, "stderr", stderr_bytes, errors)
        if len(stderr_bytes) == 0 and fields.get("stderr_sha256") not in (None, "", EMPTY_SHA256):
            errors.append("empty stderr must hash to SHA-256 of empty bytes")
    _verify_exit_consistency(fields, errors)
    _verify_command_counts(fields, errors)
    return VerifyResult(path=path, ok=not errors, errors=errors, fields=fields)


def _raw_path(
    root: Path,
    receipt: Path,
    declared: str | None,
    default_name: str,
    errors: list[str],
) -> Path | None:
    try:
        if declared:
            return resolve_under_root(root, Path(declared.replace("\\", "/")))
        return resolve_under_root(root, Path("raw") / default_name)
    except FailClosedError as exc:
        errors.append(str(exc))
        return None
    except Exception as exc:  # noqa: BLE001 - fail closed on unexpected path errors
        errors.append(f"raw path error for {receipt}: {exc}")
        return None


def _verify_exit_consistency(fields: Mapping[str, str], errors: list[str]) -> None:
    codes = parse_command_exit_codes(fields.get("command_exit_codes"))
    if "command_exit_codes" in fields and codes is None:
        errors.append("command_exit_codes is not a list of integers")
        return
    if codes is None or "exit_code" not in fields:
        return
    try:
        header = int(fields["exit_code"])
    except ValueError:
        errors.append("exit_code is not an integer")
        return
    expected = aggregate_exit(codes)
    if header != expected:
        errors.append(
            f"exit_code={header} does not match aggregate of command_exit_codes={codes} ({expected})"
        )
    if truthy(fields.get("expected_red")) and header == 0:
        errors.append("expected_red=true but exit_code=0 (RED rewritten to success)")


def _verify_command_counts(fields: Mapping[str, str], errors: list[str]) -> None:
    raw_commands = fields.get("commands")
    if raw_commands is None:
        return
    try:
        loaded = json.loads(raw_commands)
    except json.JSONDecodeError:
        errors.append("commands is not JSON")
        return
    if not isinstance(loaded, list) or not loaded:
        errors.append("commands must be a non-empty JSON list")
        return
    codes = parse_command_exit_codes(fields.get("command_exit_codes"))
    if not codes:
        errors.append("command_exit_codes must be a non-empty integer list")
        return
    if len(loaded) != len(codes):
        errors.append(
            f"commands count {len(loaded)} != command_exit_codes count {len(codes)}"
        )


def _print_verify(results: Sequence[VerifyResult]) -> int:
    failed = 0
    for item in results:
        status = "VERIFY_OK" if item.ok else "VERIFY_FAIL_CLOSED"
        print(f"{status} {item.path}")
        for error in item.errors:
            print(f"  - {error}")
        if not item.ok:
            failed += 1
    return FAIL_CLOSED_EXIT if failed else 0


def _cmd_run(args: argparse.Namespace) -> int:
    spec_path = args.spec.resolve()
    try:
        spec = load_spec(spec_path)
        executed = (
            f'python -B "{Path(__file__).resolve().as_posix()}" '
            f'run --spec "{spec_path.as_posix()}"'
        )
        result = run_spec(spec, executed_command=executed)
        dest = write_receipt(result, Path(str(spec["output_dir"])))
    except FailClosedError as exc:
        print(str(exc), file=sys.stderr)
        return FAIL_CLOSED_EXIT
    print(f"wrote {dest.as_posix()}")
    print(f"exit_code={result.exit_code}")
    print(f"command_exit_codes={json.dumps(result.command_exit_codes)}")
    print(f"stdout_sha256={result.stdout_sha256}")
    print(f"stderr_sha256={result.stderr_sha256}")
    return int(result.exit_code)


def _cmd_verify(args: argparse.Namespace) -> int:
    root = args.output_root.resolve() if args.output_root else None
    results = [verify_receipt(path, output_root=root) for path in args.receipts]
    return _print_verify(results)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fail-closed receipt runner (Wave 1 G1)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run_p = sub.add_parser("run")
    run_p.add_argument("--spec", type=Path, required=True)
    run_p.set_defaults(func=_cmd_run)
    ver = sub.add_parser("verify")
    ver.add_argument("receipts", nargs="+", type=Path)
    ver.add_argument("--output-root", type=Path)
    ver.set_defaults(func=_cmd_verify)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
