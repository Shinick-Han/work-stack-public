# Source Capture Integrity Milestone — 2026-08-31

## Outcome

The Microsoft source-to-Task path is now retry-safe, explicit about its destination, and fail-closed when the reviewed source changes. The work was split across independent backend, frontend, desktop-shell, and adversarial-test packets, then integrated and verified as one milestone.

## Product behavior

- A new source dialog owns one canonical UUID `intent_id` for its lifetime.
- The frontend sends that same intent in both the request body and the idempotency key. A lost response can therefore be retried without creating another Task.
- Reusing an intent with changed Task fields returns a conflict instead of silently returning or creating the wrong Task.
- Distinct intents may still create distinct Tasks from the same Capture. Work Stack does not guess that two separately requested actions mean the same work.
- Creating a Task from a Capture links the matching unlinked action item atomically when exactly one action matches the reviewed title, detail, priority, and due date. This removes the second creation path from that action button.
- Re-ingesting an exact source packet remains duplicate-safe. Reusing the same source fingerprint with changed reviewed content fails closed as `source_revision_conflict`.
- The dialog separates source title from Task title and supports both destinations: create a new Task or attach the reviewed source to an existing Task.
- Existing Tasks can be found by Task ID or title.
- Provider is inferred from the active Microsoft source and is displayed read-only rather than as a user-selectable radio group.
- Source URLs are retained only when they match the inferred provider, use HTTPS, contain no credential-like query or fragment parameters, and use an exact supported host. OneNote support is deliberately limited to `www.onenote.com`.
- Provenance remains `manual_web_capture`; this milestone does not label browser content as OOB verified.

## Desktop bridge structure

The Python webview shell now separates host-message parsing and dispatch from capture orchestration. Pure parsers cover Work Stack and Microsoft-source messages, while bounded helpers own clipboard, Outlook visible-selection, seed construction, and dispatch.

Measured structural changes:

- backend `create_task_from_capture`: CCN 21 → 6
- desktop `_on_workstack_message`: CCN 27 → 5
- desktop `_send_source_capture`: CCN 22 → 6
- `SourceCaptureDialog`: decomposed into bounded components; the production quality gate reports no max-lines violation
- production quality gate: 96 files passed

## Verification receipt

- Python unit suite: 348 passed, 1 environment-dependent skip
- Frontend unit suite: 60 files, 269 tests passed
- Vite production build: 968 modules transformed
- Export/privacy audit: 393 UTF-8 source-policy files passed
- Production structure gate: 96 files passed
- Adversarial source-capture packet: 5 Python and 4 TypeScript scenarios included in the full suites
- `git diff --check`: passed

The first fully parallel frontend run had one timeout while the production build and Python suite competed for the same host. The affected App test passed alone (19/19) and the full frontend suite passed on the immediate non-contended rerun (269/269); no product or test expectation was weakened.

## Click-by-click user guide

1. Open **Context Inbox** and select Outlook, Teams, or OneNote in the embedded source surface.
2. Select the useful source text, then choose the source-review action in Work Stack.
3. Confirm the inferred provider, source title, reviewed action detail, and captured item URL.
4. Choose **Create a new Task** or **Attach to existing Task**.
5. For a new Task, edit the execution-oriented Task title, objective, priority, and due date. For an attachment, search by Task ID or title and choose the intended Task.
6. Submit once. If the connection fails after submission, retry from the still-open dialog; its stable intent prevents a second Task.
7. Open the resulting Task and verify the source context. A matching action item will already point to that Task rather than offering another creation path.

## Explicit nonclaims and remaining debt

- This does not add a new Microsoft extraction mechanism. Outlook visible capture and Teams/OneNote clipboard paths keep their existing capabilities and limitations.
- It does not make Microsoft content OOB verified, monitor hidden page content, recipients, or attachments, or send source material to Conduit.
- It does not perform semantic deduplication when no explicit `intent_id` is supplied.
- It does not guess which action to link when zero or multiple actions match.
- It does not permit arbitrary `*.onenote.com` hosts.
- It does not implement field-level merge or revision-aware resolution for simultaneous local and remote SSOT edits. That remains the next data-integrity milestone.
