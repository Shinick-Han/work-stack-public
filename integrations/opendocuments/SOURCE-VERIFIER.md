# OpenDocuments source verifier (pilot)

This file documents `source_verifier_config.py`, `source_verifier_main.py`,
`source_verifier_notion.py` and the source-checkout entry
`source_verifier_entry.py`. It is a **source-checkout pilot**, not an installed
connector or opener. The existing driver (`DRIVER.md`) and `source_access` stay
unchanged.

The Notion half added in R24 is an **access** observation. It says whether this
verifier can reach a mapped page right now; it never says the captured text is
still correct. `GET /v1/pages/{id}` returns page *properties*, not page content,
and a page timestamp says nothing about the blocks or child pages under it -- so
a Notion entry never returns `current` or `stale`, whatever its
`expected_source_version` is and whatever `last_edited_time` comes back. Live
provider acceptance is still outstanding evidence: nothing in this pilot has
talked to Notion.

`source_verifier_entry.py` is trusted checkout bootstrap:
`[absolute_python, absolute_source_verifier_entry.py]`. A future packaged
adapter is a different artifact.

## Shape

```
owner process
  └─ child: python <abs>/integrations/opendocuments/source_verifier_entry.py
       ├─ puts checkout root first on sys.path (from __file__)
       ├─ reads od-verifier.json   (WORKSTACK_OD_VERIFIER_CONFIG only)
       ├─ admits stdin with workstack.knowledge_verification_protocol
       ├─ nas.file:    source_access.verify_source (indexed_digest=None)
       ├─ notion.page: source_verifier_notion, GET https://api.notion.com/v1/pages/<id>
       └─ admits stdout with the same protocol
```

There is no source open, no crawl, no indexing, no body persistence and no Task
or Store access. The only outbound call this child can make is the pinned
Notion page GET above, and only for a document that is mapped, in a granted
corpus, not revoked and carrying an extractable page id.

`checked_at` is read **after** the observation and is also the instant the
result is validated at, so a Notion batch that overran the request's 60 s
window is refused on the way out instead of being backdated.

## Operator file: `workstack.opendocuments-verifier.v1`

Exact keys: `schema`, `connection_alias`, `upstream_workspace_uid`,
`corpus_grants`, `mappings`. Bounded at 64 KiB. Path is explicit and absolute
through **only** `WORKSTACK_OD_VERIFIER_CONFIG`. A document carrying an
`api_key` or any other extra field is refused.

Each mapping is exactly one of two field sets, never a blend:

| backend | exact fields |
| --- | --- |
| NAS | `document_ref`, `corpus`, `allowed_root`, `relative_location`, `revoked` |
| Notion | `document_ref`, `corpus`, `page_url`, `revoked` |

The sets are compared for equality, so a mapping carrying both `allowed_root`
and `page_url`, or either shape plus an unknown key, is `invalid_mappings`. The
loader constructs the released `SourceMapping` for the matched backend -- NAS
with `allowed_root=Path(...)` and that type's safe defaults, Notion with
`page_url=...` -- so the *released* host allow-list (HTTPS, `notion.so`,
`www.notion.so` or an exact `.notion.site` suffix, no userinfo, port, query or
fragment) is the only rule about which URLs exist. At most 200 mappings;
`document_ref` is unique; `corpus` must be one of `corpus_grants`;
`allowed_root` is an absolute path; `page_url` is at most 1024 characters.

A `page_url` that passes the allow-list but names no page (a workspace index,
say) still loads: that one document observes `refused` / `source_refused`, and
every other mapping in the file keeps working.

The file may contain paths; they never leave the adapter on stderr, stdout,
`str`/`repr` of a refusal, or a public observation.

## Notion token file

`WORKSTACK_OD_NOTION_TOKEN_FILE` is **optional** and is read only by this
external child, never by Work Stack. It is an absolute path to a file holding
one bounded printable ASCII token (8-512 characters after trimming surrounding
whitespace, at most 4096 bytes on disk). It is opened only after the requested
document is mapped, in a granted corpus, not revoked, on the Notion backend and
carrying an extractable page id -- so a revoked or out-of-corpus document is
answered without the secret ever being read.

