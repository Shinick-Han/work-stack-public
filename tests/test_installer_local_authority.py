from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/windows/Resolve-WorkStackInstallerAuthority.py"
PROFILE = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
UID = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"

AST_HARNESS = r'''
param([string]$CasePath)
$ErrorActionPreference = 'Stop'
$case = Get-Content -Raw -LiteralPath $CasePath | ConvertFrom-Json
$text = [IO.File]::ReadAllText($case.installer)
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseInput($text,[ref]$tokens,[ref]$errors)
if ($errors.Count) { throw 'Installer AST parse failure' }
$firstWriter = $ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Write-BytesAtomic'},$true)
$selection = $text.Substring(0,$firstWriter.Extent.StartOffset)
$revalidate = $ast.Find({param($n) $n -is [Management.Automation.Language.AssignmentStatementAst] -and $n.Left.Extent.Text -eq '$revalidatedAuthority'},$true)
$refusal = $ast.Find({param($n) $n -is [Management.Automation.Language.IfStatementAst] -and $n.Clauses[0].Item1.Extent.Text -eq '$revalidatedAuthority.binding -cne $initialAuthority.binding'},$true)
$backup = $ast.Find({param($n) $n -is [Management.Automation.Language.IfStatementAst] -and $n.Clauses[0].Item1.Extent.Text -eq "Test-Path -LiteralPath (Join-Path `$dataPath 'workspace.json')"},$true)
$persist = $ast.Find({param($n) $n -is [Management.Automation.Language.AssignmentStatementAst] -and $n.Left.Extent.Text -eq "`$configValues['data_dir']"},$true)
$stop = $ast.Find({param($n) $n -is [Management.Automation.Language.CommandAst] -and $n.Extent.Text -eq '& $stopScript -InstallRoot $installPath'},$true)
if (-not $revalidate -or -not $refusal -or -not $backup -or -not $persist -or -not $stop) { throw 'Required installer AST missing' }
if ($revalidate.Extent.StartOffset -ge $stop.Extent.StartOffset -or $refusal.Extent.EndOffset -ge $stop.Extent.StartOffset) { throw 'Revalidation is after Stop' }
$script:effects = @()
$script:stops = @()
function Record-Backup { $script:effects += ,@($args); $global:LASTEXITCODE=0 }
function Record-Stop { $script:stops += ,@($args) }
$arguments = @{SourceRoot=$case.source;InstallRoot=$case.install;StateRoot=$case.state;NoShortcut=$true}
if ($null -ne $case.explicit) { $arguments.DataDir=$case.explicit }
try {
    . ([scriptblock]::Create($selection)) @arguments
    if ($case.mutation) {
        $mutationPath = [IO.Path]::GetFullPath([string]$case.mutation.path)
        if (-not $mutationPath.StartsWith([string]$case.root + '\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Mutation escaped fixture' }
        if ($case.mutation.remove) { Remove-Item -LiteralPath $mutationPath }
        else {
            [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($mutationPath)) | Out-Null
            [IO.File]::WriteAllBytes($mutationPath,[Convert]::FromBase64String($case.mutation.bytes))
        }
    }
    $staging = $case.staging
    . ([scriptblock]::Create($revalidate.Extent.Text + "`n" + $refusal.Extent.Text))
    $stopScript='Record-Stop'
    . ([scriptblock]::Create($stop.Extent.Text))
    $stagedPython='Record-Backup'
    $stagedEntry=Join-Path $staging 'run_work_stack.py'
    . ([scriptblock]::Create($backup.Extent.Text))
    $configValues=[ordered]@{}
    . ([scriptblock]::Create($persist.Extent.Text))
    @{ok=$true;data=$dataPath;persisted=$configValues['data_dir'];backup=$script:effects;stops=$script:stops;binding=$initialAuthority.binding;port=$Port;retention=$BackupRetention;backup_root=$backupRoot} | ConvertTo-Json -Depth 8 -Compress
} catch {
    @{ok=$false;error=$_.Exception.Message;backup=$script:effects;stops=$script:stops} | ConvertTo-Json -Depth 8 -Compress
}
'''


