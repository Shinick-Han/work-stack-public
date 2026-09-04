# Command reference

`<pfx>` is the verified source-checkout launcher prefix. `<data-dir>` is an
existing v3 authority, and `<ws-uid>` is its independently supplied canonical
workspace UUID. Substitute those placeholders before execution. The Skill
does not discover or create an authority.

## Start: status

```text
<pfx> --data-dir <data-dir> agent --workspace-uid <ws-uid> status
```

Success exits 0 and emits one JSON object:

```json
{
  "contract": "workstack.cli.v1",
  "data": {
    "actual_workspace_uid": "11111111-1111-4111-8111-111111111111",
    "capability_reason": null,
    "capability_supported": true,
    "contract": "workstack.cli.v1",
    "data_dir_available": true,
    "exclusive_local_available": true,
    "expected_workspace_uid": "11111111-1111-4111-8111-111111111111",
    "ready": true,
    "running_server_available": false,
    "storage_format": "v3"
  },
  "meta": {
    "command": "agent.status",
    "transport": "exclusive-local",
    "workspace_uid": "11111111-1111-4111-8111-111111111111"
  }
}
```

If status exits 1, inspect `error.code` and stop. Do not try to open the Store
or switch authority paths. A normal refusal omits mutation metadata:

```json
{
  "contract": "workstack.cli.v1",
  "error": {
    "code": "invalid_authority",
    "details": {},
    "message": "the resolved authority does not exist, is unrecognizable or cannot be inspected"
  },
  "meta": {"command": "agent.status"}
}
```

## Read one selected Task: context

```text
<pfx> --data-dir <data-dir> agent --workspace-uid <ws-uid> context --task T-0001
```

Success exits 0. The Task projection is allowlisted, `recent_worklog` contains
at most five entries, and `omitted` names context deliberately excluded from
the response:

```json
{
  "contract": "workstack.cli.v1",
  "data": {
    "omitted": [
      "attachments",
      "captures",
      "objectives",
      "relationships",
      "work_sessions"
    ],
    "recent_worklog": [
      {
        "blockers": [],
        "date": "2026-09-02",
        "done": ["Designed the admission flow."],
        "next": ["Wire the preflight into the runtime."]
      }
    ],
    "task": {
      "detail": "Add the workspace-identity and format probe.",
      "due": null,
      "id": "T-0001",
      "priority": "P1",
      "revision": 4,
      "status": "started",
      "title": "Implement authority preflight",
      "uid": "22222222-2222-4222-8222-222222222222"
    },
    "workspace_uid": "11111111-1111-4111-8111-111111111111"
  },
  "meta": {
    "command": "agent.context",
    "task_id": "T-0001",
    "transport": "exclusive-local",
    "workspace_uid": "11111111-1111-4111-8111-111111111111"
  }
}
```

## Append a checkpoint

Choose one stable 8–128 character intent ID for this logical checkpoint. The
CLI receives exactly one five-field UTF-8 JSON object on stdin:

```text
<pfx> --data-dir <data-dir> agent --workspace-uid <ws-uid> checkpoint --intent-id checkpoint-20260902-0001 --stdin
```

```json
{
  "blockers": [],
  "date": "2026-09-02",
  "done": ["Implemented the workspace preflight."],
  "next": ["Add response-loss coverage."],
  "task_id": "T-0001"
}
```

Each of `done`, `next`, and `blockers` is a JSON list. At least one item across
the three lists is required. A first commit or idempotent replay exits 0:

```json
{
  "contract": "workstack.cli.v1",
  "data": {
    "blockers": [],
    "date": "2026-09-02",
    "done": ["Implemented the workspace preflight."],
    "next": ["Add response-loss coverage."],
    "task": "Implement authority preflight",
    "task_id": "T-0001"
  },
  "meta": {
    "command": "agent.checkpoint",
    "commit_state": "committed",
    "intent_id": "checkpoint-20260902-0001",
    "replayed": false,
    "task_id": "T-0001",
    "transport": "exclusive-local",
    "workspace_uid": "11111111-1111-4111-8111-111111111111"
  }
}
```

`replayed: true` means the same intent and canonical body were already
committed. Never reuse an intent ID with a different body.

### Unknown commit outcome

This failure is valid only after a running-server POST may have arrived and
the CLI's one bounded identical replay also could not establish the result:

```json
{
  "contract": "workstack.cli.v1",
  "error": {
    "code": "commit_unknown",
    "details": {},
    "message": "the mutation outcome is unverifiable after the bounded identical replay",
    "retryable": false
  },
  "meta": {
    "command": "agent.checkpoint",
    "commit_state": "unknown",
    "intent_id": "checkpoint-20260902-0001",
    "task_id": "T-0001",
    "transport": "running-server",
    "workspace_uid": "11111111-1111-4111-8111-111111111111"
  }
}
```

On `commit_unknown`, **stop and retain the same intent ID**. Report the
uncertainty; do not issue another checkpoint and do not infer success from
matching Worklog text.

## Optional diagnostic Worklog read

```text
<pfx> --data-dir <data-dir> worklog list --date 2026-09-02
```

This legacy read can provide diagnostic evidence only. It cannot resolve
`commit_unknown`, because Worklog entries do not expose an intent ID.

## Exit and envelope rules

| Exit | Meaning |
|---:|---|
| 0 | Success or idempotent replay |
| 1 | Parsed command failed; inspect `error.code` |
| 2 | Command-line usage/parser failure; no agent envelope |

- Success has `data` and no `error`; failure has `error` and no `data`.
- Command-inapplicable metadata is omitted, never filled with placeholders.
- `commit_state: "committed"` appears only on successful checkpoint output.
- `commit_state: "unknown"` appears only on `commit_unknown`.
- `retryable`, when sound, is an `error` member rather than a `meta` member.
- The final UTF-8 envelope is bounded to 32 KiB.
