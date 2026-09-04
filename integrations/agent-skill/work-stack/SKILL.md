---
name: work-stack
description: Read one explicitly selected Work Stack Task and append bounded, idempotent Daily Review checkpoints through the supported CLI.
---

# Work Stack Agent Skill

Use this Skill only when the user has selected one existing Task in an existing
Work Stack v3 authority and wants the agent to read its bounded context or
record observable `done` / `next` / `blockers` facts.

Read these references before issuing a command:

- [references/commands.md](references/commands.md) — exact command surface,
  payloads, envelopes, and exit handling.
- [references/journal-policy.md](references/journal-policy.md) — checkpoint
  content and uncertainty policy.

## Workflow

1. Run `agent status` with the explicitly configured prefix, data directory,
   and expected workspace UID. Stop on any refusal or unavailable owner.
2. Ask the user to select or confirm exactly one existing Task.
3. Run `agent context` for that Task.
4. At a meaningful milestone, blocker, or final handoff, run one `agent
   checkpoint` with one stable intent ID.
5. Use legacy `worklog list` only as optional diagnostic evidence. It cannot
   prove whether an uncertain checkpoint committed.

## Stop conditions

- Missing explicit authority, identity mismatch, unsupported format,
  recovery/synchronization issue, or unavailable owner: stop.
- `commit_unknown`: stop immediately, retain the same intent ID, and report
  the uncertainty. Do not issue another checkpoint for that logical intent.

## Safety boundary

- Never edit JSON, NDJSON, database, or SSOT files directly.
- Never mutate Tasks, Objectives, relationships, Git, or external systems.
- Never adopt, restore, migrate, synchronize, rebind, or send messages.
- Never access SSH credentials, browser profiles, tokens, or credentials.
- Never place prompts, hidden reasoning, command transcripts, process
  environment state, or broad changed-file inventories in a checkpoint.
- Never place secrets, credentials, or tokens in a checkpoint.
