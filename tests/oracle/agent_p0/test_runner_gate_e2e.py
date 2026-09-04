import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import fixture_support


def invoke_runner(arguments: list) -> subprocess.CompletedProcess:
    arguments = list(arguments)
    if "--interface-manifest" not in arguments:
        root_flag = "--candidate-root" if "--candidate-root" in arguments else "--implementation-root"
        fixture_root = Path(arguments[arguments.index(root_flag) + 1]).parent
        manifest = fixture_root / "manifest.v1.json"
        manifest.write_bytes(fixture_support.fixture_manifest_bytes())
        arguments.extend(["--interface-manifest", str(manifest)])
    return subprocess.run(
        [sys.executable, "-I", str(fixture_support.RUNNER_PATH)] + arguments,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=600,
    )


def build_pair(work: Path, gate: str, subject_relative: str, subject_content: bytes, extra_impl_files=None, test_module_content=None):
    impl_root, conf_root, base_sha, impl_sha = fixture_support.build_pair_fixture(
        work,
        gate,
        subject_relative,
        subject_content,
        test_module_content=test_module_content,
        extra_impl_files=extra_impl_files,
    )
    impl_packet_path = work / "impl-packet.json"
    conf_packet_path = work / "conf-packet.json"
    is_g10 = gate == "G10"
    fixture_support.write_packet(
        impl_packet_path,
        fixture_support.make_packet(
            base_sha=base_sha,
            packet_id="agent-p0-selftest-impl",
            owned_paths=[subject_relative],
            required_outputs=[subject_relative],
            required_exports=["Runner", "STATUS_COMMAND", "Token", "contract_fixture_bytes"] if is_g10 else [],
            forbidden_imports=[] if is_g10 else ["subprocess", "workstack.storage"],
            forbidden_calls=[] if is_g10 else ["os.system", "subprocess.*"],
            allowed_change_types=["modify"] if is_g10 else ["add"],
            required_gates=[gate],
        ),
    )
    fixture_support.write_packet(
        conf_packet_path,
        fixture_support.make_conformance_packet(
            base_sha=base_sha,
            packet_id="agent-p0-selftest-conf",
            owned_paths=[fixture_support._LANE_TEST_FILES[gate]],
            required_outputs=[fixture_support._LANE_TEST_FILES[gate]],
            forbidden_imports=[],
            forbidden_calls=[],
            required_gates=[gate],
        ),
    )
    return impl_root, conf_root, impl_packet_path, conf_packet_path


