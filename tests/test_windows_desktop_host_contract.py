"""Source, AST and recording controls for the same-process desktop host.

Nothing here compiles, launches or installs anything. The C# host is inspected as
source only. The PowerShell contracts are checked through the PowerShell parser's
own AST, and the ownership rules are exercised by executing the *real* pure helper
functions extracted from the owned scripts by extent offsets — never a behavioural
copy maintained in this file, and never a script's top level.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WINDOWS = ROOT / "scripts" / "windows"
HOST_SOURCE = ROOT / "desktop" / "python-webview-shell" / "WorkStackHost.cs"
PWSH = shutil.which("pwsh") or shutil.which("powershell")

FIXTURE_ROOT = Path(
    os.environ.get("WORKSTACK_TEST_FIXTURE_ROOT") or (Path(tempfile.gettempdir()) / "workstack-host-contract")
).resolve()
RESULT_ROOT = Path(os.environ["WORKSTACK_TEST_RESULTS_ROOT"])
if not RESULT_ROOT.is_absolute() or not FIXTURE_ROOT.is_relative_to(RESULT_ROOT.resolve()):
    raise AssertionError("fixture root must be inside the absolute results root before any fixture write")


def _fixture_environment() -> dict:
    environment = dict(os.environ)
    contained = Path(environment["WORKSTACK_TEST_RESULTS_ROOT"]).resolve()
    for name in ("TEMP", "TMP", "TMPDIR", "APPDATA", "LOCALAPPDATA", "WORK_STACK_HOME",
                 "WORK_STACK_RUNTIME", "XDG_CACHE_HOME", "PYTHONPYCACHEPREFIX"):
        path = Path(environment[name])
        if not path.is_absolute() or not path.resolve().is_relative_to(contained):
            raise AssertionError("fixture environment is not a contained Windows path: " + name)
    if not FIXTURE_ROOT.is_relative_to(contained):
        raise AssertionError("fixture root is outside containment")
    environment.update(
        TEMP=str(FIXTURE_ROOT),
        TMP=str(FIXTURE_ROOT),
        PYTHONDONTWRITEBYTECODE="1",
    )
    return environment


def _run_pwsh(body: str, arguments: list) -> str:
    FIXTURE_ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=str(FIXTURE_ROOT)) as directory:
        script = Path(directory) / "probe.ps1"
        script.write_text(body, encoding="utf-8")
        completed = subprocess.run(
            [PWSH, "-NoProfile", "-NonInteractive", "-File", str(script), *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            cwd=directory,
            env=_fixture_environment(),
        )
    if completed.returncode != 0:
        raise AssertionError("pwsh failed: " + (completed.stdout or "") + " | " + (completed.stderr or ""))
    return completed.stdout


_EXTRACT = """param([string]$Path, [string]$Names)
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Path, [ref]$tokens, [ref]$errors)
if ($errors) { throw (($errors | ForEach-Object { $_.Message }) -join '; ') }
$wanted = $Names -split ','
$found = @{}
$seen = @{}
foreach ($function in $ast.FindAll({
    param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst]
}, $true)) {
    if ($wanted -contains $function.Name) {
        # Cardinality is counted on the ORIGINAL file, before the dictionary can
        # collapse a duplicate definition and hide which text would have run.
        if ($seen.ContainsKey($function.Name)) { $seen[$function.Name] = $seen[$function.Name] + 1 }
        else { $seen[$function.Name] = 1 }
        $found[$function.Name] = $function.Extent.Text
    }
}
foreach ($name in $wanted) {
    # Fail closed: a required node that is missing or renamed stops the probe
    # BEFORE any extent is executed.
    if (-not $found.ContainsKey($name)) { throw "required function is missing: $name" }
    if ($seen[$name] -ne 1) { throw "required function is not unique in the original source: $name" }
}
($wanted | ForEach-Object { $found[$_] }) -join "`n"
"""


def extract_functions(path: Path, names: list) -> str:
    """Return the verbatim source of the named functions, or fail closed."""

    return _run_pwsh(_EXTRACT, [str(path), ",".join(names)])


# The closed domain. Membership is by exact identity, never by literal syntax: a
# permitted receiver does not make every member on it permitted, and a permitted
# member name does not travel to another receiver.
# Constructions the selected source may ask the SUBSTITUTE for. New-Object is a
# substituted leaf, so the real cmdlet never runs: no COM object, no arbitrary
# type and no effectful constructor argument can be built from selected source.
ALLOWED_CONSTRUCTORS = frozenset(
    {
        "System.Text.StringBuilder",
        "System.Collections.Generic.List[string]",
        "psobject",
    }
)
ALLOWED_COM_PROGIDS = frozenset({"WScript.Shell"})
ALLOWED_COMMANDS = frozenset(
    {
        "Split-WorkStackCommandLine",
        "Split-WorkStackShortcutArguments",
        "Test-WorkStackExactPath",
        "Test-WorkStackAbsolutePath",
        "Test-WorkStackIntegerValue",
        "Test-WorkStackDesktopGrammar",
        "Test-WorkStackDesktopInvocation",
        "Remove-OwnedShortcut",
        "Join-Path",
        "Test-Path",
        "Write-Warning",
        "ForEach-Object",
        "Where-Object",
        "Add-Member",
        "ConvertTo-Json",
    }
)
# (receiver, member) pairs. IO.Path is allowed for exactly these members:
# GetTempFileName, GetRandomFileName and friends are NOT reachable through it.
ALLOWED_MEMBERS = frozenset(
    {
        ("IO.Path", "GetFullPath"),
        ("IO.Path", "GetInvalidPathChars"),
        ("IO.Path", "DirectorySeparatorChar"),
        ("char", "IsHighSurrogate"),
        ("char", "IsLowSurrogate"),
        ("char", "ConvertToUtf32"),
        ("char", "IsWhiteSpace"),
        ("string", "Equals"),
        ("bool", "Parse"),
        ("System.Text.StringBuilder", "new"),
        ("System.Collections.Generic.List[string]", "new"),
        ("string", "IsNullOrWhiteSpace"),
        ("string", "IsNullOrEmpty"),
        ("Math", "Floor"),
        ("object", "ReferenceEquals"),
        ("regex", "Replace"),
        ("StringComparison", "OrdinalIgnoreCase"),
        ("StringComparison", "Ordinal"),
    }
)
# Instance members reachable on values the extents build themselves.
ALLOWED_INSTANCE_MEMBERS = frozenset(
    {
        "Append",
        "Clear",
        "ToString",
        "ToLowerInvariant",
        "Equals",
        "ToArray",
        "Add",
        "Trim",
        "TrimEnd",
        "StartsWith",
        "EndsWith",
        "Contains",
        "IndexOf",
        "IndexOfAny",
        "Substring",
        "ToCharArray",
        "ContainsKey",
        "Length",
        "Count",
        # An inert constant read in the product notification guard.
        "MaxNotifyPath",
        "ShcneCreate",
        "ShcneUpdateItem",
        "ShcnfPathW",
        "ShcnfFlushNoWait",
        "Exception",
        "Message",
    }
)
ALLOWED_TYPES = frozenset(
    {
        "string",
        "string[]",
        "bool",
        "int",
        "char",
        "char[]",
        "void",
        "regex",
        "hashtable",
        "Math",
        "IO.Path",
        "StringComparison",
        "System.Collections.Generic.List[string]",
        "System.Text.StringBuilder",
        "switch",
        "psobject",
        "object",
        "pscustomobject",
    }
)
# Members that must never appear, whatever the receiver.
FORBIDDEN_MEMBERS = frozenset(
    {
        "GetType",
        "InvokeMember",
        "GetMethod",
        "GetProperty",
        "Invoke",
        "GetTempFileName",
        "GetRandomFileName",
        "Create",
        "Delete",
        "WriteAllText",
        "WriteAllBytes",
        "AppendAllText",
        "Start",
        "Assembly",
        "Module",
    }
)
# Receiver-bound instance members: a member name never travels to another
# receiver. Each pair names the retained fake variable that may carry it.
ALLOWED_RECEIVER_MEMBERS = frozenset(
    {
        ("compile", "WaitForExit"),
        ("compile", "Kill"),
        ("compile", "ExitCode"),
    }
)
# The one construction fake, installed by the trusted preamble. Only the helper's
# own inert StringBuilder/List constructions are honoured; the COM branch returns a
# retained fake shortcut and never touches WScript.Shell.
NEW_OBJECT_FAKE = (
    "param([Parameter(Position = 0)]$TypeName, $ComObject); "
    "if (-not $ComObject) { "
    "if ($TypeName -eq 'System.Text.StringBuilder') { return [System.Text.StringBuilder]::new() }; "
    "if ($TypeName -eq 'psobject') { return [pscustomobject]@{} }; "
    "if ($TypeName -ceq 'System.Collections.Generic.List[string]') { return ,[System.Collections.Generic.List[string]]::new() }; "
    "throw 'REJECT: unknown fake constructor' }; "
    "if ($TypeName -or $ComObject -cne 'WScript.Shell') { throw 'REJECT: unknown fake COM identity' }; "
    "if ($env:WS_COM -eq 'throw') { throw 'fake COM failure' }; "
    "$shortcut = [pscustomobject]@{ TargetPath = $env:WS_TARGET; Arguments = $env:WS_ARGUMENTS }; "
    "$shell = [pscustomobject]@{ Link = $shortcut }; "
    "$shell | Add-Member -MemberType ScriptMethod -Name CreateShortcut -Value { return $this.Link }; "
    "return $shell"
)
OWNED_COMPILER_FAKE = r"""
$ownedCompiler = [pscustomobject]@{ ExitCode = [int]$env:WS_EXIT }
$ownedCompiler | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value {
    param($milliseconds)
    $script:calls += ('wait:' + $milliseconds)
    if ($milliseconds -eq 30000) {
        if ($env:WS_INITIAL -eq 'throw') { throw 'fake initial wait failure' }
        return ($env:WS_INITIAL -eq 'true')
    }
    if ($milliseconds -ne 5000) { throw 'unapproved fake wait' }
    if ($env:WS_CONFIRM -eq 'throw') { throw 'fake confirmation failure' }
    return ($env:WS_CONFIRM -eq 'true')
}
$ownedCompiler | Add-Member -MemberType ScriptMethod -Name Kill -Value {
    $script:calls += 'kill'
    if ($env:WS_KILL -eq 'throw') { throw 'fake kill failure' }
}
Set-Variable -Name compile -Value $ownedCompiler -Option ReadOnly -Scope Script
"""
SHORTCUT_RECORDING_FAKE = r"""
param($Path)
$link=[pscustomobject]@{Path=$Path;TargetPath='';Arguments='';WorkingDirectory='';IconLocation=''}
$link | Add-Member -MemberType ScriptMethod -Name Save -Value {
 $script:order += ('save:'+$this.Path)
 $script:recordedSaves += [pscustomobject]@{Path=$this.Path;TargetPath=$this.TargetPath;Arguments=$this.Arguments;WorkingDirectory=$this.WorkingDirectory;IconLocation=$this.IconLocation;Argv=(Split-WorkStackShortcutArguments -Arguments $this.Arguments)}
}
return $link
"""
STAGING_COPY_FAKE = r"""
param($LiteralPath,$Destination,[switch]$Recurse)
$script:calls += ('copy:'+$LiteralPath)
$kind=$script:sourceKinds.$LiteralPath
if(-not $kind){throw ('source missing: '+$LiteralPath)}
$script:stageKinds[$Destination]=$kind
if($Recurse){
 foreach($property in $script:sourceKinds.PSObject.Properties){
  if($property.Name.StartsWith($LiteralPath+'\',[StringComparison]::Ordinal)){
   $target=$Destination+$property.Name.Substring($LiteralPath.Length)
   $script:stageKinds[$target]=$property.Value
   $script:calls += ('stage:'+ $target)
  }
 }
}
"""
EFFECT_LEAVES = (
    "New-WorkStackShellChangeNotifier",
    "New-Object",
    "Get-CimInstance",
    "Stop-Process",
    "Remove-Item",
    "Copy-Item",
    "Start-Process",
    "New-Item",
    "Set-Content",
    "Invoke-Expression",
    "Add-Type",
    "Test-Path",
)

_PREFLIGHT = """
$ErrorActionPreference = 'Stop'
function Get-WorkStackSelectedWrites {
    param($Ast)
    foreach ($node in $Ast.FindAll({param($n) $true}, $true)) {
        $target = $null
        if ($node -is [System.Management.Automation.Language.AssignmentStatementAst]) { $target = $node.Left }
        elseif ($node -is [System.Management.Automation.Language.ForEachStatementAst]) { $target = $node.Variable }
        elseif ($node -is [System.Management.Automation.Language.ParameterAst]) { $target = $node.Name }
        elseif ($node -is [System.Management.Automation.Language.UnaryExpressionAst] -and
                @('PlusPlus','MinusMinus','PostfixPlusPlus','PostfixMinusMinus') -contains $node.TokenKind.ToString()) { $target = $node.Child }
        if ($null -eq $target) { continue }
        foreach ($variable in $target.FindAll({param($n) $n -is [System.Management.Automation.Language.VariableExpressionAst]}, $true)) {
            [pscustomobject]@{ Owner = $node; Target = $target; Variable = $variable }
        }
    }
}

function Assert-WorkStackSelectedWrite {
    param($Write)
    $name = $Write.Variable.VariablePath.UserPath
    # There are no selected provider writes. The only scoped storage is our
    # recording data and the builder's genuine preservation flag. A name alone
    # does not admit it: only these complete inert assignment shapes do.
    if (-not $Write.Variable.VariablePath.IsUnqualified) {
        $inert = @(
            '$script:calls=@()', '$script:removed=@()', '$script:warned=@()',
            '$script:stoppedIds=@()', '$script:recordedSaves=@()', '$script:order=@()',
            '$script:stageKinds=@{}', '$script:sourceKinds=$env:WS_KINDS|ConvertFrom-Json',
            '$script:WorkStackPreserveTemporary=$false', '$script:WorkStackPreserveTemporary=$true',
            '$script:calls+=''stop''', '$script:calls+=''backup''', '$script:calls+=''move'''
        )
        if ($Write.Owner -isnot [System.Management.Automation.Language.AssignmentStatementAst] -or
            $Write.Target -isnot [System.Management.Automation.Language.VariableExpressionAst] -or
            $inert -cnotcontains ($Write.Owner.Extent.Text -replace '\\s+', '')) {
            throw 'REJECT: scoped/provider write outside exact inert recording assignments'
        }
        return
    }
    if ($name -ieq 'compile' -or $name -ieq 'ownedCompiler' -or
        $name -match '(?i)Preference$' -or
        @('PSDefaultParameterValues','ExecutionContext','Host','Error','PSCmdlet','PSBoundParameters',
          'PSScriptRoot','PSCommandPath','MyInvocation','PSVersionTable','HOME','PID','PROFILE',
          'substituteIdentity','selected','selectedHash','true','false','this','input','args') -contains $name) {
        throw 'REJECT: protected variable write'
    }
}

function Assert-WorkStackSelectedAttribute {
    param($Attribute)
    # These are parameter metadata, not general attributed expressions or type
    # conversions. No executable validation/transformation attribute is selected.
    if ($Attribute.Parent -isnot [System.Management.Automation.Language.ParameterAst]) {
        throw 'REJECT: attribute outside a parameter'
    }
    $identity = $Attribute.TypeName.FullName
    if (@('Parameter','System.Management.Automation.ParameterAttribute') -contains $identity) {
        if (@($Attribute.PositionalArguments).Count) { throw 'REJECT: positional Parameter arguments' }
        $seen = @{}
        foreach ($argument in $Attribute.NamedArguments) {
            if ($argument.ArgumentName -ine 'Mandatory' -or $seen.ContainsKey($argument.ArgumentName) -or
                $argument.ExpressionOmitted -or
                $argument.Argument -isnot [System.Management.Automation.Language.VariableExpressionAst] -or
                -not $argument.Argument.VariablePath.IsUnqualified -or
                @('true','false') -notcontains $argument.Argument.VariablePath.UserPath) {
                throw 'REJECT: Parameter requires an explicit literal Mandatory boolean'
            }
            $seen[$argument.ArgumentName] = $true
        }
    } elseif (@('AllowEmptyString','System.Management.Automation.AllowEmptyStringAttribute') -contains $identity) {
        if (@($Attribute.PositionalArguments).Count -or @($Attribute.NamedArguments).Count) {
            throw 'REJECT: AllowEmptyString takes no arguments'
        }
    } else { throw 'REJECT: unknown or executable attribute' }
}

function Assert-WorkStackClosedDomain {
    param(
        [string]$Source,
        [string[]]$AllowedCommands,
        [string[]]$AllowedTypes,
        [string[]]$AllowedMembers,
        [string[]]$AllowedInstanceMembers,
        [string[]]$ForbiddenMembers,
        [string[]]$Substitutes,
        [string[]]$RequiredExtents,
        [string[]]$AllowedConstructors,
        [string[]]$AllowedComProgIds,
        [string[]]$AllowedReceiverMembers,
        [string[]]$AllowedInjectionPoints
        , [bool]$OwnedCompile = $false
    )

    $errors = $null
    $tokens = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseInput($Source, [ref]$tokens, [ref]$errors)
    if ($errors) { throw 'REJECT: the selected source does not parse' }
    if ([string]::IsNullOrWhiteSpace($Source)) { throw 'REJECT: empty selection' }
    if ($ast.ScriptRequirements) { throw 'REJECT: script requirements' }
    $syntax = @('ScriptBlockAst','NamedBlockAst','ParamBlockAst','ParameterAst','AttributeAst','NamedAttributeArgumentAst',
        'TypeConstraintAst','PipelineAst','CommandExpressionAst','VariableExpressionAst','ConstantExpressionAst',
        'StringConstantExpressionAst','ExpandableStringExpressionAst','SubExpressionAst','StatementBlockAst',
        'FunctionDefinitionAst','IfStatementAst','WhileStatementAst','ForStatementAst','ForEachStatementAst',
        'TryStatementAst','CatchClauseAst','ThrowStatementAst','ReturnStatementAst','BreakStatementAst','ContinueStatementAst',
        'AssignmentStatementAst','UnaryExpressionAst','BinaryExpressionAst','ArrayLiteralAst','ArrayExpressionAst',
        'HashtableAst','ConvertExpressionAst','ParenExpressionAst','IndexExpressionAst','InvokeMemberExpressionAst',
        'MemberExpressionAst','TypeExpressionAst','CommandAst','CommandParameterAst','ScriptBlockExpressionAst',
        'AttributedExpressionAst','SwitchStatementAst')
    foreach($node in $ast.FindAll({param($n) $true},$true)) {
        if($syntax -cnotcontains $node.GetType().Name){throw ('REJECT: unsupported syntax '+$node.GetType().Name)}
        if ($node -is [System.Management.Automation.Language.SwitchStatementAst] -and
            ($node.Flags.ToString() -split ', ' -contains 'File')) { throw 'REJECT: switch file input' }
        if ($node -is [System.Management.Automation.Language.ForEachStatementAst] -and
            $node.Flags.ToString() -ne 'None') { throw 'REJECT: nonlocal foreach mode' }
    }

    foreach ($name in $RequiredExtents) {
        $matches = @($ast.FindAll({
            param($node)
            $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name
        }, $true))
        if ($matches.Count -ne 1) { throw "REJECT: required extent is missing or not unique: $name" }
    }

    foreach ($definition in $ast.FindAll({
        param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst]
    }, $true)) {
        if ($definition.Name.Contains(':') -or $Substitutes -contains $definition.Name) {
            throw "REJECT: the selected source redefines a substitute: $($definition.Name)"
        }
    }

    # The only process-shaped object is installed in the trusted preamble.
    # The selected text may READ its exact receiver, never create, replace,
    # shadow, alias or export it. All other variable spellings reject.
    foreach ($variable in $ast.FindAll({
        param($n) $n -is [System.Management.Automation.Language.VariableExpressionAst]
    }, $true)) {
        $name = $variable.VariablePath.UserPath
        if (-not $variable.VariablePath.IsUnqualified -and $name -notmatch '^(?i)env:' -and
            @('script:calls','script:removed','script:warned','script:stoppedIds',
              'script:recordedSaves','script:order','script:stageKinds','script:sourceKinds',
              'script:WorkStackPreserveTemporary') -cnotcontains $name) {
            throw 'REJECT: scoped/provider reference outside inert fixture data'
        }
        if (@('PSDefaultParameterValues','ExecutionContext','Host','Error','PSCmdlet',
              'PSBoundParameters','MyInvocation','substituteIdentity','selected','selectedHash',
              'ownedCompiler') -contains $name) { throw 'REJECT: foreign runtime object' }
        if (($name -split ':')[-1] -ine 'compile') { continue }
        if (-not $OwnedCompile -or $name -cne 'compile' -or
            $variable.Parent -isnot [System.Management.Automation.Language.MemberExpressionAst] -or
            $variable.Parent.Expression -ne $variable) {
            throw 'REJECT: compile must be the trusted immutable receiver'
        }
        $access = $variable.Parent
        if ($access.Parent -is [System.Management.Automation.Language.AssignmentStatementAst] -or
            $access.Parent -is [System.Management.Automation.Language.UnaryExpressionAst]) {
            throw 'REJECT: compiler member assignment'
        }
        if ($access -is [System.Management.Automation.Language.InvokeMemberExpressionAst]) {
            $arguments = @($access.Arguments | Where-Object { $null -ne $_ })
            if ($access.Member.Value -ceq 'Kill') {
                if ($arguments.Count) { throw 'REJECT: Kill arguments' }
            } elseif ($access.Member.Value -ceq 'WaitForExit') {
                if ($arguments.Count -ne 1 -or
                    $arguments[0] -isnot [System.Management.Automation.Language.ConstantExpressionAst] -or
                    @(30000,5000) -notcontains $arguments[0].Value) { throw 'REJECT: wait budget' }
            } else { throw 'REJECT: compiler method' }
        } elseif ($access.Member.Value -cne 'ExitCode') { throw 'REJECT: compiler property' }
    }

    # Scriptblocks are validated recursively below, including parameter inline
    # arguments. Redirections, splatting and provider variable writes are never
    # needed by these inert selected extents.
    if (@($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.RedirectionAst] }, $true)).Count) {
        throw 'REJECT: redirection'
    }
    $writes = @(Get-WorkStackSelectedWrites -Ast $ast)
    foreach ($write in $writes) { Assert-WorkStackSelectedWrite -Write $write }
    foreach ($attribute in $ast.FindAll({param($n) $n -is [System.Management.Automation.Language.AttributeAst]}, $true)) {
        Assert-WorkStackSelectedAttribute -Attribute $attribute
    }

    foreach ($command in $ast.FindAll({
        param($node) $node -is [System.Management.Automation.Language.CommandAst]
    }, $true)) {
        $name = $command.GetCommandName()
        if (@('ForEach-Object','Where-Object') -ccontains $name -and
            ($command.CommandElements.Count -ne 2 -or $command.CommandElements[1] -isnot [System.Management.Automation.Language.ScriptBlockExpressionAst])) {
            throw 'REJECT: only recursively validated scriptblock pipeline callbacks'
        }
        if ($command.InvocationOperator -eq [System.Management.Automation.Language.TokenKind]::Dot) { throw 'REJECT: selected dot-source' }
        foreach ($argument in $command.CommandElements) {
            if ($argument -is [System.Management.Automation.Language.VariableExpressionAst] -and $argument.Splatted) { throw 'REJECT: splatted arguments' }
            if ($argument -is [System.Management.Automation.Language.CommandParameterAst]) {
                # Common output-variable parameters also write variables. Reject
                # their names, aliases and accepted abbreviations, including an
                # inline argument. None is needed by any selected helper.
                $parameter = $argument.ParameterName
                if ($name -ceq 'Join-Path' -and 'Resolve'.StartsWith($parameter, [StringComparison]::OrdinalIgnoreCase)) {
                    throw 'REJECT: Join-Path may compose names but never resolve providers'
                }
                foreach ($writer in @('OutVariable','ErrorVariable','WarningVariable','InformationVariable','PipelineVariable','ov','ev','wv','iv','pv')) {
                    if ($writer.StartsWith($parameter, [StringComparison]::OrdinalIgnoreCase)) {
                        throw 'REJECT: command variable-output parameter'
                    }
                }
            }
        }
        if (-not $name) {
            # The ONLY dynamism admitted is the product's own declared scriptblock
            # injection points, invoked as a bare parameter variable the control
            # itself supplied. & $env:ANYTHING and every computed target stay refused.
            $first = $command.CommandElements[0]
            if ($command.InvocationOperator -ne [System.Management.Automation.Language.TokenKind]::Ampersand -or
                $first -isnot [System.Management.Automation.Language.VariableExpressionAst] -or
                $first.VariablePath.IsUnqualified -ne $true -or
                $AllowedInjectionPoints -cnotcontains $first.VariablePath.UserPath) {
                throw 'REJECT: a dynamic command invocation is not supported'
            }
            continue
        }
        if ($AllowedCommands -cnotcontains $name -and $Substitutes -cnotcontains $name) {
            throw "REJECT: command outside the closed domain: $name"
        }
        if ($name -ceq 'Add-Member') { throw 'REJECT: selected method installation' }
        if ($name -ceq 'New-Object') {
            # The construction itself is checked, not just the command name: an
            # effectful or unknown constructor argument never reaches the fake.
            $elements = @($command.CommandElements)
            $isCom = $false
            if ($elements.Count -eq 2 -and $elements[1] -is [System.Management.Automation.Language.StringConstantExpressionAst]) {
                $requested = $elements[1].Value
            } elseif ($elements.Count -eq 3 -and $elements[1] -is [System.Management.Automation.Language.CommandParameterAst] -and
                $null -eq $elements[1].Argument -and $elements[2] -is [System.Management.Automation.Language.StringConstantExpressionAst]) {
                if (@('TypeName','ComObject') -cnotcontains $elements[1].ParameterName) { throw 'REJECT: constructor parameter' }
                $requested = $elements[2].Value
                $isCom = $elements[1].ParameterName -ceq 'ComObject'
            } else { throw 'REJECT: unsupported constructor arguments (inline/repeated/mixed/computed)' }
            if ($null -eq $requested) { throw 'REJECT: New-Object without a constant construction argument' }
            if ($isCom) {
                if ($AllowedComProgIds -cnotcontains $requested) {
                    throw "REJECT: COM construction outside the closed domain: $requested"
                }
            } elseif ($AllowedConstructors -cnotcontains $requested) {
                throw "REJECT: construction outside the closed domain: $requested"
            }
        }
    }

    foreach ($member in $ast.FindAll({
        param($node) $node -is [System.Management.Automation.Language.MemberExpressionAst]
    }, $true)) {
        if ($member.Member -isnot [System.Management.Automation.Language.StringConstantExpressionAst]) {
            throw 'REJECT: a dynamic member access is not supported'
        }
        $memberName = $member.Member.Value
        if (@('Save','CreateShortcut') -ccontains $memberName) {
            $receiver = if($member.Expression -is [System.Management.Automation.Language.VariableExpressionAst]){$member.Expression.VariablePath.UserPath}else{''}
            $expectedReceiver = if($memberName -ceq 'Save'){'shortcut'}else{'shell'}
            $construction = if($memberName -ceq 'Save'){'$shortcut = New-RecordingShortcut -Path $descriptor.Path'}else{'$shell = New-Object -ComObject WScript.Shell'}
            $constructions=@($ast.FindAll({param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and $n.Left.Extent.Text -ceq ('$'+$expectedReceiver)},$true))
            $parameters=@($ast.FindAll({param($n) $n -is [System.Management.Automation.Language.ParameterAst] -and $n.Name.VariablePath.UserPath -ieq $expectedReceiver},$true))
            if($receiver -cne $expectedReceiver -or $constructions.Count -ne 1 -or $parameters.Count -ne 0 -or $constructions[0].Extent.Text -cne $construction){throw 'REJECT: shortcut receiver provenance'}
            foreach ($write in $writes) {
                if ($write.Variable.VariablePath.UserPath -ine $expectedReceiver) { continue }
                if ($write.Owner -eq $constructions[0]) { continue }
                if ($memberName -ceq 'Save' -and
                    $write.Owner -is [System.Management.Automation.Language.AssignmentStatementAst] -and
                    $write.Owner.Operator.ToString() -eq 'Equals' -and
                    $write.Target -is [System.Management.Automation.Language.MemberExpressionAst] -and
                    $write.Target.Expression -eq $write.Variable -and
                    @('TargetPath','Arguments','WorkingDirectory','IconLocation') -ccontains $write.Target.Member.Value) { continue }
                throw 'REJECT: retained shortcut receiver write outside construction and descriptor fields'
            }
        }
        if ($ForbiddenMembers -contains $memberName) {
            throw "REJECT: forbidden member: $memberName"
        }
        if ($member.Static) {
            if ($member.Expression -isnot [System.Management.Automation.Language.TypeExpressionAst]) {
                throw 'REJECT: a dynamic static receiver is not supported'
            }
            $receiver = $member.Expression.TypeName.FullName
            if ($AllowedMembers -cnotcontains "$receiver.$memberName") {
                throw "REJECT: member outside the closed domain: $receiver.$memberName"
            }
            if ($memberName -ceq 'new' -and @($member.Arguments | Where-Object { $null -ne $_ }).Count) {
                throw 'REJECT: supported static constructors take no arguments'
            }
        } elseif ($member.Expression -is [System.Management.Automation.Language.VariableExpressionAst]) {
            # A known member name is not enough: it must sit on the retained fake
            # that the control actually built, so Kill and WaitForExit cannot be
            # aimed at a foreign object.
            $receiver = $member.Expression.VariablePath.UserPath
            $pair = "$receiver.$memberName"
            if ($AllowedReceiverMembers -ccontains $pair) { continue }
            if ($AllowedInstanceMembers -cnotcontains $memberName) {
                throw "REJECT: instance member outside the closed domain: $memberName"
            }
            if ($AllowedReceiverMembers -match "\.$([regex]::Escape($memberName))$") {
                throw "REJECT: receiver-bound member on a foreign receiver: $pair"
            }
        } elseif ($AllowedInstanceMembers -cnotcontains $memberName) {
            throw "REJECT: instance member outside the closed domain: $memberName"
        }
    }

    foreach ($typeNode in $ast.FindAll({
        param($node) $node -is [System.Management.Automation.Language.TypeExpressionAst] -or
            $node -is [System.Management.Automation.Language.TypeConstraintAst]
    }, $true)) {
        $typeName = $typeNode.TypeName.FullName
        if ($AllowedTypes -cnotcontains $typeName) {
            throw "REJECT: type outside the closed domain: $typeName"
        }
    }
}
"""


def _recorders_for_probe(selected, injection_points, recorders):
    if not selected.strip():
        raise AssertionError("guarded_probe requires nonempty selected source")
    if injection_points:
        raise AssertionError("Use the exact recording extents; dynamic callbacks are not selected")
    result = dict(recorders or {})
    if result.get("New-Object", NEW_OBJECT_FAKE) != NEW_OBJECT_FAKE:
        raise AssertionError("construction substitute must retain its exact audited identity")
    result.setdefault("New-Object", NEW_OBJECT_FAKE)
    return result


def guarded_probe(selected: str, required_extents=(), extra_commands=(), extra_instance=(), recorders=None,
                  injection_points=(), owned_compile=False, parse_only=False) -> str:
    """Return a probe that validates EXACTLY the text it will execute.

    There is no second, unvalidated tail and no empty-selection path: the
    fixtures, the extracted product extents and the assertions are one string,
    that string is what the validator inspects, and that same string is what
    runs. Anything the fixtures need - New-Object, Add-Member, every instance
    receiver - must therefore be inside the declared domain rather than allowed
    by never being looked at.
    """

    # Recording substitutes are installed by the TRUSTED preamble, never by the
    # selected source: the selected source may call them but may not define them.
    recorders = _recorders_for_probe(selected, injection_points, recorders)
    # Construction is always served by the one trusted fake: the real cmdlet is
    # shadowed on every path, and the preflight still checks each requested type.
    stubs = "".join(
        (
            "function {0} {{ {1} }}\n".format(name, recorders[name])
            if name in recorders
            else "function {0} {{ throw '{0} is not permitted in a source test' }}\n".format(name)
        )
        for name in EFFECT_LEAVES
    )
    stubs += "".join(
        "function {0} {{ {1} }}\n".format(name, script)
        for name, script in recorders.items()
        if name not in EFFECT_LEAVES
    )
    commands = "','".join(sorted(set(ALLOWED_COMMANDS) | set(extra_commands)))
    types = "','".join(sorted(ALLOWED_TYPES))
    members = "','".join(sorted("{0}.{1}".format(a, b) for a, b in ALLOWED_MEMBERS))
    instance = "','".join(sorted(set(ALLOWED_INSTANCE_MEMBERS) | set(extra_instance)))
    forbidden = "','".join(sorted(FORBIDDEN_MEMBERS))
    constructors = "','".join(sorted(ALLOWED_CONSTRUCTORS))
    progids = "','".join(sorted(ALLOWED_COM_PROGIDS))
    receivers = "','".join(sorted("{0}.{1}".format(a, b) for a, b in ALLOWED_RECEIVER_MEMBERS))
    injections = "','".join(sorted(injection_points))
    leaves = "','".join(sorted(set(EFFECT_LEAVES) | set(recorders)))
    domain = (
        "-AllowedCommands @('" + commands + "') -AllowedTypes @('" + types + "') "
        "-AllowedMembers @('" + members + "') -AllowedInstanceMembers @('" + instance + "') "
        "-ForbiddenMembers @('" + forbidden + "') -Substitutes @('" + leaves + "') "
        "-AllowedConstructors @('" + constructors + "') -AllowedComProgIds @('" + progids + "') "
        "-AllowedReceiverMembers @('" + receivers + "') "
        "-AllowedInjectionPoints @('" + injections + "')"
        + " -OwnedCompile $" + str(bool(owned_compile)).lower()
    )
    required = "','".join(required_extents) if required_extents else ""
    required_clause = " -RequiredExtents @('" + required + "')" if required else ""
    sentinels = (
        ("member effect", "$null = [IO.Path]::GetTempFileName()"),
        ("reflection", "$null = 'x'.GetType()"),
        ("dynamic member", "$name = 'Trim'; $null = 'x'.$name()"),
        ("dynamic command", "& $env:WS_ANY"),
        ("computed injection target", "& ($factory) 'x'"),
        ("undeclared injection point", "& $Rogue 'x'"),
        ("substitute redefinition", "function Remove-Item { }"),
        # The six expressions an independent parser-only review found wrongly
        # admitted at 605a. Each must now be refused BEFORE anything executes.
        ("arbitrary COM construction", "$s = New-Object -ComObject Scripting.FileSystemObject"),
        ("effectful constructor argument",
         "$w = New-Object System.IO.StreamWriter ([IO.Path]::GetTempFileName())"),
        ("dynamic constructor type", "$t = 'System.Diagnostics.Process'; $p = New-Object $t"),
        ("foreign receiver Kill", "$foreign.Kill()"),
        ("foreign receiver WaitForExit", "$foreign.WaitForExit(1)"),
        ("construction substitute redefinition", "function New-Object { }"),
        ("inline constructor decoy", "New-Object -TypeName:IO.StreamWriter System.Text.StringBuilder"),
        ("inline COM decoy", "New-Object -ComObject:Scripting.FileSystemObject WScript.Shell"),
        ("scoped constructor definition", "function script:New-Object { return 1 }"),
        ("scoped deletion definition", "function global:Remove-Item { return 1 }"),
        ("compiler reassignment", "$compile=$foreign; $compile.Kill()"),
        ("compiler parameter", "function Witness($compile) { $compile.WaitForExit(30000) }"),
        ("compiler alias", "$other=$compile; $other.Kill()"),
        ("unknown command", "Get-Process -Name explorer"),
        ("unknown type", "[System.Diagnostics.Process]::GetCurrentProcess()"),
    )
    sentinel_lines = "".join(
        "$fired = $false\n"
        "try { Assert-WorkStackClosedDomain -Source '" + source.replace("'", "''") + "' " + domain + " } "
        "catch { $fired = $true }\n"
        "if (-not $fired) { throw 'REJECT: the " + label + " sentinel was not refused' }\n"
        for label, source in sentinels
    )
    missing_extent = (
        "$fired = $false\n"
        "try { Assert-WorkStackClosedDomain -Source 'function Other { }' " + domain
        + " -RequiredExtents @('No-SuchExtent') } catch { $fired = $true }\n"
        "if (-not $fired) { throw 'REJECT: the missing-extent sentinel was not refused' }\n"
    )
    identity = (
        "$substituteIdentity = @{}\n"
        "foreach ($leaf in @('" + leaves + "')) {\n"
        "    $command = Get-Command -Name $leaf -CommandType Function -ErrorAction SilentlyContinue\n"
        "    if (-not $command) { throw \"REJECT: substitution is missing for $leaf\" }\n"
        "    $substituteIdentity[$leaf] = $command.ScriptBlock\n"
        "}\n"
    )
    execute = "'PARSE_ACCEPTED'\n" if parse_only else ". ([ScriptBlock]::Create($selected))\n"
    return (
        _PREFLIGHT
        + stubs
        + (OWNED_COMPILER_FAKE if owned_compile else "")
        + identity
        + sentinel_lines
        + missing_extent
        + "$selected = $env:WS_SELECTED\n"
        "if ([string]::IsNullOrWhiteSpace($selected)) { throw 'REJECT: the selected source is missing' }\n"
        # Source-to-validator identity: the validated text and the executed text
        # are the same variable, hashed here so a reviewer can compare them.
        "$selectedHash = [BitConverter]::ToString([Security.Cryptography.SHA256]::Create()"
        ".ComputeHash([Text.Encoding]::UTF8.GetBytes($selected))).Replace('-','')\n"
        "Assert-WorkStackClosedDomain -Source $selected " + domain + required_clause + "\n"
        + "foreach ($leaf in $substituteIdentity.Keys) { if (-not [object]::ReferenceEquals((Get-Command -Name $leaf -CommandType Function).ScriptBlock, $substituteIdentity[$leaf])) { throw 'REJECT: substitute identity changed' } }\n"
        + execute
    )


@unittest.skipIf(PWSH is None, "PowerShell is required for AST extraction")
class DesktopHostSourceTest(unittest.TestCase):
    """The C# host is read as source; it is never compiled or executed."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.source = HOST_SOURCE.read_text(encoding="utf-8")

    def test_entry_is_a_single_sta_py_main_and_not_a_launcher(self) -> None:
        self.assertIn("[STAThread]", self.source)
        self.assertEqual(1, self.source.count("Py_Main(pythonArgs.Count, argvBlock)"))
        # Call syntax, so an explanatory comment can neither satisfy nor break this.
        for forbidden in ("Py_Initialize(", "Py_Finalize(", "Process.Start(", "CreateProcess"):
            self.assertNotIn(forbidden, self.source)

    def test_runtime_is_loaded_with_the_restricted_search_and_verified(self) -> None:
        self.assertIn("LoadLibrarySearchDllLoadDir = 0x00000100", self.source)
        self.assertIn("LoadLibrarySearchSystem32 = 0x00000800", self.source)
        self.assertIn("LoadLibrarySearchDllLoadDir | LoadLibrarySearchSystem32", self.source)
        self.assertIn("GetModuleFileNameW(loaded", self.source)
        self.assertIn("a different Python runtime was loaded", self.source)
        for forbidden in ("SetDllDirectory", "AddDllDirectory", "LOAD_WITH_ALTERED_SEARCH_PATH"):
            self.assertNotIn(forbidden, self.source)

    def test_absolute_input_is_validated_before_normalization(self) -> None:
        # A relative or drive-relative path must be refused on the INPUT, before
        # GetFullPath can silently resolve it against the working directory.
        shape = self.source[self.source.index("private static string NormalizeShape"):]
        shape = shape[: shape.index("private static bool IsFullyAbsolute")]
        self.assertLess(shape.index("IsFullyAbsolute(value)"), shape.index("Path.GetFullPath(value)"))
        self.assertIn("value[2] == '\\\\' || value[2] == '/'", self.source)

    def test_a_drive_root_is_not_trimmed_into_a_drive_relative_path(self) -> None:
        self.assertIn("private static string TrimTrailingSeparator", self.source)
        self.assertIn("if (value.Length <= 3)", self.source)

    def test_the_image_basename_and_payload_containment_are_enforced(self) -> None:
        self.assertIn('HostImageName = "WorkStack.exe"', self.source)
        self.assertIn("the host image is not the installed Work Stack executable", self.source)
        self.assertIn("RejectReparseAncestors(installRoot, full, description)", self.source)
        self.assertIn("is reached through a reparse point", self.source)

    def test_argv_ownership_has_no_throwing_gap(self) -> None:
        # The bookkeeping capacity is reserved before the first unmanaged
        # allocation, so List growth cannot throw between allocation and ownership.
        body = self.source[self.source.index("argvBlock = Marshal.AllocHGlobal"):]
        body = body[: body.index("phase = \"run\"")]
        self.assertLess(
            body.index("originalStrings.Capacity = pythonArgs.Count"),
            body.index("Marshal.StringToHGlobalUni"),
        )
        self.assertIn("checked((pythonArgs.Count + 1) * IntPtr.Size)", self.source)
        self.assertIn("Marshal.WriteIntPtr(argvBlock, pythonArgs.Count * IntPtr.Size, IntPtr.Zero)", self.source)

    def test_the_grammar_admits_only_the_existing_gui_options(self) -> None:
        for option in (
            "--install-root",
            "--state-root",
            "--url",
            "--probe-provider",
            "--probe-result",
            "--auto-close-seconds",
        ):
            self.assertIn('"' + option + '"', self.source)
        self.assertIn("an unsupported desktop option was supplied", self.source)
        self.assertIn("a desktop option was supplied more than once", self.source)
        self.assertIn("only the packaged desktop entry may be launched", self.source)
        self.assertIn("the supplied install root does not match this installation", self.source)
        # Not an accepted option key; the comment that names it is not a grammar entry.
        self.assertNotIn('"--check-remote-connection"', self.source)

    def test_every_native_declaration_pins_the_exact_export_spelling(self) -> None:
        self.assertEqual(4, self.source.count("ExactSpelling = true"))

    def test_auto_close_keeps_pythons_integer_meaning(self) -> None:
        # Shape validation only: the admitted text is forwarded verbatim so Python
        # type=int and its own clamping keep their meaning, and a legitimate value
        # beyond Int32 is neither rejected nor rewritten.
        self.assertNotIn("int.TryParse", self.source)
        integer = self.source[self.source.index("case ArgumentKind.Integer:"):]
        integer = integer[: integer.index("default:")]
        self.assertIn("return value;", integer)
        self.assertNotIn("ToString(CultureInfo.InvariantCulture)", integer)

    def test_an_invalid_explicit_log_root_is_refused_without_fallback(self) -> None:
        diagnostic = self.source[self.source.index("private static string TryRecordDiagnostic"):]
        # Absent falls back to the known folder; present but invalid returns null.
        self.assertIn("if (supplied == null)", diagnostic)
        self.assertLess(diagnostic.index("supplied == null"), diagnostic.index("IsFullyAbsolute(supplied)"))
        self.assertIn("return null;", diagnostic)

    def test_a_drive_root_still_contains_its_children(self) -> None:
        contained = self.source[self.source.index("private static bool IsContained"):]
        contained = contained[: contained.index("private static void RejectReparseAncestors")]
        self.assertIn("EndsWith(Path.DirectorySeparatorChar.ToString()", contained)

    def test_the_image_leaf_and_root_are_verified_like_the_payload(self) -> None:
        self.assertIn("RejectReparseDirectory(root, \"installation root\")", self.source)
        existing = self.source[self.source.index("private static string NormalizeExisting"):]
        existing = existing[: existing.index("private static string RequirePayloadLeaf")]
        self.assertIn("FileAttributes.ReparsePoint", existing)

    def test_pre_python_diagnostics_honour_explicit_localappdata_and_never_mask_failure(self) -> None:
        self.assertIn('Environment.GetEnvironmentVariable("LOCALAPPDATA")', self.source)
        diagnostic = self.source[self.source.index("private static string TryRecordDiagnostic"):]
        self.assertLess(
            diagnostic.index('GetEnvironmentVariable("LOCALAPPDATA")'),
            diagnostic.index("Environment.SpecialFolder.LocalApplicationData"),
        )
        self.assertIn("desktop-startup.log", self.source)
        # The dialog and the non-zero exit are independent of logging success.
        fail = self.source[self.source.index("private static int FailVisibly"):]
        fail = fail[: fail.index("private static string TryRecordDiagnostic")]
        self.assertIn("MessageBoxW(", fail)
        self.assertIn("return 1;", fail)