- unset or empty -> `unverifiable` / `no_origin_verifier`
- set but relative, absent, unreadable, empty, oversize or malformed ->
  `unverifiable` / `verification_unavailable`

The token, its path, and any page title, property or text never appear on
stdout, stderr, a `repr`, an exception or an observation. No real token is
needed to develop or test this adapter, and none is authorised.

## Where the operator sets these variables

Both verifier variables belong in the **nested** `verification.environment` of
the alias' `workstack.knowledge-drivers.v1` entry -- *not* in the entry's
top-level `environment`, which is the search driver's own child and has a
different allowed set. The registry never rewrites, inherits or reuses the
search stanza for verification, and `environment` is the whole environment each
child gets: nothing is merged in from the server process or from `os.environ`.

```jsonc
{"schema": "workstack.knowledge-drivers.v1", "drivers": [{
  "alias": "team-nas",
  "upstream_workspace_uid": "<canonical uuid>",
  "command": ["C:\\Python\\python.exe", "C:\\...\\opendocuments\\driver_entry.py"],
  "environment": {
    "WORKSTACK_OD_DRIVER_CONFIG": "C:\\...\\od-driver.json",
    "SystemRoot": "C:\\Windows",
    "PATH": "C:\\Windows\\System32"
  },
  "verification": {
    "command": ["C:\\Python\\python.exe", "C:\\...\\opendocuments\\source_verifier_entry.py"],
    "environment": {
      "WORKSTACK_OD_VERIFIER_CONFIG": "C:\\...\\od-verifier.json",
      "WORKSTACK_OD_NOTION_TOKEN_FILE": "C:\\...\\notion-token.txt",
      "SystemRoot": "C:\\Windows",
      "PATH": "C:\\Windows\\System32"
    }
  }
}]}
```

`WORKSTACK_OD_NOTION_TOKEN_FILE` is optional: omit the name entirely and every
Notion mapping observes `unverifiable` / `no_origin_verifier`, which is the
supported state, not a broken one. Put the *path* here, never the token text.
On Windows `SystemRoot`/`WINDIR` and `PATH` must be stated in the verification
stanza too, because TLS initialisation needs them and nothing is inherited.

## Notion request, pinned

| what | value |
| --- | --- |
| origin | `https://api.notion.com` port 443, TLS verified |
| path | `/v1/pages/<canonical page uuid>` |
| header | `Notion-Version: 2025-09-03` |
| body read | at most 64 KiB |
| redirects | never followed; a 3xx is a refusal |

The operator's `page_url` supplies **only** the terminal page UUID: a
hyphenated UUID, a bare 32-hex segment, or a slug ending in 32 hex characters.
Nothing else from it reaches the wire, and there is no environment variable,
config field or request field that can change the origin. Rate is at most one
Notion request per 350 ms, at most 10 entries per request under the protocol
bound, a 30 s wall budget for the whole batch and a per-call timeout of 5 s or
whatever is left of that budget, whichever is smaller. There is no automatic
retry: a later explicit source check is a new observation.

## Request pin

After protocol A admits the stdin document:

- connection `alias` and `upstream_workspace_uid` must equal this file
- request `corpus_refs` must be a **subset** of `corpus_grants` (not a silent
  widening)
- each matched mapping must belong to the request corpora **before** any read

`nas.file` evidence is answered by `verify_source`; `notion.page` evidence is
answered by `source_verifier_notion`. Evidence whose mapping is filed under the
*other* backend is `refused` / `source_refused` rather than answered from the
wrong place. Any other `source_type` stays `unverifiable` /
`unsupported_source_type` and calls neither.

## Coarse public codes

`source_access` keeps a richer vocabulary. This child maps it onto the shared
protocol pairs only:

