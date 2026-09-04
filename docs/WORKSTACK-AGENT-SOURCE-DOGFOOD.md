# Work Stack Agent Skill — source dogfood guide

## Scope

P0 supports manual user-scope installation from a verified source checkout.
Automatic Skill lifecycle, packaged launchers, and PATH management are P0b.

## Prerequisites

- A clean Work Stack source checkout at the expected commit.
- Python 3.10 or newer (CI uses Python 3.12).
- An existing local v3 authority selected by the user.
- The authority's independently obtained workspace UID.
- The verified user-scope Skills directory for the chosen agent host.

## 1. Verify the checkout

```text
git status --porcelain
git log --oneline -1
```

Require empty status output and the expected commit. Do not install from an
unreviewed or modified checkout.

## 2. Verify the source launcher

The source-checkout command prefix is:

```text
python -I <checkout-root>/run_work_stack.py
```

On Windows, use the same command with the platform path represented by
`<checkout-root>`. Run a read-only help probe before configuring the agent:

```text
python -I <checkout-root>/run_work_stack.py --help
```

Record that verified command as `<pfx>` in the agent's local invocation
policy. Do not embed the checkout path or authority path in the canonical
Skill tree.

## 3. Install the documentation-only Skill

Copy the complete canonical directory:

```text
<checkout-root>/integrations/agent-skill/work-stack
```

to `work-stack` below the agent host's verified user-scope Skills directory.
For Codex, the user-scope destination is:

```text
$HOME/.agents/skills/work-stack
```

On Windows this is:

```text
%USERPROFILE%\.agents\skills\work-stack
```

Perform G40 from a clean user profile or an otherwise empty destination. Do
not merge the canonical tree into a pre-existing or modified installation.
The installed tree must contain exactly:

- `SKILL.md`
- `references/commands.md`
- `references/journal-policy.md`

No script or executable belongs in the P0 Skill tree. Product-specific
automatic discovery and lifecycle claims remain out of scope until verified.

## 4. Validate before use

From `<checkout-root>`, run the pinned repository-owned validator:

```text
python -I quality/agent-p0-oracle/validate_skill.py integrations/agent-skill/work-stack
```

Success exits 0 and emits `{"skill":"work-stack","valid":true,"violations":[]}`.
An unpinned user-profile validator is advisory only.

Run the same pinned validator against the installed copy. On POSIX:

```text
python -I quality/agent-p0-oracle/validate_skill.py "$HOME/.agents/skills/work-stack"
```

On Windows:

```text
python -I quality/agent-p0-oracle/validate_skill.py "%USERPROFILE%\.agents\skills\work-stack"
```

Both validations must exit 0 with the exact success object above. Also require
the canonical and installed trees to have the same three-file roster and the
same relative-path-to-SHA-256 map. G40 proves the copied tree at the official
`.agents/skills/work-stack` location; it does not claim that an arbitrary agent
host discovered or loaded the Skill. This is manual installation, not an
automatic lifecycle claim.

## 5. Configure explicit authority inputs

Configure the agent invocation policy with these three values:

```text
command prefix: <pfx>
data directory: <data-dir>
expected workspace UID: <ws-uid>
```

They are invocation inputs, not files for the Skill to discover. Do not use an
active-profile fallback and do not create a missing authority.

## 6. Authority smoke test

```text
<pfx> --data-dir <data-dir> agent --workspace-uid <ws-uid> status
```

Proceed only when exit 0 returns contract `workstack.cli.v1`, matching
workspace identity, storage format `v3`, `capability_supported: true`, and
`ready: true`. Any refusal ends the smoke test; there is no direct-Store
fallback.

The command must emit exactly one UTF-8 JSON envelope and no non-envelope
stdout. Record only a digest of the envelope; do not copy authority paths or
Task content into evidence.

## 7. Capture the selected Task baseline

The user must select or confirm exactly one existing Task. Read only that Task:

```text
<pfx> --data-dir <data-dir> agent --workspace-uid <ws-uid> context --task <task-id>
```

Require exit 0, contract `workstack.cli.v1`, the expected workspace UID and
Task ID, no more than five `recent_worklog` entries, and a final envelope no
larger than 32 KiB. Canonically hash `data.task` as the pre-checkpoint Task
projection. Keep raw Task content outside the G40 receipt.

Confirm that no entry in the bounded `recent_worklog` projection exactly
matches the planned five-field checkpoint. The runner later checks the complete
legacy Worklog list for the selected date and requires exactly one match after
the commit and replay sequence.

## 8. Prepare one bounded checkpoint

Choose a fresh stable 8–128 character intent ID before the first invocation.
Write one UTF-8 JSON file outside both the checkout and authority. It must have
exactly these five fields:

```json
{
  "blockers": [],
  "date": "2026-09-02",
  "done": ["Describe one observable completed result."],
  "next": ["Describe the next executable action."],
  "task_id": "T-0001"
}
```

Replace the example date, Task ID and journal text with the selected Task's
actual, user-approved checkpoint. At least one item across `done`, `next`, and
`blockers` is required. Follow `references/journal-policy.md`: do not include
prompts, hidden reasoning, transcripts, environment dumps, broad changed-file
inventories, credentials, tokens, or unrelated personal data.

Do not edit or regenerate this file between the first invocation and replay.
Feed its bytes to stdin. This platform-neutral helper preserves the file bytes
and can be used in shells without input redirection:

```text
python -c "import pathlib,subprocess,sys; p=pathlib.Path(sys.argv[1]).read_bytes(); raise SystemExit(subprocess.run(sys.argv[2:],input=p).returncode)" <checkpoint-file> python -I <checkout-root>/run_work_stack.py --data-dir <data-dir> agent --workspace-uid <ws-uid> checkpoint --intent-id <intent-id> --stdin
```