@unittest.skipIf(PWSH is None, "PowerShell is required for AST extraction")
class DesktopOwnershipRecordingTest(unittest.TestCase):
    """Executes the real extracted ownership helpers; no process is enumerated."""

    INSTALL = r"C:\FixtureRoots\u\Programs\WorkStack"
    HOST = INSTALL + r"\WorkStack.exe"
    LEGACY = INSTALL + r"\runtime\pythonw.exe"
    ENTRY = INSTALL + r"\desktop\python-webview-shell\workstack_desktop.py"

    STOP_HELPERS = [
        "Split-WorkStackCommandLine",
        "Test-WorkStackExactPath",
        "Test-WorkStackAbsolutePath",
        "Test-WorkStackIntegerValue",
        "Test-WorkStackDesktopGrammar",
        "Test-WorkStackDesktopInvocation",
    ]

    def evaluate(self, command_line: str, image: str, script_required: bool = False) -> bool:
        extents = extract_functions(WINDOWS / "Stop-WorkStack.ps1", self.STOP_HELPERS)
        tail = (
            "$required = [bool]::Parse($env:WS_SCRIPT_REQUIRED)\n"
            "(Test-WorkStackDesktopInvocation -CommandLine $env:WS_COMMAND_LINE "
            "-ExpectedImage $env:WS_IMAGE -ExpectedEntry $env:WS_ENTRY "
            "-ExpectedInstallRoot $env:WS_ROOT -ScriptRequired:$required)"
            ".ToString().ToLowerInvariant()\n"
        )
        selected = extents + chr(10) + tail
        body = guarded_probe(selected, required_extents=self.STOP_HELPERS)
        FIXTURE_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=str(FIXTURE_ROOT)) as directory:
            script = Path(directory) / "probe.ps1"
            script.write_text(body, encoding="utf-8")
            environment = _fixture_environment()
            environment["WS_SELECTED"] = selected
            environment.update(
                WS_COMMAND_LINE=command_line,
                WS_IMAGE=image,
                WS_ENTRY=self.ENTRY,
                WS_ROOT=self.INSTALL,
                WS_SCRIPT_REQUIRED="true" if script_required else "false",
            )
            completed = subprocess.run(
                [PWSH, "-NoProfile", "-NonInteractive", "-File", str(script)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
                cwd=directory,
                env=environment,
            )
        if completed.returncode != 0:
            raise AssertionError("pwsh failed: " + (completed.stdout or "") + " | " + (completed.stderr or ""))
        return completed.stdout.strip() == "true"

    def test_genuine_host_and_legacy_invocations_are_owned(self) -> None:
        host_line = '"{0}" "{1}" --install-root "{2}"'.format(self.HOST, self.ENTRY, self.INSTALL)
        legacy_line = '"{0}" "{1}" --state-root "{2}"'.format(self.LEGACY, self.ENTRY, self.INSTALL)
        self.assertTrue(self.evaluate(host_line, self.HOST))
        self.assertTrue(self.evaluate(legacy_line, self.LEGACY, script_required=True))

    def test_the_hosts_zero_user_argument_form_is_owned(self) -> None:
        # The host builds argv itself, so an installed link may omit the redundant
        # script; ownership and host grammar must agree about that.
        self.assertTrue(self.evaluate('"{0}"'.format(self.HOST), self.HOST))

    def test_the_legacy_form_still_requires_the_script(self) -> None:
        self.assertFalse(self.evaluate('"{0}"'.format(self.LEGACY), self.LEGACY, script_required=True))

    def test_the_complete_grammar_refuses_invalid_invocations(self) -> None:
        other = r"C:\FixtureRoots\u\Programs\WorkStackOther"
        cases = {
            "wrong supplied install root": '"{0}" "{1}" --install-root "{2}"'.format(self.HOST, self.ENTRY, other),
            "duplicate option": '"{0}" "{1}" --state-root "{2}" --state-root "{2}"'.format(
                self.HOST, self.ENTRY, self.INSTALL
            ),
            "unknown option": '"{0}" "{1}" --debug 1'.format(self.HOST, self.ENTRY),
            "missing option value": '"{0}" "{1}" --state-root'.format(self.HOST, self.ENTRY),
            "another script with the same tail": '"{0}" "{1}\desktop\python-webview-shell\workstack_desktop.py"'.format(
                self.HOST, other
            ),
            "shared prefix without a segment boundary": '"{0}\WorkStack.exe" "{1}"'.format(other, self.ENTRY),
            "foreign image": '"{0}\runtime\python.exe" "{1}"'.format(self.INSTALL, self.ENTRY),
            "unbalanced quoting": '"{0} "{1}"'.format(self.HOST, self.ENTRY),
            "relative image": 'WorkStack.exe "{0}"'.format(self.ENTRY),
            "relative script": '"{0}" desktop.py'.format(self.HOST),
        }
        for label, line in cases.items():
            with self.subTest(case=label):
                self.assertFalse(self.evaluate(line, self.HOST))

    def test_a_spaced_and_unicode_path_round_trips(self) -> None:
        install = r"C:\FixtureRoots\사용자\Program Files\Work Stack"
        entry = install + r"\desktop\python-webview-shell\workstack_desktop.py"
        extents = extract_functions(WINDOWS / "Stop-WorkStack.ps1", self.STOP_HELPERS)
        tail = (
            "(Test-WorkStackDesktopInvocation -CommandLine $env:WS_COMMAND_LINE "
            "-ExpectedImage $env:WS_IMAGE -ExpectedEntry $env:WS_ENTRY "
            "-ExpectedInstallRoot $env:WS_ROOT).ToString().ToLowerInvariant()\n"
        )
        selected = extents + chr(10) + tail
        body = guarded_probe(selected, required_extents=self.STOP_HELPERS)
        FIXTURE_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=str(FIXTURE_ROOT)) as directory:
            script = Path(directory) / "probe.ps1"
            script.write_text(body, encoding="utf-8")
            environment = _fixture_environment()
            environment["WS_SELECTED"] = selected
            environment.update(
                WS_COMMAND_LINE='"{0}\WorkStack.exe" "{1}"'.format(install, entry),
                WS_IMAGE=install + r"\WorkStack.exe",
                WS_ENTRY=entry,
                WS_ROOT=install,
            )
            completed = subprocess.run(
                [PWSH, "-NoProfile", "-NonInteractive", "-File", str(script)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
                cwd=directory,
                env=environment,
            )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("true", completed.stdout.strip())

    def test_a_missing_required_function_fails_the_harness_closed(self) -> None:
        with self.assertRaises(AssertionError):
            extract_functions(WINDOWS / "Stop-WorkStack.ps1", ["No-SuchFunctionExists"])


_EXTRACT_STATEMENT = """param([string]$Path, [string]$Needle, [string]$Kind, [string]$Enclosing)
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Path, [ref]$tokens, [ref]$errors)
if ($errors) { throw 'the script does not parse' }
$candidates = @($ast.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.StatementAst] -and $node.Extent.Text.Contains($Needle) -and (-not $Kind -or $node.GetType().Name -eq $Kind)
}, $true))
if ($candidates.Count -eq 0) { throw "no statement contains $Needle" }
$shortest = ($candidates | Measure-Object -Property { $_.Extent.Text.Length } -Minimum).Minimum
$hit = @($candidates | Where-Object { $_.Extent.Text.Length -eq $shortest })
# Ambiguity is refused rather than resolved by ordering: two equally small
# statements would make the selected bytes depend on traversal order.
if ($hit.Count -ne 1) { throw "the statement containing $Needle is not unique" }
$hit = $hit[0]
if ($Enclosing -eq 'Finally') {
    # Binding, not text: the selected statement must be reached through the
    # ORIGINAL enclosing try statement's finally block. A mutant that removes the
    # branch from the finally and appends identical bytes afterwards is refused,
    # because the selected node's ancestry no longer passes through Finally.
    $bound = $false
    $node = $hit
    while ($node.Parent) {
        $parent = $node.Parent
        if ($parent -is [System.Management.Automation.Language.TryStatementAst]) {
            if ($parent.Finally -and $parent.Finally.Extent.StartOffset -le $hit.Extent.StartOffset -and
                $parent.Finally.Extent.EndOffset -ge $hit.Extent.EndOffset) {
                $bound = $true
                break
            }
        }
        $node = $parent
    }
    if (-not $bound) { throw "the statement containing $Needle is not inside a real finally block" }
}
$hit.Extent.Text
"""

# Parse the ORIGINAL file once. All three lifetime statements and the creation
# assignment must be siblings in the same Try body owning the complete finally.
# Unrelated packaging statements are omitted by original AST offsets; the Try
# scaffold and COMPLETE finally bytes are retained, never invented by the test.
_EXTRACT_COMPILER = r"""param([string]$Path)
$errors=$null; $tokens=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($Path,[ref]$tokens,[ref]$errors)
if($errors){throw 'REJECT: compiler source parse'}
$waits=@($ast.FindAll({param($n) $n -is [System.Management.Automation.Language.InvokeMemberExpressionAst] -and $n.Member.Value -eq 'WaitForExit'},$true))
if($waits.Count -ne 2){throw 'REJECT: compiler wait cardinality'}
$initial=@($waits | Where-Object {$_.Extent.Text -ceq '$compile.WaitForExit(30000)'})
$confirm=@($waits | Where-Object {$_.Extent.Text -ceq '$compile.WaitForExit(5000)'})
$cleanup=@($ast.FindAll({param($n) $n -is [System.Management.Automation.Language.IfStatementAst] -and $n.Extent.Text.Contains('may still be using it')},$true))
$owners=@($ast.FindAll({param($n) $n -is [System.Management.Automation.Language.TryStatementAst] -and $n.Finally -and $n.Finally.Extent.Text.Contains('may still be using it')},$true))
if($initial.Count -ne 1 -or $confirm.Count -ne 1 -or $cleanup.Count -ne 1 -or $owners.Count -ne 1){throw 'REJECT: original lifetime cardinality'}
$owner=$owners[0]
if($owner.Finally.Statements.Count -ne 1 -or $owner.Finally.Statements[0] -ne $cleanup[0]){throw 'REJECT: complete original finally changed'}
$body=@($owner.Body.Statements)
$start=@($body | Where-Object {$_.Extent.Text -like '$compile = Start-Process *'})
$initialStatement=@($body | Where-Object {$_.Extent.StartOffset -le $initial[0].Extent.StartOffset -and $_.Extent.EndOffset -ge $initial[0].Extent.EndOffset})
$timeoutStatement=@($body | Where-Object {$_.Extent.StartOffset -le $confirm[0].Extent.StartOffset -and $_.Extent.EndOffset -ge $confirm[0].Extent.EndOffset})
$exitStatement=@($body | Where-Object {$_.Extent.Text -like 'if ($compile.ExitCode -ne 0)*'})
if($start.Count -ne 1 -or $initialStatement.Count -ne 1 -or $timeoutStatement.Count -ne 1 -or $exitStatement.Count -ne 1){throw 'REJECT: finally is not owned by the original compiler lifetime'}
foreach($variable in $owner.FindAll({param($n) $n -is [System.Management.Automation.Language.VariableExpressionAst] -and ($n.VariablePath.UserPath -split ':')[-1] -ieq 'compile'},$true)){
 if($variable -eq $start[0].Left){continue}
 if($variable.VariablePath.UserPath -cne 'compile' -or $variable.Parent -isnot [System.Management.Automation.Language.MemberExpressionAst] -or $variable.Parent.Expression -ne $variable -or $variable.Parent.Parent -is [System.Management.Automation.Language.AssignmentStatementAst]){throw 'REJECT: original compiler provenance changed'}
}
if($start[0].Extent.StartOffset -ge $initialStatement[0].Extent.StartOffset -or $initialStatement[0].Extent.EndOffset -gt $timeoutStatement[0].Extent.StartOffset -or $timeoutStatement[0].Extent.EndOffset -gt $exitStatement[0].Extent.StartOffset){throw 'REJECT: compiler source ordering'}
$keep=@($initialStatement[0],$timeoutStatement[0],$exitStatement[0])
$source=$owner.Extent.Text
foreach($statement in ($body | Sort-Object {$_.Extent.StartOffset} -Descending)){
 if($keep -contains $statement){continue}
 $offset=$statement.Extent.StartOffset-$owner.Extent.StartOffset
 $source=$source.Remove($offset,$statement.Extent.EndOffset-$statement.Extent.StartOffset).Insert($offset,'# omitted unrelated packaging/creation; trusted owned compiler substituted')
}
$source
"""

_EXTRACT_SHORTCUT_RECORDING = r"""param([string]$Path)
$errors=$null;$tokens=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($Path,[ref]$tokens,[ref]$errors)
if($errors){throw 'REJECT: shortcut parse'}
$functions=@($ast.FindAll({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq 'Invoke-WorkStackShortcutFinalization'},$true))
if($functions.Count -ne 1){throw 'REJECT: finalizer cardinality'}
$function=$functions[0]
$loops=@($function.Body.FindAll({param($n) $n -is [System.Management.Automation.Language.ForEachStatementAst]},$true))
if($loops.Count -ne 2 -or -not $loops[0].Extent.Text.Contains('$shortcut.Save()') -or -not $loops[1].Extent.Text.Contains('Send-WorkStackShortcutNotification')){throw 'REJECT: original Save/notification loop order'}
$save=$loops[0]
$assignments=@($save.FindAll({param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and @('$shortcut','$existed') -ccontains $n.Left.Extent.Text},$true))
if($assignments.Count -ne 2){throw 'REJECT: construction/existence leaf cardinality'}
$text=$save.Extent.Text
foreach($assignment in ($assignments | Sort-Object {$_.Extent.StartOffset} -Descending)){
 $replacement=if($assignment.Left.Extent.Text -ceq '$shortcut'){'$shortcut = New-RecordingShortcut -Path $descriptor.Path'}else{'$existed = Test-Path -LiteralPath $descriptor.Path -PathType Leaf'}
 $offset=$assignment.Extent.StartOffset-$save.Extent.StartOffset
 $text=$text.Remove($offset,$assignment.Extent.EndOffset-$assignment.Extent.StartOffset).Insert($offset,$replacement)
}
# The actual assignments, Save and subsequent notification loop remain verbatim;
# only the two external effect leaves are substituted at exact AST extents.
$text+"`n"+$loops[1].Extent.Text
"""


def extract_statement(path: Path, needle: str, kind: str = "", enclosing: str = "") -> str:
    """Return the unique smallest real statement containing the needle, or fail closed.

    With enclosing="Finally" the statement must additionally be reached through
    the ORIGINAL enclosing try statement's finally block, so a control cannot be
    satisfied by identical bytes that the product moved out of its finally.
    """

    return _run_pwsh(_EXTRACT_STATEMENT, [str(path), needle, kind, enclosing])


@unittest.skipIf(PWSH is None, "PowerShell is required for AST extraction")
class CompilerLifetimeRecordingTest(unittest.TestCase):
    """Drives fake process objects through the REAL extracted lifetime statements.

    No compiler is started, nothing is killed and no tree is removed: the fake
    records which waits and terminations the product source actually performed.
    """

    def run_case(self, initial, kill, confirm) -> dict:
        result = CompilerCleanupRecordingTest().run_case(initial, kill, confirm, 0)
        result["calls"] = ",".join(call for call in result["calls"].split(",")
                                   if call.startswith("wait:") or call == "kill")
        return result

    def test_a_natural_exit_neither_kills_nor_preserves(self) -> None:
        result = self.run_case("true", "ok", "true")
        self.assertEqual("wait:30000", result["calls"])
        self.assertFalse(result["preserved"])
        self.assertEqual("", result["thrown"])

    def test_a_timeout_that_is_reaped_kills_once_and_does_not_preserve(self) -> None:
        result = self.run_case("false", "ok", "true")
        self.assertEqual("wait:30000,kill,wait:5000", result["calls"])
        self.assertFalse(result["preserved"])
        self.assertIn("timed out after 30 seconds", result["thrown"])

    def test_an_unconfirmed_exit_preserves_the_tree(self) -> None:
        result = self.run_case("false", "ok", "false")
        self.assertEqual("wait:30000,kill,wait:5000", result["calls"])
        self.assertTrue(result["preserved"])
        self.assertIn("could not be", result["thrown"])
        self.assertIn("preserved", result["thrown"])

    def test_a_throwing_initial_wait_preserves_the_tree(self) -> None:
        result = self.run_case("throw", "ok", "true")
        self.assertEqual("wait:30000", result["calls"])
        self.assertTrue(result["preserved"])
        self.assertIn("exit is unknown", result["thrown"])

    def test_a_throwing_confirmation_preserves_the_tree(self) -> None:
        result = self.run_case("false", "ok", "throw")
        self.assertEqual("wait:30000,kill,wait:5000", result["calls"])
        self.assertTrue(result["preserved"])
        self.assertIn("preserved", result["thrown"])

    def test_a_failing_kill_still_confirms_once_and_reports(self) -> None:
        result = self.run_case("false", "throw", "false")
        self.assertEqual("wait:30000,kill,wait:5000", result["calls"])
        self.assertTrue(result["preserved"])
        self.assertIn("fake kill failure", result["thrown"])

    def test_the_lifetime_is_bounded_to_one_kill_and_two_waits(self) -> None:
        for initial, kill, confirm in (("false", "ok", "true"), ("false", "throw", "false")):
            with self.subTest(case=(initial, kill, confirm)):
                calls = self.run_case(initial, kill, confirm)["calls"].split(",")
                self.assertEqual(1, calls.count("kill"))
                self.assertEqual(1, calls.count("wait:30000"))
                self.assertEqual(1, calls.count("wait:5000"))


@unittest.skipIf(PWSH is None, "PowerShell is required for AST extraction")
class PythonIntegerDomainTest(unittest.TestCase):
    """The accepted auto-close shape must match the PINNED interpreter, measured here."""

    ACCEPTED = (" 2 ", "1_0", "\u0661\u0662", "\U0001E950", "\U0001D7CE", "+7", "-7",
                "12345678901234567890123456789")
    REFUSED = ("1__0", "_1", "1_", "", " ", "1.0", "0x10", "\ud800")

    def test_the_pinned_interpreter_agrees_with_the_expected_domain(self) -> None:
        # stdlib only: no product import, no subprocess beyond this interpreter.
        for value in self.ACCEPTED:
            with self.subTest(accepted=value):
                int(value)
        for value in self.REFUSED:
            with self.subTest(refused=value):
                with self.assertRaises((ValueError, TypeError)):
                    int(value)

    def test_the_powershell_helper_matches_the_pinned_domain(self) -> None:
        extents = extract_functions(WINDOWS / "Stop-WorkStack.ps1", ["Test-WorkStackIntegerValue"])
        tail = "(Test-WorkStackIntegerValue -Value $env:WS_VALUE).ToString().ToLowerInvariant()\n"
        selected = extents + chr(10) + tail
        body = guarded_probe(selected, required_extents=("Test-WorkStackIntegerValue",))
        for value, expected in [(v, True) for v in self.ACCEPTED] + [(v, False) for v in self.REFUSED]:
            with self.subTest(value=value):
                FIXTURE_ROOT.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(dir=str(FIXTURE_ROOT)) as directory:
                    script = Path(directory) / "probe.ps1"
                    script.write_text(body, encoding="utf-8")
                    environment = _fixture_environment()
                    environment["WS_SELECTED"] = selected
                    environment["WS_VALUE"] = value
                    completed = subprocess.run(
                        [PWSH, "-NoProfile", "-NonInteractive", "-File", str(script)],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=180,
                        cwd=directory,
                        env=environment,
                    )
                self.assertEqual(0, completed.returncode, completed.stderr)
                self.assertEqual(str(expected).lower(), completed.stdout.strip())

    def test_the_host_table_records_its_pinned_derivation(self) -> None:
        source = HOST_SOURCE.read_text(encoding="utf-8")
        self.assertIn("CPython 3.12.10, unicodedata 15.0.0", source)
        self.assertIn("0x1E950, 0x1E959", source)
        self.assertIn("0x1D7CE, 0x1D7FF", source)
        self.assertIn("char.ConvertToUtf32", source)
        # The framework's UTF-16 char classification must not be the decider.
        self.assertNotIn("CharUnicodeInfo.GetUnicodeCategory", source)


@unittest.skipIf(PWSH is None, "PowerShell is required for AST extraction")
class DesktopPackagingContractTest(unittest.TestCase):
    """Static contracts on the owned scripts; no script top level is executed."""

    def read(self, name: str) -> str:
        return (WINDOWS / name).read_text(encoding="utf-8")

    def test_the_shortcut_targets_the_branded_host_with_the_exact_arguments(self) -> None:
        source = self.read("WorkStack-Shortcuts.ps1")
        self.assertIn("$launcher = Join-Path $InstallPath 'WorkStack.exe'", source)
        self.assertIn("$entry = Join-Path $InstallPath 'desktop\\python-webview-shell\\workstack_desktop.py'", source)
        self.assertIn("--install-root", source)
        self.assertIn("--state-root", source)
        # The maintenance link is unchanged.
        self.assertIn("TargetPath = 'powershell.exe'", source)

    def test_the_builder_compiles_the_host_with_one_bounded_pinned_invocation(self) -> None:
        source = self.read("Build-WindowsInstaller.ps1")
        self.assertIn(r"Framework64\v4.0.30319\csc.exe", source)
        for switch in ("/target:winexe", "/platform:x64", "/optimize+", "/codepage:65001"):
            self.assertIn(switch, source)
        self.assertIn("/win32icon:", source)
        self.assertIn("WorkStack.exe", source)
        self.assertIn("WaitForExit(30000)", source)
        self.assertIn("timed out after 30 seconds", source)
        # One version truth, validated numerically before it is emitted.
        self.assertIn("'^(\\d{1,5})\\.(\\d{1,5})\\.(\\d{1,5})$'", source)
        self.assertIn("AssemblyFileVersion", source)
        self.assertNotIn("/deterministic", source)

    def test_the_installer_requires_and_revalidates_the_host_payload(self) -> None:
        source = self.read("Install-WorkStack.ps1")
        self.assertIn("'runtime\\python312.dll', 'WorkStack.exe'", source)
        self.assertIn("The staged Work Stack desktop host is incomplete", source)
        staged = source.index("The staged Work Stack desktop host is incomplete")
        # The staged guard runs before Stop-WorkStack, the backup and the move.
        self.assertLess(staged, source.index("Stop-WorkStack.ps1"))

    def test_the_updater_restarts_the_host_and_keeps_its_acceptance_window(self) -> None:
        source = self.read("Apply-WorkStackUpdate.ps1")
        self.assertIn("$desktopHost = Join-Path $installPath 'WorkStack.exe'", source)
        self.assertIn("Start-Process -FilePath $desktopHost", source)
        self.assertIn("WaitForExit(1500)", source)
        self.assertIn("COMMIT BOUNDARY", source)

    def test_uninstall_recognizes_both_targets_and_parses_arguments(self) -> None:
        source = self.read("Uninstall-WorkStack.ps1")
        self.assertIn("$desktopTargets = @((Join-Path $installPath 'WorkStack.exe')", source)
        self.assertIn("runtime\\pythonw.exe", source)
        self.assertIn("Split-WorkStackShortcutArguments", source)
        self.assertIn("[Environment]::GetFolderPath('Desktop')", source)
        # Maintenance handling and RemoveData are untouched.
        self.assertIn("Work Stack Maintenance.lnk", source)
        self.assertIn("if ($RemoveData)", source)

    def test_uninstall_argument_parsing_selects_and_preserves_correctly(self) -> None:
        extents = extract_functions(WINDOWS / "Uninstall-WorkStack.ps1", ["Split-WorkStackShortcutArguments"])
        # The line under test travels through the environment rather than argv, so
        # the host shell cannot re-quote it before the parser under test sees it.
        # This tokenizer once ran its extracted source with no guard at all; it
        # is now the same one validated string as every other probe.
        selected = (
            extents
            + "\n$argv = Split-WorkStackShortcutArguments -Arguments $env:WS_ARGUMENTS\n"
            + "if ($null -eq $argv) { 'MALFORMED' } else { ($argv -join '|') }\n"
        )
        body = guarded_probe(
            selected,
            required_extents=("Split-WorkStackShortcutArguments",),
            recorders={"New-Object": NEW_OBJECT_FAKE},
        )

        def evaluate(arguments: str) -> str:
            FIXTURE_ROOT.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=str(FIXTURE_ROOT)) as directory:
                script = Path(directory) / "probe.ps1"
                script.write_text(body, encoding="utf-8")
                environment = _fixture_environment()
                environment["WS_SELECTED"] = selected
                environment["WS_ARGUMENTS"] = arguments
                completed = subprocess.run(
                    [PWSH, "-NoProfile", "-NonInteractive", "-File", str(script)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=180,
                    cwd=directory,
                    env=environment,
                )
            if completed.returncode != 0:
                raise AssertionError("pwsh failed: " + (completed.stdout or "") + " | " + (completed.stderr or ""))
            return completed.stdout.strip()

        entry = "C:\\i\\desktop\\python-webview-shell\\workstack_desktop.py"
        good = evaluate('"' + entry + '" --install-root "C:\\i"')
        self.assertEqual(entry + "|--install-root|C:\\i", good)
        # An unbalanced quote is ambiguous: the link must be preserved, not removed.
        # One unbalanced quote: the line is ambiguous, so the link is preserved.
        self.assertEqual('MALFORMED', evaluate(chr(34) + entry + ' --install-root C:'))


@unittest.skipIf(PWSH is None, "PowerShell is required for AST extraction")
class FinallyBindingAuditTest(unittest.TestCase):
    """The cleanup control must bind to the REAL finally, not to identical bytes.

    A parser-only review showed that a builder which moved the cleanup branch out
    of its finally and appended the identical branch afterwards would still satisfy
    a text-selected control. The healthy source must pass and that mutant must be
    refused; nothing from either source is executed here.
    """

    NEEDLE = "may still be using it"

    def _mutate(self, source: str) -> str:
        """Move the cleanup branch out of the finally, byte-identical, after it."""

        cleanup = extract_statement(
            WINDOWS / "Build-WindowsInstaller.ps1", self.NEEDLE, "IfStatementAst", enclosing="Finally"
        )
        self.assertIn("Remove-Item", cleanup)
        self.assertEqual(1, source.count(cleanup))
        without = source.replace(cleanup, "", 1)
        # Appended at top level: same bytes, no longer reached through the finally.
        return without + chr(10) + cleanup + chr(10)

    def test_the_healthy_builder_binds_and_the_detached_mutant_is_refused(self) -> None:
        source = (WINDOWS / "Build-WindowsInstaller.ps1").read_text(encoding="utf-8")
        healthy = extract_statement(
            WINDOWS / "Build-WindowsInstaller.ps1", self.NEEDLE, "IfStatementAst", enclosing="Finally"
        )
        self.assertIn("Remove-Item", healthy)

        FIXTURE_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=str(FIXTURE_ROOT)) as directory:
            mutant = Path(directory) / "detached-builder.ps1"
            mutant.write_text(self._mutate(source), encoding="utf-8")
            # The mutant still parses and still contains exactly the same branch text.
            self.assertIn(healthy, mutant.read_text(encoding="utf-8"))
            unbound = extract_statement(mutant, self.NEEDLE, "IfStatementAst")
            self.assertEqual(healthy, unbound)
            with self.assertRaises(AssertionError) as refused:
                extract_statement(mutant, self.NEEDLE, "IfStatementAst", enclosing="Finally")
        self.assertIn("not inside a real finally block", str(refused.exception))

    def test_the_complete_finally_belongs_to_the_same_original_compiler(self) -> None:
        builder = WINDOWS / "Build-WindowsInstaller.ps1"
        original = builder.read_text(encoding="utf-8")
        healthy = _run_pwsh(_EXTRACT_COMPILER, [str(builder)])
        self.assertIn("WaitForExit(30000)", healthy)
        self.assertIn("WaitForExit(5000)", healthy)
        self.assertIn("ExitCode", healthy)
        cleanup = extract_statement(builder, self.NEEDLE, "IfStatementAst", enclosing="Finally")
        self.assertIn(cleanup, healthy)
        for suffix in (cleanup, "try {} finally {\n" + cleanup + "\n}"):
            with self.subTest(suffix=suffix[:20]), tempfile.TemporaryDirectory(dir=str(FIXTURE_ROOT)) as directory:
                mutant = Path(directory) / "builder.ps1"
                mutant.write_text(original.replace(cleanup, "", 1) + "\n" + suffix, encoding="utf-8")
                with self.assertRaises(AssertionError):
                    _run_pwsh(_EXTRACT_COMPILER, [str(mutant)])


