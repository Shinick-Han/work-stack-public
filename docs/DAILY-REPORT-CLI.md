# Daily report owner CLI (`report create`)

Status: implemented owner-required draft create. No local writer, timer,
custom template, revise, finalize, or send.

```
work-stack --data-dir <selected-dir> report create --date YYYY-MM-DD
```

`--date` is required and must be a canonical civil day. Template is fixed
`daily-v1`. There is no implicit today.

The command requires the running GUI owner. After the existing owner
preflight it GETs `/api/v1/reports/daily-preview` once for that day, then
POSTs `/api/v1/reports` with the six-field body admitted by
`normalize_report_request("create", body)`:

`workspace_uid`, `template`, `period`, `source_digest`, `source_generated_at`,
`markdown`.

`context_catalog` is not copied. The POST path uses the pinned workspace UID.
Each explicit invocation mints one `cli-report-` Idempotency-Key. Ambiguous
transport replays that identical path/body/key once. Create success pairing
is `201` with `meta.replayed=false`, and replay is `201` with
`meta.replayed=true`. Determinate HTTP refusals, including duplicate period
and source changed, stay nonzero.

Stdout is bounded summary JSON only: `uid`, `workspace_uid`, `template`,
`period`, `state`, `revision`, `source_digest`, `replayed`. A saved draft
still needs human review in the GUI.
