"""Shared fixtures and helpers for the O1 Oracle seed self-tests."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[3]
ORACLE_ROOT = REPO_ROOT
ORACLE_DIR = REPO_ROOT / "quality" / "agent-p0-oracle"
RUNNER_PATH = REPO_ROOT / "scripts" / "run_agent_p0_gates.py"
DIRECTIVES_DOC = (
    REPO_ROOT
    / "docs"
    / "WORKSTACK-CLI-AGENT-SKILL-HEADLESS-WORKER-DIRECTIVES-2026-09-02.md"
)
WORKER_DIRECTIVE_FILE = ORACLE_DIR / "worker-directive.v1.txt"
SUPERVISOR_DIRECTIVE_FILE = ORACLE_DIR / "supervisor-directive.v1.txt"

FIXTURE_TEST_MODULE = '''import unittest


class PlaceholderContractTest(unittest.TestCase):
    def test_placeholder_passes(self):
        self.assertTrue(True)


if __name__ == "__main__":
    unittest.main()
'''
SKIPPED_TEST_MODULE = '''import unittest


class SkippedContractTest(unittest.TestCase):
    @unittest.skip("intentional fixture skip")
    def test_skipped_placeholder(self):
        self.fail("must never run")


if __name__ == "__main__":
    unittest.main()
'''

_LANE_TEST_FILES = {
    "G10": "tests/test_agent_cli_contract.py",
    "G21-A": "tests/test_agent_transport_contract.py",
    "G21-B1": "tests/test_agent_authority_contract.py",
    "G21-B2": "tests/test_agent_local_backend_contract.py",
    "G21-C1": "tests/test_agent_command_status_contract.py",
    "G21-C2": "tests/test_agent_command_context_contract.py",
    "G21-C3": "tests/test_agent_command_checkpoint_contract.py",
    "G21-D": "tests/test_agent_skill_contract.py",
}

_LANE_PATHS = {
    "G10": "workstack/agent_cli_contract.py",
    "G21-A": "workstack/agent_transport.py",
    "G21-B1": "workstack/agent_authority.py",
    "G21-B2": "workstack/agent_local_backend.py",
    "G21-C1": "workstack/agent_command_status.py",
    "G21-C2": "workstack/agent_command_context.py",
    "G21-C3": "workstack/agent_command_checkpoint.py",
    "G21-D": "integrations/agent-skill/work-stack/**",
}


def fixture_manifest() -> dict[str, Any]:
    modules = {}
    module_rules = {}
    lane_names = {"G10": "O2", "G21-A": "A", "G21-B1": "B1", "G21-B2": "B2", "G21-C1": "C1", "G21-C2": "C2", "G21-C3": "C3", "G21-D": "D"}
    lanes = [
        {"lane": "M0", "owned_paths": ["quality/agent-p0-oracle/manifest.v1.json"]},
        {"lane": "Q0", "owned_paths": ["quality/quality-config.json"]},
        {"lane": "O1", "owned_paths": ["scripts/run_agent_p0_gates.py"]},
    ]
    for gate, path in _LANE_PATHS.items():
        module_name = path[:-3].replace("/", ".") if path.endswith(".py") else None
        if module_name:
            modules[module_name] = (
                {
                    "exports": ["Runner", "STATUS_COMMAND", "Token", "contract_fixture_bytes"],
                    "callables": {"contract_fixture_bytes": {"params": [], "returns": "bytes"}},
                    "dataclasses": {
                        "Token": {
                            "fields": [{"name": "value", "type": "str", "required": True}],
                            "frozen": True,
                            "keyword_only": True,
                            "all_constructor_fields_required": True,
                        }
                    },
                    "protocols": {
                        "Runner": {
                            "methods": {
                                "run": {
                                    "params": [{"name": "token", "type": "Token"}],
                                    "returns": "str",
                                }
                            }
                        }
                    },
                    "values": {"STATUS_COMMAND": "status"},
                }
                if gate == "G10"
                else {"exports": []}
            )
            module_rules[module_name] = {
                "forbidden_imports": ["subprocess", "workstack.storage"] if gate != "G10" else [],
                "forbidden_calls": ["os.system", "subprocess.*"] if gate != "G10" else [],
            }
        lanes.append({"lane": lane_names[gate], "gate": gate, "owned_paths": [path], "paired_conformance": _LANE_TEST_FILES[gate]})
        if gate == "G10":
            lanes.append({"lane": "T0", "gate": "G10", "owned_paths": [_LANE_TEST_FILES[gate]]})
    lanes.extend([
        {"lane": "TE", "gate": "G30", "owned_paths": ["tests/test_agent_cli_e2e_contract.py"]},
        {"lane": "I1", "owned_paths": ["workstack/agent_commands.py"]},
        {"lane": "I2", "owned_paths": ["workstack/agent_runtime.py"]},
        {"lane": "I3", "owned_paths": ["workstack/cli.py"]},
    ])
    projection = ["admission", "backend_results", "cli_contract", "commands", "envelope", "errors", "limits", "transport_rules"]
    manifest = {
        "schema_version": 1,
        "admission": {"fixture": True},
        "abi": {"modules": modules},
        "backend_results": {"fixture": True},
        "cli_contract": {"fixture": True},
        "commands": {"fixture": True},
        "digest_recipes": {
            "contract": {"expected_sha256": "0" * 64},
            "contract_fixture_projection": projection,
        },
        "envelope": {"fixture": True},
        "errors": {"fixture": True},
        "limits": {"fixture": True},
        "module_rules": module_rules,
        "ownership": {"lanes": lanes},
        "transport_rules": {"fixture": True},
    }
    expected = canonical_bytes({name: manifest[name] for name in projection})
    manifest["digest_recipes"]["contract"]["expected_sha256"] = sha256_hex(expected)
    return manifest


def fixture_manifest_bytes() -> bytes:
    return canonical_bytes(fixture_manifest())


def contract_fixture_result() -> bytes:
    manifest = fixture_manifest()
    return canonical_bytes({name: manifest[name] for name in manifest["digest_recipes"]["contract_fixture_projection"]})


def contract_module_bytes(label: str = "Self-test contract fixture") -> bytes:
    return (
        '"""%s."""\n\nfrom dataclasses import dataclass\nfrom typing import Protocol\n\n'
        '__all__ = ["Runner", "STATUS_COMMAND", "Token", "contract_fixture_bytes"]\n'
        'STATUS_COMMAND = "status"\n\n@dataclass(frozen=True, kw_only=True)\nclass Token:\n    value: str\n\n'
        'class Runner(Protocol):\n    def run(self, *, token: Token) -> str: ...\n\n'
        'def contract_fixture_bytes() -> bytes:\n    return %r\n' % (label, contract_fixture_result())
    ).encode("utf-8")

_RUNNER_MODULE = None


def runner_module():
    global _RUNNER_MODULE
    if _RUNNER_MODULE is None:
        spec = importlib.util.spec_from_file_location("run_agent_p0_gates", RUNNER_PATH)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _RUNNER_MODULE = module
    return _RUNNER_MODULE


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=str(repo),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
    )
    if result.returncode != 0:
        raise AssertionError(
            "git %s failed in %s: %s" % (" ".join(arguments), repo, result.stderr.decode("utf-8", "replace").strip())
        )
    return result.stdout.decode("utf-8")


def _configure_git(repo: Path) -> None:
    git(repo, "config", "user.email", "oracle-selftest@localhost")
    git(repo, "config", "user.name", "Oracle Self-Test")
    git(repo, "config", "commit.gpgsign", "false")
    git(repo, "config", "core.autocrlf", "false")


def _write_files(root: Path, files: dict[str, Any]) -> None:
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_bytes(content.encode("utf-8"))


def base_files() -> dict[str, Any]:
    return {
        "workstack/__init__.py": "",
        "workstack/agent_cli_contract.py": contract_module_bytes(),
    }


def build_pair_fixture(
    work_dir: Path,
    gate: str,
    subject_relative: str,
    subject_content: bytes,
    test_relative: Optional[str] = None,
    test_module_content: Optional[str] = None,
    extra_impl_files: Optional[dict] = None,
) -> tuple[Path, Path, str, str]:
    """Create one origin repo with a base commit plus impl/conf branches and two clones."""
    origin = work_dir / "origin"
    origin.mkdir(parents=True)
    git(origin, "init", "-q", "-b", "main")
    _configure_git(origin)
    _write_files(origin, base_files())
    git(origin, "add", "-A")
    git(origin, "commit", "-q", "-m", "base")
    base_sha = git(origin, "rev-parse", "HEAD").strip()

    git(origin, "checkout", "-q", "-b", "impl")
    impl_files = {subject_relative: subject_content}
    if extra_impl_files:
        impl_files.update(extra_impl_files)
    _write_files(origin, impl_files)
    git(origin, "add", "-A")
    git(origin, "commit", "-q", "-m", "impl candidate")
    impl_sha = git(origin, "rev-parse", "HEAD").strip()

    git(origin, "checkout", "-q", "main")
    git(origin, "checkout", "-q", "-b", "conf")
    test_relative = test_relative if test_relative is not None else _LANE_TEST_FILES[gate]
    content = FIXTURE_TEST_MODULE if test_module_content is None else test_module_content
    _write_files(origin, {test_relative: content})
    git(origin, "add", "-A")
    git(origin, "commit", "-q", "-m", "conformance candidate")
    conf_sha = git(origin, "rev-parse", "HEAD").strip()

    impl_root = work_dir / "impl-root"
    conf_root = work_dir / "conf-root"
    git(origin.parent, "clone", "-q", "-b", "impl", str(origin), str(impl_root))
    git(origin.parent, "clone", "-q", "-b", "conf", str(origin), str(conf_root))
    _configure_git(impl_root)
    _configure_git(conf_root)
    # A clone materializes files through the ambient newline filters; re-check-out with
    # the fixture's core.autocrlf=false so the trees stay byte-identical to the index.
    git(impl_root, "checkout", "-q", "-f", "HEAD")
    git(conf_root, "checkout", "-q", "-f", "HEAD")
    return impl_root, conf_root, base_sha, impl_sha


def build_single_fixture(work_dir: Path, candidate_files: dict[str, Any]) -> tuple[Path, str, str]:
    """Create one repo with a base commit and one candidate commit."""
    repo = work_dir / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    _configure_git(repo)
    _write_files(repo, base_files())
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    base_sha = git(repo, "rev-parse", "HEAD").strip()
    _write_files(repo, candidate_files)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "candidate")
    candidate_sha = git(repo, "rev-parse", "HEAD").strip()
    return repo, base_sha, candidate_sha


def make_packet(**overrides: Any) -> dict[str, Any]:
    packet: dict[str, Any] = {
        "packet_version": 1,
        "packet_kind": "authoring",
        "role": "production",
        "packet_id": "agent-p0-selftest-b1",
        "base_sha": "0" * 40,
        "oracle_seed_sha": oracle_seed_sha(),
        "contract_sha256": sha256_hex(contract_fixture_result()),
        "interface_manifest_sha256": sha256_hex(fixture_manifest_bytes()),
        "worker_directive_sha256": sha256_hex(WORKER_DIRECTIVE_FILE.read_bytes().replace(b"\r\n", b"\n")),
        "owned_paths": ["workstack/agent_authority.py"],
        "required_outputs": ["workstack/agent_authority.py"],
        "declared_context_paths": ["workstack/agent_cli_contract.py"],
        "forbidden_paths": ["workstack/storage/**", "frontend/**"],
        "allowed_change_types": ["add"],
        "required_exports": [],
        "forbidden_imports": ["subprocess", "workstack.storage"],
        "forbidden_calls": ["os.system", "subprocess.*"],
        "required_gates": ["G21-B1"],
        "dependency_receipts": [],
        "worker_resource_class": "author",
        "gate_resource_class": "light-test",
        "timeout_seconds": 600,
    }
    packet.update(overrides)
    return packet


def make_conformance_packet(**overrides: Any) -> dict[str, Any]:
    overrides.setdefault("packet_id", "agent-p0-selftest-tb1")
    overrides.setdefault("role", "conformance")
    overrides.setdefault("owned_paths", ["tests/test_agent_authority_contract.py"])
    overrides.setdefault("required_outputs", ["tests/test_agent_authority_contract.py"])
    overrides.setdefault("forbidden_imports", [])
    overrides.setdefault("forbidden_calls", [])
    return make_packet(**overrides)


def write_packet(path: Path, packet: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_bytes(packet))


def oracle_seed_sha() -> str:
    return git(REPO_ROOT, "rev-parse", "HEAD").strip()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def fixture_dir() -> Path:
    return Path(__file__).resolve().parent


def subject_file(name: str) -> bytes:
    return (fixture_dir() / name).read_bytes()


def mutant_file(name: str) -> bytes:
    return (ORACLE_DIR / "mutants" / name).read_bytes()