@unittest.skipIf(PWSH is None, "PowerShell is required for AST extraction")
class PreflightAuditTest(unittest.TestCase):
    """Audits the guard itself: each hostile shape must be refused before execution."""

    HOSTILE = {
        "member effect": "$null = [IO.Path]::GetTempFileName()",
        "reflection": "$null = 'x'.GetType()",
        "dynamic member": "$name = 'Trim'; $null = 'x'.$name()",
        "dynamic command": "& $env:WS_ANY",
        "substitute redefinition": "function Remove-Item { }",
        "unknown command": "Get-Process -Name explorer",
        "unknown type": "[System.Diagnostics.Process]::GetCurrentProcess()",
        "module directive": "using module './not-loaded.psm1'",
        "method pipeline": "$Host | ForEach-Object -MemberName SetShouldExit -ArgumentList 1",
        "computed static constructor": "$x=[System.Text.StringBuilder]::new((1+2))",
        "inline type decoy": "New-Object -TypeName:IO.StreamWriter System.Text.StringBuilder",
        "inline COM decoy": "New-Object -ComObject:Scripting.FileSystemObject WScript.Shell",
        "scoped constructor": "function script:New-Object { return 1 }",
        "global substitute": "function global:Remove-Item { return 1 }",
        "compiler alias": "$other=$compile; $other.Kill()",
        "compiler reassignment": "$compile=$foreign; $compile.Kill()",
        "compiler shadow": "function Witness($compile) { $compile.WaitForExit(30000) }",
        "compiler property increment": "$compile.ExitCode++",
        "uninitialized compiler": "$compile.Kill()",
        "inline nested call": "Write-Warning -Message:(Get-Process)",
        "selected method installation": "$o=New-Object psobject; $o | Add-Member -MemberType ScriptMethod -Name Save -Value {}",
    }

    def refuse(self, source: str, required=()) -> str:
        tail = "'the selected source was executed'\n"
        selected = source + chr(10) + tail
        body = guarded_probe(selected, required_extents=required, parse_only=True)
        FIXTURE_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=str(FIXTURE_ROOT)) as directory:
            script = Path(directory) / "audit.ps1"
            script.write_text(body, encoding="utf-8")
            environment = _fixture_environment()
            environment["WS_SELECTED"] = selected
            completed = subprocess.run(
                [PWSH, "-NoProfile", "-NonInteractive", "-File", str(script)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
                cwd=directory,
                env=environment,
            )
        return (completed.stdout or "") + (completed.stderr or "")

    def test_every_hostile_shape_is_refused_before_execution(self) -> None:
        for label, source in self.HOSTILE.items():
            with self.subTest(shape=label):
                output = self.refuse(source)
                self.assertIn("REJECT", output)
                self.assertNotIn("the selected source was executed", output)

    def test_a_missing_or_duplicated_required_extent_is_refused(self) -> None:
        output = self.refuse("function Other { }", required=("No-SuchExtent",))
        self.assertIn("REJECT: required extent is missing or not unique", output)
        duplicated = "function Dup { }\nfunction Dup { }\n"
        output = self.refuse(duplicated, required=("Dup",))
        self.assertIn("REJECT: required extent is missing or not unique", output)

    def test_healthy_construction_and_owned_receiver_are_parsed_without_execution(self) -> None:
        selected = "$x=New-Object System.Text.StringBuilder; $y=New-Object -TypeName psobject; $s=New-Object -ComObject WScript.Shell; $compile.WaitForExit(30000); $compile.Kill(); $compile.ExitCode"
        body = guarded_probe(selected, owned_compile=True, parse_only=True)
        with tempfile.TemporaryDirectory(dir=str(FIXTURE_ROOT)) as directory:
            script=Path(directory)/'healthy-parse.ps1'
            script.write_text(body,encoding='utf-8')
            environment=_fixture_environment();environment['WS_SELECTED']=selected;environment['WS_EXIT']='0'
            result=subprocess.run([PWSH,'-NoProfile','-NonInteractive','-File',str(script)],cwd=directory,env=environment,capture_output=True,text=True)
        self.assertEqual(0,result.returncode,result.stderr)
        self.assertIn('PARSE_ACCEPTED',result.stdout)

    def test_the_trusted_constructor_fake_itself_rejects_unknown_values(self) -> None:
        # Only our audited fake is called. No real constructor/COM sentinel.
        body = 'function New-Object { ' + NEW_OBJECT_FAKE + ' }\n'
        body += "$count=0;foreach($case in @(@{TypeName='IO.StreamWriter'},@{ComObject='Scripting.FileSystemObject'},@{TypeName='psobject';ComObject='WScript.Shell'})){try {New-Object @case;throw 'accepted'}catch{if($_.Exception.Message -notlike 'REJECT:*'){throw};$count++}};if($count -ne 3){throw 'count'}; 'rejected three'"
        self.assertIn('rejected three',_run_pwsh(body,[]))

    def parse_owned_case(self, source: str) -> tuple[int, str]:
        # Syntax validity is a separate prerequisite; an invalid PowerShell
        # witness must never masquerade as a closed-domain refusal.
        syntax = (
            "$tokens=$null;$errors=$null;"
            "[void][System.Management.Automation.Language.Parser]::ParseInput($env:WS_SELECTED,[ref]$tokens,[ref]$errors);"
            "if($errors){throw 'WITNESS_SYNTAX_INVALID'}\n"
        )
        body = syntax + guarded_probe(source, owned_compile=True, parse_only=True)
        with tempfile.TemporaryDirectory(dir=str(FIXTURE_ROOT)) as directory:
            script = Path(directory) / "owned-parse.ps1"
            script.write_text(body, encoding="utf-8")
            environment = _fixture_environment()
            environment.update(WS_SELECTED=source, WS_EXIT="0")
            result = subprocess.run(
                [PWSH, "-NoProfile", "-NonInteractive", "-File", str(script)],
                cwd=directory, env=environment, capture_output=True, text=True,
            )
        output = result.stdout + result.stderr
        self.assertNotIn("WITNESS_SYNTAX_INVALID", output)
        return result.returncode, output

    def test_every_writable_form_protects_providers_and_owned_receivers(self) -> None:
        cases = (
            "foreach($env:APPDATA in @('X:\\not-owned')){}",
            "foreach($GLOBAL:outside in @(1)){}", "$env:APPDATA++",
            "foreach(${function:Remove-Item} in @({param($x) 'replacement'})){}",
            "--${FuNcTiOn:Remove-Item}", "$a,$env:APPDATA=@(1,2)",
            "function F {param($env:APPDATA)}", "$env:APPDATA += 'x'",
            "foreach($compile in @(1)){}", "$compile++", "[int]$compile.ExitCode=1",
            "$local:compile=1", "$script:sourceKinds=$foreign",
            "$shell = New-Object -ComObject WScript.Shell;foreach($shell in @(1)){};$shell.CreateShortcut('x')",
            "$PSDefaultParameterValues['*:OutVariable']='global:x'",
            "ConvertTo-Json -OutVariable global:x", "ConvertTo-Json -ov:+env:APPDATA",
            "ConvertTo-Json -OutV global:x", "$global:unknown.Clear()", "$Error.Clear()",
            "Join-Path 'X:\\never-read' '*' -Resolve",
            "switch -File 'X:\\never-read' {default {break}}",
        )
        for source in cases:
            with self.subTest(source=source):
                code, output = self.parse_owned_case(source)
                self.assertNotEqual(code, 0)
                self.assertIn("REJECT", output)
                self.assertNotIn("PARSE_ACCEPTED", output)

    def test_attributes_require_exact_inert_parameter_metadata(self) -> None:
        cases = (
            "function F {param([System.Obsolete('parser-only')]$x)}",
            "function F {param([System.IO.FileInfo('X:\\never-instantiated')]$x)}",
            "function F {param([ValidateScript({return $true})]$x)}",
            "function F {param([Parameter(Mandatory=1)]$x)}",
            "function F {param([Parameter(Mandatory='true')]$x)}",
            "function F {param([Parameter('x')]$x)}",
            "function F {param([Parameter(ValueFromPipeline=$true)]$x)}",
            "function F {param([AllowEmptyString('x')]$x)}",
            "[Parameter(Mandatory=$true)]$x=1",
        )
        for source in cases:
            with self.subTest(source=source):
                code, output = self.parse_owned_case(source)
                self.assertNotEqual(code, 0)
                self.assertIn("REJECT", output)

    def test_unused_selection_dispatch_is_refused_with_required_pipeline_controls(self) -> None:
        # No selected product extent needs Select-Object: version discovery is
        # outside the retained compiler try. Refuse the unused command itself,
        # including inert First forms, rather than filtering property names.
        cases = (
            "[IO.Path] | Select-Object -Property Assembly",
            "[IO.Path] | Select-Object -ExpandProperty Module",
            "[IO.Path] | Select-Object Assembly",
            "[IO.Path] | Select-Object -Property Module,Assembly",
            '[IO.Path] | Select-Object -Property @{Name="hidden";Expression="Module"}',
            '$field="Module"; [IO.Path] | Select-Object -ExpandProperty $field',
            "[IO.Path] | Select-Object -ExpandProperty Mod*",
            "[IO.Path] | Select-Object -Exp Module",
            "[IO.Path] | Select-Object -ExpandProperty:Module",
            "[IO.Path] | Select-Object -Property:Module",
            "[IO.Path] | select -ExpandProperty Module",
            "[IO.Path] | select-object -ExpandProperty Module",
            r"[IO.Path] | Microsoft.PowerShell.Utility\Select-Object -ExpandProperty Module",
            "[IO.Path] | Select-Object -Property Module -First 1",
            '$cmd="Select-Object"; [IO.Path] | & $cmd -Property Module',
            '[IO.Path] | Select-Object -Property @{Name="hidden";Expression={$_.Module}}',
            "@(1,2) | Select-Object -First 1",
            "@(1,2) | Select-Object -First:1",
        )
        for source in cases:
            with self.subTest(source=source):
                code, output = self.parse_owned_case(source)
                self.assertNotEqual(code, 0)
                self.assertIn("REJECT", output)
                self.assertNotIn("PARSE_ACCEPTED", output)
        for source in ("@(1,2) | ForEach-Object { $_ + 1 }",
                       "@(1,2) | Where-Object { $_ -eq 1 }"):
            with self.subTest(source=source):
                code, output = self.parse_owned_case(source)
                self.assertEqual(code, 0, output)
                self.assertIn("PARSE_ACCEPTED", output)

    def test_ordinary_local_writes_and_required_metadata_remain_accepted(self) -> None:
        cases = (
            "foreach($item in @(1,2)){$count++};$x,$y=@(1,2);$x+=2;--$y",
            "function F {param([string]$x,[int]$n=0) $n++;return $x}",
            "function F {param([Parameter(Mandatory=$true)][AllowEmptyString()][string]$x)}",
            "function F {param([parameter(Mandatory=$FALSE)][string]$x)}",
            "function F {param([System.Management.Automation.ParameterAttribute(Mandatory=$true)][System.Management.Automation.AllowEmptyStringAttribute()][string]$x)}",
            "$script:calls=@();$script:calls+='stop';$script:WorkStackPreserveTemporary=$false",
            "$compile.WaitForExit(30000);$compile.Kill();$compile.ExitCode",
            "switch ('x') {'x' {break} default {return}}",
        )
        for source in cases:
            with self.subTest(source=source):
                code, output = self.parse_owned_case(source)
                self.assertEqual(code, 0, output)
                self.assertIn("PARSE_ACCEPTED", output)

SEP = chr(92)
NEWLINE = chr(10)


def _run_probe(body: str, selected: str, environment_extra: dict, name: str) -> dict:
    """Run one guarded probe and return its JSON summary."""

    FIXTURE_ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=str(FIXTURE_ROOT)) as directory:
        script = Path(directory) / name
        script.write_text(body, encoding="utf-8")
        environment = _fixture_environment()
        environment["WS_SELECTED"] = selected
        environment.update(environment_extra)
        completed = subprocess.run(
            [PWSH, "-NoProfile", "-NonInteractive", "-File", str(script)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            cwd=directory,
            env=environment,
        )
    if completed.returncode != 0:
        raise AssertionError("pwsh failed: " + (completed.stdout or "") + " | " + (completed.stderr or ""))
    return json.loads(completed.stdout.strip())


@unittest.skipIf(PWSH is None, "PowerShell is required for AST extraction")
class CompilerCleanupRecordingTest(unittest.TestCase):
    """All seven lifetime cases reach the REAL cleanup branch taken from the finally."""

    def run_case(self, initial: str, kill: str, confirm: str, exit_code: int) -> dict:
        builder = WINDOWS / "Build-WindowsInstaller.ps1"
        lifetime = _run_pwsh(_EXTRACT_COMPILER, [str(builder)])
        fake = (
            "$script:calls = @()\n"
            "$script:WorkStackPreserveTemporary = $false\n"
            "$temporary = 'fixture-temporary-tree'\n"
        )
        tail = (
            "$thrown = ''\ntry {\n" + lifetime
            + "\n} catch { $thrown = $_.Exception.Message }\n"
            + "[pscustomobject]@{ calls = ($script:calls -join ','); "
            + "preserved = [bool]$script:WorkStackPreserveTemporary; thrown = $thrown } | ConvertTo-Json -Compress\n"
        )
        selected = fake + tail
        body = guarded_probe(
            selected,
            owned_compile=True,
            recorders={
                "New-Object": NEW_OBJECT_FAKE,
                "Test-Path": "param($LiteralPath, $PathType) $script:calls += 'test-path'; return $true",
                "Remove-Item": "param($LiteralPath, [switch]$Recurse, [switch]$Force) $script:calls += 'remove'",
                "Write-Warning": "param($Message) $script:calls += 'warn'",
            },
        )
        return _run_probe(
            body,
            selected,
            {"WS_INITIAL": initial, "WS_KILL": kill, "WS_CONFIRM": confirm, "WS_EXIT": str(exit_code)},
            "cleanup.ps1",
        )

    def test_a_confirmed_exit_removes_the_tree_exactly_once(self) -> None:
        for label, exit_code in (("natural zero", 0), ("natural non-zero", 1)):
            with self.subTest(case=label):
                result = self.run_case("true", "ok", "true", exit_code)
                calls = result["calls"].split(",")
                self.assertEqual("wait:30000", calls[0])
                self.assertNotIn("kill", calls)
                self.assertEqual(1, calls.count("remove"))
                self.assertFalse(result["preserved"])
                if exit_code:
                    self.assertIn("exit code 1", result["thrown"])
                else:
                    self.assertEqual("", result["thrown"])

    def test_a_reaped_timeout_still_removes_the_tree(self) -> None:
        result = self.run_case("false", "ok", "true", 0)
        self.assertEqual("wait:30000,kill,wait:5000,test-path,remove", result["calls"])
        self.assertFalse(result["preserved"])
        self.assertIn("timed out after 30 seconds", result["thrown"])

    def test_an_unknown_exit_preserves_and_never_removes(self) -> None:
        cases = {
            "unconfirmed": ("false", "ok", "false"),
            "initial wait throws": ("throw", "ok", "true"),
            "confirmation throws": ("false", "ok", "throw"),
            "kill throws and stays unconfirmed": ("false", "throw", "false"),
        }
        for label, (initial, kill, confirm) in cases.items():
            with self.subTest(case=label):
                result = self.run_case(initial, kill, confirm, 0)
                calls = result["calls"].split(",")
                self.assertTrue(result["preserved"], result)
                self.assertNotIn("remove", calls)
                self.assertIn("warn", calls)
                self.assertLessEqual(calls.count("kill"), 1)
                self.assertEqual(1, calls.count("wait:30000"))
                self.assertLessEqual(calls.count("wait:5000"), 1)


@unittest.skipIf(PWSH is None, "PowerShell is required for AST extraction")
class StopSelectionRecordingTest(unittest.TestCase):
    """Executes the REAL Stop foreach against synthetic records; nothing is enumerated."""

    INSTALL = "C:" + SEP + "Users" + SEP + "u" + SEP + "Programs" + SEP + "WorkStack"
    OTHER = "C:" + SEP + "Users" + SEP + "u" + SEP + "Programs" + SEP + "WorkStackOther"
    HOST = INSTALL + SEP + "WorkStack.exe"
    LEGACY = INSTALL + SEP + "runtime" + SEP + "pythonw.exe"
    CONSOLE = INSTALL + SEP + "runtime" + SEP + "python.exe"
    ENTRY = INSTALL + SEP + "desktop" + SEP + "python-webview-shell" + SEP + "workstack_desktop.py"
    SERVER = INSTALL + SEP + "run_work_stack.py"

    HELPERS = [
        "Split-WorkStackCommandLine",
        "Test-WorkStackExactPath",
        "Test-WorkStackAbsolutePath",
        "Test-WorkStackIntegerValue",
        "Test-WorkStackDesktopGrammar",
        "Test-WorkStackDesktopInvocation",
    ]

    def record(self, pid: int, image: str, command_line: str) -> dict:
        return {"ProcessId": pid, "ExecutablePath": image, "CommandLine": command_line}

    def run_foreach(self, records, process_id: int = 0) -> dict:
        stop = WINDOWS / "Stop-WorkStack.ps1"
        helpers = extract_functions(stop, self.HELPERS)
        loop = extract_statement(stop, "foreach ($candidate in Get-CimInstance", "ForEachStatementAst")
        self.assertIn("Stop-Process", loop)
        self.assertIn("ownsServer", loop)

        def literal(name: str, value: str) -> str:
            return "$" + name + " = '" + value + "'" + NEWLINE

        prologue = (
            literal("installPath", self.INSTALL)
            + literal("pythonPath", self.CONSOLE)
            + literal("pythonwPath", self.LEGACY)
            + literal("entryPath", self.SERVER)
            + literal("desktopEntryPath", self.ENTRY)
            + literal("hostPath", self.HOST)
            + "$ProcessId = " + str(process_id) + NEWLINE
            + "$stopped = 0" + NEWLINE
            + "$script:stoppedIds = @()" + NEWLINE
        )
        tail = (
            "[pscustomobject]@{ stopped = ($script:stoppedIds -join ',') } | ConvertTo-Json -Compress" + NEWLINE
        )
        selected = helpers + NEWLINE + prologue + loop + NEWLINE + tail
        body = guarded_probe(
            selected,
            extra_commands=("ConvertFrom-Json",),
            extra_instance=("ProcessId", "ExecutablePath", "CommandLine"),
            recorders={
                "Get-CimInstance": "param($ClassName) return ($env:WS_RECORDS | ConvertFrom-Json)",
                "Stop-Process": "param($Id, [switch]$Force) $script:stoppedIds += $Id",
            },
        )
        return _run_probe(body, selected, {"WS_RECORDS": json.dumps(records)}, "stop.ps1")

    def test_the_real_loop_selects_only_genuine_work_stack_processes(self) -> None:
        quoted = '"{0}" "{1}"'
        records = [
            self.record(101, self.HOST, quoted.format(self.HOST, self.ENTRY)),
            self.record(102, self.HOST, '"{0}"'.format(self.HOST)),
            self.record(103, self.LEGACY, quoted.format(self.LEGACY, self.ENTRY)),
            self.record(104, self.CONSOLE, quoted.format(self.CONSOLE, self.SERVER)),
            self.record(201, self.HOST,
                        '"{0}" "{1}" --install-root "{2}"'.format(self.HOST, self.ENTRY, self.OTHER)),
            self.record(202, self.HOST,
                        '"{0}" "{1}" --STATE-ROOT "C:{2}state"'.format(self.HOST, self.ENTRY, SEP)),
            self.record(203, self.HOST,
                        '"{0}" "{1}" --state-root "C:{2}bad|path"'.format(self.HOST, self.ENTRY, SEP)),
            self.record(204, self.HOST,
                        '"{0}" "{1}" --probe-provider bogus'.format(self.HOST, self.ENTRY)),
            self.record(205, self.LEGACY, '"{0}"'.format(self.LEGACY)),
            self.record(206, self.OTHER + SEP + "WorkStack.exe",
                        '"{0}{1}WorkStack.exe" "{2}"'.format(self.OTHER, SEP, self.ENTRY)),
            self.record(207, self.HOST, '"{0} "{1}"'.format(self.HOST, self.ENTRY)),
            self.record(208, self.HOST,
                        '"{0}" "{1}" --state-root'.format(self.HOST, self.ENTRY)),
        ]
        stopped = sorted(self.run_foreach(records)["stopped"].split(","))
        self.assertEqual(["101", "102", "103", "104"], stopped)

    def test_the_real_loop_honours_the_process_id_restriction(self) -> None:
        records = [
            self.record(101, self.HOST, '"{0}" "{1}"'.format(self.HOST, self.ENTRY)),
            self.record(102, self.HOST, '"{0}" "{1}"'.format(self.HOST, self.ENTRY)),
        ]
        self.assertEqual("102", self.run_foreach(records, process_id=102)["stopped"])


@unittest.skipIf(PWSH is None, "PowerShell is required for AST extraction")
class UninstallSelectionRecordingTest(unittest.TestCase):
    """Executes the REAL Remove-OwnedShortcut with a recording deletion leaf.

    Nothing is deleted: Remove-Item is the trusted preamble's recorder, so the
    control observes which links the product source WOULD have removed.
    """

    INSTALL = StopSelectionRecordingTest.INSTALL
    HOST = StopSelectionRecordingTest.HOST
    LEGACY = StopSelectionRecordingTest.LEGACY
    ENTRY = StopSelectionRecordingTest.ENTRY
    OTHER = StopSelectionRecordingTest.OTHER

    HELPERS = [
        "Split-WorkStackShortcutArguments",
        "Test-WorkStackExactPath",
        "Test-WorkStackAbsolutePath",
        "Test-WorkStackIntegerValue",
        "Test-WorkStackDesktopGrammar",
        "Remove-OwnedShortcut",
    ]

    def run_link(self, target: str, arguments: str, com: str = "ok") -> dict:
        helpers = extract_functions(WINDOWS / "Uninstall-WorkStack.ps1", self.HELPERS)
        self.assertIn("Remove-Item -LiteralPath $Path -Force", helpers)
        link = "C:" + SEP + "links" + SEP + "Work Stack.lnk"
        prologue = (
            "$script:removed = @()" + NEWLINE
            + "$script:warned = @()" + NEWLINE
            + "$desktopTargets = @('" + self.HOST + "', '" + self.LEGACY + "')" + NEWLINE
        )
        tail = (
            "Remove-OwnedShortcut -Path '" + link + "' -ExpectedTargets $desktopTargets "
            + "-ExpectedArgumentPath '" + self.ENTRY + "' -ExpectedInstallRoot '" + self.INSTALL + "' "
            + "-DesktopGrammar -ScriptRequired" + NEWLINE
            + "[pscustomobject]@{ removed = ($script:removed -join ','); warned = ($script:warned -join ',') } "
            + "| ConvertTo-Json -Compress" + NEWLINE
        )
        selected = helpers + NEWLINE + prologue + tail
        body = guarded_probe(
            selected,
            extra_instance=("Add-Member", "CreateShortcut", "TargetPath", "Arguments", "Link"),
            recorders={
                "New-Object": NEW_OBJECT_FAKE,
                "Test-Path": "param($LiteralPath, $PathType) return $true",
                "Remove-Item": "param($LiteralPath, [switch]$Force) $script:removed += $LiteralPath",
                "Write-Warning": "param($Message) $script:warned += 'warned'",
            },
        )
        return _run_probe(
            body,
            selected,
            {"WS_TARGET": target, "WS_ARGUMENTS": arguments, "WS_COM": com},
            "uninstall.ps1",
        )

    def test_a_genuine_new_or_legacy_link_is_selected(self) -> None:
        for target in (self.HOST, self.LEGACY):
            with self.subTest(target=target):
                result = self.run_link(
                    target, '"{0}" --install-root "{1}"'.format(self.ENTRY, self.INSTALL)
                )
                self.assertIn("Work Stack.lnk", result["removed"])

    def test_foreign_borrowed_and_unreadable_links_are_preserved(self) -> None:
        cases = {
            "entry borrowed as a url value": (self.HOST, '--url "{0}"'.format(self.ENTRY), "ok"),
            "foreign install target": (
                self.OTHER + SEP + "WorkStack.exe", '"{0}"'.format(self.ENTRY), "ok"),
            "wrong supplied install root": (
                self.HOST, '"{0}" --install-root "{1}"'.format(self.ENTRY, self.OTHER), "ok"),
            "uppercase option spelling": (
                self.HOST, '"{0}" --STATE-ROOT "C:{1}state"'.format(self.ENTRY, SEP), "ok"),
            "malformed quoting": (self.HOST, '"{0}'.format(self.ENTRY), "ok"),
            "unreadable COM": (self.HOST, '"{0}"'.format(self.ENTRY), "throw"),
        }
        for label, (target, arguments, com) in cases.items():
            with self.subTest(case=label):
                result = self.run_link(target, arguments, com)
                self.assertEqual("", result["removed"], label)
                if com == "throw":
                    self.assertIn("warned", result["warned"])



@unittest.skipIf(PWSH is None, "PowerShell is required for AST extraction")
class InstallerStagingRecordingTest(unittest.TestCase):
    """Executes the REAL installer root-file copy loop and staged-leaf validation.

    Nothing is copied, stopped, backed up or moved: Copy-Item is a recording leaf
    and Test-Path answers from a fixture-owned table, so the control observes the
    order the product source actually imposes. The destructive statements that
    follow are represented by a recorded marker, which a refusal must never reach.
    """

    STAGING = "C:" + SEP + "stage"
    SOURCE = "C:" + SEP + "payload"
    CRITICAL = ("WorkStack.exe", "desktop" + SEP + "python-webview-shell" + SEP + "workstack_desktop.py",
                "runtime" + SEP + "python312.dll")

    def run_stage(self, leaf_kinds: dict) -> dict:
        installer = WINDOWS / "Install-WorkStack.ps1"
        copy_loop = extract_statement(installer, "'README.md', 'SECURITY.md'", "ForEachStatementAst")
        directory_loop = extract_statement(installer, "\'workstack\', \'contracts\', \'licenses\', \'web\', \'runtime\', \'desktop\'", "ForEachStatementAst")
        leaf_loop = extract_statement(
            installer, "The staged Work Stack desktop host is incomplete", "ForEachStatementAst"
        )
        source = installer.read_text(encoding="utf-8")
        for effect in ("$stopScript =", "--data-dir $dataPath maintenance backup", "Move-Item -LiteralPath $installPath"):
            self.assertLess(source.index(leaf_loop), source.index(effect))
        # The real loops, not a reimplementation: the root-file list and the three
        # critical staged leaves are the product's own literals.
        self.assertIn("Copy-Item", copy_loop)
        self.assertIn("WorkStack.exe", copy_loop)
        for leaf in self.CRITICAL:
            self.assertIn(leaf, leaf_loop)

        prologue = (
            "$staging = '" + self.STAGING + "'" + NEWLINE
            + "$sourcePath = '" + self.SOURCE + "'" + NEWLINE
            + "$script:calls = @()" + NEWLINE
            + "$script:sourceKinds = $env:WS_KINDS | ConvertFrom-Json" + NEWLINE
            + "$script:stageKinds = @{}" + NEWLINE
        )
        tail = (
            # Reached only if the real validation loop did not refuse: these stand
            # for Stop-WorkStack, the pre-upgrade backup and the payload moves.
            "$script:calls += 'stop'" + NEWLINE
            + "$script:calls += 'backup'" + NEWLINE
            + "$script:calls += 'move'" + NEWLINE
        )
        summary = (
            "[pscustomobject]@{ calls = ($script:calls -join ','); thrown = $thrown } | ConvertTo-Json -Compress"
            + NEWLINE
        )
        selected = (
            prologue
            + "$thrown = ''" + NEWLINE
            + "try {" + NEWLINE + directory_loop + NEWLINE + copy_loop + NEWLINE + leaf_loop + NEWLINE + tail
            + "} catch { $thrown = $_.Exception.Message }" + NEWLINE
            + summary
        )
        body = guarded_probe(
            selected,
            extra_commands=("ConvertFrom-Json",),
            recorders={
                # Every filesystem leaf is recording or answering: no real copy and
                # no real probe of any path outside the fixture root.
                "Copy-Item": STAGING_COPY_FAKE,
                "Test-Path": "param($LiteralPath,$PathType) "
                             "$script:calls += ('test:'+$LiteralPath+':'+$PathType); "
                             "return $script:stageKinds.ContainsKey($LiteralPath) -and "
                             "($script:stageKinds[$LiteralPath] -eq $PathType)",
            },
        )
        return _run_probe(body, selected, {"WS_KINDS": json.dumps(leaf_kinds)}, "staging.ps1")

    def _kinds(self, override: dict = None) -> dict:
        kinds = {self.SOURCE + SEP + leaf: "Leaf" for leaf in self.CRITICAL}
        for directory in ('workstack','contracts','licenses','web','runtime','desktop'):
            kinds[self.SOURCE + SEP + directory] = 'Container'
        for name in ('run_work_stack.py','requirements.txt','requirements-windows-desktop.txt','README.md','SECURITY.md','THIRD_PARTY_NOTICES.md'):
            kinds[self.SOURCE + SEP + name] = 'Leaf'
        kinds.update(override or {})
        return kinds

    def test_the_host_entry_and_dll_are_staged_and_validated_before_any_effect(self) -> None:
        result = self.run_stage(self._kinds())
        calls = result["calls"].split(",")
        self.assertEqual("", result["thrown"])
        # The root files, including the branded host, are copied by the real loop.
        self.assertIn("copy:" + self.SOURCE + SEP + "WorkStack.exe", calls)
        self.assertIn("copy:" + self.SOURCE + SEP + "run_work_stack.py", calls)
        # Each critical leaf is validated on the STAGED tree, and every validation
        # precedes the first destructive step.
        first_effect = calls.index("stop")
        for leaf in self.CRITICAL:
            probe = "test:" + self.STAGING + SEP + leaf + ":Leaf"
            self.assertIn(probe, calls)
            self.assertLess(calls.index(probe), first_effect, leaf)
            if leaf != 'WorkStack.exe':
                self.assertIn('stage:' + self.STAGING + SEP + leaf, calls)
        self.assertEqual(["stop", "backup", "move"], calls[first_effect:])

    def test_a_critical_input_shaped_as_a_directory_refuses_before_any_effect(self) -> None:
        for leaf in self.CRITICAL:
            with self.subTest(leaf=leaf):
                kinds = self._kinds({self.SOURCE + SEP + leaf: "Container"})
                result = self.run_stage(kinds)
                calls = result["calls"].split(",")
                self.assertTrue("incomplete" in result["thrown"] or "source missing" in result["thrown"], result)
                self.assertIn(leaf, result["thrown"])
                for effect in ("stop", "backup", "move"):
                    self.assertNotIn(effect, calls)

    def test_a_missing_critical_input_refuses_before_any_effect(self) -> None:
        for leaf in self.CRITICAL:
            with self.subTest(leaf=leaf):
                kinds = self._kinds()
                del kinds[self.SOURCE + SEP + leaf]
                result = self.run_stage(kinds)
                self.assertTrue("incomplete" in result["thrown"] or "source missing" in result["thrown"], result)
                for effect in ("stop", "backup", "move"):
                    self.assertNotIn(effect, result["calls"].split(","))


@unittest.skipIf(PWSH is None, "PowerShell is required for AST extraction")
class ShortcutDescriptorSaveRecordingTest(unittest.TestCase):
    """Executes the REAL save loop for ALL THREE managed descriptors.

    The Shell and its shortcuts are retained fakes with a recorded Save, and the
    notifier is recorded too: no COM object is created and no link is written.
    """

    HELPERS = [
        "Get-WorkStackShortcutNotificationConstant", "Get-WorkStackShortcutIconPath",
        "ConvertTo-WorkStackCommandLineArgument", "Get-WorkStackManagedShortcut",
    ]

    def run_finalization(self, install: str) -> dict:
        shortcuts = WINDOWS / "WorkStack-Shortcuts.ps1"
        helpers = extract_functions(shortcuts, self.HELPERS)
        tokenizer = extract_functions(WINDOWS / "Uninstall-WorkStack.ps1", ["Split-WorkStackShortcutArguments"])
        loops = _run_pwsh(_EXTRACT_SHORTCUT_RECORDING, [str(shortcuts)])
        prologue = (
            "$script:recordedSaves = @(); $script:order = @(); $saved=@(); $notifications=@()\n"
            "$descriptors = Get-WorkStackManagedShortcut -InstallPath $env:WS_INSTALL "
            "-StatePath $env:WS_STATE -StartMenuPath 'C:\\menu' -DesktopPath 'C:\\desk'\n"
        )
        selected = tokenizer + "\n" + helpers + "\n" + prologue + loops + "\n" + (
            "[pscustomobject]@{saved=$script:recordedSaves;order=($script:order -join ',');"
            "complete=(@($notifications | Where-Object {-not $_.Notified}).Count -eq 0)} | ConvertTo-Json -Depth 5 -Compress"
        )
        body = guarded_probe(
            selected, required_extents=tuple(self.HELPERS) + ("Split-WorkStackShortcutArguments",),
            extra_commands=tuple(self.HELPERS),
            extra_instance=("Save", "Path", "TargetPath", "Arguments", "WorkingDirectory", "IconLocation", "Notified", "Existed", "Argv"),
            recorders={
                "New-RecordingShortcut": SHORTCUT_RECORDING_FAKE,
                "Test-Path": "param($LiteralPath,$PathType) return $false",
                "Send-WorkStackShortcutNotification": (
                    "param($Path,$Existed,$Notifier) "
                    "$c=Get-WorkStackShortcutNotificationConstant; "
                    "if($Path.Length -ge $c.MaxNotifyPath){throw 'recording path exceeds bound'}; "
                    "$script:order += ('notify:'+$Path); "
                    "return [pscustomobject]@{Path=$Path;Notified=$true}"
                ),
            },
        )
        return _run_probe(body, selected, {"WS_INSTALL": install, "WS_STATE": install + SEP + "state"}, "descriptors.ps1")

    def test_all_three_descriptors_save_exact_fields_and_round_trip(self) -> None:
        for label, install in {
            "ordinary": "C:" + SEP + "Programs" + SEP + "WorkStack",
            "trailing backslash": "C:" + SEP + "Programs" + SEP + "WorkStack" + SEP,
            "spaced unicode": "C:" + SEP + "Program Files" + SEP + "워크 스택",
            "quote in the path": "C:" + SEP + "Programs" + SEP + 'Work"Stack',
        }.items():
            with self.subTest(case=label):
                result = self.run_finalization(install)
                saved = result["saved"]
                self.assertEqual(3, len(saved), label)
                order = result["order"].split(",")
                # T7 tail: every save precedes every notification.
                saves = [i for i, call in enumerate(order) if call.startswith("save:")]
                notifies = [i for i, call in enumerate(order) if call.startswith("notify:")]
                self.assertEqual(3, len(saves))
                self.assertEqual(3, len(notifies))
                self.assertLess(max(saves), min(notifies))
                for entry in saved:
                    self.assertTrue(entry["IconLocation"], entry)
                    self.assertTrue(entry["WorkingDirectory"], entry)
                    # The decoded argv survives the exact Arguments string.
                    self.assertIsNotNone(entry["Argv"], entry)
                maintenance = [e for e in saved if e["Path"].endswith("Maintenance.lnk")]
                self.assertEqual(1, len(maintenance))
                self.assertEqual("powershell.exe", maintenance[0]["TargetPath"])
                argv = maintenance[0]["Argv"]
                for token in ("-NoProfile", "-ExecutionPolicy", "Bypass", "-InstallRoot"):
                    self.assertIn(token, argv, label)
                self.assertTrue(result['complete'])
                self.assertEqual(['C:\\menu\\Work Stack.lnk','C:\\desk\\Work Stack.lnk','C:\\menu\\Work Stack Maintenance.lnk'],[entry['Path'] for entry in saved])
                expected_entry=install.rstrip('\\')+SEP+'desktop'+SEP+'python-webview-shell'+SEP+'workstack_desktop.py'
                icon=install.rstrip('\\')+SEP+'desktop'+SEP+'python-webview-shell'+SEP+'assets'+SEP+'WorkStack-Mark-Lime-v2.ico,0'
                for entry in saved:
                    self.assertEqual(install,entry['WorkingDirectory'])
                    self.assertEqual(icon,entry['IconLocation'])
                for entry in saved[:2]:
                    self.assertEqual(install.rstrip('\\')+SEP+'WorkStack.exe',entry['TargetPath'])
                    self.assertEqual([expected_entry,'--install-root',install,'--state-root',install+SEP+'state'],entry['Argv'])
                self.assertEqual(['-NoProfile','-WindowStyle','Hidden','-ExecutionPolicy','Bypass','-File',install.rstrip('\\')+SEP+'scripts'+SEP+'windows'+SEP+'Maintain-WorkStack.ps1','-InstallRoot',install,'-StateRoot',install+SEP+'state'],argv)


if __name__ == "__main__":
    unittest.main()
