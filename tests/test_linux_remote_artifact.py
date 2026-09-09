"""Contract tests for the Linux remote artifact builder.

Temporary source/wheelhouse/absent output only. No live .artifacts, profile,
home, network, SSH or production-artifact claim. Missing verified manylinux
unicodedata2 stays LINUX_WHEEL_LOCK_MISSING.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BUILDER_PATH = ROOT / "scripts" / "build_linux_remote_artifact.py"
FIXTURE_ROOT = Path(os.environ["WORKSTACK_TEST_FIXTURE_ROOT"]).resolve()
RESULT_ROOT = Path(os.environ["WORKSTACK_TEST_RESULTS_ROOT"]).resolve()
if not RESULT_ROOT.is_absolute() or not FIXTURE_ROOT.is_relative_to(RESULT_ROOT):
    raise AssertionError("fixture root must stay inside the contained results root")


def load_builder():
    spec = importlib.util.spec_from_file_location("build_linux_remote_artifact", BUILDER_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("builder module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILDER = load_builder()
TARGET = BUILDER.TARGET_ID
ZIP = BUILDER.ZIP


def refuse_zip_member(info: object, seen: set[str]) -> None:
    """Call the writer module's member gate with the builder's error taxonomy."""

    ZIP.refuse_zip_member(info, seen, BUILDER.ArtifactBuildError)


@contextlib.contextmanager
def bounded(name: str, value: int):
    """Shrink one bound on the writer module and on the builder's re-export."""

    with mock.patch.object(BUILDER, name, value), mock.patch.object(ZIP, name, value):
        yield


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_hashes(root: Path) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            mapping[path.relative_to(root).as_posix()] = sha256_file(path)
    return mapping


def git(source: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=str(source),
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def commit_source(source: Path) -> None:
    git(source, "init")
    git(source, "config", "user.email", "artifact-test.invalid")
    git(source, "config", "user.name", "Artifact Test")
    git(source, "config", "commit.gpgsign", "false")
    git(source, "config", "core.autocrlf", "false")
    (source / ".gitignore").write_text(".artifacts/\n", encoding="utf-8", newline="\n")
    git(source, "add", "-A")
    git(source, "commit", "-m", "fixture")
    seal_dist(source)


def write_frontend_inputs(root: Path) -> None:
    """The build-input roster the dist/source gate content-addresses."""

    (root / "frontend" / "src").mkdir(parents=True, exist_ok=True)
    (root / "frontend" / "src" / "main.tsx").write_text("export const main = 1\n", encoding="utf-8")
    (root / "frontend" / "index.html").write_text("<div id=root></div>\n", encoding="utf-8")
    (root / "frontend" / "package.json").write_text('{"name":"ui"}\n', encoding="utf-8")
    (root / "frontend" / "package-lock.json").write_text('{"lockfileVersion":3}\n', encoding="utf-8")
    (root / "frontend" / "vite.config.ts").write_text("export default {}\n", encoding="utf-8")
    for name in ("tsconfig.json", "tsconfig.app.json", "tsconfig.node.json"):
        (root / "frontend" / name).write_text("{}\n", encoding="utf-8")
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "scripts" / "generate-theme-tokens.mjs").write_text(
        "export const tokens = 1\n", encoding="utf-8"
    )
    (root / "theme").mkdir(parents=True, exist_ok=True)
    (root / "theme" / "theme-tokens.json").write_text('{"color":{}}\n', encoding="utf-8")
    generated = root / "desktop" / "python-webview-shell" / "generated"
    generated.mkdir(parents=True, exist_ok=True)
    (generated / "theme_tokens.py").write_text("TOKENS = {}\n", encoding="utf-8")
    fixtures = root / "tests" / "fixtures"
    fixtures.mkdir(parents=True, exist_ok=True)
    (fixtures / "checkpoint_change_v1.json").write_text('{"event_id":1}\n', encoding="utf-8")


def seal_dist(root: Path) -> Path:
    """Record the dist/source receipt the builder now demands before packaging."""

    gate = BUILDER.DIST_GATE
    receipt = gate.receipt_path(root)
    gate.write_receipt(
        receipt,
        gate.build_receipt(gate.source_entries(root), gate.dist_entries(root / "frontend" / "dist")),
    )
    return receipt


def write_source(root: Path, *, lock: str | None = None, version: str = "1.0.7") -> None:
    (root / "workstack").mkdir(parents=True)
    (root / "workstack" / "__init__.py").write_text(
        '__version__ = "{0}"\nREMOTE_PROTOCOL_VERSION = 1\n'.format(version),
        encoding="utf-8",
    )
    (root / "contracts").mkdir()
    (root / "contracts" / "c.json").write_text("{}\n", encoding="utf-8")
    (root / "web").mkdir()
    (root / "web" / "index.html").write_text("<html></html>\n", encoding="utf-8")
    (root / "frontend" / "dist").mkdir(parents=True)
    (root / "frontend" / "dist" / "index.html").write_text("frozen\n", encoding="utf-8")
    write_frontend_inputs(root)
    (root / "licenses").mkdir()
    (root / "licenses" / "NOTICE.txt").write_text("notice\n", encoding="utf-8")
    (root / "desktop" / "python-webview-shell").mkdir(parents=True, exist_ok=True)
    (root / "desktop" / "python-webview-shell" / "remote_entry.py").write_text(
        "raise SystemExit('remote-entry')\n", encoding="utf-8"
    )
    (root / "desktop" / "python-webview-shell" / "remote_command_contract.py").write_text(
        "PROTOCOL = 1\n", encoding="utf-8"
    )
    (root / "desktop" / "python-webview-shell" / "remote_owner.py").write_text(
        "OWNER = 1\n", encoding="utf-8"
    )
    for helper in (
        "remote_owner_receipt.py", "remote_owner_stop.py", "remote_stop_result.py",
        "remote_update_owner_observation.py", "remote_update_maintenance.py",
        "remote_update_maintenance_receipts.py",
    ):
        (root / "desktop" / "python-webview-shell" / helper).write_text(
            "OWNER_HELPER = 1\n", encoding="utf-8"
        )
    (root / "desktop" / "python-webview-shell" / "remote_process_handle.py").write_text(
        "HANDLE = 1\n", encoding="utf-8"
    )
    (root / "desktop" / "python-webview-shell" / "remote_receipt_guard.py").write_text(
        "GUARD = 1\n", encoding="utf-8"
    )
    (root / "desktop" / "python-webview-shell" / "remote_receipt_io.py").write_text(
        "IO = 1\n", encoding="utf-8"
    )
    (root / "desktop" / "python-webview-shell" / "remote_skill_install.py").write_text(
        "SKILL_INSTALL = 1\n", encoding="utf-8"
    )
    (root / "run_work_stack.py").write_text("print('run')\n", encoding="utf-8")
    (root / "README.md").write_text("readme\n", encoding="utf-8")
    (root / "LICENSE").write_bytes((ROOT / "LICENSE").read_bytes())
    (root / "SECURITY.md").write_text("security\n", encoding="utf-8")
    (root / "THIRD_PARTY_NOTICES.md").write_text("notices\n", encoding="utf-8")
    skill_src = ROOT / "integrations" / "agent-skill" / "work-stack"
    skill_dest = root / "integrations" / "agent-skill" / "work-stack"
    for relative in ("SKILL.md", "references/commands.md", "references/journal-policy.md"):
        target = skill_dest / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((skill_src / relative).read_bytes())
    (root / "requirements.txt").write_text(lock or "", encoding="utf-8")