## 9. Run the owner-operated G40 gate

The trusted Oracle checkout and candidate checkout must both be clean and at
committed SHAs. The G40 runner, pinned validator, and G30 runner must be tracked
at the Oracle's `HEAD`. G40 deliberately refuses to run from an uncommitted
Oracle, including while the runner itself is still untracked.

The installed Skill must already exist at exactly:

```text
<profile-root>/.agents/skills/work-stack
```

The runner does not create the authority, profile, or Skill installation. The
passing canonical G30 receipt, checkpoint packet, and receipt output directory
must be outside and disjoint from the candidate, authority, isolated profile,
and Oracle roots. The final receipt target must not already exist.

Run the gate from the clean trusted Oracle checkout:

```text
python -I quality/agent-p0-oracle/run_g40.py \
  --candidate-root <checkout-root> \
  --candidate-sha <candidate-40-hex> \
  --g30-receipt <passing-canonical-g30-receipt.json> \
  --data-dir <existing-v3-data-dir> \
  --workspace-uid <ws-uid> \
  --task-id <existing-task-id> \
  --checkpoint-packet <exact-five-field-checkpoint.json> \
  --intent-id <fresh-stable-intent-id> \
  --installed-skill-dir <profile-root>/.agents/skills/work-stack \
  --output-dir <external-receipt-directory>
```

Use PowerShell backticks or one line instead of `\` on Windows. The runner
derives the trusted Oracle root from its own committed location; there is no
`--oracle-root` override.

The owner-operated runner performs the normative G40 sequence:

1. It verifies the candidate SHA and clean checkout, the clean committed
   Oracle, the canonical passing G30 receipt, and all protected path
   boundaries before checkpoint mutation.
2. It independently reruns the trusted G30 runner into a temporary output and
   requires the emitted and written canonical receipt bytes to equal the
   supplied G30 receipt exactly.
3. It validates the canonical and installed Skill with the pinned validator,
   requires the exact three-file roster, and requires both trees to be
   byte-identical.
4. It runs the literal isolated launcher
   `python -I <checkout-root>/run_work_stack.py --help`, then uses that same
   launcher with the explicit v3 data directory for all Work Stack commands.
5. It requires `agent status` to admit the `exclusive-local` transport and
   captures the selected Task's bounded baseline context.
6. It submits the checkpoint packet bytes once, requiring committed/non-replay
   evidence, then submits the identical bytes and intent ID once more,
   requiring committed/replay evidence and identical response data.
7. It reads context and the legacy Worklog list back, requiring exactly one
   matching entry and an unchanged Task projection.

The standalone helper in step 8 remains useful for manual troubleshooting, but
the runner's stdin handoff and checks are the authoritative G40 procedure.

## 10. Authority, replay, and profile invariants

Before the first checkpoint, the runner inventories and hashes the existing v3
authority. The first successful checkpoint must preserve the file and directory
roster and change exactly these two root documents:

- `activity.json`
- `worklog.json`

The resulting authority must contain exactly one semantic idempotency record
for the supplied intent and exactly one matching Worklog entry. The identical
replay must leave the complete authority inventory byte-identical to the
post-first-checkpoint snapshot. Subsequent context and legacy Worklog reads
must also leave it byte-identical.

G40 separately rechecks the candidate, canonical/installed Skill bytes, and
the complete isolated profile inventory after the temporary runtime is gone.
Any collateral authority change, replay rewrite, candidate dirtiness, Skill
change, or profile mutation fails the gate. G40 does not assert agent-host
discovery or Daily Review UI visibility; those require separate product or
human observation.

## 11. Receipt and operating assumptions

The runner writes one flat, canonical, privacy-redacted JSON receipt under
`<output-dir>/G40/<candidate-sha>.json`. Its schema and evidence field set are
defined by the committed runner, not by a duplicated hand-authored example in
this guide. The final file is created atomically and exclusively; G40 never
overwrites an existing receipt.

The receipt binds the candidate and Oracle SHAs, the supplied and independently
reproduced G30 receipt, contract/interface hashes, launcher and validator
observations, Skill trees, first/replay/readback envelopes, Task projection,
authority snapshots, and pass/fail checks. Workspace UID, Task ID, intent ID,
checkpoint bytes, Task/checkpoint content, environment, and absolute paths are
omitted or represented only by content-free hashes. An invalid candidate SHA is
never echoed verbatim. Missing or unverifiable evidence produces a canonical
failed receipt rather than a partial pass.

Operational limits are deliberately fail-closed:

- command timeout: 30 seconds;
- checkpoint and success envelope bound: 32 KiB;
- general evidence document bound: 1 MiB;
- authority/profile inventory: at most 256 regular files;
- individual authority/profile file bound: 16 MiB;
- symlinks and non-regular files in inspected trees are rejected.

The child-process environment is reduced to a platform allowlist plus the
isolated profile and ephemeral Work Stack runtime. Tokens, proxy variables,
SSH agent state, and unrelated Work Stack variables are not forwarded. The
runner itself invokes no network operation; this is operational isolation, not
a kernel-enforced network sandbox. Run G40 on a host where that distinction is
acceptable or add an external OS sandbox.

## P0 boundary

| Capability | P0 |
|---|---|
| Manual user-scope Skill install | Supported |
| Verified source launcher prefix | Supported |
| Explicit v3 path and workspace UID | Required |
| Automatic Skill update/remove | P0b |
| Packaged launcher/PATH integration | P0b |
| Workspace-local discovery | Deferred |
| v4 mutation parity | P0b |
| Desktop SSH/Linux registry | P0b |
| Frontend provenance UI | P0b |
| Installer/updater/public browser matrix | Public release gate |
