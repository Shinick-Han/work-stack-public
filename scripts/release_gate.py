#!/usr/bin/env python3
"""Fail-closed release identity, selection, and immutable-bundle checks."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Iterable, Mapping, Sequence


FULL_SHA_RE = re.compile(r"\A[0-9a-f]{40}\Z")
VERSION_RE = re.compile(r"\A(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
VERSION_LINE_RE = re.compile(r'^__version__\s*=\s*"([^"]+)"\s*$', re.MULTILINE)
RECEIPT_NAME = "build-receipt.json"
DIST_MANIFEST_NAME = "frozen-dist-manifest.json"
BUNDLE_VERIFIER_NAME = "Test-WorkStackReleaseBundle.ps1"


class ReleaseGateError(RuntimeError):
    pass


def _canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_bytes(_canonical_json(value))
    temporary.replace(path)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ReleaseGateError(f"invalid JSON file {path}: {error}") from error


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(repo: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *arguments], cwd=repo, text=True, capture_output=True, encoding="utf-8", errors="replace"
    )
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise ReleaseGateError(f"git {' '.join(arguments)} failed: {detail}")
    return result


def load_path_policy(path: Path) -> dict[str, Any]:
    policy = read_json(path)
    if not isinstance(policy, dict) or policy.get("schema_version") != 1:
        raise ReleaseGateError("release path policy must use schema_version 1")
    always = policy.get("always_gates")
    optional = policy.get("optional_gates")
    rules = policy.get("rules")
    if not all(isinstance(item, list) for item in (always, optional, rules)):
        raise ReleaseGateError("release path policy gate lists and rules must be arrays")
    gates = always + optional
    if any(not isinstance(gate, str) or not gate for gate in gates) or len(set(gates)) != len(gates):
        raise ReleaseGateError("release path policy gates must be unique non-empty strings")
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) != {"patterns", "gates"}:
            raise ReleaseGateError("each release path rule must contain only patterns and gates")
        if not rule["patterns"] or any(not isinstance(pattern, str) for pattern in rule["patterns"]):
            raise ReleaseGateError("release path rule patterns must be a non-empty string array")
        if any(gate not in gates for gate in rule["gates"]):
            raise ReleaseGateError("release path rule names an unknown gate")
    return policy


def classify_paths(paths: Iterable[str], policy: Mapping[str, Any]) -> dict[str, Any]:
    normalized = sorted({path.replace("\\", "/").lstrip("./") for path in paths if path.strip()})
    gate_names = list(policy["always_gates"]) + list(policy["optional_gates"])
    gates = {gate: gate in policy["always_gates"] for gate in gate_names}
    reasons: dict[str, list[str]] = {gate: [] for gate in gate_names}
    unknown: list[str] = []
    for path in normalized:
        matched = False
        for rule in policy["rules"]:
            if any(fnmatch.fnmatchcase(path, pattern) for pattern in rule["patterns"]):
                matched = True
                for gate in rule["gates"]:
                    gates[gate] = True
                    reasons[gate].append(path)
        if not matched:
            unknown.append(path)
    if unknown:
        for gate in gates:
            gates[gate] = True
            reasons[gate].extend(unknown)
    return {
        "schema_version": 1,
        "changed_paths": normalized,
        "unknown_paths": unknown,
        "gates": gates,
        "reasons": {gate: sorted(set(values)) for gate, values in reasons.items()},
    }


def changed_paths(repo: Path, base: str, head: str) -> list[str]:
    if not FULL_SHA_RE.fullmatch(head):
        raise ReleaseGateError("head must be a full lowercase commit SHA")
    if base and not FULL_SHA_RE.fullmatch(base):
        raise ReleaseGateError("base must be a full lowercase commit SHA")
    if not base or base == "0" * 40:
        parent = _git(repo, "rev-parse", f"{head}^").stdout.strip()
        base = parent
    ancestry = _git(repo, "merge-base", "--is-ancestor", base, head, check=False)
    if ancestry.returncode != 0:
        raise ReleaseGateError("release base must be an ancestor of the candidate head")
    output = _git(repo, "diff", "--name-only", "--diff-filter=ACDMRTUXB", base, head).stdout
    return [line.strip() for line in output.splitlines() if line.strip()]


def validate_candidate(repo: Path, sha: str, version: str, default_ref: str) -> dict[str, Any]:
    if not FULL_SHA_RE.fullmatch(sha):
        raise ReleaseGateError("candidate must be a full lowercase commit SHA")
    if not VERSION_RE.fullmatch(version):
        raise ReleaseGateError("version must be canonical X.Y.Z without prefixes or leading zeroes")
    resolved = _git(repo, "rev-parse", f"{sha}^{{commit}}").stdout.strip()
    if resolved != sha:
        raise ReleaseGateError("candidate commit resolution mismatch")
    reachable = _git(repo, "merge-base", "--is-ancestor", sha, default_ref, check=False)
    if reachable.returncode != 0:
        raise ReleaseGateError(f"candidate is not reachable from protected default ref {default_ref}")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    if head != sha:
        raise ReleaseGateError(f"checked-out HEAD {head} does not match candidate {sha}")
    status = _git(repo, "status", "--porcelain=v1", "--untracked-files=all").stdout
    if status:
        raise ReleaseGateError("release checkout is dirty")
    version_path = repo / "workstack" / "__init__.py"
    match = VERSION_LINE_RE.search(version_path.read_text(encoding="utf-8"))
    source_version = match.group(1) if match else None
    if source_version != version:
        raise ReleaseGateError(f"source version {source_version!r} does not match requested {version!r}")
    tree = _git(repo, "rev-parse", f"{sha}^{{tree}}").stdout.strip()
    submodules = _git(repo, "submodule", "status", "--recursive").stdout.splitlines()
    if any(line.startswith(("-", "+", "U")) for line in submodules):
        raise ReleaseGateError("release checkout contains uninitialized or drifting submodules")
    return {
        "schema_version": 1,
        "candidate_sha": sha,
        "tree_sha": tree,
        "version": version,
        "default_ref": default_ref,
    }


def _tree_entries(root: Path) -> list[dict[str, Any]]:
    if not root.is_dir():
        raise ReleaseGateError(f"tree root is not a directory: {root}")
    entries: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise ReleaseGateError(f"frozen trees may not contain symlinks: {path}")
        if path.is_file():
            entries.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "size": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
            )
    if not entries:
        raise ReleaseGateError("frozen tree must contain at least one file")
    return entries


def freeze_tree(root: Path, output: Path) -> dict[str, Any]:
    root = root.resolve()
    output = output.resolve()
    if output == root or root in output.parents:
        raise ReleaseGateError("frozen manifest must be written outside the frozen tree")
    manifest = {"schema_version": 1, "files": _tree_entries(root)}
    write_json(output, manifest)
    return manifest


def verify_tree(root: Path, manifest_path: Path) -> dict[str, Any]:
    manifest = read_json(manifest_path)
    expected = {"schema_version": 1, "files": _tree_entries(root.resolve())}
    if manifest != expected:
        raise ReleaseGateError("frozen tree mismatch: path, size, or SHA-256 changed")
    return expected


def _expected_bundle_names(version: str) -> set[str]:
    installer = f"WorkStack-Setup-{version}.ps1"
    return {
        installer,
        f"{installer}.sha256",
        "workstack-update.json",
        DIST_MANIFEST_NAME,
        BUNDLE_VERIFIER_NAME,
        RECEIPT_NAME,
    }


def write_build_receipt(bundle: Path, candidate: Mapping[str, Any]) -> dict[str, Any]:
    version = candidate.get("version")
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise ReleaseGateError("candidate receipt has an invalid canonical version")
    expected = _expected_bundle_names(version) - {RECEIPT_NAME}
    actual = {path.name for path in bundle.iterdir() if path.is_file()}
    if actual != expected:
        raise ReleaseGateError(f"release payload set mismatch: expected {sorted(expected)}, got {sorted(actual)}")
    payloads = []
    for name in sorted(actual):
        path = bundle / name
        payloads.append({"name": name, "size": path.stat().st_size, "sha256": sha256_file(path)})
    receipt = {
        "schema_version": 1,
        "candidate_sha": candidate.get("candidate_sha"),
        "tree_sha": candidate.get("tree_sha"),
        "version": version,
        "payloads": payloads,
    }
    if not FULL_SHA_RE.fullmatch(str(receipt["candidate_sha"])) or not FULL_SHA_RE.fullmatch(
        str(receipt["tree_sha"])
    ):
        raise ReleaseGateError("candidate receipt must contain full source commit and tree SHAs")
    write_json(bundle / RECEIPT_NAME, receipt)
    return receipt


def verify_bundle(bundle: Path) -> dict[str, Any]:
    receipt = read_json(bundle / RECEIPT_NAME)
    if not isinstance(receipt, dict) or receipt.get("schema_version") != 1:
        raise ReleaseGateError("build receipt must use schema_version 1")
    version = receipt.get("version")
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise ReleaseGateError("build receipt version is invalid")
    actual_names = {path.name for path in bundle.iterdir() if path.is_file()}
    expected_names = _expected_bundle_names(version)
    if actual_names != expected_names:
        raise ReleaseGateError("release bundle file set does not match the build receipt contract")
    payloads = receipt.get("payloads")
    if not isinstance(payloads, list):
        raise ReleaseGateError("build receipt payloads must be an array")
    expected_payload_names = expected_names - {RECEIPT_NAME}
    recorded_names: set[str] = set()
    for entry in payloads:
        if not isinstance(entry, dict) or set(entry) != {"name", "size", "sha256"}:
            raise ReleaseGateError("build receipt payload entry is malformed")
        name = entry["name"]
        if name in recorded_names or name not in expected_payload_names:
            raise ReleaseGateError("build receipt payload names are duplicated or unexpected")
        recorded_names.add(name)
        path = bundle / name
        if path.stat().st_size != entry["size"] or sha256_file(path) != entry["sha256"]:
            raise ReleaseGateError(f"payload hash mismatch: {name}")
    if recorded_names != expected_payload_names:
        raise ReleaseGateError("build receipt omits a required payload")
    installer_name = f"WorkStack-Setup-{version}.ps1"
    installer_digest = sha256_file(bundle / installer_name)
    checksum_text = (bundle / f"{installer_name}.sha256").read_text(encoding="utf-8")
    if checksum_text != f"{installer_digest}  {installer_name}\n":
        raise ReleaseGateError("checksum sidecar does not name the exact installer digest")
    update = read_json(bundle / "workstack-update.json")
    if update.get("version") != version or update.get("installer", {}).get("name") != installer_name:
        raise ReleaseGateError("update manifest does not identify the exact release installer")
    if update.get("installer", {}).get("sha256") != installer_digest:
        raise ReleaseGateError("update manifest installer hash mismatch")
    return receipt


def evaluate_policy(selection: Mapping[str, Any], results: Mapping[str, str]) -> dict[str, Any]:
    gates = selection.get("gates")
    if not isinstance(gates, dict) or any(not isinstance(value, bool) for value in gates.values()):
        raise ReleaseGateError("selection gates must be a boolean object")
    required = ["release_build", *sorted(gate for gate, selected in gates.items() if selected)]
    blocking = [f"{job}:{results.get(job, 'missing')}" for job in required if results.get(job) != "success"]
    return {
        "schema_version": 1,
        "required_jobs": required,
        "results": {job: results.get(job, "missing") for job in sorted(set(results) | set(required))},
        "blocking_results": blocking,
        "allow_publish": not blocking,
    }


def _parse_results(values: Sequence[str]) -> dict[str, str]:
    results: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ReleaseGateError("results must use job=result syntax")
        name, result = value.split("=", 1)
        if not name or not result or name in results:
            raise ReleaseGateError("result names must be unique and non-empty")
        results[name] = result
    return results


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    classify = commands.add_parser("classify")
    classify.add_argument("--repo", type=Path, default=Path("."))
    classify.add_argument("--policy", type=Path, default=Path("quality/release-path-policy.json"))
    classify.add_argument("--base", default="")
    classify.add_argument("--head", required=True)
    classify.add_argument("--paths-file", type=Path)
    classify.add_argument("--output", type=Path, required=True)
    candidate = commands.add_parser("validate-candidate")
    candidate.add_argument("--repo", type=Path, default=Path("."))
    candidate.add_argument("--sha", required=True)
    candidate.add_argument("--version", required=True)
    candidate.add_argument("--default-ref", required=True)
    candidate.add_argument("--output", type=Path, required=True)
    freeze = commands.add_parser("freeze-tree")
    freeze.add_argument("--root", type=Path, required=True)
    freeze.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify-tree")
    verify.add_argument("--root", type=Path, required=True)
    verify.add_argument("--manifest", type=Path, required=True)
    receipt = commands.add_parser("write-receipt")
    receipt.add_argument("--bundle", type=Path, required=True)
    receipt.add_argument("--candidate", type=Path, required=True)
    bundle = commands.add_parser("verify-bundle")
    bundle.add_argument("--bundle", type=Path, required=True)
    policy = commands.add_parser("evaluate-policy")
    policy.add_argument("--selection", type=Path, required=True)
    policy.add_argument("--result", action="append", default=[])
    policy.add_argument("--output", type=Path, required=True)
    policy.add_argument("--github-output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "classify":
        if args.paths_file:
            paths = args.paths_file.read_text(encoding="utf-8").splitlines()
        else:
            paths = changed_paths(args.repo.resolve(), args.base, args.head)
        value = classify_paths(paths, load_path_policy(args.policy))
        write_json(args.output, value)
    elif args.command == "validate-candidate":
        value = validate_candidate(args.repo.resolve(), args.sha, args.version, args.default_ref)
        write_json(args.output, value)
    elif args.command == "freeze-tree":
        value = freeze_tree(args.root, args.output)
    elif args.command == "verify-tree":
        value = verify_tree(args.root, args.manifest)
    elif args.command == "write-receipt":
        value = write_build_receipt(args.bundle, read_json(args.candidate))
    elif args.command == "verify-bundle":
        value = verify_bundle(args.bundle)
    else:
        value = evaluate_policy(read_json(args.selection), _parse_results(args.result))
        write_json(args.output, value)
        if args.github_output:
            with args.github_output.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(f"allow_publish={'true' if value['allow_publish'] else 'false'}\n")
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReleaseGateError as error:
        print(f"release gate failed: {error}", file=sys.stderr)
        raise SystemExit(1)