def hashes(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


class InstallerLocalAuthorityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.container = Path(os.environ.get("WORK_STACK_TEST_RESULT_ROOT", tempfile.gettempdir())).resolve()
        cls.temporary = tempfile.TemporaryDirectory(dir=cls.container, prefix="i")
        cls.root = Path(cls.temporary.name).resolve()
        assert cls.root.is_relative_to(cls.container)
        cls.old_env = dict(os.environ)
        cls.old_temp = tempfile.tempdir
        for key, name in {"TEMP":"t","TMP":"t","TMPDIR":"t","WORK_STACK_HOME":"h","WORK_STACK_RUNTIME":"r","LOCALAPPDATA":"l"}.items():
            destination = cls.root / name
            assert destination.resolve().is_relative_to(cls.container)
            destination.mkdir(exist_ok=True)
            os.environ[key] = str(destination)
        tempfile.tempdir = str(cls.root / "t")
        cls.source = cls.root / "s"
        cls.staging = cls.root / "stage"
        for package in (cls.source, cls.staging):
            for name in ("workstack", "contracts"):
                shutil.copytree(ROOT / name, package / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            shell = package / "desktop/python-webview-shell"
            shell.mkdir(parents=True)
            for name in ("connection_registry.py", "local_workspace_rebind.py", "profile_inspection.py"):
                shutil.copy2(ROOT / "desktop/python-webview-shell" / name, shell / name)
            windows = package / "scripts/windows"
            windows.mkdir(parents=True)
            shutil.copy2(SCRIPT, windows / SCRIPT.name)
            runtime = package / "runtime"
            runtime.mkdir()
            shutil.copy2(sys.executable, runtime / "python.exe")
            base = Path(sys.base_prefix)
            for name in ("python312.dll", "python3.dll", "vcruntime140.dll", "vcruntime140_1.dll"):
                if (base / name).is_file():
                    shutil.copy2(base / name, runtime / name)
            (runtime / "python312._pth").write_text("\n".join(str(base / p) for p in ("Lib", "DLLs", "Lib/site-packages")) + "\nimport site\n", encoding="utf-8")
        cls.harness = cls.root / "probe.ps1"
        cls.harness.write_text(AST_HARNESS, encoding="utf-8")
        spec = importlib.util.spec_from_file_location("installer_authority_test", SCRIPT)
        cls.resolver = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.resolver
        spec.loader.exec_module(cls.resolver)

    @classmethod
    def tearDownClass(cls):
        os.environ.clear()
        os.environ.update(cls.old_env)
        tempfile.tempdir = cls.old_temp
        cls.temporary.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=self.root / "t", prefix="c")
        self.addCleanup(self.temp.cleanup)
        self.case = Path(self.temp.name).resolve()
        self.state = self.case / "state"
        self.state.mkdir()
        self.a, self.b = self.case / "a", self.case / "b"
        for path in (self.a, self.b):
            self.assertTrue(path.resolve().is_relative_to(self.container))
            shutil.copytree(ROOT / "tests/fixtures/store-v3/populated", path)
        self.runtime = self.case / "runtime"
        self.runtime.mkdir()
        self.env = mock.patch.dict(os.environ, {"WORK_STACK_RUNTIME": str(self.runtime)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.registry_path = self.state / "connection-registry.json"
        self.profile = dict(profile_id=PROFILE,label="Local fixture",kind="local",enabled=True,live_updates=True,data_dir=str(self.b),expected_workspace_id=UID)
        self.registry = dict(schema_version=1, active_profile_id=PROFILE, profiles=[self.profile])
        self.config = self.state / "config.json"
        self.config.write_text(json.dumps(dict(data_dir=str(self.a),port=8877,backup_retention=19,backup_dir=str(self.case / "backups"))), encoding="utf-8")

    def save_registry(self):
        self.registry_path.write_text(json.dumps(self.registry), encoding="utf-8")

    def manifest(self):
        from workstack.store import _validate_store_manifest_header, _validate_store_manifest_files, _validate_store_manifest_tasks
        value = dict(version=1,workspace_id=UID,store_schema_version=3,generation=0,
                     files={name:"sha256:"+hashlib.sha256((self.b/name).read_bytes()).hexdigest() for name in self.resolver.inspection.STORE_FILES},tasks={})
        _validate_store_manifest_header(value)
        _validate_store_manifest_files(value["files"])
        _validate_store_manifest_tasks(value["tasks"])
        return value

    def baseline_path(self):
        return self.resolver.derive_store_runtime_root(self.b) / self.resolver.STORE_MANIFEST_NAME

    def save_baseline(self, value=None):
        path = self.baseline_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.manifest() if value is None else value), encoding="utf-8")
        return path

    def resolve_pure(self):
        before = hashes(self.case)
        try:
            return self.resolver.resolve_authority(self.state)
        finally:
            self.assertEqual(hashes(self.case), before)

    def ast(self, explicit=None, mutation=None):
        import base64
        change = None
        if mutation is not None:
            path, payload = mutation
            change = dict(path=str(path), remove=payload is None, bytes=base64.b64encode(payload or b"").decode())
        case = dict(root=str(self.case),installer=str(ROOT / "scripts/windows/Install-WorkStack.ps1"),source=str(self.source),staging=str(self.staging),install=str(self.case / "install"),state=str(self.state),explicit=str(explicit) if explicit is not None else None,mutation=change)
        input_path = self.case / "case.json"
        input_path.write_text(json.dumps(case), encoding="utf-8")
        before = hashes(self.case)
        completed = subprocess.run(["pwsh", "-NoProfile", "-File", str(self.harness), "-CasePath", str(input_path)], capture_output=True, text=True, env=dict(os.environ), check=True)
        result = json.loads(completed.stdout.strip().splitlines()[-1])
        if not result["ok"]:
            self.assertEqual(result["stops"], [])
            self.assertEqual(result["backup"], [])
        if mutation is not None:
            path, payload = mutation
            key = str(path.relative_to(self.case))
            if payload is None:
                before.pop(key)
            else:
                before[key] = hashlib.sha256(payload).hexdigest()
        self.assertEqual(hashes(self.case), before)
        return result

    def test_absent_registry_preserves_config_explicit_and_default(self):
        self.assertEqual(self.resolve_pure()["status"], "absent-registry")
        self.assertEqual(self.ast()["persisted"], str(self.a))
        self.assertEqual(self.ast(explicit=self.b)["persisted"], str(self.b))
        self.config.unlink()
        self.assertEqual(self.ast()["persisted"], str(Path(os.environ["LOCALAPPDATA"]) / "WorkStack/data"))

    def test_selected_b_drives_actual_backup_and_persistence_with_existing_config_policy(self):
        self.save_registry()
        result = self.ast()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stops"], [["-InstallRoot", str(self.case / "install")]])
        self.assertEqual(result["persisted"], str(self.b))
        self.assertEqual(result["backup"][0][1:], ["--data-dir", str(self.b), "maintenance", "backup", "--out", str(self.case / "backups")])
        self.assertEqual((result["port"],result["retention"]), (8877,19))

    def test_absent_and_matching_baseline_are_accepted_without_writes(self):
        self.save_registry()
        self.assertEqual(self.resolve_pure()["baseline"]["state"], "absent")
        self.assertFalse(self.baseline_path().exists())
        self.save_baseline()
        self.assertEqual(self.resolve_pure()["baseline"]["state"], "present")
        self.assertTrue(self.ast()["ok"])

    def test_wrong_uid_hash_and_malformed_baseline_refuse(self):
        self.save_registry()
        for change in (dict(workspace_id=OTHER),dict(files={}),dict(tasks=[])):
            with self.subTest(change=change):
                self.save_baseline({**self.manifest(), **change})
                with self.assertRaises(self.resolver.AuthorityError): self.resolve_pure()
        value=self.manifest();value["files"]["notes.json"]="sha256:"+"0"*64
        self.save_baseline(value)
        self.assertFalse(self.ast()["ok"])

    def test_registry_nonregular_malformed_empty_and_disabled_active_refuse(self):
        self.registry_path.mkdir()
        with self.assertRaises(self.resolver.AuthorityError): self.resolve_pure()
        self.registry_path.rmdir()
        for raw in (b"{",b"",json.dumps({**self.registry,"profiles":[]}).encode(),
                    json.dumps({**self.registry,"profiles":[self.profile,self.profile]}).encode()):
            self.registry_path.write_bytes(raw)
            result=self.ast();self.assertFalse(result["ok"]);self.assertEqual(result["backup"],[])
        self.profile["enabled"]=False;self.save_registry()
        self.assertFalse(self.ast()["ok"])

    def test_ssh_missing_directory_wrong_identity_and_v4_refuse(self):
        for change in (dict(data_dir=str(self.case / "missing")),dict(expected_workspace_id=OTHER)):
            with self.subTest(change=change):
                self.registry["profiles"]=[{**self.profile,**change}];self.save_registry()
                self.assertFalse(self.ast()["ok"])
        self.registry["profiles"]=[self.profile];self.save_registry()
        (self.b / "store.json").write_text("{}",encoding="utf-8")
        self.assertFalse(self.ast()["ok"])
        (self.b / "store.json").unlink()
        self.registry["profiles"]=[dict(profile_id=PROFILE,label="SSH",kind="ssh",enabled=True,live_updates=True,expected_workspace_id=UID,ssh_host_alias="fixture",remote_app_dir="/srv/app",remote_data_dir="/srv/data",preferred_forward_port=18765,remote_port=8765)]
        self.save_registry();self.assertFalse(self.ast()["ok"])

    def test_unrelated_disabled_profile_and_matching_explicit_are_accepted(self):
        self.registry["profiles"].append({**self.profile,"profile_id":"cccccccc-cccc-4ccc-8ccc-cccccccccccc","enabled":False,"expected_workspace_id":OTHER,"data_dir":str(self.a)})
        self.save_registry()
        self.assertTrue(self.ast(explicit=self.b)["ok"])
        result=self.ast(explicit=self.a)
        self.assertFalse(result["ok"]);self.assertIn("Explicit DataDir conflicts",result["error"]);self.assertEqual(result["backup"],[])

    def test_actual_staged_revalidation_refuses_profile_and_registry_changes(self):
        self.save_registry()
        original=self.registry_path.read_bytes()
        changed=json.dumps({**self.registry,"profiles":[{**self.profile,"label":"Renamed"}]}).encode()
        for payload in (changed,None,b"{" , original+b" "):
            with self.subTest(payload=payload):
                self.registry_path.write_bytes(original)
                result=self.ast(mutation=(self.registry_path,payload))
                self.assertFalse(result["ok"]);self.assertEqual(result["backup"],[])

    def test_actual_staged_revalidation_refuses_current_file_and_baseline_changes(self):
        self.save_registry()
        notes=self.b / "notes.json";original=notes.read_bytes()
        result=self.ast(mutation=(notes,original+b" "))
        self.assertFalse(result["ok"]);self.assertEqual(result["backup"],[])
        notes.write_bytes(original)
        baseline=self.baseline_path()
        body=json.dumps(self.manifest()).encode()
        result=self.ast(mutation=(baseline,body))
        self.assertFalse(result["ok"]);self.assertEqual(result["backup"],[])
        for payload in (None,body+b" "):
            baseline.write_bytes(body)
            result=self.ast(mutation=(baseline,payload))
            self.assertFalse(result["ok"]);self.assertEqual(result["backup"],[])

    def test_actual_staged_revalidation_refuses_absent_registry_transition(self):
        result=self.ast(mutation=(self.registry_path,json.dumps(self.registry).encode()))
        self.assertFalse(result["ok"]);self.assertEqual(result["backup"],[])

    def test_registry_growth_at_initial_read_is_bounded_without_repair(self):
        self.save_registry()
        limit=4096; grown=self.registry_path.read_bytes()+b" "*(limit*4)
        real_open=Path.open; reads=[];expected=hashes(self.case)
        expected[str(self.registry_path.relative_to(self.case))]=hashlib.sha256(grown).hexdigest()
        class Observed:
            def __init__(self,stream):self.stream=stream
            def __enter__(self):return self
            def __exit__(self,*args):return self.stream.__exit__(*args)
            def read(self,size=-1):
                payload=self.stream.read(size);reads.append((size,len(payload)));return payload
        def opening(path,mode="r",*args,**kwargs):
            if path==self.registry_path and mode=="rb":
                with real_open(path,"wb") as stream:stream.write(grown)
                return Observed(real_open(path,mode,*args,**kwargs))
            return real_open(path,mode,*args,**kwargs)
        with mock.patch.object(self.resolver,"MAX_REGISTRY_BYTES",limit),mock.patch.object(Path,"open",opening):
            with self.assertRaises(self.resolver.AuthorityError):self.resolver.resolve_authority(self.state)
        self.assertEqual(reads,[(limit+1,limit+1)])
        self.assertEqual(hashes(self.case),expected)

    def test_observed_registry_disappearance_and_unreadability_never_mean_absence(self):
        self.save_registry()
        real_open=Path.open
        for failure in (PermissionError("secret path"),FileNotFoundError("vanished")):
            with self.subTest(failure=type(failure).__name__):
                def opening(path,mode="r",*args,**kwargs):
                    if path==self.registry_path and mode=="rb":raise failure
                    return real_open(path,mode,*args,**kwargs)
                before=hashes(self.case)
                with mock.patch.object(Path,"open",opening),self.assertRaises(self.resolver.AuthorityError) as raised:
                    self.resolver.resolve_authority(self.state)
                self.assertNotIn("secret",str(raised.exception))
                self.assertEqual(hashes(self.case),before)

    def test_baseline_growth_at_open_is_bounded_and_refused_without_repair(self):
        self.save_registry()
        target = self.save_baseline()
        limit = 4096
        original = target.read_bytes()
        self.assertLess(len(original), limit)
        grown = original + b" " * (limit * 4)
        expected = hashes(self.case)
        expected[str(target.relative_to(self.case))] = hashlib.sha256(grown).hexdigest()
        real_open = Path.open
        reads = []
        class Observed:
            def __init__(self, stream): self.stream = stream
            def __enter__(self): return self
            def __exit__(self, *args): return self.stream.__exit__(*args)
            def read(self, size=-1):
                raw = self.stream.read(size)
                reads.append((size, len(raw)))
                return raw
        def opening(path, mode="r", *args, **kwargs):
            if path == target and mode == "rb":
                with real_open(target, "wb") as stream: stream.write(grown)
                return Observed(real_open(path, mode, *args, **kwargs))
            return real_open(path, mode, *args, **kwargs)
        with mock.patch.object(self.resolver, "MANIFEST_READ_LIMIT", limit), mock.patch.object(Path, "open", opening):
            with self.assertRaisesRegex(self.resolver.AuthorityError, "evidence_too_large"):
                self.resolver.resolve_authority(self.state)
        self.assertEqual(reads, [(limit + 1, limit + 1)])
        self.assertEqual(hashes(self.case), expected)

    def test_baseline_nonregular_unreadable_and_observed_disappearance_refuse(self):
        self.save_registry()
        target = self.baseline_path()
        target.mkdir(parents=True)
        with self.assertRaises(self.resolver.AuthorityError): self.resolve_pure()
        target.rmdir()
        self.save_baseline()
        real_open = Path.open
        for failure in (PermissionError("private"), FileNotFoundError("lost")):
            def opening(path, mode="r", *args, **kwargs):
                if path == target and mode == "rb": raise failure
                return real_open(path, mode, *args, **kwargs)
            before = hashes(self.case)
            with mock.patch.object(Path, "open", opening), self.assertRaises(self.resolver.AuthorityError):
                self.resolver.resolve_authority(self.state)
            self.assertEqual(hashes(self.case), before)

    def test_staged_reader_is_required_before_recorded_effects(self):
        self.save_registry()
        path = self.staging / "scripts/windows" / SCRIPT.name
        saved = path.read_bytes()
        self.assertTrue(path.resolve().is_relative_to(self.container))
        path.unlink()
        try:
            result = self.ast()
            self.assertFalse(result["ok"])
            self.assertIn("reader is unavailable", result["error"])
        finally:
            path.write_bytes(saved)

    def test_dangling_registry_junction_is_not_absence(self):
        target = self.case / "junction-target"
        target.mkdir()
        self.assertTrue(target.resolve().is_relative_to(self.container))
        subprocess.run(["cmd", "/c", "mklink", "/J", str(self.registry_path), str(target)], check=True, capture_output=True, env=dict(os.environ))
        target.rmdir()
        with self.assertRaisesRegex(self.resolver.AuthorityError, "evidence_reparse"):
            self.resolver.resolve_authority(self.state)

    def test_staged_revalidation_refuses_oversized_registry_and_baseline(self):
        self.save_registry()
        original = self.registry_path.read_bytes()
        result = self.ast(mutation=(self.registry_path, original + b" " * self.resolver.MAX_REGISTRY_BYTES))
        self.assertFalse(result["ok"])
        self.registry_path.write_bytes(original)
        baseline = self.save_baseline()
        result = self.ast(mutation=(baseline, baseline.read_bytes() + b" " * self.resolver.MANIFEST_READ_LIMIT))
        self.assertFalse(result["ok"])

    def test_in_call_evidence_replacement_is_compared_to_the_original_record(self):
        self.save_registry()
        baseline = self.save_baseline()
        for target in (self.registry_path, baseline):
            with self.subTest(target=target.name):
                original = target.read_bytes()
                changed = original + b" "
                expected = hashes(self.case)
                expected[str(target.relative_to(self.case))] = hashlib.sha256(changed).hexdigest()
                real_open = Path.open
                count = [0]
                def opening(path, mode="r", *args, **kwargs):
                    if path == target and mode == "rb":
                        count[0] += 1
                        if count[0] == 2:
                            with real_open(path, "wb") as stream: stream.write(changed)
                    return real_open(path, mode, *args, **kwargs)
                with mock.patch.object(Path, "open", opening), self.assertRaises(self.resolver.AuthorityError):
                    self.resolver.resolve_authority(self.state)
                self.assertEqual(count[0], 2)
                self.assertEqual(hashes(self.case), expected)
                target.write_bytes(original)


if __name__ == "__main__":
    unittest.main()