def ignore_generated_dist(source: Path) -> None:
    """Mirror the repository, where frontend/dist is generated and ignored."""

    (source / ".gitignore").write_text(
        ".artifacts/\nfrontend/dist/\n", encoding="utf-8", newline="\n"
    )
    git(source, "rm", "-r", "-q", "--cached", "frontend/dist")
    git(source, "add", ".gitignore")
    git(source, "commit", "-m", "ignore generated dist")


def drop_and_commit(source: Path, relative: str) -> None:
    """Remove a tracked path and commit it, leaving the fixture worktree clean."""
    git(source, "rm", "-r", "-q", "--", relative)
    git(source, "commit", "-m", "drop " + relative)


def write_wheel(directory: Path, distribution: str, version: str, tag: str, files: dict[str, bytes]) -> Path:
    filename = f"{distribution.replace('-', '_')}-{version}-{tag}.whl"
    path = directory / filename
    dist_info = f"{distribution.replace('-', '_')}-{version}.dist-info"
    atomic = [
        "-".join((one, two, three))
        for one in tag.split("-")[0].split(".")
        for two in tag.split("-")[1].split(".")
        for three in tag.split("-")[2].split(".")
    ]
    lines = "".join("Tag: {0}\n".format(item) for item in sorted(atomic))
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\n" + lines,
        )
        archive.writestr(f"{dist_info}/METADATA", "Name: {0}\nVersion: {1}\n".format(distribution, version))
        for name, data in files.items():
            archive.writestr(name, data)
    return path


OFFICIAL_SPECS = (
    (
        "unicodedata2",
        "17.0.0",
        "cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64",
        {"unicodedata2.py": b"payload = 1\n"},
    ),
    ("attrs", "26.1.0", "py3-none-any", {"attrs.py": b"payload = 1\n"}),
    ("jsonschema", "4.26.0", "py3-none-any", {"jsonschema.py": b"payload = 1\n"}),
    ("jsonschema-specifications", "2025.9.1", "py3-none-any", {"jsonschema_specifications.py": b"payload = 1\n"}),
    ("referencing", "0.37.0", "py3-none-any", {"referencing.py": b"payload = 1\n"}),
    (
        "rpds-py",
        "2026.6.3",
        "cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64",
        {"rpds.py": b"payload = 1\n"},
    ),
    ("typing-extensions", "4.16.0", "py3-none-any", {"typing_extensions.py": b"payload = 1\n"}),
)


def pin(name: str, version: str, path: Path) -> str:
    return "{0}=={1} \\\n    --hash=sha256:{2}\n".format(name, version, sha256_file(path))


def lock_from_wheels(house: Path) -> str:
    chunks = []
    for path in sorted(child for child in house.iterdir() if child.suffix == ".whl"):
        distribution, version, _python, _abi, _platform = BUILDER.parse_wheel_filename(path.name)
        chunks.append(pin(BUILDER.canonical_name(distribution), version, path))
    return "".join(chunks)


def copy_house(src: Path, dest: Path) -> Path:
    dest.mkdir(parents=True)
    for child in src.iterdir():
        shutil.copy2(child, dest / child.name)
    return dest


def happy_fixture(base: Path, *, product_version: str = "1.0.7") -> tuple[Path, Path]:
    source = base / "source"
    wheels = base / "wheelhouse"
    source.mkdir(parents=True)
    wheels.mkdir(parents=True)
    lock = ""
    for name, version, tag, files in OFFICIAL_SPECS:
        lock += pin(name, version, write_wheel(wheels, name, version, tag, files))
    write_source(source, lock=lock, version=product_version)
    commit_source(source)
    return source, wheels


class LinuxRemoteArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        FIXTURE_ROOT.mkdir(parents=True, exist_ok=True)
        self.workspace = tempfile.TemporaryDirectory(dir=str(FIXTURE_ROOT))
        self.base = Path(self.workspace.name)

    def tearDown(self) -> None:
        self.workspace.cleanup()

    def output_dir(self, name: str = "out") -> Path:
        return self.base / name

    def refuse(self, source: Path, wheels: Path, output: Path, code: str) -> None:
        with self.assertRaises(BUILDER.ArtifactBuildError) as raised:
            BUILDER.build_artifact(source, wheels, output, TARGET)
        self.assertEqual(code, raised.exception.code)

    def test_shuffled_wheelhouses_emit_byte_identical_zip_and_sidecar(self) -> None:
        source, first_house = happy_fixture(self.base / "one")
        second_house = self.base / "two-house"
        second_house.mkdir()
        for child in reversed(list(first_house.iterdir())):
            shutil.copy2(child, second_house / child.name)
        first_out = self.output_dir("out-a")
        second_out = self.output_dir("out-b")
        before_source = tree_hashes(source)
        before_first = tree_hashes(first_house)
        before_second = tree_hashes(second_house)
        archive_a, sidecar_a = BUILDER.build_artifact(source, first_house, first_out, TARGET)
        archive_b, sidecar_b = BUILDER.build_artifact(source, second_house, second_out, TARGET)
        self.assertEqual(archive_a.read_bytes(), archive_b.read_bytes())
        self.assertEqual(sidecar_a.read_bytes(), sidecar_b.read_bytes())
        self.assertEqual(before_source, tree_hashes(source))
        self.assertEqual(before_first, tree_hashes(first_house))
        self.assertEqual(before_second, tree_hashes(second_house))

    def test_sidecar_and_payload_hashes_match_recomputed_bytes(self) -> None:
        source, wheels = happy_fixture(self.base / "hash")
        archive, sidecar = BUILDER.build_artifact(source, wheels, self.output_dir(), TARGET)
        document = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(sorted(path.name for path in self.output_dir().iterdir()), sorted([archive.name, sidecar.name]))
        self.assertEqual(document["archive"]["name"], archive.name)
        self.assertEqual(document["archive"]["size"], archive.stat().st_size)
        self.assertEqual(document["archive"]["sha256"], "sha256:" + sha256_file(archive))
        self.assertEqual(document["target_id"], TARGET)
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(document["product_version"], "1.0.7")
        self.assertEqual(document["remote_protocol_version"], 1)
        with zipfile.ZipFile(archive) as handle:
            manifest_bytes = handle.read("artifact.json")
            self.assertEqual(document["artifact_manifest_sha256"], "sha256:" + hashlib.sha256(manifest_bytes).hexdigest())
            manifest = json.loads(manifest_bytes.decode("utf-8"))
            self.assertEqual(manifest["entrypoint"], "desktop/python-webview-shell/remote_entry.py")
            self.assertEqual(manifest["target"]["wheel_platform"], "manylinux_2_17_x86_64")
            self.assertEqual(manifest_bytes, BUILDER.canonical_json(manifest))
            names = [info.filename.replace("\\", "/") for info in handle.infolist() if not info.filename.endswith("/")]
            self.assertEqual(1, names.count("payload/desktop/python-webview-shell/remote_entry.py"))
            self.assertEqual(1, names.count("payload/desktop/python-webview-shell/remote_command_contract.py"))
            self.assertEqual(1, names.count("payload/run_work_stack.py"))
            self.assertTrue(any(name.startswith("payload/frontend/dist/") for name in names))
            payload_files = {item["path"]: item for item in manifest["files"]}
            for path, record in payload_files.items():
                data = handle.read("payload/" + path)
                self.assertEqual(record["size"], len(data))
                self.assertEqual(record["sha256"], "sha256:" + hashlib.sha256(data).hexdigest())
                self.assertEqual(420, record["mode"])
            self.assertEqual(7, len(manifest["wheels"]))
            for wheel in manifest["wheels"]:
                self.assertEqual(wheel["sha256"], "sha256:" + sha256_file(wheels / wheel["filename"]))
                self.assertEqual(wheel["version"], BUILDER.APPROVED_LOCK[wheel["distribution"]])

    def test_cli_writes_exactly_two_output_files(self) -> None:
        source, wheels = happy_fixture(self.base / "cli")
        output = self.output_dir("cli-out")
        completed = subprocess.run(
            [
                sys.executable,
                str(BUILDER_PATH),
                "--source-root",
                str(source),
                "--wheelhouse",
                str(wheels),
                "--output-dir",
                str(output),
                "--target",
                TARGET,
            ],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(2, len(list(output.iterdir())))

    def test_builder_owns_its_canonical_zip_writer(self) -> None:
        source = BUILDER_PATH.read_text(encoding="utf-8")
        writer = (ROOT / "scripts" / "linux_remote_artifact_zip.py").read_text(encoding="utf-8")
        # The QR packager writes bare 0644 with no file-type bit, which the
        # Linux installer refuses, so the builder must not delegate to it.
        self.assertNotIn("package_for_qr.py", source)
        self.assertIn("linux_remote_artifact_zip.py", source)
        self.assertIn("create_canonical_zip", writer)
        self.assertIn("stat.S_IFREG | FILE_MODE", writer)
        self.assertIn("linux_remote_artifact_git.py", source)
        self.assertIn("LINUX_WHEEL_LOCK_MISSING", source)
        self.assertIn("No pip, network, compiler, WSL, SSH", source)
        self.assertIn("APPROVED_LOCK", source)
        self.assertIn("Arbitrary requirements graphs are not resolved", source)

    def test_existing_output_empty_or_nonempty_is_refused(self) -> None:
        source, wheels = happy_fixture(self.base / "exists")
        empty = self.output_dir("empty")
        empty.mkdir()
        nonempty = self.output_dir("nonempty")
        nonempty.mkdir()
        (nonempty / "stale.txt").write_text("stale\n", encoding="utf-8")
        self.refuse(source, wheels, empty, "OUTPUT_EXISTS")
        self.refuse(source, wheels, nonempty, "OUTPUT_EXISTS")
        self.assertEqual(["stale.txt"], [path.name for path in nonempty.iterdir()])
        self.assertEqual([], list(empty.iterdir()))

    def test_dirty_and_unpinned_source_are_refused(self) -> None:
        source, wheels = happy_fixture(self.base / "dirty")
        (source / "README.md").write_text("changed\n", encoding="utf-8")
        self.refuse(source, wheels, self.output_dir("dirty-out"), "SOURCE_DIRTY")
        bare = self.base / "bare"
        write_source(bare, lock="attrs==26.1.0\n    --hash=sha256:" + ("ab" * 32) + "\n")
        self.refuse(bare, wheels, self.output_dir("bare-out"), "SOURCE_UNPINNED")

    def test_missing_extra_duplicate_wrong_version_hash_and_sdist_are_refused(self) -> None:
        source, wheels = happy_fixture(self.base / "admit")
        missing = self.base / "missing-house"
        missing.mkdir()
        shutil.copy2(next(path for path in wheels.iterdir() if path.name.startswith("unicodedata2-")), missing)
        self.refuse(source, missing, self.output_dir("missing"), "MISSING_WHEEL")
        extra = self.base / "extra-house"
        extra.mkdir()
        for child in wheels.iterdir():
            shutil.copy2(child, extra)
        write_wheel(extra, "requests", "2.0.0", "py3-none-any", {"requests.py": b"x=1\n"})
        self.refuse(source, extra, self.output_dir("extra"), "EXTRA_WHEEL")
        duplicate = self.base / "dup-house"
        duplicate.mkdir()
        for child in wheels.iterdir():
            shutil.copy2(child, duplicate)
        original = next(path for path in duplicate.iterdir() if path.name.startswith("unicodedata2-"))
        # Same wheel under a PEP 427 build-tag name, so its own filename tags
        # still match its metadata and the duplicate distribution is what refuses.
        build_tagged = original.name.split("-")
        build_tagged.insert(2, "1")
        shutil.copy2(original, duplicate / "-".join(build_tagged))
        self.refuse(source, duplicate, self.output_dir("dup"), "DUPLICATE_WHEEL")
        sdist_house = self.base / "sdist-house"
        sdist_house.mkdir()
        for child in wheels.iterdir():
            shutil.copy2(child, sdist_house)
        (sdist_house / "attrs-26.1.0.tar.gz").write_bytes(b"sdist")
        self.refuse(source, sdist_house, self.output_dir("sdist"), "SDIST")
        version_house = copy_house(wheels, self.base / "version-house")
        next(version_house.glob("attrs-*")).unlink()
        write_wheel(version_house, "attrs", "1.0.0", "py3-none-any", {"attrs.py": b"x=1\n"})
        self.refuse(source, version_house, self.output_dir("version"), "WRONG_VERSION")
        hash_house = copy_house(wheels, self.base / "hash-house")
        next(hash_house.glob("attrs-*")).unlink()
        write_wheel(hash_house, "attrs", "26.1.0", "py3-none-any", {"attrs.py": b"other\n"})
        self.refuse(source, hash_house, self.output_dir("hash"), "UNKNOWN_HASH")

    def test_wrong_tag_musl_glibc_and_filename_metadata_mismatch_are_refused(self) -> None:
        _source, template = happy_fixture(self.base / "tag-template")
        musl_house = copy_house(template, self.base / "musl-house")
        next(musl_house.glob("unicodedata2-*")).unlink()
        write_wheel(musl_house, "unicodedata2", "17.0.0", "cp312-cp312-musllinux_1_2_x86_64", {"unicodedata2.py": b"x=1\n"})
        musl_source = self.base / "musl-source"
        musl_source.mkdir()
        write_source(musl_source, lock=lock_from_wheels(musl_house))
        commit_source(musl_source)
        self.refuse(musl_source, musl_house, self.output_dir("musl"), "MUSL")
        glibc_house = copy_house(template, self.base / "glibc-house")
        next(glibc_house.glob("unicodedata2-*")).unlink()
        write_wheel(glibc_house, "unicodedata2", "17.0.0", "cp312-cp312-manylinux_2_28_x86_64", {"unicodedata2.py": b"x=1\n"})
        glibc_source = self.base / "glibc-source"
        glibc_source.mkdir()
        write_source(glibc_source, lock=lock_from_wheels(glibc_house))
        commit_source(glibc_source)
        self.refuse(glibc_source, glibc_house, self.output_dir("glibc"), "GLIBC_MISMATCH")
        mismatch_house = copy_house(template, self.base / "mismatch-house")
        next(mismatch_house.glob("unicodedata2-*")).unlink()
        path = mismatch_house / "unicodedata2-17.0.0-cp312-cp312-manylinux_2_17_x86_64.whl"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(
                "unicodedata2-17.0.0.dist-info/WHEEL",
                "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            )
            archive.writestr("unicodedata2-17.0.0.dist-info/METADATA", "Name: unicodedata2\nVersion: 17.0.0\n")
            archive.writestr("unicodedata2.py", b"x=1\n")
        mismatch_source = self.base / "mismatch-source"
        mismatch_source.mkdir()
        write_source(mismatch_source, lock=lock_from_wheels(mismatch_house))
        commit_source(mismatch_source)
        self.refuse(mismatch_source, mismatch_house, self.output_dir("mismatch"), "FILENAME_WHEEL_TAG_MISMATCH")
        abi_house = copy_house(template, self.base / "abi-house")
        next(abi_house.glob("unicodedata2-*")).unlink()
        write_wheel(abi_house, "unicodedata2", "17.0.0", "cp311-cp311-manylinux_2_17_x86_64", {"unicodedata2.py": b"x=1\n"})
        abi_source = self.base / "abi-source"
        abi_source.mkdir()
        write_source(abi_source, lock=lock_from_wheels(abi_house))
        commit_source(abi_source)
        self.refuse(abi_source, abi_house, self.output_dir("abi"), "WRONG_TAG")

    def test_incompatible_rpds_py_and_missing_manylinux_unicodedata2_are_refused(self) -> None:
        source, wheels = happy_fixture(self.base / "rpds")
        rpds_house = copy_house(wheels, self.base / "rpds-house")
        next(rpds_house.glob("rpds_py-*")).unlink()
        write_wheel(rpds_house, "rpds-py", "2026.6.3", "cp312-cp312-win_amd64", {"rpds.py": b"x=1\n"})
        rpds_source = self.base / "rpds-source"
        rpds_source.mkdir()
        write_source(rpds_source, lock=lock_from_wheels(rpds_house))
        commit_source(rpds_source)
        self.refuse(rpds_source, rpds_house, self.output_dir("rpds-out"), "WRONG_TAG")
        lock_only = self.base / "lock-source"
        lock_only.mkdir()
        empty = self.base / "empty-house"
        empty.mkdir()
        production = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        write_source(lock_only, lock=production)
        commit_source(lock_only)
        self.refuse(lock_only, empty, self.output_dir("prod-lock"), "LINUX_WHEEL_LOCK_MISSING")
        any_house = copy_house(wheels, self.base / "any-house")
        next(any_house.glob("unicodedata2-*")).unlink()
        write_wheel(any_house, "unicodedata2", "17.0.0", "py3-none-any", {"unicodedata2.py": b"x=1\n"})
        any_source = self.base / "any-source"
        any_source.mkdir()
        write_source(any_source, lock=lock_from_wheels(any_house))
        commit_source(any_source)
        self.refuse(any_source, any_house, self.output_dir("any-out"), "LINUX_WHEEL_LOCK_MISSING")

    def test_symlink_nonregular_scripts_collision_and_path_rules_are_refused(self) -> None:
        source, wheels = happy_fixture(self.base / "members")
        scripts = copy_house(wheels, self.base / "scripts-house")
        next(scripts.glob("unicodedata2-*")).unlink()
        path = scripts / "unicodedata2-17.0.0-cp312-cp312-manylinux_2_17_x86_64.whl"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(
                "unicodedata2-17.0.0.dist-info/WHEEL",
                "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: cp312-cp312-manylinux_2_17_x86_64\n",
            )
            archive.writestr("unicodedata2-17.0.0.dist-info/METADATA", "Name: unicodedata2\nVersion: 17.0.0\n")
            archive.writestr("unicodedata2-17.0.0.data/scripts/tool", b"#!/bin/sh\n")
            archive.writestr("unicodedata2.py", b"x=1\n")
        scripts_source = self.base / "scripts-source"
        scripts_source.mkdir()
        write_source(scripts_source, lock=lock_from_wheels(scripts))
        commit_source(scripts_source)
        self.refuse(scripts_source, scripts, self.output_dir("scripts-out"), "DATA_SCRIPTS")
        purelib = copy_house(wheels, self.base / "purelib-house")
        next(purelib.glob("unicodedata2-*")).unlink()
        pure_path = purelib / "unicodedata2-17.0.0-cp312-cp312-manylinux_2_17_x86_64.whl"
        with zipfile.ZipFile(pure_path, "w") as archive:
            archive.writestr(
                "unicodedata2-17.0.0.dist-info/WHEEL",
                "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: cp312-cp312-manylinux_2_17_x86_64\n",
            )
            archive.writestr("unicodedata2-17.0.0.dist-info/METADATA", "Name: unicodedata2\nVersion: 17.0.0\n")
            archive.writestr("unicodedata2-17.0.0.data/purelib/x.py", b"x=1\n")
        pure_source = self.base / "pure-source"
        pure_source.mkdir()
        write_source(pure_source, lock=lock_from_wheels(purelib))
        commit_source(pure_source)
        self.refuse(pure_source, purelib, self.output_dir("pure-out"), "DATA_SCRIPTS")
        collide = copy_house(wheels, self.base / "collide-house")
        next(collide.glob("attrs-*")).unlink()
        write_wheel(collide, "attrs", "26.1.0", "py3-none-any", {"run_work_stack.py": b"hijack\n"})
        collide_source = self.base / "collide-source"
        collide_source.mkdir()
        write_source(collide_source, lock=lock_from_wheels(collide))
        commit_source(collide_source)
        self.refuse(collide_source, collide, self.output_dir("collide-out"), "COLLISION")
        traversal = copy_house(wheels, self.base / "trav-house")
        next(traversal.glob("unicodedata2-*")).unlink()
        trav_wheel = traversal / "unicodedata2-17.0.0-cp312-cp312-manylinux_2_17_x86_64.whl"
        with zipfile.ZipFile(trav_wheel, "w") as archive:
            archive.writestr(
                "unicodedata2-17.0.0.dist-info/WHEEL",
                "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: cp312-cp312-manylinux_2_17_x86_64\n",
            )
            archive.writestr("unicodedata2-17.0.0.dist-info/METADATA", "Name: unicodedata2\nVersion: 17.0.0\n")
            archive.writestr("../evil.py", b"x=1\n")
        trav_source = self.base / "trav-source"
        trav_source.mkdir()
        write_source(trav_source, lock=lock_from_wheels(traversal))
        commit_source(trav_source)
        self.refuse(trav_source, traversal, self.output_dir("trav-out"), "TRAVERSAL")
        class FakeInfo:
            filename = "nested\\evil.py"
            file_size = 1
            external_attr = 0
            create_system = 0

        with self.assertRaises(BUILDER.ArtifactBuildError) as raised:
            refuse_zip_member(FakeInfo(), set())
        self.assertEqual("BACKSLASH", raised.exception.code)

        class DirTrav:
            filename = "../"
            file_size = 0
            external_attr = 0
            create_system = 0

        with self.assertRaises(BUILDER.ArtifactBuildError) as raised:
            refuse_zip_member(DirTrav(), set())
        self.assertEqual("TRAVERSAL", raised.exception.code)

        class EmptyName:
            filename = ""
            file_size = 0
            external_attr = 0
            create_system = 0

        with self.assertRaises(BUILDER.ArtifactBuildError) as raised:
            refuse_zip_member(EmptyName(), set())
        self.assertEqual("TRAVERSAL", raised.exception.code)

        class DotName:
            filename = "."
            file_size = 0
            external_attr = 0
            create_system = 0

        with self.assertRaises(BUILDER.ArtifactBuildError) as raised:
            refuse_zip_member(DotName(), set())
        self.assertEqual("TRAVERSAL", raised.exception.code)

        class ControlName:
            filename = "a\x00b.py"
            file_size = 1
            external_attr = 0
            create_system = 0

        with self.assertRaises(BUILDER.ArtifactBuildError) as raised:
            refuse_zip_member(ControlName(), set())
        self.assertEqual("TRAVERSAL", raised.exception.code)

        class FifoInfo:
            filename = "fifo"
            file_size = 1
            create_system = 3
            external_attr = 0o010644 << 16

        with self.assertRaises(BUILDER.ArtifactBuildError) as raised:
            refuse_zip_member(FifoInfo(), set())
        self.assertEqual("NONREGULAR", raised.exception.code)

        class SockInfo:
            filename = "sock"
            file_size = 1
            create_system = 3
            external_attr = 0o140644 << 16

        with self.assertRaises(BUILDER.ArtifactBuildError) as raised:
            refuse_zip_member(SockInfo(), set())
        self.assertEqual("NONREGULAR", raised.exception.code)
        link_house = copy_house(wheels, self.base / "link-house")
        next(link_house.glob("unicodedata2-*")).unlink()
        link_wheel = link_house / "unicodedata2-17.0.0-cp312-cp312-manylinux_2_17_x86_64.whl"
        with zipfile.ZipFile(link_wheel, "w") as archive:
            archive.writestr(
                "unicodedata2-17.0.0.dist-info/WHEEL",
                "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: cp312-cp312-manylinux_2_17_x86_64\n",
            )
            archive.writestr("unicodedata2-17.0.0.dist-info/METADATA", "Name: unicodedata2\nVersion: 17.0.0\n")
            link = zipfile.ZipInfo("unicodedata2.py")
            link.create_system = 3
            link.external_attr = 0o120777 << 16
            archive.writestr(link, b"target")
        link_source = self.base / "link-source"
        link_source.mkdir()
        write_source(link_source, lock=lock_from_wheels(link_house))
        commit_source(link_source)
        self.refuse(link_source, link_house, self.output_dir("link-out"), "SYMLINK")
        meta_house = copy_house(wheels, self.base / "meta-house")
        next(meta_house.glob("unicodedata2-*")).unlink()
        meta_wheel = meta_house / "unicodedata2-17.0.0-cp312-cp312-manylinux_2_17_x86_64.whl"
        with zipfile.ZipFile(meta_wheel, "w") as archive:
            archive.writestr(
                "unicodedata2-17.0.0.dist-info/WHEEL",
                "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: cp312-cp312-manylinux_2_17_x86_64\n",
            )
            archive.writestr("unicodedata2-17.0.0.dist-info/METADATA", "Name: spoofed\nVersion: 17.0.0\n")
            archive.writestr("unicodedata2.py", b"x=1\n")
        meta_source = self.base / "meta-source"
        meta_source.mkdir()
        write_source(meta_source, lock=lock_from_wheels(meta_house))
        commit_source(meta_source)
        self.refuse(meta_source, meta_house, self.output_dir("meta-out"), "WRONG_TAG")
        weird = self.base / "dir-house"
        weird.mkdir()
        (weird / "not-a-wheel").mkdir()
        self.refuse(source, weird, self.output_dir("dir-out"), "NONREGULAR")

    def test_every_size_and_count_bound_is_enforced(self) -> None:
        source, wheels = happy_fixture(self.base / "bounds")
        output = self.output_dir("path-out")
        with bounded("MAX_PATH_BYTES", 8):
            self.refuse(source, wheels, output, "BOUNDS")
        with bounded("MAX_FILE_BYTES", 4):
            self.refuse(source, wheels, self.output_dir("file-out"), "BOUNDS")
        with bounded("MAX_FILE_COUNT", 3):
            self.refuse(source, wheels, self.output_dir("count-out"), "BOUNDS")
        with bounded("MAX_UNCOMPRESSED_BYTES", 8):
            self.refuse(source, wheels, self.output_dir("sum-out"), "BOUNDS")
        with bounded("MAX_ARCHIVE_BYTES", 1):
            self.refuse(source, wheels, self.output_dir("archive-out"), "BOUNDS")

    def test_injected_verify_failure_cleans_only_same_inode_output(self) -> None:
        source, wheels = happy_fixture(self.base / "inject")
        output = self.output_dir("inject-out")
        marker = self.base / "unrelated.txt"
        marker.write_text("leave-me\n", encoding="utf-8")

        def injected(staging: Path, archive: Path, files: list) -> None:
            shutil.rmtree(output)
            output.mkdir()
            (output / "attacker.txt").write_text("replaced\n", encoding="utf-8")
            raise RuntimeError("injected verify failure")

        with mock.patch.object(BUILDER, "write_archive", injected):
            with self.assertRaises(RuntimeError):
                BUILDER.build_artifact(source, wheels, output, TARGET)
        self.assertTrue((output / "attacker.txt").is_file())
        self.assertEqual("replaced\n", (output / "attacker.txt").read_text(encoding="utf-8"))
        self.assertTrue(marker.is_file())
        self.assertEqual("leave-me\n", marker.read_text(encoding="utf-8"))

    def test_lock_bypass_shapes_are_refused(self) -> None:
        source = self.base / "bypass"
        source.mkdir()
        house = self.base / "bypass-house"
        house.mkdir()
        write_source(source, lock="attrs @ https://example.invalid/attrs.whl\n")
        commit_source(source)
        self.refuse(source, house, self.output_dir("bypass-out"), "LOCK_BYPASS")
        partial = self.base / "partial"
        partial.mkdir()
        write_source(
            partial,
            lock="unicodedata2==17.0.0 \\\n    --hash=sha256:" + ("ab" * 32)
            + "\nattrs==26.1.0 \\\n    --hash=sha256:" + ("cd" * 32) + "\n",
        )
        commit_source(partial)
        self.refuse(partial, house, self.output_dir("partial-out"), "LOCK_BYPASS")

    def test_dirty_requirements_lock_is_refused_as_source_dirty(self) -> None:
        source, wheels = happy_fixture(self.base / "dirty-lock")
        (source / "requirements.txt").write_text(
            (source / "requirements.txt").read_text(encoding="utf-8").replace("26.1.0", "26.1.1", 1),
            encoding="utf-8",
        )
        self.refuse(source, wheels, self.output_dir("dirty-lock-out"), "SOURCE_DIRTY")

    def test_bytecode_cache_is_skipped_and_left_on_disk(self) -> None:
        source, wheels = happy_fixture(self.base / "cache")
        cache = source / "workstack" / "__pycache__"
        cache.mkdir()
        pyc = cache / "__init__.cpython-312.pyc"
        pyc.write_bytes(b"\x00bytecode")
        archive, _sidecar = BUILDER.build_artifact(source, wheels, self.output_dir("cache-out"), TARGET)
        self.assertTrue(pyc.is_file())
        self.assertEqual(b"\x00bytecode", pyc.read_bytes())
        with zipfile.ZipFile(archive) as handle:
            names = [info.filename.replace("\\", "/") for info in handle.infolist()]
        self.assertFalse(any("__pycache__" in name or name.endswith(".pyc") for name in names))

    def test_relative_and_dangling_symlink_output_are_refused(self) -> None:
        source, wheels = happy_fixture(self.base / "outpath")
        with self.assertRaises(BUILDER.ArtifactBuildError) as raised:
            BUILDER.build_artifact(source, wheels, Path("relative-out"), TARGET)
        self.assertEqual("OUTPUT_EXISTS", raised.exception.code)
        dangling = self.output_dir("dangle-out")
        target = self.base / "missing-target"
        created = False
        try:
            os.symlink(str(target), str(dangling), target_is_directory=True)
            created = True
        except OSError:
            try:
                os.symlink(str(target), str(dangling), target_is_directory=False)
                created = True
            except OSError:
                target.mkdir()
                completed = subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(dangling), str(target)],
                    capture_output=True,
                    text=True,
                )
                if completed.returncode == 0:
                    target.rmdir()
                    created = True
        if not created:
            self.skipTest("cannot create a dangling symlink on this host")
        self.assertTrue(os.path.lexists(dangling))
        self.refuse(source, wheels, dangling, "OUTPUT_EXISTS")
        try:
            dangling.unlink()
        except OSError:
            dangling.rmdir()

    def test_eol_checkouts_of_the_same_tree_emit_byte_identical_artifacts(self) -> None:
        source, wheels = happy_fixture(self.base / "eol-base")
        for path in source.rglob("*"):
            if not path.is_file() or ".git" in path.parts:
                continue
            raw = path.read_bytes()
            if b"\0" in raw:
                continue
            converted = raw.replace(b"\r\n", b"\n")
            if converted != raw:
                path.write_bytes(converted)
        git(source, "add", "-A")
        git(source, "commit", "-m", "store-lf-blobs")
        (source / ".gitignore").write_text(
            ".artifacts/\nfrontend/dist/\n", encoding="utf-8", newline="\n"
        )
        git(source, "rm", "-r", "--cached", "frontend/dist")
        git(source, "add", ".gitignore")
        git(source, "commit", "-m", "ignore generated dist")
        dist = source / "frontend" / "dist"
        lf_root = self.base / "eol-lf"
        crlf_root = self.base / "eol-crlf"
        subprocess.run(
            ["git", "clone", "-c", "core.autocrlf=false", str(source), str(lf_root)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "clone", "-c", "core.autocrlf=true", str(source), str(crlf_root)],
            check=True,
            capture_output=True,
            text=True,
        )
        shutil.copytree(dist, lf_root / "frontend" / "dist")
        shutil.copytree(dist, crlf_root / "frontend" / "dist")
        seal_dist(lf_root)
        seal_dist(crlf_root)
        self.assertIn(b"\r\n", (crlf_root / "README.md").read_bytes())
        self.assertNotIn(b"\r\n", (lf_root / "README.md").read_bytes())
        self.assertNotEqual((crlf_root / "README.md").read_bytes(), (lf_root / "README.md").read_bytes())
        archive_lf, sidecar_lf = BUILDER.build_artifact(lf_root, wheels, self.output_dir("out-lf"), TARGET)
        archive_crlf, sidecar_crlf = BUILDER.build_artifact(crlf_root, wheels, self.output_dir("out-crlf"), TARGET)
        self.assertEqual(archive_lf.read_bytes(), archive_crlf.read_bytes())
        self.assertEqual(sidecar_lf.read_bytes(), sidecar_crlf.read_bytes())
        with zipfile.ZipFile(archive_crlf) as handle:
            packed = handle.read("payload/README.md")
        self.assertEqual(b"readme\n", packed)
        self.assertNotIn(b"\r\n", packed)

    def test_source_head_movement_during_blob_load_is_refused(self) -> None:
        source, wheels = happy_fixture(self.base / "move")
        original = BUILDER.GIT.frozen_payload_and_lock

        def load_and_move(*arguments, **keyword_arguments):
            result = original(*arguments, **keyword_arguments)
            git(source, "commit", "--allow-empty", "-m", "move-head")
            return result

        with mock.patch.object(BUILDER.GIT, "frozen_payload_and_lock", load_and_move):
            self.refuse(source, wheels, self.output_dir("move-out"), "SOURCE_UNPINNED")

    def test_required_roster_file_set_covers_the_documented_product_files(self) -> None:
        self.assertEqual(
            (
                "run_work_stack.py",
                "desktop/python-webview-shell/remote_entry.py",
                "desktop/python-webview-shell/remote_command_contract.py",
                "desktop/python-webview-shell/remote_owner.py",
                "desktop/python-webview-shell/remote_owner_receipt.py",
                "desktop/python-webview-shell/remote_owner_stop.py",
                "desktop/python-webview-shell/remote_stop_result.py",
                "desktop/python-webview-shell/remote_update_owner_observation.py",
                "desktop/python-webview-shell/remote_update_maintenance.py",
                "desktop/python-webview-shell/remote_update_maintenance_receipts.py",
                "desktop/python-webview-shell/remote_process_handle.py",
                "desktop/python-webview-shell/remote_receipt_guard.py",
                "desktop/python-webview-shell/remote_receipt_io.py",
                "desktop/python-webview-shell/remote_skill_install.py",
                "README.md",
                "LICENSE",
                "SECURITY.md",
                "THIRD_PARTY_NOTICES.md",
                "integrations/agent-skill/work-stack/SKILL.md",
                "integrations/agent-skill/work-stack/references/commands.md",
                "integrations/agent-skill/work-stack/references/journal-policy.md",
            ),
            BUILDER.ROSTER_FILES,
        )
        self.assertIn(BUILDER.ENTRYPOINT, BUILDER.ROSTER_FILES)
        self.assertEqual(("workstack", "contracts", "web", "licenses"), BUILDER.FROZEN_DIRS)
        self.assertIn("frontend/dist", BUILDER.ROSTER_DIRS)

    def test_payload_contains_exact_agent_skill_bytes(self) -> None:
        source, wheels = happy_fixture(self.base / "skill-bytes")
        archive, _sidecar = BUILDER.build_artifact(source, wheels, self.output_dir("skill-out"), TARGET)
        skill_paths = (
            "integrations/agent-skill/work-stack/SKILL.md",
            "integrations/agent-skill/work-stack/references/commands.md",
            "integrations/agent-skill/work-stack/references/journal-policy.md",
        )
        with zipfile.ZipFile(archive) as handle:
            names = [info.filename.replace("\\", "/") for info in handle.infolist()]
            for relative in skill_paths:
                packed = handle.read("payload/" + relative)
                self.assertEqual(packed, (source / relative).read_bytes())
                self.assertEqual(packed, (ROOT / relative).read_bytes())
                self.assertEqual(1, names.count("payload/" + relative))

    def test_each_required_roster_file_deleted_in_a_clean_commit_is_refused(self) -> None:
        for index, required in enumerate(BUILDER.ROSTER_FILES):
            with self.subTest(required=required):
                source, wheels = happy_fixture(self.base / "file-{0}".format(index))
                drop_and_commit(source, required)
                self.assertEqual("", git(source, "status", "--porcelain"))
                self.assertNotIn(required, git(source, "ls-tree", "-r", "--name-only", "HEAD").splitlines())
                output = self.output_dir("file-out-{0}".format(index))
                with self.assertRaises(BUILDER.ArtifactBuildError) as raised:
                    BUILDER.build_artifact(source, wheels, output, TARGET)
                self.assertEqual("ROSTER_MISSING", raised.exception.code)
                self.assertIn(required, raised.exception.detail)
                self.assertFalse(os.path.lexists(output))

    def test_each_required_roster_directory_emptied_in_a_clean_commit_is_refused(self) -> None:
        for index, root in enumerate(BUILDER.FROZEN_DIRS):
            with self.subTest(root=root):
                source, wheels = happy_fixture(self.base / "dir-{0}".format(index))
                drop_and_commit(source, root)
                self.assertEqual("", git(source, "status", "--porcelain"))
                output = self.output_dir("dir-out-{0}".format(index))
                with self.assertRaises(BUILDER.ArtifactBuildError) as raised:
                    BUILDER.build_artifact(source, wheels, output, TARGET)
                self.assertEqual("ROSTER_MISSING", raised.exception.code)
                self.assertIn(root, raised.exception.detail)
                self.assertFalse(os.path.lexists(output))

    def test_generated_dist_emptied_after_the_frozen_check_is_still_refused(self) -> None:
        """The dist/source gate refuses it before any payload byte is assembled."""

        source, wheels = happy_fixture(self.base / "dist-gone")
        (source / ".gitignore").write_text(
            ".artifacts/\nfrontend/dist/\n", encoding="utf-8", newline="\n"
        )
        git(source, "rm", "-r", "-q", "--cached", "frontend/dist")
        git(source, "add", ".gitignore")
        git(source, "commit", "-m", "ignore generated dist")
        for child in (source / "frontend" / "dist").iterdir():
            child.unlink()
        self.assertEqual("", git(source, "status", "--porcelain"))
        output = self.output_dir("dist-out")
        with self.assertRaises(BUILDER.ArtifactBuildError) as raised:
            BUILDER.build_artifact(source, wheels, output, TARGET)
        self.assertEqual("DIST_MISSING", raised.exception.code)
        self.assertIn("frontend/dist", raised.exception.detail)
        self.assertFalse(os.path.lexists(output))

    def test_dist_that_does_not_match_its_recorded_source_is_refused(self) -> None:
        """A committed frontend source change alone invalidates the dist."""

        source, wheels = happy_fixture(self.base / "dist-drift")
        (source / "frontend" / "src" / "main.tsx").write_text(
            "export const main = 2\n", encoding="utf-8"
        )
        git(source, "add", "-A")
        git(source, "commit", "-m", "advance the ui source")
        self.assertEqual("", git(source, "status", "--porcelain"))

        output = self.output_dir("dist-drift-out")
        with self.assertRaises(BUILDER.ArtifactBuildError) as raised:
            BUILDER.build_artifact(source, wheels, output, TARGET)

        self.assertEqual("DIST_SOURCE_DRIFT", raised.exception.code)
        self.assertIn("refresh-dist", raised.exception.detail)
        self.assertFalse(os.path.lexists(output))

    def test_packaging_without_a_dist_receipt_refuses_with_the_repair_command(self) -> None:
        source, wheels = happy_fixture(self.base / "dist-unsealed")
        BUILDER.DIST_GATE.receipt_path(source).unlink()

        output = self.output_dir("dist-unsealed-out")
        with self.assertRaises(BUILDER.ArtifactBuildError) as raised:
            BUILDER.build_artifact(source, wheels, output, TARGET)

        self.assertEqual("DIST_RECEIPT_MISSING", raised.exception.code)
        self.assertIn("refresh-dist", raised.exception.detail)
        self.assertFalse(os.path.lexists(output))

    def test_hand_edited_dist_is_refused_even_when_the_source_is_unchanged(self) -> None:
        source, wheels = happy_fixture(self.base / "dist-tampered")
        ignore_generated_dist(source)
        (source / "frontend" / "dist" / "index.html").write_text("tampered\n", encoding="utf-8")
        self.assertEqual("", git(source, "status", "--porcelain"))

        output = self.output_dir("dist-tampered-out")
        with self.assertRaises(BUILDER.ArtifactBuildError) as raised:
            BUILDER.build_artifact(source, wheels, output, TARGET)

        self.assertEqual("DIST_CONTENT_DRIFT", raised.exception.code)
        self.assertFalse(os.path.lexists(output))

    def test_uncommitted_frontend_build_inputs_are_refused(self) -> None:
        """The receipt binds dist to content; this binds that content to the commit."""

        source, wheels = happy_fixture(self.base / "dist-dirty")
        (source / "frontend" / "src" / "main.tsx").write_text(
            "export const main = 3\n", encoding="utf-8"
        )
        seal_dist(source)

        output = self.output_dir("dist-dirty-out")
        with self.assertRaises(BUILDER.ArtifactBuildError) as raised:
            BUILDER.build_artifact(source, wheels, output, TARGET)

        self.assertEqual("SOURCE_DIRTY", raised.exception.code)
        self.assertFalse(os.path.lexists(output))

    def test_frontend_build_inputs_are_admission_only_and_never_payload(self) -> None:
        for path in BUILDER.GENERATED_SOURCE_PATHS:
            with self.subTest(path=path):
                self.assertIn(path, BUILDER.CLEAN_PATHS)
                self.assertNotIn(path, BUILDER.ADMISSION_PATHS)
                self.assertNotIn(path, BUILDER.FROZEN_ROOTS)
        self.assertEqual(
            BUILDER.ADMISSION_PATHS,
            BUILDER.ROSTER_DIRS + BUILDER.ROSTER_FILES + ("requirements.txt",),
        )

    def test_required_roster_file_of_the_wrong_type_is_refused(self) -> None:
        link_source, wheels = happy_fixture(self.base / "roster-link")
        git(link_source, "config", "core.symlinks", "false")
        hashed = subprocess.run(
            ["git", "hash-object", "-w", "--stdin"],
            cwd=str(link_source),
            input=b"SECURITY.md",
            capture_output=True,
            check=True,
        )
        sha = hashed.stdout.decode("ascii").strip()
        git(link_source, "update-index", "--add", "--cacheinfo", "120000,{0},README.md".format(sha))
        (link_source / "README.md").write_bytes(b"SECURITY.md")
        git(link_source, "commit", "-m", "readme-as-symlink")
        self.assertEqual("", git(link_source, "status", "--porcelain"))
        link_out = self.output_dir("roster-link-out")
        self.refuse(link_source, wheels, link_out, "SYMLINK")
        self.assertFalse(os.path.lexists(link_out))
        dir_source, dir_wheels = happy_fixture(self.base / "roster-dir")
        drop_and_commit(dir_source, "README.md")
        (dir_source / "README.md").mkdir()
        (dir_source / "README.md" / "inner.md").write_text("readme\n", encoding="utf-8")
        git(dir_source, "add", "-A")
        git(dir_source, "commit", "-m", "readme-as-directory")
        dir_out = self.output_dir("roster-dir-out")
        with self.assertRaises(BUILDER.ArtifactBuildError) as raised:
            BUILDER.build_artifact(dir_source, dir_wheels, dir_out, TARGET)
        self.assertEqual("ROSTER_MISSING", raised.exception.code)
        self.assertIn("README.md", raised.exception.detail)
        self.assertFalse(os.path.lexists(dir_out))

    def test_frozen_git_symlink_payload_is_refused(self) -> None:
        source, wheels = happy_fixture(self.base / "gitlink")
        git(source, "config", "core.symlinks", "false")
        hashed = subprocess.run(
            ["git", "hash-object", "-w", "--stdin"],
            cwd=str(source),
            input=b"target",
            capture_output=True,
            check=True,
        )
        sha = hashed.stdout.decode("ascii").strip()
        git(source, "update-index", "--add", "--cacheinfo", "120000,{0},workstack/link".format(sha))
        (source / "workstack" / "link").write_bytes(b"target")
        git(source, "commit", "-m", "symlink-blob")
        self.refuse(source, wheels, self.output_dir("git-symlink"), "SYMLINK")

    # --- what the packager actually consumes -------------------------------

    def racing_materialize(self, mutate, restore=None):
        """Change the live frontend/dist across the payload copy, as any process may.

        frontend/dist is generated and git-ignored, so the gate call above it
        cannot hold it still. The mutation lands after the gate has admitted the
        tree and before materialize_payload reads a byte of it.
        """

        original = BUILDER.materialize_payload

        def wrapper(source, payload, wheels, blobs):
            mutate()
            try:
                original(source, payload, wheels, blobs)
            finally:
                if restore is not None:
                    restore()

        return mock.patch.object(BUILDER, "materialize_payload", wrapper)

    def seal_two_file_dist(self, source: Path) -> Path:
        ignore_generated_dist(source)
        assets = source / "frontend" / "dist" / "assets"
        assets.mkdir(parents=True, exist_ok=True)
        (assets / "app.js").write_bytes(b"export const build = 1\n")
        seal_dist(source)
        return assets / "app.js"

    def refuse_staged(self, source: Path, wheels: Path, output: Path, expected: str):
        with self.assertRaises(BUILDER.ArtifactBuildError) as raised:
            BUILDER.build_artifact(source, wheels, output, TARGET)
        self.assertEqual("DIST_STAGED_DRIFT", raised.exception.code)
        self.assertIn(expected, raised.exception.detail)
        self.assertFalse(os.path.lexists(output))
        return raised.exception

    def test_the_archive_ships_exactly_the_dist_bytes_the_gate_admitted(self) -> None:
        source, wheels = happy_fixture(self.base / "dist-admitted")
        ignore_generated_dist(source)
        admitted = BUILDER.assert_dist_matches_source(source)

        archive, _sidecar = BUILDER.build_artifact(
            source, wheels, self.output_dir("dist-admitted-out"), TARGET
        )

        prefix = "payload/frontend/dist/"
        with zipfile.ZipFile(archive) as handle:
            shipped = {
                info.filename[len(prefix):]: BUILDER.sha256_bytes(handle.read(info))
                for info in handle.infolist()
                if info.filename.replace("\\", "/").startswith(prefix)
            }
        self.assertTrue(shipped)
        self.assertEqual(
            {entry["path"]: entry["sha256"] for entry in admitted["dist_files"]}, shipped
        )

    def test_an_emitted_asset_mutated_after_the_gate_cannot_reach_the_archive(self) -> None:
        source, wheels = happy_fixture(self.base / "dist-race")
        ignore_generated_dist(source)
        emitted = source / "frontend" / "dist" / "index.html"
        output = self.output_dir("dist-race-out")

        with self.racing_materialize(lambda: emitted.write_bytes(b"swapped\n")):
            self.refuse_staged(source, wheels, output, "index.html")

        self.assertEqual(b"swapped\n", emitted.read_bytes())

    def test_a_live_dist_change_restored_during_materialization_is_still_refused(self) -> None:
        """The staged copy is a mixed snapshot even though the live tree is honest."""

        source, wheels = happy_fixture(self.base / "dist-restore")
        ignore_generated_dist(source)
        emitted = source / "frontend" / "dist" / "index.html"
        original = emitted.read_bytes()
        output = self.output_dir("dist-restore-out")

        with self.racing_materialize(
            lambda: emitted.write_bytes(b"swapped\n"), lambda: emitted.write_bytes(original)
        ):
            self.refuse_staged(source, wheels, output, "index.html")

        # A second live check after the copy would have passed, which is exactly
        # why it is not what binds the payload.
        self.assertEqual(original, emitted.read_bytes())
        self.assertIsNotNone(BUILDER.assert_dist_matches_source(source))

    def test_an_emitted_file_added_during_materialization_is_refused(self) -> None:
        source, wheels = happy_fixture(self.base / "dist-added")
        ignore_generated_dist(source)
        injected = source / "frontend" / "dist" / "injected.js"
        output = self.output_dir("dist-added-out")

        with self.racing_materialize(
            lambda: injected.write_bytes(b"export const injected = 1\n"), injected.unlink
        ):
            self.refuse_staged(source, wheels, output, "injected.js")

        self.assertFalse(injected.exists())
        self.assertIsNotNone(BUILDER.assert_dist_matches_source(source))

    def test_an_emitted_file_deleted_during_materialization_is_refused(self) -> None:
        source, wheels = happy_fixture(self.base / "dist-deleted")
        asset = self.seal_two_file_dist(source)
        original = asset.read_bytes()
        output = self.output_dir("dist-deleted-out")

        with self.racing_materialize(asset.unlink, lambda: asset.write_bytes(original)):
            self.refuse_staged(source, wheels, output, "assets/app.js")

        self.assertEqual(original, asset.read_bytes())
        self.assertIsNotNone(BUILDER.assert_dist_matches_source(source))


if __name__ == "__main__":
    unittest.main()
