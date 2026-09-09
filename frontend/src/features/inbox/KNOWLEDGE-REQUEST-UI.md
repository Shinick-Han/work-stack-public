# Knowledge request editor

`KnowledgeRequestDialog` lets a person write one scoped knowledge request, read exactly
what would be sent, ask the server to issue it, and copy the request the server issued.
`knowledgeRequestDraft.ts` holds the arithmetic: what a usable draft is, whether an issued
document answers it, and when that document's window has closed.

## Status: callable, not yet wired

Nothing in the product renders this dialog. That is deliberate, not an oversight: the two
things a parent needs do not exist yet.

1. **An issuing API.** There is no route that mints a `KnowledgeRequest v1`.
   `contracts/knowledge-request-v1.md` is a validation primitive; nothing in Work Stack
   constructs, stores or serves a request. The issuer also owns the `request_id` ledger
   described in that contract's caller obligations. This dialog deliberately ships with no
   fetch of its own rather than a placeholder one.
2. **A nonsecret corpus registry.** `corpusOptions` must come from a registry of granted
   aliases. That registry is a prerequisite for the first out-of-band flow and does not
   exist yet.

Until both land, the dialog is exercised only by its tests.

## Props

| Prop | Type | Notes |
| --- | --- | --- |
| `open` | `boolean` | Controlled by the parent, like every other dialog here. |
| `workspaceUid` | `string` | The **active** workspace, from the parent's own trusted state. |
| `task` | `KnowledgeRequestTaskRef \| null` | The open Task at the revision the parent read. `null` is a valid workspace-only request. |
| `corpusOptions` | `readonly KnowledgeCorpusOption[]` | `{ alias, label, description? }`. `alias` is the nonsecret registry label that goes on the wire; `label` is display copy the server authored. |
| `onIssue` | `(draft) => Promise<unknown>` | Hands the reviewed draft to the parent, resolves with whatever the issuer returned. |
| `onClose` | `() => void` | Close request. The dialog never closes itself. |
| `now` | `() => number` | Optional injectable clock in epoch ms. Defaults to `Date.now`. |
| `copyText` | `(value, isCurrent) => Promise<void>` | Optional clipboard adapter. Defaults to `utils/clipboard`. |

### `onIssue` is a callback, not an HTTP schema

The `KnowledgeRequestDraft` argument is the part of a request a person actually reviewed:

```ts
{ binding, purpose, query, corpus_refs, result_limit }
```

It has no `schema`, no `request_id`, no `requested_at` and no `expires_at`, because this
screen must never mint identity or authority — a locally invented document would look
exactly like an issued one to a reader. The parent owns the transport and may shape the
body however the future API requires; the draft is not that body.

The resolved value is treated as untrusted. `validateIssuedKnowledgeRequest` checks it is
a closed, well-formed `workstack.knowledge-request.v1` document **and** that it answers
the submitted binding, query, corpora, purpose and limit. Anything else is a refusal with
authored copy; no returned value is ever quoted into a diagnostic.

## What a parent still has to supply

- The issuing call itself, plus its ledger, replay refusal and CSRF-protected session.
  The issuer transport is being defined in its own lane; its request body carries the
  draft's fields alongside transport and authority values — an intent id and a connection
  alias — that this screen has no business choosing. That is one more reason the draft is
  not the body: the parent completes it from state this dialog cannot see.
- `corpusOptions` from the granted-alias registry, refreshed when a grant changes.
- `task` at the revision it was actually read under, or `null`. Never a partial trio.
- A place to open the dialog from, and the decision about who may open it.

## Boundaries this component keeps

- No fetch, route, endpoint, provider branch or connector name.
- No minted `request_id`, timestamp, window or schema string.
- No Task detail, note or context is attached. A Task **title** may prefill the editable
  query, which the user then reads and can rewrite.
- No URL, path, command or source opener is built from anything on screen, and nothing
  supplied is rendered as HTML.
- The receipt is on screen and nowhere else: no `localStorage`, no address bar, no
  console, no analytics.
- Expiry is read from the issuer's own `expires_at` against the injected clock. It is
  never extended. A lapsed request needs a fresh one the user explicitly asks for.
  The visible Active/Expired state is scheduled against the remaining lifetime and
  rechecked at the deadline, on visibility/focus resume, and if Copy discovers the
  window has closed. JavaScript timers are not hard realtime while a tab is suspended;
  resume rechecks immediately rather than relying on a fixed one-second poll.
- Query length is Unicode code points, matching Python `len` on the knowledge-request
  contract. The textarea clips whole code points rather than UTF-16 units, so a
  600-character astral query is in bounds and a 1001st code point is refused.
- Returned `requested_at` / `expires_at` values are checked with the same strict
  RFC3339 calendar and offset grammar as `workstack.capture.parse_rfc3339`. Impossible
  days are refused; allowed fractional digits are compared exactly, so a 1 ns window
  is not collapsed by millisecond rounding.

## Invalidation

Every guard reads the current values at the moment it runs. A change to the workspace,
the Task identity or revision, the question, the scope selection, the purpose or the limit
retires the in-flight issue and clears any receipt, so a reply that arrives late is dropped
rather than shown against a question nobody asked. The same guard runs again inside the
clipboard adapter, so a copy cancels at each point it still can.

The editor stays editable while a request is in flight — an edit is a different question,
so it retires the flight instead of being blocked by it. Only the Generate action is
blocked while one is pending, which is what prevents a double submission.

## Tests

- `knowledgeRequestDraft.test.ts` — draft refusals, identity keying, issued-envelope shape
  and correspondence, expiry, receipt serialisation, Unicode query bounds, calendar and
  sub-millisecond returned windows.
- `knowledgeRequestTime.test.ts` — strict RFC3339 calendar dates, leap/non-leap February,
  offsets and exact fractional comparison.
- `KnowledgeRequestDialog.test.tsx` — successful issue and copy, refusals before issue,
  the no-corpus explanation, issuer rejection, malformed and mismatched responses,
  clipboard failure and retry, expiry including an off-tick 100 ms window, Copy and
  visibility resume rechecks, timer cleanup, late replies after a Task or question
  change, double submission, unmount during flight, a withdrawn corpus selection, and
  an astral query near the 1000-character bound.