class PairwiseGateE2ETest(unittest.TestCase):
    def _assert_g10_contract_rejected(self, contract: bytes, oracle_id: str):
        with tempfile.TemporaryDirectory(prefix="p0-e2e-g10-reject-") as temporary:
            work = Path(temporary)
            impl_root, conf_root, impl_packet, conf_packet = build_pair(
                work, "G10", "workstack/agent_cli_contract.py", contract
            )
            result = invoke_runner(pairwise_arguments(work, "out", impl_root, conf_root, impl_packet, conf_packet))
            self.assertEqual(result.returncode, 1, result.stderr.decode("utf-8", "replace"))
            failure = json.loads(result.stderr.decode("utf-8"))
            self.assertEqual(failure["oracle_id"], oracle_id)

    def _run_manifest_mutation(self, mutate):
        with tempfile.TemporaryDirectory(prefix="p0-e2e-manifest-variant-") as temporary:
            work = Path(temporary)
            impl_root, conf_root, impl_packet, conf_packet = build_pair(
                work, "G21-B1", "workstack/agent_authority.py", fixture_support.subject_file("fixture_good_authority.py")
            )
            arguments = pairwise_arguments(work, "out", impl_root, conf_root, impl_packet, conf_packet)
            manifest_path = Path(arguments[arguments.index("--interface-manifest") + 1])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            mutate(manifest)
            manifest_bytes = fixture_support.canonical_bytes(manifest)
            manifest_path.write_bytes(manifest_bytes)
            manifest_digest = fixture_support.sha256_hex(manifest_bytes)
            for packet_path in (impl_packet, conf_packet):
                packet = json.loads(packet_path.read_text(encoding="utf-8"))
                packet["interface_manifest_sha256"] = manifest_digest
                fixture_support.write_packet(packet_path, packet)
            return invoke_runner(arguments)

    def test_g10_contract_pair_emits_receipt_from_fixture_bytes(self):
        contract = fixture_support.contract_module_bytes("O2 candidate fixture")
        conformance = '''import unittest
from workstack.agent_cli_contract import contract_fixture_bytes

class ContractTest(unittest.TestCase):
    def test_frozen_fixture(self):
        self.assertTrue(contract_fixture_bytes().endswith(b'\\n'))
'''
        with tempfile.TemporaryDirectory(prefix="p0-e2e-g10-") as temporary:
            work = Path(temporary)
            impl_root, conf_root, impl_packet, conf_packet = build_pair(
                work, "G10", "workstack/agent_cli_contract.py", contract, test_module_content=conformance
            )
            result = invoke_runner(pairwise_arguments(work, "out", impl_root, conf_root, impl_packet, conf_packet))
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
            receipt = json.loads(result.stdout.decode("utf-8"))
            self.assertEqual(receipt["gate"], "G10")
            self.assertEqual(receipt["contract_sha256"], fixture_support.sha256_hex(fixture_support.contract_fixture_result()))
            self.assertEqual(receipt["interface_manifest_sha256"], fixture_support.sha256_hex(fixture_support.fixture_manifest_bytes()))
            check_ids = [check["id"] for check in receipt["checks"]]
            self.assertIn("abi-equality", check_ids)
            self.assertIn("contract-fixture-bytes", check_ids)

    def test_interface_manifest_digest_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="p0-e2e-m0-mismatch-") as temporary:
            work = Path(temporary)
            impl_root, conf_root, impl_packet, conf_packet = build_pair(
                work, "G21-B1", "workstack/agent_authority.py", fixture_support.subject_file("fixture_good_authority.py")
            )
            bad = json.loads(impl_packet.read_text(encoding="utf-8"))
            bad["interface_manifest_sha256"] = "f" * 64
            fixture_support.write_packet(impl_packet, bad)
            result = invoke_runner(pairwise_arguments(work, "out", impl_root, conf_root, impl_packet, conf_packet))
            self.assertEqual(result.returncode, 1)
            receipt = json.loads(result.stdout.decode("utf-8"))
            self.assertEqual(receipt["verdict"], "fail")
            self.assertEqual(receipt["checks"][0]["id"], "packet-schema-implementation")

    def test_extra_dunder_all_is_rejected(self):
        contract = fixture_support.contract_module_bytes("extra all").replace(
            b'"contract_fixture_bytes"]', b'"contract_fixture_bytes", "EXTRA"]'
        ) + b'EXTRA = 1\n'
        self._assert_g10_contract_rejected(contract, "abi-equality")

    def test_wrong_callable_signature_is_rejected(self):
        contract = fixture_support.contract_module_bytes("wrong signature").replace(
            b"def contract_fixture_bytes() -> bytes:", b"def contract_fixture_bytes(extra) -> bytes:"
        )
        self._assert_g10_contract_rejected(contract, "abi-equality")

    def test_wrong_protocol_method_signature_is_rejected(self):
        contract = fixture_support.contract_module_bytes("wrong protocol").replace(
            b"def run(self, *, token: Token) -> str:", b"def run(self, token: Token) -> str:"
        )
        self._assert_g10_contract_rejected(contract, "abi-equality")

    def test_non_frozen_positional_dataclass_is_rejected(self):
        contract = fixture_support.contract_module_bytes("mutable dataclass").replace(
            b"@dataclass(frozen=True, kw_only=True)", b"@dataclass"
        )
        self._assert_g10_contract_rejected(contract, "abi-equality")

    def test_constant_mismatch_is_rejected(self):
        contract = fixture_support.contract_module_bytes("wrong constant").replace(
            b'STATUS_COMMAND = "status"', b'STATUS_COMMAND = "wrong"'
        )
        self._assert_g10_contract_rejected(contract, "abi-equality")

    def test_fixture_bytes_mismatch_is_rejected_even_with_packet_hash_pinned(self):
        contract = fixture_support.contract_module_bytes("wrong fixture").replace(
            ("return %r" % fixture_support.contract_fixture_result()).encode("utf-8"),
            b"return b'wrong\\n'",
        )
        self._assert_g10_contract_rejected(contract, "contract-fixture-bytes")

    def test_candidate_fixture_digest_cannot_override_manifest_projection(self):
        wrong_fixture = b"wrong fixture\n"
        contract = fixture_support.contract_module_bytes("wrong fixture digest").replace(
            ("return %r" % fixture_support.contract_fixture_result()).encode("utf-8"),
            ("return %r" % wrong_fixture).encode("utf-8"),
        )
        with tempfile.TemporaryDirectory(prefix="p0-e2e-g10-packet-fixture-") as temporary:
            work = Path(temporary)
            impl_root, conf_root, impl_packet, conf_packet = build_pair(
                work, "G10", "workstack/agent_cli_contract.py", contract
            )
            candidate_digest = fixture_support.sha256_hex(wrong_fixture)
            for packet_path in (impl_packet, conf_packet):
                packet = json.loads(packet_path.read_text(encoding="utf-8"))
                packet["contract_sha256"] = candidate_digest
                fixture_support.write_packet(packet_path, packet)
            result = invoke_runner(pairwise_arguments(work, "out", impl_root, conf_root, impl_packet, conf_packet))
            self.assertEqual(result.returncode, 1, result.stderr.decode("utf-8", "replace"))
            failure = json.loads(result.stderr.decode("utf-8"))
            self.assertEqual(failure["oracle_id"], "packet-schema-implementation")
            self.assertIn(b"contract_sha256 does not equal the M0 fixture projection", result.stderr)

    def test_packet_lane_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="p0-e2e-lane-mismatch-") as temporary:
            work = Path(temporary)
            impl_root, conf_root, impl_packet, conf_packet = build_pair(
                work, "G21-B1", "workstack/agent_authority.py", fixture_support.subject_file("fixture_good_authority.py")
            )
            bad = json.loads(impl_packet.read_text(encoding="utf-8"))
            bad["lane"] = "A"
            fixture_support.write_packet(impl_packet, bad)
            result = invoke_runner(pairwise_arguments(work, "out", impl_root, conf_root, impl_packet, conf_packet))
            self.assertEqual(result.returncode, 1)
            self.assertIn(b"packet lane does not equal", result.stderr)

    def test_manifest_lane_permutation_is_semantically_equivalent(self):
        result = self._run_manifest_mutation(lambda manifest: manifest["ownership"]["lanes"].reverse())
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))

    def test_manifest_duplicate_lane_is_rejected(self):
        def duplicate(manifest):
            manifest["ownership"]["lanes"].append(dict(manifest["ownership"]["lanes"][0]))

        result = self._run_manifest_mutation(duplicate)
        self.assertEqual(result.returncode, 2)
        self.assertIn(b"duplicate lane ids", result.stderr)

    def test_manifest_missing_lane_is_rejected(self):
        result = self._run_manifest_mutation(lambda manifest: manifest["ownership"]["lanes"].pop())
        self.assertEqual(result.returncode, 2)
        self.assertIn(b"missing lane ids", result.stderr)

    def test_manifest_unknown_lane_is_rejected(self):
        def add_unknown(manifest):
            manifest["ownership"]["lanes"].append({"lane": "UNKNOWN", "owned_paths": []})

        result = self._run_manifest_mutation(add_unknown)
        self.assertEqual(result.returncode, 2)
        self.assertIn(b"unknown lane ids", result.stderr)

    def test_manifest_unknown_gate_label_is_rejected(self):
        def change_gate(manifest):
            next(item for item in manifest["ownership"]["lanes"] if item["lane"] == "B1")["gate"] = "G21-Z"

        result = self._run_manifest_mutation(change_gate)
        self.assertEqual(result.returncode, 2)
        self.assertIn(b"unexpected gate label", result.stderr)

    def test_wrong_conformance_lane_label_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="p0-e2e-conf-lane-mismatch-") as temporary:
            work = Path(temporary)
            impl_root, conf_root, impl_packet, conf_packet = build_pair(
                work, "G10", "workstack/agent_cli_contract.py", fixture_support.contract_module_bytes("lane candidate")
            )
            packet = json.loads(conf_packet.read_text(encoding="utf-8"))
            packet["lane"] = "O2"
            fixture_support.write_packet(conf_packet, packet)
            result = invoke_runner(pairwise_arguments(work, "out", impl_root, conf_root, impl_packet, conf_packet))
            self.assertEqual(result.returncode, 1)
            self.assertIn(b"conformance packet lane does not equal", result.stderr)

    def test_crlf_manifest_and_directives_keep_canonical_digests(self):
        with tempfile.TemporaryDirectory(prefix="p0-e2e-crlf-assets-") as temporary:
            work = Path(temporary)
            impl_root, conf_root, impl_packet, conf_packet = build_pair(
                work, "G21-B1", "workstack/agent_authority.py", fixture_support.subject_file("fixture_good_authority.py")
            )
            arguments = pairwise_arguments(work, "out", impl_root, conf_root, impl_packet, conf_packet)
            manifest_path = Path(arguments[arguments.index("--interface-manifest") + 1])
            canonical_manifest = manifest_path.read_bytes()
            manifest_path.write_bytes(canonical_manifest[:-1] + b"\r\n")

            oracle_root = work / "oracle-root"
            copied_assets = oracle_root / "quality" / "agent-p0-oracle"
            shutil.copytree(fixture_support.ORACLE_DIR, copied_assets)
            for name in ("worker-directive.v1.txt", "supervisor-directive.v1.txt"):
                path = copied_assets / name
                canonical_text = path.read_bytes().replace(b"\r\n", b"\n")
                path.write_bytes(canonical_text.replace(b"\n", b"\r\n"))
            fixture_support.git(oracle_root, "init", "-q", "-b", "main")
            fixture_support._configure_git(oracle_root)
            fixture_support.git(oracle_root, "add", "-A")
            fixture_support.git(oracle_root, "commit", "-q", "-m", "CRLF Oracle fixture")
            oracle_seed = fixture_support.git(oracle_root, "rev-parse", "HEAD").strip()
            for packet_path in (impl_packet, conf_packet):
                packet = json.loads(packet_path.read_text(encoding="utf-8"))
                packet["oracle_seed_sha"] = oracle_seed
                fixture_support.write_packet(packet_path, packet)
            arguments[arguments.index("--oracle-root") + 1] = str(oracle_root)

            result = invoke_runner(arguments)
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
            receipt = json.loads(result.stdout.decode("utf-8"))
            self.assertEqual(receipt["interface_manifest_sha256"], fixture_support.sha256_hex(canonical_manifest))
            self.assertIn("directive-digests", [check["id"] for check in receipt["checks"]])

    def test_compliant_pair_passes_with_deterministic_receipt(self):
        with tempfile.TemporaryDirectory(prefix="p0-e2e-pass-") as temporary:
            work = Path(temporary)
            impl_root, conf_root, impl_packet, conf_packet = build_pair(
                work,
                "G21-B1",
                "workstack/agent_authority.py",
                fixture_support.subject_file("fixture_good_authority.py"),
            )
            first = invoke_runner(pairwise_arguments(work, "r1", impl_root, conf_root, impl_packet, conf_packet))
            self.assertEqual(first.returncode, 0, first.stderr.decode("utf-8", "replace"))
            receipt = json.loads(first.stdout.decode("utf-8"))
            self.assertEqual(receipt["verdict"], "pass")
            self.assertEqual(receipt["gate"], "G21-B1")
            self.assertEqual(receipt["skipped_tests"], 0)
            self.assertTrue(all(check["exit"] == 0 for check in receipt["checks"]))
            check_ids = [check["id"] for check in receipt["checks"]]
            self.assertIn("ownership-implementation", check_ids)
            self.assertIn("tests-composition", check_ids)
            self.assertIn("contract-digest", check_ids)
            second = invoke_runner(pairwise_arguments(work, "r2", impl_root, conf_root, impl_packet, conf_packet))
            self.assertEqual(second.returncode, 0)
            self.assertEqual(first.stdout, second.stdout)
            first_file = work / "r1" / "G21-B1" / ("%s-%s.json" % (receipt["implementation_sha"], receipt["conformance_sha"]))
            second_file = work / "r2" / "G21-B1" / ("%s-%s.json" % (receipt["implementation_sha"], receipt["conformance_sha"]))
            self.assertEqual(first_file.read_bytes(), second_file.read_bytes())
            self.assertEqual(first_file.read_bytes(), first.stdout)

    def test_real_abi_lane_does_not_require_probe_only_exports(self):
        with tempfile.TemporaryDirectory(prefix="p0-e2e-transport-") as temporary:
            work = Path(temporary)
            impl_root, conf_root, impl_packet, conf_packet = build_pair(
                work,
                "G21-A",
                "workstack/agent_transport.py",
                fixture_support.subject_file("fixture_good_transport.py"),
            )
            result = invoke_runner(pairwise_arguments(work, "out", impl_root, conf_root, impl_packet, conf_packet))
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
            receipt = json.loads(result.stdout.decode("utf-8"))
            self.assertEqual(receipt["verdict"], "pass")
            probe_ids = [check["id"] for check in receipt["checks"] if check["id"].startswith("probe-")]
            self.assertEqual(probe_ids, [])

    def assert_gate_failure(self, work: Path, gate: str, subject_relative: str, subject_content: bytes, expected_oracle_id: str, expect_no_tests: bool = False):
        impl_root, conf_root, impl_packet, conf_packet = build_pair(work, gate, subject_relative, subject_content)
        result = invoke_runner(pairwise_arguments(work, "out", impl_root, conf_root, impl_packet, conf_packet))
        self.assertEqual(result.returncode, 1)
        receipt = json.loads(result.stdout.decode("utf-8"))
        self.assertEqual(receipt["verdict"], "fail")
        failure_packet = json.loads(result.stderr.decode("utf-8"))
        self.assertEqual(failure_packet["oracle_id"], expected_oracle_id)
        self.assertEqual(failure_packet["repair_owner"], "implementation")
        self.assertIn("--invariant", failure_packet["reproduction"])
        check_ids = [check["id"] for check in receipt["checks"]]
        if expect_no_tests:
            self.assertNotIn("tests-composition", check_ids)
        return receipt, failure_packet

    def test_forbidden_path_candidate_is_rejected_before_tests(self):
        with tempfile.TemporaryDirectory(prefix="p0-e2e-own-") as temporary:
            work = Path(temporary)
            impl_root, conf_root, impl_packet, conf_packet = build_pair(
                work,
                "G21-B1",
                "workstack/agent_authority.py",
                fixture_support.subject_file("fixture_good_authority.py"),
                extra_impl_files={"frontend/intrusion.txt": b"no\n"},
            )
            result = invoke_runner(pairwise_arguments(work, "out", impl_root, conf_root, impl_packet, conf_packet))
            self.assertEqual(result.returncode, 1)
            failure_packet = json.loads(result.stderr.decode("utf-8"))
            self.assertEqual(failure_packet["oracle_id"], "ownership-implementation")
            receipt = json.loads(result.stdout.decode("utf-8"))
            check_ids = [check["id"] for check in receipt["checks"]]
            self.assertNotIn("tests-composition", check_ids)

    def test_forbidden_import_candidate_is_rejected_before_tests(self):
        with tempfile.TemporaryDirectory(prefix="p0-e2e-imp-") as temporary:
            work = Path(temporary)
            impl_root, conf_root, impl_packet, conf_packet = build_pair(
                work,
                "G21-B1",
                "workstack/agent_authority.py",
                b"import subprocess\n\nVALUE = 1\n",
            )
            result = invoke_runner(pairwise_arguments(work, "out", impl_root, conf_root, impl_packet, conf_packet))
            self.assertEqual(result.returncode, 1)
            failure_packet = json.loads(result.stderr.decode("utf-8"))
            self.assertEqual(failure_packet["oracle_id"], "import-scan-implementation")

    def test_skipped_conformance_test_fails_the_gate(self):
        with tempfile.TemporaryDirectory(prefix="p0-e2e-skip-") as temporary:
            work = Path(temporary)
            impl_root, conf_root, impl_packet, conf_packet = build_pair(
                work,
                "G21-B1",
                "workstack/agent_authority.py",
                fixture_support.subject_file("fixture_good_authority.py"),
                test_module_content=fixture_support.SKIPPED_TEST_MODULE,
            )
            result = invoke_runner(pairwise_arguments(work, "out", impl_root, conf_root, impl_packet, conf_packet))
            self.assertEqual(result.returncode, 1)
            receipt = json.loads(result.stdout.decode("utf-8"))
            self.assertEqual(receipt["skipped_tests"], 1)
            failure_packet = json.loads(result.stderr.decode("utf-8"))
            self.assertEqual(failure_packet["oracle_id"], "tests-composition")

    def test_wrong_base_candidate_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="p0-e2e-base-") as temporary:
            work = Path(temporary)
            impl_root, conf_root, _impl_packet, conf_packet = build_pair(
                work,
                "G21-B1",
                "workstack/agent_authority.py",
                fixture_support.subject_file("fixture_good_authority.py"),
            )
            fake_base = "f" * 40
            bad_impl_packet = work / "bad-impl-packet.json"
            bad_conf_packet = work / "bad-conf-packet.json"
            fixture_support.write_packet(
                bad_impl_packet,
                fixture_support.make_packet(
                    base_sha=fake_base,
                    packet_id="agent-p0-selftest-impl",
                    owned_paths=["workstack/agent_authority.py"],
                    required_outputs=["workstack/agent_authority.py"],
                    required_gates=["G21-B1"],
                ),
            )
            fixture_support.write_packet(
                work / "conf-fake-base.json",
                fixture_support.make_conformance_packet(
                    base_sha=fake_base,
                    packet_id="agent-p0-selftest-conf",
                    owned_paths=["tests/test_agent_authority_contract.py"],
                    required_outputs=["tests/test_agent_authority_contract.py"],
                    forbidden_imports=[],
                    forbidden_calls=[],
                    required_gates=["G21-B1"],
                ),
            )
            result = invoke_runner(pairwise_arguments(work, "out", impl_root, conf_root, bad_impl_packet, work / "conf-fake-base.json"))
            self.assertEqual(result.returncode, 1)
            failure_packet = json.loads(result.stderr.decode("utf-8"))
            self.assertEqual(failure_packet["oracle_id"], "identity-implementation")

    def test_invariant_filter_reruns_single_ownership_check(self):
        with tempfile.TemporaryDirectory(prefix="p0-e2e-inv-") as temporary:
            work = Path(temporary)
            impl_root, conf_root, impl_packet, conf_packet = build_pair(
                work,
                "G21-B1",
                "workstack/agent_authority.py",
                fixture_support.subject_file("fixture_good_authority.py"),
            )
            arguments = pairwise_arguments(work, "out", impl_root, conf_root, impl_packet, conf_packet)
            result = invoke_runner(arguments + ["--invariant", "ownership-implementation"])
            self.assertEqual(result.returncode, 0)
            receipt = json.loads(result.stdout.decode("utf-8"))
            self.assertEqual([check["id"] for check in receipt["checks"]], ["ownership-implementation"])


