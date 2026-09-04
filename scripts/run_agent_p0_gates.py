#!/usr/bin/env python3
"""Trusted Work Stack Agent P0 Oracle gate runner (O1 seed).

The runner is executed from a separately pinned Oracle checkout:

    python -I <trusted-oracle>/scripts/run_agent_p0_gates.py --oracle-root <trusted-oracle> --implementation-root <candidate> --conformance-root <test-candidate> --implementation-packet <packet.json> --conformance-packet <test-packet.json>

    python -I <trusted-oracle>/scripts/run_agent_p0_gates.py --oracle-root <trusted-oracle> --candidate-root <candidate> --gate G30 --base <sha> --candidate <sha>

Every Oracle asset (packet/terminal schemas, directive pins, probes, Skill validator) is loaded
from --oracle-root, never from the candidate. The runner derives all facts from Git and from
process exits; candidate-generated claims and tests are never trusted. Ownership comes from the
normalized `git diff --raw -z --no-abbrev --find-renames <base>...<head>` tuples, including
status, modes, object IDs and both rename paths. Skipped and expected-failure tests fail the
gate. Receipts are canonical JSON (sorted keys, compact separators, one trailing LF) and contain
no timestamps, durations, temporary paths or ports. Symlink (120000), gitlink (160000),
case-fold collisions, unknown change types and non-canonical paths are rejected.

The M0 interface manifest is independently loaded and digest-bound to both packets. Its ownership,
module rules, complete isolated ABI and contract fixture projection are enforced before a receipt
can pass. G30 ownership rejects Oracle/workflow edits because no packet allowlist exists for the
composition gate.

Exit codes: 0 pass (receipt written), 1 gate failure (receipt plus machine-readable failure
packet on stderr), 2 usage or protocol error (no receipt).
"""

from __future__ import annotations

import argparse
import base64
import ast
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ORACLE_DIR = Path("quality") / "agent-p0-oracle"
MANIFEST_NAME = "manifest.v1.json"
PACKET_SCHEMA_NAME = "packet-schema.v1.json"
TERMINAL_SCHEMA_NAME = "terminal-schema.v1.json"
WORKER_DIRECTIVE_NAME = "worker-directive.v1.txt"
SUPERVISOR_DIRECTIVE_NAME = "supervisor-directive.v1.txt"
VALIDATE_SKILL_NAME = "validate_skill.py"
PROBES_SUBDIR = "probes"
CONTRACT_MODULE_PATH = "workstack/agent_cli_contract.py"
SKILL_ROOT_PATH = "integrations/agent-skill/work-stack"
RECEIPT_VERSION = 1
G30_PROTECTED_PATHS = ("quality/agent-p0-oracle/**", ".github/**")
DEFAULT_TIMEOUT_SECONDS = 900

GIT_TIMEOUT_SECONDS = 120
PROBE_TIMEOUT_SECONDS = 300

RAN_RE = re.compile(r"^Ran (\d+) tests?", re.MULTILINE)
SUMMARY_COUNT_RE = re.compile(r"(failures|errors|skipped|expected failures|unexpected successes)=(\d+)")

PROBE_FILES: Dict[str, str] = {
    "authority-preflight": "probe_authority_preflight.py",
    "idempotent-replay": "probe_idempotent_replay.py",
    "no-local-fallback": "probe_no_local_fallback.py",
    "output-canary": "probe_output_canary.py",
}
GATE_PROBES: Dict[str, List[str]] = {
    # These bootstrap probes prove the Oracle's sentinel families against pinned
    # mutants.  They are deliberately not executed against product modules: the
    # public M0 ABI exposes create/admit/render boundaries, not the probe-only
    # functions used by the mutants.  Real lane behavior is exercised by the
    # independently authored conformance module and the cross-lane TE/G30 suite.
    "G10": [],
    "G21-A": [],
    "G21-B1": [],
    "G21-B2": [],
    "G21-C1": [],
    "G21-C2": [],
    "G21-C3": [],
    "G21-D": [],
    "G30": [],
}
PROBE_VIOLATION_IDS: Dict[str, str] = {
    "P0-STORE-BEFORE-PREFLIGHT": "authority-preflight",
    "P0-PREFLIGHT-TREE-MUTATION": "authority-preflight",
    "P0-PREFLIGHT-EXCEPTION": "authority-preflight",
    "P0-RETRY-LOOP": "idempotent-replay",
    "P0-FRESH-KEY": "idempotent-replay",
    "P0-DUPLICATE-WORKLOG": "idempotent-replay",
    "P0-REPLAY-EXCEPTION": "idempotent-replay",
    "P0-NO-FALLBACK": "no-local-fallback",
    "P0-ONLINE-RETRY-BOUND": "no-local-fallback",
    "P0-DISPATCH-EXCEPTION": "no-local-fallback",
    "P0-STDOUT-SINGLE-JSON": "output-canary",
    "P0-SECRET-CANARY-STDOUT": "output-canary",
    "P0-SECRET-CANARY-STDERR": "output-canary",
    "P0-EMIT-EXCEPTION": "output-canary",
}
LANE_TEST_MODULES: Dict[str, str] = {
    "G10": "tests.test_agent_cli_contract",
    "G21-A": "tests.test_agent_transport_contract",
    "G21-B1": "tests.test_agent_authority_contract",
    "G21-B2": "tests.test_agent_local_backend_contract",
    "G21-C1": "tests.test_agent_command_status_contract",
    "G21-C2": "tests.test_agent_command_context_contract",
    "G21-C3": "tests.test_agent_command_checkpoint_contract",
    "G21-D": "tests.test_agent_skill_contract",
}
G30_TEST_ARGV: List[List[str]] = [
    ["-m", "unittest", "tests.test_agent_cli_contract", "-v"],
    ["-m", "unittest", "tests.test_agent_transport_contract", "-v"],
    ["-m", "unittest", "tests.test_agent_authority_contract", "-v"],
    ["-m", "unittest", "tests.test_agent_local_backend_contract", "-v"],
    ["-m", "unittest", "tests.test_agent_command_status_contract", "-v"],
    ["-m", "unittest", "tests.test_agent_command_context_contract", "-v"],
    ["-m", "unittest", "tests.test_agent_command_checkpoint_contract", "-v"],
    ["-m", "unittest", "tests.test_agent_skill_contract", "-v"],
    ["-m", "unittest", "tests.test_agent_cli_e2e_contract", "-v"],
    ["-m", "unittest", "discover", "-s", "tests/oracle/agent_p0", "-p", "test_*.py", "-v"],
    [
        "-m",
        "unittest",
        "tests.test_cli_characterization",
        "tests.test_agent_apply",
        "tests.test_intent_mutations_v1",
        "-v",
    ],
    ["scripts/quality_gate.py", "check", "--root", "."],
]

STATUS_TO_CHANGE_TYPE = {
    "A": "add",
    "M": "modify",
    "D": "delete",
    "R": "rename",
    "C": "copy",
    "T": "typechange",
}
BAD_MODES = {"120000", "160000"}
EXPECTED_LANE_IDS = frozenset(
    ["M0", "Q0", "O1", "O2", "T0", "A", "B1", "B2", "C1", "C2", "C3", "D", "TE", "I1", "I2", "I3"]
)
EXPECTED_LANE_GATES = {
    "M0": None,
    "Q0": None,
    "O1": None,
    "O2": "G10",
    "T0": "G10",
    "A": "G21-A",
    "B1": "G21-B1",
    "B2": "G21-B2",
    "C1": "G21-C1",
    "C2": "G21-C2",
    "C3": "G21-C3",
    "D": "G21-D",
    "TE": "G30",
    "I1": None,
    "I2": None,
    "I3": None,
}
PRODUCTION_LANE_BY_GATE = {
    "G10": "O2",
    "G21-A": "A",
    "G21-B1": "B1",
    "G21-B2": "B2",
    "G21-C1": "C1",
    "G21-C2": "C2",
    "G21-C3": "C3",
    "G21-D": "D",
}

ABI_PROBE_SCRIPT = r'''
import dataclasses, importlib, inspect, json, sys
sys.path.insert(0, sys.argv[1])
module = importlib.import_module(sys.argv[2])

def annotation(value):
    if value is inspect.Signature.empty:
        return None
    if isinstance(value, str):
        text = value
    elif value is None or value is type(None):
        text = "null"
    elif isinstance(value, type):
        text = (value.__module__ + "." if value.__module__ not in ("builtins", "__main__") else "") + value.__qualname__
    else:
        text = str(value).replace("typing.", "")
    return text.replace(module.__name__ + ".", "").replace("NoneType", "null").replace("None", "null").replace(" ", "")

def callable_shape(value, drop_self=False):
    signature = inspect.signature(value)
    params = []
    for item in signature.parameters.values():
        if drop_self and item.name in ("self", "cls"):
            continue
        params.append({
            "name": item.name,
            "kind": item.kind.name,
            "type": annotation(item.annotation),
            "required": item.default is inspect.Parameter.empty,
        })
    return {"params": params, "returns": annotation(signature.return_annotation)}

requested = json.loads(sys.argv[3])
result = {"all": list(getattr(module, "__all__", [])), "symbols": {}}
for name, info in requested.items():
    kind = info["kind"]
    value = getattr(module, name, None)
    if kind == "callable":
        result["symbols"][name] = callable_shape(value) if callable(value) else None
    elif kind == "dataclass":
        if isinstance(value, type) and dataclasses.is_dataclass(value):
            params = getattr(value, "__dataclass_params__")
            result["symbols"][name] = {
                "fields": [
                    {
                        "name": field.name,
                        "type": annotation(field.type),
                        "kw_only": bool(field.kw_only),
                        "required": field.default is dataclasses.MISSING and field.default_factory is dataclasses.MISSING,
                    }
                    for field in dataclasses.fields(value)
                ],
                "frozen": bool(params.frozen),
            }
        else:
            result["symbols"][name] = None
    elif kind == "protocol":
        if isinstance(value, type) and bool(getattr(value, "_is_protocol", False)):
            result["symbols"][name] = {method: callable_shape(getattr(value, method), True) for method in info["methods"]}
        else:
            result["symbols"][name] = None
    else:
        result["symbols"][name] = value
sys.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
'''


