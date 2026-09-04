# Journal policy

## Allowed checkpoint content

- Concise observable facts: completed results in `done`, the next executable
  step in `next`, and current impediments in `blockers`.
- A date in canonical `YYYY-MM-DD` form.
- Exactly one already-selected Task ID.
- One stable intent ID chosen before invoking the logical checkpoint.

The input object contains exactly `task_id`, `date`, `done`, `next`, and
`blockers`. Each list has at most 20 items; every item is non-empty, trimmed,
and at most 1000 characters. At least one item across all three lists is
required. The complete stdin object is at most 32 KiB.

## Content that must stay out

- Raw prompts, hidden reasoning, command transcripts, environment dumps, or
  broad changed-file inventories.
- Secrets, credentials, tokens, API keys, SSH configuration, or browser data.
- Absolute user paths or workspace-absolute file paths.
- Task changes, status transitions, Objective mutations, or relationship
  edits.
- Direct edits to JSON, NDJSON, database, or SSOT files.
- Raw SQL mutation commands.

## Timing

Write a checkpoint only for a meaningful milestone, blocker, or final
handoff—not for each command. Keep the facts sufficient to resume the selected
Task without copying a terminal transcript.

## Idempotency and uncertainty

- Same intent ID plus identical canonical body represents the same logical
  checkpoint and can produce an idempotent replay.
- Never use one intent ID for different content.
- The CLI transport, not the Skill, owns its one bounded identical replay
  after possible response loss.
- On `commit_unknown`: **stop and retain the same intent ID**. Report that the
  outcome is unknown. Do not invoke another checkpoint, change the key, or
  infer success from matching Task or Worklog values.

The Skill never falls back to direct Store access after a running-server
failure.