class G30GateTest(unittest.TestCase):
    def test_g30_contract_digest_uses_fixture_bytes_not_source_bytes(self):
        with tempfile.TemporaryDirectory(prefix="p0-g30-fixture-") as temporary:
            work = Path(temporary)
            repo, base_sha, candidate_sha = fixture_support.build_single_fixture(
                work, {"workstack/agent_runtime.py": "VALUE = 1\n"}
            )
            result = invoke_runner([
                "--oracle-root", str(fixture_support.ORACLE_ROOT), "--candidate-root", str(repo),
                "--gate", "G30", "--base", base_sha, "--candidate", candidate_sha,
                "--output-dir", str(work / "out"), "--invariant", "contract-fixture-bytes",
            ])
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
            receipt = json.loads(result.stdout.decode("utf-8"))
            self.assertEqual([check["id"] for check in receipt["checks"]], ["contract-fixture-bytes"])
            fixture_sha = fixture_support.sha256_hex(fixture_support.contract_fixture_result())
            source_sha = fixture_support.sha256_hex((repo / "workstack" / "agent_cli_contract.py").read_bytes())
            self.assertEqual(receipt["contract_sha256"], fixture_sha)
            self.assertNotEqual(receipt["contract_sha256"], source_sha)
            self.assertEqual(receipt["interface_manifest_sha256"], fixture_support.sha256_hex(fixture_support.fixture_manifest_bytes()))

    def test_protected_path_candidate_is_rejected_before_tests(self):
        with tempfile.TemporaryDirectory(prefix="p0-g30-") as temporary:
            work = Path(temporary)
            repo, base_sha, candidate_sha = fixture_support.build_single_fixture(
                work,
                {"quality/agent-p0-oracle/intrusion.json": b"{}\n"},
            )
            result = invoke_runner(
                [
                    "--oracle-root",
                    str(fixture_support.ORACLE_ROOT),
                    "--candidate-root",
                    str(repo),
                    "--gate",
                    "G30",
                    "--base",
                    base_sha,
                    "--candidate",
                    candidate_sha,
                    "--output-dir",
                    str(work / "out"),
                ]
            )
            self.assertEqual(result.returncode, 1)
            receipt = json.loads(result.stdout.decode("utf-8"))
            self.assertEqual(receipt["verdict"], "fail")
            self.assertEqual(receipt["gate"], "G30")
            failure_packet = json.loads(result.stderr.decode("utf-8"))
            self.assertEqual(failure_packet["oracle_id"], "ownership-candidate")
            check_ids = [check["id"] for check in receipt["checks"]]
            self.assertNotIn("tests-01", check_ids)

    def test_incomplete_arguments_fail_as_usage_error(self):
        with tempfile.TemporaryDirectory(prefix="p0-g30-usage-") as temporary:
            repo = Path(temporary) / "repo"
            repo.mkdir()
            result = invoke_runner(
                [
                    "--oracle-root",
                    str(fixture_support.ORACLE_ROOT),
                    "--candidate-root",
                    str(repo),
                    "--gate",
                    "G30",
                ]
            )
            self.assertEqual(result.returncode, 2)

    def test_g30_happy_path_ownership_only_scope(self):
        with tempfile.TemporaryDirectory(prefix="p0-g30-pass-") as temporary:
            work = Path(temporary)
            repo, base_sha, candidate_sha = fixture_support.build_single_fixture(
                work,
                {"workstack/agent_runtime.py": "REGISTRY = ('status', 'context', 'checkpoint')\n"},
            )
            result = invoke_runner(
                [
                    "--oracle-root",
                    str(fixture_support.ORACLE_ROOT),
                    "--candidate-root",
                    str(repo),
                    "--gate",
                    "G30",
                    "--base",
                    base_sha,
                    "--candidate",
                    candidate_sha,
                    "--output-dir",
                    str(work / "out"),
                    "--invariant",
                    "oracle-seed",
                ]
            )
            self.assertEqual(result.returncode, 0)
            receipt = json.loads(result.stdout.decode("utf-8"))
            self.assertEqual([check["id"] for check in receipt["checks"]], ["oracle-seed"])


def pairwise_arguments(work: Path, out_name: str, impl_root: Path, conf_root: Path, impl_packet: Path, conf_packet: Path) -> list:
    manifest = work / "manifest.v1.json"
    manifest.write_bytes(fixture_support.fixture_manifest_bytes())
    return [
        "--oracle-root",
        str(fixture_support.ORACLE_ROOT),
        "--implementation-root",
        str(impl_root),
        "--conformance-root",
        str(conf_root),
        "--implementation-packet",
        str(impl_packet),
        "--conformance-packet",
        str(conf_packet),
        "--interface-manifest",
        str(manifest),
        "--output-dir",
        str(work / out_name),
    ]


if __name__ == "__main__":
    unittest.main()