class OracleError(RuntimeError):
    """Bounded runner failure with a stable, path-free message."""


class TerminalError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Canonical bytes and digests
# ---------------------------------------------------------------------------


def canonical_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def canonical_lf_text_bytes(data: bytes, origin: str) -> bytes:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise OracleError("%s is not valid UTF-8" % origin) from error
    normalized = text.replace("\r\n", "\n")
    if "\r" in normalized:
        raise OracleError("%s contains a bare carriage return" % origin)
    return normalized.encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_sha256(value: Any) -> str:
    return sha256_hex(canonical_json_bytes(value))


def load_json_bytes(data: bytes, origin: str) -> Any:
    def reject_duplicates(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key %r" % key)
            result[key] = value
        return result

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise OracleError("%s is not valid UTF-8" % origin) from error
    try:
        return json.loads(text, object_pairs_hook=reject_duplicates)
    except ValueError as error:
        raise OracleError("%s is not valid JSON: %s" % (origin, error)) from error


# ---------------------------------------------------------------------------
# Minimal JSON Schema evaluation (the subset pinned by the two schema files)
# ---------------------------------------------------------------------------


def _type_matches(expected: str, instance: Any) -> bool:
    if expected == "object":
        return isinstance(instance, dict)
    if expected == "array":
        return isinstance(instance, list)
    if expected == "string":
        return isinstance(instance, str)
    if expected == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    if expected == "number":
        return isinstance(instance, (int, float)) and not isinstance(instance, bool)
    if expected == "boolean":
        return isinstance(instance, bool)
    if expected == "null":
        return instance is None
    return False


def evaluate_schema(schema: Any, instance: Any, path: str = "$") -> List[str]:
    errors: List[str] = []
    if not isinstance(schema, dict):
        return ["%s: invalid schema node" % path]
    if "oneOf" in schema:
        matches = sum(1 for sub in schema["oneOf"] if not evaluate_schema(sub, instance, path))
        if matches != 1:
            errors.append("%s: expected exactly one oneOf variant to match, found %d" % (path, matches))
        return errors
    expected_type = schema.get("type")
    if expected_type is not None:
        names = expected_type if isinstance(expected_type, list) else [expected_type]
        if not any(_type_matches(name, instance) for name in names):
            errors.append("%s: expected type %s, found %s" % (path, names, type(instance).__name__))
            return errors
    if "const" in schema and instance != schema["const"]:
        errors.append("%s: expected const %r, found %r" % (path, schema["const"], instance))
    if "enum" in schema and instance not in schema["enum"]:
        errors.append("%s: %r is not one of %r" % (path, instance, schema["enum"]))
    if isinstance(instance, str):
        if "pattern" in schema and re.search(schema["pattern"], instance) is None:
            errors.append("%s: %r does not match pattern %r" % (path, instance, schema["pattern"]))
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append("%s: shorter than minLength %d" % (path, schema["minLength"]))
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append("%s: longer than maxLength %d" % (path, schema["maxLength"]))
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append("%s: below minimum %r" % (path, schema["minimum"]))
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append("%s: above maximum %r" % (path, schema["maximum"]))
    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append("%s: fewer than minItems %d" % (path, schema["minItems"]))
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append("%s: more than maxItems %d" % (path, schema["maxItems"]))
        if schema.get("uniqueItems"):
            serialized = [canonical_json_bytes(item) for item in instance]
            if len(serialized) != len(set(serialized)):
                errors.append("%s: items are not unique" % path)
        item_schema = schema.get("items")
        if item_schema is not None:
            for index, item in enumerate(instance):
                errors.extend(evaluate_schema(item_schema, item, "%s[%d]" % (path, index)))
    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append("%s: missing required field %r" % (path, key))
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        for key in sorted(instance):
            child_path = "%s.%s" % (path, key)
            if key in properties:
                errors.extend(evaluate_schema(properties[key], instance[key], child_path))
            elif additional is False:
                errors.append("%s: unknown field" % child_path)
            elif isinstance(additional, dict):
                errors.extend(evaluate_schema(additional, instance[key], child_path))
    return errors


# ---------------------------------------------------------------------------
# Oracle assets, packet and terminal validation
# ---------------------------------------------------------------------------


def oracle_asset_path(oracle_root: Path, *parts: str) -> Path:
    path = oracle_root.joinpath(ORACLE_DIR, *parts)
    if not path.is_file():
        raise OracleError("Oracle asset missing from oracle root: %s" % "/".join(parts))
    return path


def load_schema(oracle_root: Path, name: str) -> Any:
    return load_json_bytes(oracle_asset_path(oracle_root, name).read_bytes(), name)


def _manifest_lane_map(manifest: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    lanes = manifest.get("ownership", {}).get("lanes")
    if not isinstance(lanes, list):
        raise OracleError("M0 interface manifest ownership lanes are missing")
    if any(not isinstance(item, dict) or not isinstance(item.get("lane"), str) or not item["lane"] for item in lanes):
        raise OracleError("M0 interface manifest contains an invalid lane entry")
    lane_ids = [item["lane"] for item in lanes]
    duplicates = sorted({lane_id for lane_id in lane_ids if lane_ids.count(lane_id) > 1})
    if duplicates:
        raise OracleError("M0 interface manifest contains duplicate lane ids: %s" % ", ".join(duplicates))
    observed = set(lane_ids)
    missing = sorted(EXPECTED_LANE_IDS - observed)
    unknown = sorted(observed - EXPECTED_LANE_IDS)
    if missing:
        raise OracleError("M0 interface manifest is missing lane ids: %s" % ", ".join(missing))
    if unknown:
        raise OracleError("M0 interface manifest contains unknown lane ids: %s" % ", ".join(unknown))
    result = {item["lane"]: item for item in lanes}
    for lane_id, expected_gate in EXPECTED_LANE_GATES.items():
        if result[lane_id].get("gate") != expected_gate:
            raise OracleError("M0 interface manifest lane %s has an unexpected gate label" % lane_id)
    return result


def load_interface_manifest(oracle_root: Path, explicit_path: Optional[Path]) -> Tuple[Dict[str, Any], bytes]:
    path = explicit_path if explicit_path is not None else oracle_root / ORACLE_DIR / MANIFEST_NAME
    if not path.is_file():
        raise OracleError("M0 interface manifest is missing")
    raw = path.read_bytes()
    manifest = load_json_bytes(raw, "M0 interface manifest")
    canonical = canonical_json_bytes(manifest)
    checkout_canonical = canonical[:-1] + b"\r\n"
    if not isinstance(manifest, dict) or raw not in (canonical, checkout_canonical):
        raise OracleError("M0 interface manifest is not canonical JSON")
    required = {"schema_version", "abi", "digest_recipes", "module_rules", "ownership"}
    if manifest.get("schema_version") != 1 or not required.issubset(manifest):
        raise OracleError("M0 interface manifest is structurally incomplete")
    recipe = manifest.get("digest_recipes", {}).get("contract")
    if not isinstance(recipe, dict) or re.fullmatch(r"[0-9a-f]{64}", str(recipe.get("expected_sha256", ""))) is None:
        raise OracleError("M0 interface manifest has an unsupported contract digest recipe")
    projection = manifest.get("digest_recipes", {}).get("contract_fixture_projection")
    if not isinstance(projection, list) or not projection or len(projection) != len(set(projection)):
        raise OracleError("M0 contract fixture projection is missing or invalid")
    if any(not isinstance(name, str) or name not in manifest for name in projection):
        raise OracleError("M0 contract fixture projection names an absent section")
    expected_fixture = canonical_json_bytes({name: manifest[name] for name in projection})
    if sha256_hex(expected_fixture) != recipe["expected_sha256"]:
        raise OracleError("M0 contract fixture projection digest is internally inconsistent")
    if not isinstance(manifest.get("abi", {}).get("modules"), dict):
        raise OracleError("M0 interface manifest ABI modules are missing")
    _manifest_lane_map(manifest)
    return manifest, canonical


def manifest_contract_fixture_bytes(manifest: Dict[str, Any]) -> bytes:
    projection = manifest["digest_recipes"]["contract_fixture_projection"]
    return canonical_json_bytes({name: manifest[name] for name in projection})


def _module_name(relative_path: str) -> Optional[str]:
    if not relative_path.endswith(".py"):
        return None
    return relative_path[:-3].replace("/", ".")


def _manifest_exports(manifest: Dict[str, Any], module_name: str) -> List[str]:
    module = manifest["abi"]["modules"].get(module_name)
    if not isinstance(module, dict):
        return []
    names = set()
    exports = module.get("exports")
    if isinstance(exports, list):
        names.update(str(value) for value in exports)
    elif isinstance(exports, dict):
        names.update(str(value) for value in exports)
    for key in ("callables", "dataclasses", "protocols", "values"):
        value = module.get(key)
        if isinstance(value, dict):
            names.update(str(name) for name in value)
    return sorted(names)


def _packet_manifest_errors(packet: Dict[str, Any], manifest: Dict[str, Any], manifest_digest: str) -> List[str]:
    errors: List[str] = []
    if packet.get("interface_manifest_sha256") != manifest_digest:
        errors.append("packet interface_manifest_sha256 does not equal the pinned M0 manifest")
    if packet.get("contract_sha256") != sha256_hex(manifest_contract_fixture_bytes(manifest)):
        errors.append("packet contract_sha256 does not equal the M0 fixture projection")
    gate = _packet_gate(packet)
    lane_map = _manifest_lane_map(manifest)
    production_lane_id = PRODUCTION_LANE_BY_GATE.get(gate)
    lane = lane_map.get(production_lane_id) if production_lane_id is not None else None
    role = packet.get("role")
    owned = list(packet.get("owned_paths", []))
    required_outputs = list(packet.get("required_outputs", []))
    if role == "production":
        if lane is None:
            errors.append("M0 has no production lane for %s" % gate)
        else:
            expected_owned = list(lane.get("owned_paths", []))
            if owned != expected_owned or required_outputs != expected_owned:
                errors.append("packet ownership does not equal the M0 production lane")
            if packet.get("lane") is not None and packet.get("lane") != lane.get("lane"):
                errors.append("packet lane does not equal the M0 lane identifier")
        if len(required_outputs) == 1:
            module_name = _module_name(required_outputs[0])
            if module_name is not None:
                expected_exports = _manifest_exports(manifest, module_name)
                if sorted(packet.get("required_exports", [])) != expected_exports:
                    errors.append("packet required_exports do not equal the M0 ABI")
                rules = manifest.get("module_rules", {}).get(module_name)
                if isinstance(rules, dict):
                    if sorted(packet.get("forbidden_imports", [])) != sorted(rules.get("forbidden_imports", [])):
                        errors.append("packet forbidden_imports do not equal the M0 module rules")
                    if sorted(packet.get("forbidden_calls", [])) != sorted(rules.get("forbidden_calls", [])):
                        errors.append("packet forbidden_calls do not equal the M0 module rules")
    elif role == "conformance":
        expected = lane.get("paired_conformance") if isinstance(lane, dict) else None
        if expected is None or owned != [expected] or required_outputs != [expected]:
            errors.append("conformance packet ownership does not equal the M0 paired conformance path")
        if packet.get("lane") is not None and expected is not None:
            directly_owned = [
                item for item in lane_map.values() if list(item.get("owned_paths", [])) == [expected]
            ]
            expected_lane_id = directly_owned[0]["lane"] if len(directly_owned) == 1 else lane.get("lane")
            if packet.get("lane") != expected_lane_id:
                errors.append("conformance packet lane does not equal the M0 lane identifier")
    return errors


def canonical_path_errors(path: str, label: str) -> List[str]:
    errors: List[str] = []
    if path.startswith("/") or path.endswith("/") or "\\" in path:
        errors.append("%s %r must be a repo-relative POSIX path" % (label, path))
    segments = path.split("/")
    if any(segment == ".." for segment in segments):
        errors.append("%s %r must not contain '..' segments" % (label, path))
    if any(segment == "" for segment in segments):
        errors.append("%s %r must not contain empty segments" % (label, path))
    return errors


def packet_errors(oracle_root: Path, packet: Any) -> List[str]:
    errors = evaluate_schema(load_schema(oracle_root, PACKET_SCHEMA_NAME), packet)
    if isinstance(packet, dict):
        for field in ("owned_paths", "required_outputs", "declared_context_paths", "forbidden_paths"):
            for value in packet.get(field, []):
                if isinstance(value, str):
                    errors.extend(canonical_path_errors(value, "%s entry" % field))
    return errors


def terminal_errors(oracle_root: Path, terminal: Any) -> List[str]:
    return evaluate_schema(load_schema(oracle_root, TERMINAL_SCHEMA_NAME), terminal)


def parse_terminal_bytes(raw: bytes) -> Any:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise TerminalError("terminal record is not valid UTF-8") from error

    def reject_duplicates(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key %r" % key)
            result[key] = value
        return result

    stripped = text.lstrip()
    if not stripped:
        raise TerminalError("terminal record is empty")
    decoder = json.JSONDecoder(object_pairs_hook=reject_duplicates)
    try:
        terminal, index = decoder.raw_decode(stripped)
    except ValueError as error:
        raise TerminalError("terminal record is not one JSON object: %s" % error) from error
    if stripped[index:].strip():
        raise TerminalError("terminal record has trailing content after the JSON object")
    if not isinstance(terminal, dict):
        raise TerminalError("terminal record must be a JSON object")
    return terminal


def evidence_digest_ok(terminal: Dict[str, Any]) -> bool:
    findings = terminal.get("findings")
    digest = terminal.get("finding_sha256")
    if not isinstance(findings, list) or not isinstance(digest, str):
        return False
    return sha256_hex(canonical_json_bytes(findings)) == digest


# ---------------------------------------------------------------------------
# Git access
# ---------------------------------------------------------------------------


def git_bytes(root: Path, *arguments: str) -> bytes:
    environment = dict(os.environ)
    for key in [name for name in environment if name.startswith("PYTHON")]:
        environment.pop(key, None)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=GIT_TIMEOUT_SECONDS,
            env=environment,
        )
    except subprocess.TimeoutExpired as error:
        raise OracleError("git %s timed out" % " ".join(arguments)) from error
    if completed.returncode != 0:
        raise OracleError("git %s failed" % " ".join(arguments))
    return completed.stdout


def git_text(root: Path, *arguments: str) -> str:
    return git_bytes(root, *arguments).decode("utf-8", "replace").strip()


def head_sha(root: Path) -> str:
    return git_text(root, "rev-parse", "HEAD")


def candidate_identity(
    root: Path,
    base_sha: str,
    candidate_sha: str,
    label: str,
) -> Tuple[List[str], Dict[str, Any]]:
    errors: List[str] = []
    observed_head = head_sha(root)
    parent_line = git_text(root, "rev-list", "--parents", "-n", "1", "HEAD")
    parts = parent_line.split()
    parent_count = max(len(parts) - 1, 0)
    parent_sha = parts[1] if len(parts) >= 2 else ""
    dirty = git_text(root, "status", "--porcelain") != ""
    facts = {
        "root": label,
        "head": observed_head,
        "candidate": candidate_sha,
        "base": base_sha,
        "parent_count": parent_count,
        "parent": parent_sha,
        "dirty": dirty,
    }
    if observed_head != candidate_sha:
        errors.append("%s HEAD does not equal candidate commit" % label)
    if observed_head == base_sha:
        errors.append("%s candidate commit equals base" % label)
    if parent_count != 1:
        errors.append("%s candidate must have exactly one parent, found %d" % (label, parent_count))
    elif parent_sha != base_sha:
        errors.append("%s candidate parent does not equal base" % label)
    if dirty:
        errors.append("%s candidate tree is dirty" % label)
    return errors, facts


# ---------------------------------------------------------------------------
# Raw diff derivation and ownership
# ---------------------------------------------------------------------------


def git_diff_raw(root: Path, base_sha: str, candidate_sha: str) -> bytes:
    return git_bytes(
        root, "diff", "--raw", "-z", "--no-abbrev", "--find-renames", "%s...%s" % (base_sha, candidate_sha)
    )


def parse_diff_raw(data: bytes) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    tokens = data.split(b"\0")
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == b"":
            break
        if not token.startswith(b":"):
            raise OracleError("malformed raw diff entry")
        header = token[1:].decode("utf-8", "replace")
        fields = header.split(" ")
        if len(fields) != 5:
            raise OracleError("malformed raw diff header")
        old_mode, new_mode, old_oid, new_oid, status_field = fields
        status = status_field[0]
        score = int(status_field[1:]) if len(status_field) > 1 else 0
        index += 1
        if index >= len(tokens):
            raise OracleError("raw diff entry is missing its path")
        first_path = tokens[index].decode("utf-8")
        index += 1
        old_path: Optional[str] = None
        path = first_path
        if status in ("R", "C"):
            if index >= len(tokens):
                raise OracleError("rename entry is missing its second path")
            second_path = tokens[index].decode("utf-8")
            index += 1
            # git emits the pre-image (source) path first and the post-image (destination)
            # path second, matching the `git diff --raw` source => destination ordering.
            old_path = first_path
            path = second_path
        entries.append(
            {
                "path": path,
                "old_path": old_path,
                "status": status,
                "score": score,
                "old_mode": old_mode,
                "new_mode": new_mode,
                "old_oid": old_oid,
                "new_oid": new_oid,
            }
        )
    return entries


def diff_digest(entries: List[Dict[str, Any]]) -> str:
    ordered = sorted(entries, key=lambda entry: (entry["path"], entry["old_path"] or ""))
    return sha256_hex(canonical_json_bytes(ordered))


def changed_paths(entries: List[Dict[str, Any]]) -> List[str]:
    paths = set()
    for entry in entries:
        paths.add(entry["path"])
        if entry["old_path"]:
            paths.add(entry["old_path"])
    return sorted(paths)


def path_matches(path: str, pattern: str) -> bool:
    if pattern == "**":
        return True
    if pattern.endswith("/**"):
        prefix = pattern[:-3]
        return path == prefix or path.startswith(prefix + "/")
    pattern_segments = pattern.split("/")
    path_segments = path.split("/")

    def match(segments: List[str], remaining: List[str]) -> bool:
        if not segments:
            return not remaining
        head = segments[0]
        if head == "**":
            return any(match(segments[1:], remaining[index:]) for index in range(len(remaining) + 1))
        if not remaining:
            return False
        if any(character in head for character in "*?["):
            if not fnmatch.fnmatchcase(remaining[0], head):
                return False
        elif head != remaining[0]:
            return False
        return match(segments[1:], remaining[1:])

    return match(pattern_segments, path_segments)


def check_ownership(
    entries: List[Dict[str, Any]],
    owned_paths: List[str],
    forbidden_paths: List[str],
    allowed_change_types: List[str],
) -> List[str]:
    violations: List[str] = []
    seen_casefold: Dict[str, str] = {}
    for entry in entries:
        path = entry["path"]
        change_type = STATUS_TO_CHANGE_TYPE.get(entry["status"])
        if change_type is None:
            violations.append("unknown change status %s for %s" % (entry["status"], path))
            continue
        if change_type not in allowed_change_types:
            violations.append("change type %s is not allowed for %s" % (change_type, path))
        for mode in (entry["old_mode"], entry["new_mode"]):
            if mode in BAD_MODES:
                violations.append("unsupported git mode %s for %s" % (mode, path))
        checked = [path]
        if entry["old_path"]:
            checked.append(entry["old_path"])
        for candidate in checked:
            violations.extend(canonical_path_errors(candidate, "changed path"))
            if not any(path_matches(candidate, pattern) for pattern in owned_paths):
                violations.append("changed path %s is outside owned_paths" % candidate)
            for pattern in forbidden_paths:
                if path_matches(candidate, pattern):
                    violations.append("changed path %s matches forbidden_paths %s" % (candidate, pattern))
            folded = candidate.casefold()
            if folded in seen_casefold and seen_casefold[folded] != candidate:
                violations.append("case-fold path collision between %s and %s" % (candidate, seen_casefold[folded]))
            else:
                seen_casefold[folded] = candidate
    return violations


# ---------------------------------------------------------------------------
# Forbidden import and call scan
# ---------------------------------------------------------------------------


def _dotted_name(node: ast.AST) -> Optional[str]:
    parts: List[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _module_matches(name: str, forbidden: List[str]) -> bool:
    return any(name == entry or name.startswith(entry + ".") for entry in forbidden)


def scan_python_source(
    relative_path: str,
    source: str,
    forbidden_imports: List[str],
    forbidden_calls: List[str],
) -> List[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        return ["%s: syntax error: %s" % (relative_path, error)]
    violations: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _module_matches(alias.name, forbidden_imports):
                    violations.append("%s: forbidden import %s" % (relative_path, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module and _module_matches(node.module, forbidden_imports):
                violations.append("%s: forbidden import %s" % (relative_path, node.module))
        elif isinstance(node, ast.Call):
            chain = _dotted_name(node.func)
            if chain is None:
                continue
            for entry in forbidden_calls:
                if entry.endswith(".*"):
                    root_name = entry[:-2]
                    if chain == root_name or chain.startswith(root_name + "."):
                        violations.append("%s: forbidden call %s" % (relative_path, chain))
                elif chain == entry:
                    violations.append("%s: forbidden call %s" % (relative_path, chain))
    return violations


def changed_python_sources(
    root: Path,
    candidate_sha: str,
    entries: List[Dict[str, Any]],
) -> List[Tuple[str, str]]:
    sources: List[Tuple[str, str]] = []
    for entry in entries:
        if entry["status"] not in ("A", "M", "T", "R", "C"):
            continue
        if not entry["path"].endswith(".py"):
            continue
        blob = git_bytes(root, "show", "%s:%s" % (candidate_sha, entry["path"]))
        sources.append((entry["path"], blob.decode("utf-8", "replace")))
    return sorted(sources)


# ---------------------------------------------------------------------------
# Bounded subprocess execution
# ---------------------------------------------------------------------------


def child_environment(data_root: Path) -> Dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("PYTHON")}
    environment["PYTHONHASHSEED"] = "0"
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["TZ"] = "UTC"
    environment["TMP"] = str(data_root)
    environment["TEMP"] = str(data_root)
    environment["TMPDIR"] = str(data_root)
    return environment


def _run_bounded(
    argv: List[str],
    root: Path,
    timeout: int,
    environment: Dict[str, str],
) -> Tuple[int, str]:
    try:
        completed = subprocess.run(
            argv,
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            env=environment,
        )
        return completed.returncode, completed.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired as error:
        raw_output = error.stdout if isinstance(error.stdout, bytes) else b""
        return 124, raw_output.decode("utf-8", "replace")


def run_test_command(root: Path, arguments: List[str], timeout: int, environment: Dict[str, str]) -> Dict[str, Any]:
    exit_code, output = _run_bounded([sys.executable] + list(arguments), root, timeout, environment)
    facts: Dict[str, Any] = {
        "argv": ["python"] + list(arguments),
        "exit": exit_code,
        "ran": 0,
        "ok": False,
        "failures": 0,
        "errors": 0,
        "skipped": 0,
        "expected_failures": 0,
        "unexpected_successes": 0,
        "timed_out": exit_code == 124,
    }
    ran_match = RAN_RE.search(output)
    if ran_match:
        facts["ran"] = int(ran_match.group(1))
    lines = [line for line in output.splitlines() if line.strip()]
    status_line = lines[-1] if lines else ""
    for name, count in SUMMARY_COUNT_RE.findall(status_line):
        facts[name.replace(" ", "_")] = int(count)
    facts["ok"] = status_line.startswith("OK") and exit_code == 0
    return facts


def run_script_command(root: Path, arguments: List[str], timeout: int, environment: Dict[str, str]) -> Dict[str, Any]:
    exit_code, _output = _run_bounded([sys.executable] + list(arguments), root, timeout, environment)
    return {
        "argv": ["python"] + list(arguments),
        "exit": exit_code,
        "ok": exit_code == 0,
        "timed_out": exit_code == 124,
    }


def skipped_count(facts: Dict[str, Any]) -> int:
    return facts["skipped"] + facts["expected_failures"] + facts["unexpected_successes"]


# ---------------------------------------------------------------------------
# Probes and Skill validation
# ---------------------------------------------------------------------------


def run_probe(
    oracle_root: Path,
    probe_name: str,
    subject_path: Path,
    working_root: Path,
    report_dir: Path,
    environment: Dict[str, str],
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    probe_file = oracle_asset_path(oracle_root, PROBES_SUBDIR, PROBE_FILES[probe_name])
    report_path = report_dir / ("%s.json" % probe_name)
    argv = [
        sys.executable,
        "-I",
        str(probe_file),
        "--subject",
        str(subject_path),
        "--report",
        str(report_path),
    ]
    exit_code, _output = _run_bounded(argv, working_root, PROBE_TIMEOUT_SECONDS, environment)
    verdict = "missing_report"
    violations: List[Dict[str, Any]] = []
    if report_path.is_file():
        try:
            report = load_json_bytes(report_path.read_bytes(), "probe report")
            if isinstance(report, dict):
                verdict = str(report.get("verdict", "invalid_report"))
                raw_violations = report.get("violations", [])
                if isinstance(raw_violations, list):
                    violations = [item for item in raw_violations if isinstance(item, dict)]
        except OracleError:
            verdict = "unreadable_report"
    facts = {"probe": probe_name, "subject": subject_path.name, "verdict": verdict, "exit": exit_code}
    failure = None
    if exit_code != 0 or verdict != "pass":
        first_violation = violations[0] if violations else None
        failure = {
            "oracle_id": str(first_violation.get("id", "P0-PROBE-ERROR")) if first_violation else "P0-PROBE-ERROR",
            "observed": first_violation if first_violation else facts,
            "expected": {"verdict": "pass"},
            "repair_owner": "implementation",
            "forbidden_repair_paths": ["quality/agent-p0-oracle/**"],
        }
    return facts, failure


def run_skill_validation(
    oracle_root: Path,
    skill_dir: Path,
    report_dir: Path,
    environment: Dict[str, str],
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    validator = oracle_asset_path(oracle_root, VALIDATE_SKILL_NAME)
    report_path = report_dir / "skill-validation.json"
    argv = [sys.executable, "-I", str(validator), str(skill_dir), "--report", str(report_path)]
    exit_code, _output = _run_bounded(argv, oracle_root, PROBE_TIMEOUT_SECONDS, environment)
    violations: List[Dict[str, Any]] = []
    valid = False
    if report_path.is_file():
        try:
            report = load_json_bytes(report_path.read_bytes(), "skill validation report")
            if isinstance(report, dict):
                valid = bool(report.get("valid"))
                raw_violations = report.get("violations", [])
                if isinstance(raw_violations, list):
                    violations = [item for item in raw_violations if isinstance(item, dict)]
        except OracleError:
            valid = False
    facts = {"valid": valid, "exit": exit_code, "violations": violations}
    failure = None
    if exit_code != 0 or not valid:
        first_violation = violations[0] if violations else None
        failure = {
            "oracle_id": str(first_violation.get("id", "P0-SKILL-INVALID")) if first_violation else "P0-SKILL-INVALID",
            "observed": first_violation if first_violation else facts,
            "expected": {"valid": True},
            "repair_owner": "implementation",
            "forbidden_repair_paths": ["quality/agent-p0-oracle/**"],
        }
    return facts, failure


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


def build_composition(
    work_dir: Path,
    impl_root: Path,
    impl_sha: str,
    conf_root: Path,
    conf_sha: str,
    conf_entries: List[Dict[str, Any]],
) -> Path:
    composition = work_dir / "composition"
    composition.mkdir(parents=True, exist_ok=True)
    archive = subprocess.Popen(
        ["git", "archive", "--format=tar", impl_sha],
        cwd=str(impl_root),
        stdout=subprocess.PIPE,
    )
    try:
        with tarfile.open(fileobj=archive.stdout, mode="r|") as tar:
            for member in tar:
                segments = [segment for segment in member.name.split("/") if segment]
                if any(segment == ".." for segment in segments):
                    raise OracleError("composition refuses unsafe archive entry")
                target = composition.joinpath(*segments)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    raise OracleError("composition refuses non-regular archive entry")
                stream = tar.extractfile(member)
                if stream is None:
                    raise OracleError("composition cannot read archive entry")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(stream.read())
    finally:
        if archive.stdout is not None:
            archive.stdout.close()
        if archive.wait() != 0:
            raise OracleError("git archive failed for implementation candidate")
    for entry in conf_entries:
        if entry["status"] == "D":
            target = composition / entry["path"]
            if target.is_file():
                target.unlink()
            continue
        if entry["status"] in ("R", "C") and entry["old_path"]:
            stale = composition / entry["old_path"]
            if stale.is_file():
                stale.unlink()
        data = git_bytes(conf_root, "show", "%s:%s" % (conf_sha, entry["path"]))
        target = composition / entry["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return composition


# ---------------------------------------------------------------------------
# Check pipeline
# ---------------------------------------------------------------------------


class Check:
    def __init__(self, check_id: str, passed: bool, facts: Dict[str, Any], failure: Optional[Dict[str, Any]] = None):
        self.check_id = check_id
        self.passed = passed
        self.facts = facts
        self.failure = failure

    def receipt_entry(self) -> Dict[str, Any]:
        return {
            "id": self.check_id,
            "exit": 0 if self.passed else 1,
            "output_sha256": canonical_sha256(self.facts),
        }


class Pipeline:
    def __init__(self, invariant: Optional[str]):
        self.invariant = invariant
        self.checks: List[Check] = []
        self.stop = False

    def selected(self, key: str) -> bool:
        if self.invariant is None:
            return True
        return key == self.invariant or PROBE_VIOLATION_IDS.get(self.invariant) == key

    def record(
        self,
        key: str,
        check_id: str,
        passed: bool,
        facts: Dict[str, Any],
        failure: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not self.selected(key):
            return
        self.checks.append(Check(check_id, passed, facts, failure if not passed else None))
        if not passed:
            self.stop = True


def _failure(
    check_id: str,
    facts: Dict[str, Any],
    owned_repair_paths: List[str],
    owner: str = "implementation",
) -> Dict[str, Any]:
    violations = facts.get("violations") or []
    first = violations[0] if violations else None
    packet: Dict[str, Any] = {
        "expected": {"pass": True},
        "repair_owner": owner,
        "owned_repair_paths": list(owned_repair_paths),
        "forbidden_repair_paths": ["quality/agent-p0-oracle/**"],
    }
    if first is not None and isinstance(first, dict):
        packet["oracle_id"] = str(first.get("id", check_id))
        packet["observed"] = first
    else:
        packet["oracle_id"] = check_id
        packet["observed"] = facts
    return packet


def _detail_failure(
    check_id: str,
    details: List[str],
    owned_repair_paths: List[str],
    owner: str = "implementation",
) -> Dict[str, Any]:
    return _failure(
        check_id,
        {"violations": [{"id": check_id, "detail": detail} for detail in details]},
        owned_repair_paths,
        owner,
    )


def _coherence_errors(
    implementation_packet: Dict[str, Any],
    conformance_packet: Dict[str, Any],
) -> List[str]:
    errors: List[str] = []
    for field in ("base_sha", "oracle_seed_sha", "contract_sha256"):
        if implementation_packet.get(field) != conformance_packet.get(field):
            errors.append("packets disagree on %s" % field)
    impl_gate = _packet_gate(implementation_packet)
    conf_gate = _packet_gate(conformance_packet)
    if impl_gate is None or conf_gate is None or impl_gate != conf_gate:
        errors.append("packets must share exactly one G10 or G21 admission gate")
    if implementation_packet.get("role") != "production":
        errors.append("implementation packet role must be production")
    if conformance_packet.get("role") != "conformance":
        errors.append("conformance packet role must be conformance")
    shared_patterns = set(implementation_packet.get("owned_paths", [])) & set(conformance_packet.get("owned_paths", []))
    if shared_patterns:
        errors.append("owned_paths overlap between packets")
    if implementation_packet.get("packet_id") == conformance_packet.get("packet_id"):
        errors.append("packets must have distinct packet_id values")
    return errors


def _packet_gate(packet: Dict[str, Any]) -> Optional[str]:
    lane_gates = [gate for gate in packet.get("required_gates", []) if gate.startswith("G21-")]
    if len(lane_gates) == 1:
        return lane_gates[0]
    if not lane_gates and "G10" in packet.get("required_gates", []):
        return "G10"
    return None


def _required_output_errors(root: Path, candidate_sha: str, packet: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    for relative in packet.get("required_outputs", []):
        try:
            git_bytes(root, "cat-file", "-e", "%s:%s" % (candidate_sha, relative))
        except OracleError:
            errors.append("required output %s is absent" % relative)
    return errors


def _required_export_errors(root: Path, packet: Dict[str, Any], environment: Dict[str, str]) -> List[str]:
    outputs = list(packet.get("required_outputs", []))
    required = list(packet.get("required_exports", []))
    if len(outputs) != 1 or not required:
        return []
    module_name = _module_name(outputs[0])
    if module_name is None:
        return []
    script = (
        "import importlib,json,sys;sys.path.insert(0,sys.argv[1]);"
        "m=importlib.import_module(sys.argv[2]);"
        "print(json.dumps(sorted(n for n in sys.argv[3:] if hasattr(m,n)),separators=(',',':')))"
    )
    exit_code, output = _run_bounded(
        [sys.executable, "-I", "-c", script, str(root), module_name, *required], root, PROBE_TIMEOUT_SECONDS, environment
    )
    if exit_code != 0:
        return ["required output module could not be imported in isolation"]
    try:
        observed = json.loads(output.strip())
    except ValueError:
        return ["required export probe returned invalid output"]
    missing = sorted(set(required) - set(observed))
    return ["required export %s is absent" % name for name in missing]


def _normalized_annotation(value: Any) -> Optional[str]:
    if value is None:
        return None
    return str(value).replace("typing.", "").replace("NoneType", "null").replace("None", "null").replace(" ", "")


def _expected_callable_shape(specification: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "params": [
            {
                "name": item["name"],
                "kind": "KEYWORD_ONLY",
                "type": _normalized_annotation(item.get("type")),
                "required": bool(item.get("required", True)),
            }
            for item in specification.get("params", [])
        ],
        "returns": _normalized_annotation(specification.get("returns")),
    }


def inspect_required_abi(
    root: Path,
    packet: Dict[str, Any],
    manifest: Dict[str, Any],
    environment: Dict[str, str],
) -> Tuple[Dict[str, Any], List[str]]:
    outputs = list(packet.get("required_outputs", []))
    if packet.get("role") != "production" or len(outputs) != 1:
        return {"applicable": False}, []
    module_name = _module_name(outputs[0])
    module_spec = manifest["abi"]["modules"].get(module_name) if module_name is not None else None
    if not isinstance(module_spec, dict):
        return {"applicable": False}, []

    expected_names = _manifest_exports(manifest, module_name)
    request: Dict[str, Dict[str, Any]] = {}
    expected_symbols: Dict[str, Any] = {}
    exports = module_spec.get("exports")
    if isinstance(exports, dict):
        for name, specification in exports.items():
            if "params" in specification:
                request[name] = {"kind": "callable"}
                expected_symbols[name] = _expected_callable_shape(specification)
            elif "value" in specification:
                request[name] = {"kind": "value"}
                expected_symbols[name] = specification["value"]
            else:
                request[name] = {"kind": "value"}
                expected_symbols[name] = specification
    for name, specification in module_spec.get("callables", {}).items():
        request[name] = {"kind": "callable"}
        expected_symbols[name] = _expected_callable_shape(specification)
    for name, specification in module_spec.get("dataclasses", {}).items():
        request[name] = {"kind": "dataclass"}
        expected_symbols[name] = {
            "fields": [
                {
                    "name": item["name"],
                    "type": _normalized_annotation(item.get("type")),
                    "kw_only": bool(specification.get("keyword_only")),
                    "required": bool(item.get("required", specification.get("all_constructor_fields_required", False))),
                }
                for item in specification.get("fields", [])
            ],
            "frozen": bool(specification.get("frozen")),
        }
    for name, specification in module_spec.get("protocols", {}).items():
        methods = specification.get("methods", {})
        request[name] = {"kind": "protocol", "methods": sorted(methods)}
        expected_symbols[name] = {method: _expected_callable_shape(methods[method]) for method in sorted(methods)}
    for name, value in module_spec.get("values", {}).items():
        request[name] = {"kind": "value"}
        expected_symbols[name] = value

    exit_code, output = _run_bounded(
        [sys.executable, "-I", "-c", ABI_PROBE_SCRIPT, str(root), str(module_name), json.dumps(request, separators=(",", ":"))],
        root,
        PROBE_TIMEOUT_SECONDS,
        environment,
    )
    if exit_code != 0:
        return {"applicable": True, "module": module_name, "imported": False}, ["ABI module could not be imported in isolation"]
    try:
        observed = json.loads(output)
    except ValueError:
        return {"applicable": True, "module": module_name, "imported": False}, ["ABI probe returned invalid JSON"]
    errors: List[str] = []
    if observed.get("all") != expected_names:
        errors.append("module __all__ does not equal the M0 export list")
    if observed.get("symbols") != expected_symbols:
        errors.append("module signatures, dataclass fields, Protocol methods, or constant values do not equal M0")
    facts = {
        "applicable": True,
        "module": module_name,
        "expected_sha256": canonical_sha256({"all": expected_names, "symbols": expected_symbols}),
        "observed_sha256": canonical_sha256(observed),
        "equal": not errors,
    }
    return facts, errors


def contract_fixture_observation(root: Path, environment: Dict[str, str]) -> Tuple[str, bytes]:
    script = (
        "import base64,hashlib,sys;sys.path.insert(0,sys.argv[1]);"
        "from workstack.agent_cli_contract import contract_fixture_bytes;"
        "value=contract_fixture_bytes();"
        "assert isinstance(value,bytes);sys.stdout.write(hashlib.sha256(value).hexdigest()+':'+base64.b64encode(value).decode('ascii'))"
    )
    exit_code, output = _run_bounded(
        [sys.executable, "-I", "-c", script, str(root)], root, PROBE_TIMEOUT_SECONDS, environment
    )
    digest, separator, encoded = output.strip().partition(":")
    if exit_code != 0 or separator != ":" or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise OracleError("contract_fixture_bytes could not be evaluated in isolation")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except ValueError as error:
        raise OracleError("contract_fixture_bytes probe returned invalid bytes") from error
    return digest, raw


def contract_fixture_observation_bounded(root: Path) -> Tuple[str, bytes]:
    with tempfile.TemporaryDirectory(prefix="p0-contract-digest-") as temporary:
        data_root = Path(temporary) / "data"
        data_root.mkdir()
        return contract_fixture_observation(root, child_environment(data_root))


def run_pairwise_gate(
    oracle_root: Path,
    implementation_root: Path,
    conformance_root: Path,
    implementation_packet_path: Path,
    conformance_packet_path: Path,
    interface_manifest_path: Optional[Path],
    invariant: Optional[str],
) -> Tuple[Dict[str, Any], int, List[Check]]:
    implementation_packet = load_json_bytes(implementation_packet_path.read_bytes(), "implementation packet")
    conformance_packet = load_json_bytes(conformance_packet_path.read_bytes(), "conformance packet")
    if canonical_json_bytes(implementation_packet) != implementation_packet_path.read_bytes():
        raise OracleError("implementation packet file is not canonical JSON")
    if canonical_json_bytes(conformance_packet) != conformance_packet_path.read_bytes():
        raise OracleError("conformance packet file is not canonical JSON")
    manifest, manifest_bytes = load_interface_manifest(oracle_root, interface_manifest_path)
    manifest_digest = sha256_hex(manifest_bytes)

    pipeline = Pipeline(invariant)
    impl_owned = list(implementation_packet.get("owned_paths", []))
    conf_owned = list(conformance_packet.get("owned_paths", []))
    impl_candidate = head_sha(implementation_root)
    conf_candidate = head_sha(conformance_root)

    errors = packet_errors(oracle_root, implementation_packet)
    if not errors:
        errors.extend(_packet_manifest_errors(implementation_packet, manifest, manifest_digest))
    pipeline.record(
        "packet-schema-implementation",
        "packet-schema-implementation",
        not errors,
        {"packet": "implementation", "errors": errors},
        None if not errors else _failure("packet-schema-implementation", {"errors": errors}, impl_owned),
    )
    errors = packet_errors(oracle_root, conformance_packet)
    if not errors:
        errors.extend(_packet_manifest_errors(conformance_packet, manifest, manifest_digest))
    pipeline.record(
        "packet-schema-conformance",
        "packet-schema-conformance",
        not errors,
        {"packet": "conformance", "errors": errors},
        None if not errors else _failure("packet-schema-conformance", {"errors": errors}, conf_owned, "conformance"),
    )
    if pipeline.stop:
        return _finalize_pairwise(pipeline, implementation_packet, conformance_packet, impl_candidate, conf_candidate, [], [])

    coherence = _coherence_errors(implementation_packet, conformance_packet)
    pipeline.record(
        "cross-packet-coherence",
        "cross-packet-coherence",
        not coherence,
        {"errors": coherence},
        None if not coherence else _failure("cross-packet-coherence", {"errors": coherence}, impl_owned),
    )

    worker_digest = sha256_hex(
        canonical_lf_text_bytes(
            oracle_asset_path(oracle_root, WORKER_DIRECTIVE_NAME).read_bytes(),
            WORKER_DIRECTIVE_NAME,
        )
    )
    supervisor_digest = sha256_hex(
        canonical_lf_text_bytes(
            oracle_asset_path(oracle_root, SUPERVISOR_DIRECTIVE_NAME).read_bytes(),
            SUPERVISOR_DIRECTIVE_NAME,
        )
    )
    directive_matches = (
        worker_digest == implementation_packet.get("worker_directive_sha256")
        and worker_digest == conformance_packet.get("worker_directive_sha256")
    )
    pipeline.record(
        "directive-digests",
        "directive-digests",
        directive_matches,
        {
            "worker_directive_sha256": worker_digest,
            "supervisor_directive_sha256": supervisor_digest,
            "matches_packet": directive_matches,
        },
        None if directive_matches else _failure(
            "directive-digests",
            {"violations": [{"id": "directive-digests", "detail": "worker directive digest does not equal packet worker_directive_sha256"}]},
            impl_owned,
        ),
    )

    observed_seed = head_sha(oracle_root)
    seed_matches = observed_seed == implementation_packet.get("oracle_seed_sha")
    pipeline.record(
        "oracle-seed",
        "oracle-seed",
        seed_matches,
        {"expected": implementation_packet.get("oracle_seed_sha"), "observed": observed_seed, "matches": seed_matches},
        None if seed_matches else _failure(
            "oracle-seed",
            {"violations": [{"id": "oracle-seed", "detail": "oracle root HEAD does not equal packet oracle_seed_sha"}]},
            impl_owned,
        ),
    )
    if pipeline.stop:
        return _finalize_pairwise(pipeline, implementation_packet, conformance_packet, impl_candidate, conf_candidate, [], [])

    base_sha = str(implementation_packet.get("base_sha"))
    identity_errors, identity_facts = candidate_identity(implementation_root, base_sha, impl_candidate, "implementation")
    pipeline.record(
        "identity-implementation",
        "identity-implementation",
        not identity_errors,
        identity_facts,
        None if not identity_errors else _failure(
            "identity-implementation",
            {"violations": [{"id": "identity-implementation", "detail": detail} for detail in identity_errors]},
            impl_owned,
        ),
    )
    identity_errors_conf, identity_facts_conf = candidate_identity(conformance_root, base_sha, conf_candidate, "conformance")
    pipeline.record(
        "identity-conformance",
        "identity-conformance",
        not identity_errors_conf,
        identity_facts_conf,
        None if not identity_errors_conf else _failure(
            "identity-conformance",
            {"violations": [{"id": "identity-conformance", "detail": detail} for detail in identity_errors_conf]},
            conf_owned,
            "conformance",
        ),
    )
    if pipeline.stop:
        return _finalize_pairwise(pipeline, implementation_packet, conformance_packet, impl_candidate, conf_candidate, [], [])

    implementation_entries = parse_diff_raw(git_diff_raw(implementation_root, base_sha, impl_candidate))
    conformance_entries = parse_diff_raw(git_diff_raw(conformance_root, base_sha, conf_candidate))

    violations = check_ownership(
        implementation_entries,
        impl_owned,
        list(implementation_packet["forbidden_paths"]),
        list(implementation_packet["allowed_change_types"]),
    )
    pipeline.record(
        "ownership-implementation",
        "ownership-implementation",
        not violations,
        {"root": "implementation", "changed_paths": changed_paths(implementation_entries), "violations": violations},
        None if not violations else _failure(
            "ownership-implementation",
            {"violations": [{"id": "ownership-implementation", "detail": detail} for detail in violations]},
            impl_owned,
        ),
    )
    violations_conf = check_ownership(
        conformance_entries,
        conf_owned,
        list(conformance_packet["forbidden_paths"]),
        list(conformance_packet["allowed_change_types"]),
    )
    pipeline.record(
        "ownership-conformance",
        "ownership-conformance",
        not violations_conf,
        {"root": "conformance", "changed_paths": changed_paths(conformance_entries), "violations": violations_conf},
        None if not violations_conf else _failure(
            "ownership-conformance",
            {"violations": [{"id": "ownership-conformance", "detail": detail} for detail in violations_conf]},
            conf_owned,
            "conformance",
        ),
    )
    overlap = sorted(set(changed_paths(implementation_entries)) & set(changed_paths(conformance_entries)))
    pipeline.record(
        "composition-disjoint",
        "composition-disjoint",
        not overlap,
        {"overlapping_paths": overlap},
        None if not overlap else _failure(
            "composition-disjoint",
            {"violations": [{"id": "composition-disjoint", "detail": "implementation and conformance changed paths overlap"}]},
            impl_owned,
        ),
    )
    if pipeline.stop:
        return _finalize_pairwise(pipeline, implementation_packet, conformance_packet, impl_candidate, conf_candidate, implementation_entries, conformance_entries)

    import_violations: List[str] = []
    for relative, source in changed_python_sources(implementation_root, impl_candidate, implementation_entries):
        import_violations.extend(
            scan_python_source(relative, source, list(implementation_packet["forbidden_imports"]), list(implementation_packet["forbidden_calls"]))
        )
    pipeline.record(
        "import-scan-implementation",
        "import-scan-implementation",
        not import_violations,
        {"root": "implementation", "violations": import_violations},
        None if not import_violations else _failure(
            "import-scan-implementation",
            {"violations": [{"id": "import-scan-implementation", "detail": detail} for detail in import_violations]},
            impl_owned,
        ),
    )
    import_violations_conf: List[str] = []
    for relative, source in changed_python_sources(conformance_root, conf_candidate, conformance_entries):
        import_violations_conf.extend(
            scan_python_source(relative, source, list(conformance_packet["forbidden_imports"]), list(conformance_packet["forbidden_calls"]))
        )
    pipeline.record(
        "import-scan-conformance",
        "import-scan-conformance",
        not import_violations_conf,
        {"root": "conformance", "violations": import_violations_conf},
        None if not import_violations_conf else _failure(
            "import-scan-conformance",
            {"violations": [{"id": "import-scan-conformance", "detail": detail} for detail in import_violations_conf]},
            conf_owned,
            "conformance",
        ),
    )

    output_errors = _required_output_errors(implementation_root, impl_candidate, implementation_packet)
    output_errors.extend(_required_output_errors(conformance_root, conf_candidate, conformance_packet))
    pipeline.record(
        "required-outputs", "required-outputs", not output_errors, {"errors": output_errors},
        None if not output_errors else _detail_failure("required-outputs", output_errors, impl_owned),
    )
    if pipeline.stop:
        return _finalize_pairwise(pipeline, implementation_packet, conformance_packet, impl_candidate, conf_candidate, implementation_entries, conformance_entries)

    gate = _packet_gate(implementation_packet) or ""
    with tempfile.TemporaryDirectory(prefix="p0-oracle-") as temporary:
        data_root = Path(temporary) / "data"
        data_root.mkdir()
        environment = child_environment(data_root)
        try:
            composition = build_composition(
                Path(temporary),
                implementation_root,
                impl_candidate,
                conformance_root,
                conf_candidate,
                conformance_entries,
            )
            pipeline.record("composition", "composition", True, {"built": True})
        except OracleError:
            pipeline.record(
                "composition",
                "composition",
                False,
                {"built": False},
                _failure(
                    "composition",
                    {"violations": [{"id": "composition", "detail": "disposable composition could not be built"}]},
                    impl_owned,
                ),
            )
            return _finalize_pairwise(pipeline, implementation_packet, conformance_packet, impl_candidate, conf_candidate, implementation_entries, conformance_entries)

        timeout = int(implementation_packet.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)
        export_errors = _required_export_errors(composition, implementation_packet, environment)
        pipeline.record(
            "required-exports", "required-exports", not export_errors, {"errors": export_errors},
            None if not export_errors else _detail_failure("required-exports", export_errors, impl_owned),
        )
        abi_facts, abi_errors = inspect_required_abi(composition, implementation_packet, manifest, environment)
        pipeline.record(
            "abi-equality", "abi-equality", not abi_errors, abi_facts,
            None if not abi_errors else _detail_failure("abi-equality", abi_errors, impl_owned),
        )
        contract_errors: List[str] = []
        contract_digest: Optional[str] = None
        fixture_equal = False
        expected_fixture = manifest_contract_fixture_bytes(manifest)
        try:
            contract_digest, observed_fixture = contract_fixture_observation(composition, environment)
            fixture_equal = observed_fixture == expected_fixture
            if not fixture_equal:
                contract_errors.append("contract fixture bytes do not equal the M0 projection")
            if contract_digest != implementation_packet.get("contract_sha256"):
                contract_errors.append("contract fixture digest does not equal packet contract_sha256")
        except OracleError as error:
            contract_errors.append(str(error))
        pipeline.record(
            "contract-fixture-bytes", "contract-fixture-bytes", fixture_equal,
            {"equal": fixture_equal, "expected_sha256": sha256_hex(expected_fixture), "observed_sha256": contract_digest},
            None if fixture_equal else _detail_failure("contract-fixture-bytes", ["contract fixture bytes differ from M0"], impl_owned),
        )
        pipeline.record(
            "contract-digest", "contract-digest", not contract_errors,
            {"present": contract_digest is not None, "digest": contract_digest, "errors": contract_errors},
            None if not contract_errors else _detail_failure("contract-digest", contract_errors, impl_owned),
        )
        if pipeline.stop:
            return _finalize_pairwise(pipeline, implementation_packet, conformance_packet, impl_candidate, conf_candidate, implementation_entries, conformance_entries)

        module = LANE_TEST_MODULES.get(gate)
        if module is None:
            pipeline.record(
                "tests-composition",
                "tests-composition",
                False,
                {"argv": [], "exit": 1},
                _failure(
                    "tests-composition",
                    {"violations": [{"id": "tests-composition", "detail": "lane gate has no declared conformance module"}]},
                    impl_owned,
                ),
            )
        else:
            facts = run_test_command(composition, ["-m", "unittest", module, "-v"], timeout, environment)
            test_ok = facts["exit"] == 0 and facts["ok"] and skipped_count(facts) == 0
            pipeline.record(
                "tests-composition",
                "tests-composition",
                test_ok,
                facts,
                None if test_ok else _failure(
                    "tests-composition",
                    {"violations": [{"id": "tests-composition", "detail": "lane conformance test failed or was skipped"}]},
                    impl_owned,
                ),
            )

            report_dir = Path(temporary) / "probe-reports"
            report_dir.mkdir()
            for probe_name in GATE_PROBES.get(gate, []):
                subject_relative = (implementation_packet.get("required_outputs") or [""])[0]
                subject_path = composition / subject_relative
                if subject_relative == "" or not subject_path.is_file():
                    pipeline.record(
                        probe_name,
                        "probe-%s" % probe_name,
                        False,
                        {"probe": probe_name, "subject": subject_relative, "verdict": "missing_subject", "exit": 1},
                        _failure(
                            "probe-%s" % probe_name,
                            {"violations": [{"id": "probe-%s" % probe_name, "detail": "subject file absent from composition"}]},
                            impl_owned,
                        ),
                    )
                    continue
                probe_facts, probe_failure = run_probe(oracle_root, probe_name, subject_path, composition, report_dir, environment)
                pipeline.record(probe_name, "probe-%s" % probe_name, probe_failure is None, probe_facts, probe_failure)

            if gate == "G21-D":
                skill_dir = composition / SKILL_ROOT_PATH
                if not skill_dir.is_dir():
                    pipeline.record(
                        "skill-validate",
                        "skill-validate",
                        False,
                        {"valid": False, "violations": []},
                        _failure(
                            "skill-validate",
                            {"violations": [{"id": "skill-validate", "detail": "canonical skill tree absent from composition"}]},
                            impl_owned,
                        ),
                    )
                else:
                    skill_facts, skill_failure = run_skill_validation(oracle_root, skill_dir, report_dir, environment)
                    pipeline.record("skill-validate", "skill-validate", skill_failure is None, skill_facts, skill_failure)

    return _finalize_pairwise(pipeline, implementation_packet, conformance_packet, impl_candidate, conf_candidate, implementation_entries, conformance_entries)


def _finalize_pairwise(
    pipeline: Pipeline,
    implementation_packet: Dict[str, Any],
    conformance_packet: Dict[str, Any],
    impl_candidate: str,
    conf_candidate: str,
    implementation_entries: List[Dict[str, Any]],
    conformance_entries: List[Dict[str, Any]],
) -> Tuple[Dict[str, Any], int, List[Check]]:
    verdict = "pass" if pipeline.checks and all(check.passed for check in pipeline.checks) else "fail"
    receipt = {
        "receipt_version": RECEIPT_VERSION,
        "gate": _packet_gate(implementation_packet) or "G21",
        "verdict": verdict,
        "base_sha": implementation_packet.get("base_sha"),
        "oracle_seed_sha": implementation_packet.get("oracle_seed_sha"),
        "contract_sha256": implementation_packet.get("contract_sha256"),
        "interface_manifest_sha256": implementation_packet.get("interface_manifest_sha256"),
        "implementation_packet_sha256": canonical_sha256(implementation_packet),
        "conformance_packet_sha256": canonical_sha256(conformance_packet),
        "implementation_sha": impl_candidate,
        "conformance_sha": conf_candidate,
        "implementation_diff_sha256": diff_digest(implementation_entries),
        "conformance_diff_sha256": diff_digest(conformance_entries),
        "implementation_changed_paths": changed_paths(implementation_entries),
        "conformance_changed_paths": changed_paths(conformance_entries),
        "checks": [check.receipt_entry() for check in pipeline.checks],
        "skipped_tests": sum(
            skipped_count(check.facts)
            for check in pipeline.checks
            if check.check_id == "tests-composition" and "skipped" in check.facts
        ),
    }
    return receipt, (0 if verdict == "pass" else 1), pipeline.checks


def run_g30_gate(
    oracle_root: Path,
    candidate_root: Path,
    base_sha: str,
    candidate_sha: str,
    interface_manifest_path: Optional[Path],
    invariant: Optional[str],
    timeout_seconds: int,
) -> Tuple[Dict[str, Any], int, List[Check]]:
    pipeline = Pipeline(invariant)
    manifest, manifest_bytes = load_interface_manifest(oracle_root, interface_manifest_path)
    manifest_digest = sha256_hex(manifest_bytes)
    observed_seed = head_sha(oracle_root)
    pipeline.record("oracle-seed", "oracle-seed", True, {"observed": observed_seed})

    identity_errors, identity_facts = candidate_identity(candidate_root, base_sha, candidate_sha, "candidate")
    pipeline.record(
        "identity-candidate",
        "identity-candidate",
        not identity_errors,
        identity_facts,
        None if not identity_errors else _failure(
            "identity-candidate",
            {"violations": [{"id": "identity-candidate", "detail": detail} for detail in identity_errors]},
            [],
        ),
    )
    if pipeline.stop:
        return _finalize_g30(pipeline, candidate_root, base_sha, candidate_sha, observed_seed, manifest_digest)

    entries = parse_diff_raw(git_diff_raw(candidate_root, base_sha, candidate_sha))
    violations = check_ownership(entries, ["**"], list(G30_PROTECTED_PATHS), sorted(set(STATUS_TO_CHANGE_TYPE.values())))
    pipeline.record(
        "ownership-candidate",
        "ownership-candidate",
        not violations,
        {"root": "candidate", "changed_paths": changed_paths(entries), "violations": violations},
        None if not violations else _failure(
            "ownership-candidate",
            {"violations": [{"id": "ownership-candidate", "detail": detail} for detail in violations]},
            [],
        ),
    )
    if pipeline.stop:
        return _finalize_g30(pipeline, candidate_root, base_sha, candidate_sha, observed_seed, manifest_digest)

    contract_digest: Optional[str] = None
    contract_present = True
    fixture_equal = False
    try:
        contract_digest, fixture_bytes = contract_fixture_observation_bounded(candidate_root)
        fixture_equal = fixture_bytes == manifest_contract_fixture_bytes(manifest)
    except OracleError:
        contract_present = False
    pipeline.record(
        "contract-fixture-bytes", "contract-fixture-bytes", contract_present and fixture_equal,
        {"present": contract_present, "equal": fixture_equal, "digest": contract_digest},
        None if contract_present and fixture_equal else _detail_failure("contract-fixture-bytes", ["G30 contract fixture differs from M0"], []),
    )
    pipeline.record(
        "contract-digest",
        "contract-digest",
        contract_present and contract_digest == sha256_hex(manifest_contract_fixture_bytes(manifest)),
        {"present": contract_present, "digest": contract_digest},
        None if contract_present and contract_digest == sha256_hex(manifest_contract_fixture_bytes(manifest)) else _failure(
            "contract-digest",
            {"violations": [{"id": "contract-digest", "detail": "contract module absent from the composition candidate"}]},
            [],
        ),
    )
    if pipeline.stop:
        return _finalize_g30(pipeline, candidate_root, base_sha, candidate_sha, observed_seed, manifest_digest)

    with tempfile.TemporaryDirectory(prefix="p0-oracle-g30-") as temporary:
        data_root = Path(temporary) / "data"
        data_root.mkdir()
        environment = child_environment(data_root)
        for index, command in enumerate(G30_TEST_ARGV):
            check_id = "tests-%02d" % (index + 1)
            if command and command[0].endswith(".py"):
                facts = run_script_command(candidate_root, command, timeout_seconds, environment)
                passed = facts["exit"] == 0
            else:
                facts = run_test_command(candidate_root, command, timeout_seconds, environment)
                passed = facts["exit"] == 0 and facts["ok"] and skipped_count(facts) == 0
            pipeline.record(
                "g30-tests",
                check_id,
                passed,
                facts,
                None if passed else _failure(check_id, {"violations": [{"id": check_id, "detail": "G30 gate command failed"}]}, []),
            )
            if pipeline.stop:
                break

    return _finalize_g30(pipeline, candidate_root, base_sha, candidate_sha, observed_seed, manifest_digest)


def _finalize_g30(
    pipeline: Pipeline,
    candidate_root: Path,
    base_sha: str,
    candidate_sha: str,
    observed_seed: str,
    manifest_digest: str,
) -> Tuple[Dict[str, Any], int, List[Check]]:
    entries = parse_diff_raw(git_diff_raw(candidate_root, base_sha, candidate_sha))
    contract_digest = None
    try:
        contract_digest, _ = contract_fixture_observation_bounded(candidate_root)
    except OracleError:
        contract_digest = None
    verdict = "pass" if pipeline.checks and all(check.passed for check in pipeline.checks) else "fail"
    receipt = {
        "receipt_version": RECEIPT_VERSION,
        "gate": "G30",
        "verdict": verdict,
        "base_sha": base_sha,
        "oracle_seed_sha": observed_seed,
        "contract_sha256": contract_digest,
        "interface_manifest_sha256": manifest_digest,
        "candidate_sha": candidate_sha,
        "candidate_diff_sha256": diff_digest(entries),
        "candidate_changed_paths": changed_paths(entries),
        "checks": [check.receipt_entry() for check in pipeline.checks],
        "skipped_tests": sum(
            skipped_count(check.facts)
            for check in pipeline.checks
            if check.check_id.startswith("tests-") and "skipped" in check.facts
        ),
    }
    return receipt, (0 if verdict == "pass" else 1), pipeline.checks


def write_receipt(output_dir: Path, receipt: Dict[str, Any]) -> Path:
    implementation_sha = receipt.get("implementation_sha") or receipt.get("candidate_sha") or "unknown"
    conformance_sha = receipt.get("conformance_sha")
    filename = "%s-%s.json" % (implementation_sha, conformance_sha) if conformance_sha else "%s.json" % implementation_sha
    target_dir = output_dir / str(receipt.get("gate", "gate"))
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / filename
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_bytes(canonical_json_bytes(receipt))
    temporary.replace(target)
    return target


def emit_failure_packet(receipt: Dict[str, Any], checks: List[Check], reproduction_tail: List[str]) -> None:
    failed = next((check for check in checks if not check.passed and check.failure is not None), None)
    if failed is None or failed.failure is None:
        return
    packet = dict(failed.failure)
    packet["candidate_sha"] = receipt.get("implementation_sha") or receipt.get("candidate_sha")
    packet["reproduction"] = ["python", "-I", "<trusted-runner>"] + reproduction_tail + ["--invariant", packet["oracle_id"]]
    sys.stderr.buffer.write(canonical_json_bytes(packet))
    sys.stderr.buffer.flush()


def _receipt_root(oracle_root: Path, output_dir: Optional[Path]) -> Path:
    if output_dir is not None:
        return output_dir
    return oracle_root / ".artifacts" / "agent-p0"


def _run_validation_output(valid: bool, errors: List[str]) -> int:
    sys.stdout.buffer.write(canonical_json_bytes({"valid": valid, "errors": errors}))
    sys.stdout.buffer.flush()
    return 0 if valid else 1


def _run_packet_validation(oracle_root: Path, path: Path) -> int:
    data = path.read_bytes()
    packet = load_json_bytes(data, "packet")
    errors: List[str] = []
    if canonical_json_bytes(packet) != data:
        errors.append("packet file is not canonical JSON")
    errors.extend(packet_errors(oracle_root, packet))
    return _run_validation_output(not errors, errors)


def _run_terminal_validation(oracle_root: Path, path: Path) -> int:
    errors: List[str] = []
    try:
        terminal = parse_terminal_bytes(path.read_bytes())
    except TerminalError as error:
        return _run_validation_output(False, [str(error)])
    errors.extend(terminal_errors(oracle_root, terminal))
    if (
        not errors
        and isinstance(terminal, dict)
        and terminal.get("result") == "evidence"
        and not evidence_digest_ok(terminal)
    ):
        errors.append("finding_sha256 does not match the canonical findings digest")
    return _run_validation_output(not errors, errors)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Trusted Work Stack Agent P0 Oracle gate runner.")
    parser.add_argument("--oracle-root", type=Path, required=True)
    parser.add_argument("--implementation-root", type=Path)
    parser.add_argument("--conformance-root", type=Path)
    parser.add_argument("--implementation-packet", type=Path)
    parser.add_argument("--conformance-packet", type=Path)
    parser.add_argument("--interface-manifest", type=Path)
    parser.add_argument("--candidate-root", type=Path)
    parser.add_argument("--gate", choices=["G30"])
    parser.add_argument("--base")
    parser.add_argument("--candidate")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--invariant")
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--validate-packet", type=Path)
    parser.add_argument("--validate-terminal", type=Path)
    arguments = parser.parse_args(argv)

    oracle_root = arguments.oracle_root.resolve()

    try:
        if arguments.validate_packet is not None:
            return _run_packet_validation(oracle_root, arguments.validate_packet.resolve())
        if arguments.validate_terminal is not None:
            return _run_terminal_validation(oracle_root, arguments.validate_terminal.resolve())

        if arguments.candidate_root is not None or arguments.gate is not None:
            if (
                arguments.candidate_root is None
                or arguments.gate != "G30"
                or arguments.base is None
                or arguments.candidate is None
            ):
                parser.error("G30 mode requires --candidate-root, --gate G30, --base and --candidate")
                return 2
            receipt, exit_code, checks = run_g30_gate(
                oracle_root,
                arguments.candidate_root.resolve(),
                arguments.base,
                arguments.candidate,
                arguments.interface_manifest.resolve() if arguments.interface_manifest is not None else None,
                arguments.invariant,
                arguments.timeout_seconds,
            )
        elif (
            arguments.implementation_root is not None
            and arguments.conformance_root is not None
            and arguments.implementation_packet is not None
            and arguments.conformance_packet is not None
        ):
            receipt, exit_code, checks = run_pairwise_gate(
                oracle_root,
                arguments.implementation_root.resolve(),
                arguments.conformance_root.resolve(),
                arguments.implementation_packet.resolve(),
                arguments.conformance_packet.resolve(),
                arguments.interface_manifest.resolve() if arguments.interface_manifest is not None else None,
                arguments.invariant,
            )
        else:
            parser.error(
                "provide either --implementation-root/--conformance-root/--implementation-packet/--conformance-packet or --candidate-root/--gate G30/--base/--candidate"
            )
            return 2

        if not receipt["checks"] and arguments.invariant is not None:
            print("run_agent_p0_gates: no check matches --invariant %s" % arguments.invariant, file=sys.stderr)
            return 2
        if exit_code != 0:
            emit_failure_packet(
                receipt,
                checks,
                ["--oracle-root", str(oracle_root)]
                + (
                    [
                        "--implementation-root",
                        str(arguments.implementation_root.resolve()),
                        "--conformance-root",
                        str(arguments.conformance_root.resolve()),
                        "--implementation-packet",
                        str(arguments.implementation_packet.resolve()),
                        "--conformance-packet",
                        str(arguments.conformance_packet.resolve()),
                        "--interface-manifest",
                        str(arguments.interface_manifest.resolve()) if arguments.interface_manifest is not None else str(oracle_root / ORACLE_DIR / MANIFEST_NAME),
                    ]
                    if arguments.implementation_root is not None
                    else [
                        "--candidate-root",
                        str(arguments.candidate_root.resolve()),
                        "--gate",
                        "G30",
                        "--base",
                        str(arguments.base),
                        "--candidate",
                        str(arguments.candidate),
                        "--interface-manifest",
                        str(arguments.interface_manifest.resolve()) if arguments.interface_manifest is not None else str(oracle_root / ORACLE_DIR / MANIFEST_NAME),
                    ]
                ),
            )
        write_receipt(_receipt_root(oracle_root, arguments.output_dir), receipt)
        sys.stdout.buffer.write(canonical_json_bytes(receipt))
        sys.stdout.buffer.flush()
        return exit_code
    except OracleError as error:
        print("run_agent_p0_gates: %s" % error, file=sys.stderr)
        return 2
    except OSError as error:
        print("run_agent_p0_gates: IO error: %s" % error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