| observation | status / code |
| --- | --- |
| hashed bytes equal the expected version | `current` / `hash_matched` |
| hashed bytes differ | `stale` / `hash_differs` |
| live root, no such file | `missing` / `file_absent` |
| root itself did not answer | `unavailable` / `root_unavailable` |
| OS denied the read | `denied` / `access_denied` |
| policy / mapping / identity refused | `refused` / `source_refused` |
| owner withdrew the mapping | `revoked` / `mapping_revoked` |
| no expected version | `unverifiable` / `no_expected_version` |
| Notion with no verifier | `unverifiable` / `no_origin_verifier` |
| neither `nas.file` nor `notion.page` | `unverifiable` / `unsupported_source_type` |
| the filesystem question could not be answered | `unverifiable` / `verification_unavailable` |

## Notion observation matrix

Every row keeps the request's `expected_source_version` exactly and returns
`observed_source_version=null`. `current`, `stale`, `hash_matched` and
`hash_differs` are unreachable for `notion.page` in this slice.

| observation | status / code |
| --- | --- |
| operator mapping revoked | `revoked` / `mapping_revoked` (no token, no network) |
| unmapped, filed under NAS, out-of-request corpus, or no page id in `page_url` | `refused` / `source_refused` (no token, no network) |
| token file not configured | `unverifiable` / `no_origin_verifier` |
| token file unreadable or malformed | `unverifiable` / `verification_unavailable` |
| HTTP 200, `object: page`, id matches, not archived or trashed, expected null | `unverifiable` / `no_expected_version` |
| the same readable page with any non-null expected version | `unverifiable` / `verification_unavailable` |
| HTTP 200 but archived or `in_trash`; HTTP 404; a redirect; any other 4xx | `refused` / `source_refused` |
| HTTP 401 or 403 | `denied` / `access_denied` |
| HTTP 429 or 5xx; network, TLS or timeout failure; exhausted batch budget | `unavailable` / `root_unavailable` |
| HTTP 200 that is invalid, oversize, truncated, not a page, or a different id | `unverifiable` / `verification_unavailable` |

A Notion 404 is never `missing` / `file_absent`: it means "absent **or**
invisible to this integration", and reporting a deletion that may not have
happened sends a human the wrong way. It is never `mapping_revoked` either --
revocation is the operator's word, not the origin's.

Only `object`, `id` and the boolean `archived` / `in_trash` flags are read, and
the flags only when present. Unrelated evolving Notion fields are ignored rather
than required, so the check does not start failing for a reason unrelated to
access.

`current` and `stale` carry a non-null `observed_source_version`. Every other
status returns `observed_source_version=null`, including `no_expected_version`
(a hash with nothing to compare against is not currentness). An
`indexed_digest` is never supplied.

The protocol version grammar is the existing opaque retrieval handle: it cannot
contain `:`. The only NAS byte-hash spelling that may appear on the verifier
wire is `sha256-<64 lowercase hex>`. This adapter translates **only** that
exact form to the released helper's internal `sha256:<hex>` and translates an
observed hash back. Null stays null. Any other opaque expected version has no
NAS byte-hash meaning and is `unverifiable` / `verification_unavailable`, never
`stale`. A stored capture whose origin version is null stays
`no_expected_version` unless the helper itself reports missing, unavailable,
denied or revoked.

## CLI contract

One stdin document, one result. No argv. Empty stdout on any refusal; one
closed stderr code (`verifier_input_refused`, `verifier_config_refused`,
`verifier_policy_refused`, `verifier_output_refused`); exit `1`; no traceback
on those paths. Absolute entry bootstrap prevents cwd-shadow imports.

The parent's existing 45 s child bound is unchanged and still bounds the whole
run, including a Notion batch's own 30 s budget. Its practical limit is worth
stating: it is an *outer* safety net around a process, so it cannot tell a slow
observation from a hung one, and it says nothing about whether an answer that
did arrive arrived in time. The batch's own bookkeeping is what does that. A
socket timeout is likewise per connect and per read, not end to end, so a
trickling response can satisfy every individual read and still outlast its
deadline; the batch re-reads the clock after each call and discards an answer
that came back late as `unavailable` / `root_unavailable`. No async framework
was added for this -- it is one clock comparison in
`NotionBatch.fetch_page`.

Protocol A is a coordinator-reviewed dependency. This adapter does not ship a
second validator and does not treat a skip or stub as acceptance.
