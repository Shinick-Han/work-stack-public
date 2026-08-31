from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class OptionalQrToolingTest(unittest.TestCase):
    def run_help(self, script: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts" / script), "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_help_does_not_require_optional_image_dependencies(self) -> None:
        for script in ("render_qr.py", "restore_from_png.py"):
            with self.subTest(script=script):
                result = self.run_help(script)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("usage:", result.stdout)

    def test_windows_qr_dependencies_are_exact_and_hash_locked(self) -> None:
        requirements = (ROOT / "requirements-qr-windows.txt").read_text(encoding="utf-8")

        for dependency in (
            "colorama==0.4.6",
            "pillow==12.3.0",
            "qrcode==8.2",
            "zxing-cpp==3.1.1",
        ):
            self.assertIn(dependency, requirements)
        self.assertEqual(requirements.count("--hash=sha256:"), 4)

    def test_qr_output_directories_must_be_new_or_empty(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("package_for_qr", ROOT / "scripts" / "package_for_qr.py")
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            frames = root / "frames"
            frames.mkdir()
            marker = frames / "keep.txt"
            marker.write_text("user-owned", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "must be empty"):
                module.make_frames(root / "archive.zip", frames, 16)
            self.assertEqual(marker.read_text(encoding="utf-8"), "user-owned")


if __name__ == "__main__":
    unittest.main()
