import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import fixture_support

PROBE_FILES = {
    "authority-preflight": "probe_authority_preflight.py",
    "idempotent-replay": "probe_idempotent_replay.py",
    "no-local-fallback": "probe_no_local_fallback.py",
    "output-canary": "probe_output_canary.py",
}


def run_probe(probe: str, subject_path: Path, work: Path) -> tuple:
    probe_file = fixture_support.ORACLE_DIR / "probes" / PROBE_FILES[probe]
    report_path = work / ("%s.json" % probe)
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            str(probe_file),
            "--subject",
            str(subject_path),
            "--report",
            str(report_path),
        ],
        cwd=str(work),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=120,
    )
    report = fixture_support.runner_module().load_json_bytes(report_path.read_bytes(), "probe report")
    return completed.returncode, report


def violation_ids(report) -> list:
    return sorted({str(item.get("id")) for item in report.get("violations", [])})


class ProbeSentinelTest(unittest.TestCase):
    def test_golden_sentinel_verdicts(self):
        records = fixture_support.read_jsonl(
            fixture_support.ORACLE_DIR / "golden" / "sentinel-verdicts.v1.jsonl"
        )
        self.assertTrue(records)
        with tempfile.TemporaryDirectory(prefix="p0-probes-") as temporary:
            work = Path(temporary)
            for record in records:
                if record["kind"] == "mutant":
                    subject = fixture_support.ORACLE_DIR / "mutants" / record["file"]
                else:
                    subject = fixture_support.fixture_dir() / record["file"]
                self.assertTrue(subject.is_file(), str(subject))
                exit_code, report = run_probe(record["probe"], subject, work)
                label = "%s/%s" % (record["file"], record["probe"])
                self.assertEqual(report.get("verdict"), record["expected_verdict"], label)
                self.assertEqual(violation_ids(report), sorted(record["expected_violation_ids"]), label)
                if record["expected_verdict"] == "pass":
                    self.assertEqual(exit_code, 0, label)
                else:
                    self.assertEqual(exit_code, 2, label)

    def test_probe_reports_are_canonical_json(self):
        good = fixture_support.fixture_dir() / "fixture_good_authority.py"
        with tempfile.TemporaryDirectory(prefix="p0-probe-canonical-") as temporary:
            probe_file = fixture_support.ORACLE_DIR / "probes" / PROBE_FILES["authority-preflight"]
            report_path = Path(temporary) / "authority-preflight.json"
            completed = subprocess.run(
                [sys.executable, "-I", str(probe_file), "--subject", str(good), "--report", str(report_path)],
                cwd=str(temporary),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=120,
            )
            self.assertEqual(completed.returncode, 0)
            self.assertEqual(
                report_path.read_bytes(),
                fixture_support.canonical_bytes(
                    fixture_support.runner_module().load_json_bytes(report_path.read_bytes(), "report")
                ),
            )

    def test_invalid_subject_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="p0-probe-invalid-") as temporary:
            subject = Path(temporary) / "not_a_subject.py"
            subject.write_bytes(b"VALUE = 1\n")
            probe_file = fixture_support.ORACLE_DIR / "probes" / PROBE_FILES["output-canary"]
            report_path = Path(temporary) / "report.json"
            completed = subprocess.run(
                [sys.executable, "-I", str(probe_file), "--subject", str(subject), "--report", str(report_path)],
                cwd=str(temporary),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=120,
            )
            self.assertEqual(completed.returncode, 3)
            report = fixture_support.runner_module().load_json_bytes(report_path.read_bytes(), "report")
            self.assertEqual(report["verdict"], "invalid_subject")

    def test_missing_seam_function_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="p0-probe-missing-") as temporary:
            subject = Path(temporary) / "partial.py"
            subject.write_bytes(b"def emit(*, payload, secrets, out, err):\n    return 0\n")
            probe_file = fixture_support.ORACLE_DIR / "probes" / PROBE_FILES["authority-preflight"]
            report_path = Path(temporary) / "report.json"
            completed = subprocess.run(
                [sys.executable, "-I", str(probe_file), "--subject", str(subject), "--report", str(report_path)],
                cwd=str(temporary),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=120,
            )
            self.assertEqual(completed.returncode, 3)
            report = fixture_support.runner_module().load_json_bytes(report_path.read_bytes(), "report")
            self.assertEqual(report["verdict"], "invalid_subject")


if __name__ == "__main__":
    unittest.main()
