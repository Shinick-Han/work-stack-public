# OpenDocuments Adapter chat client

This directory holds transport for the **separate Adapter process**, not Work
Stack core. The source-verifier lane owns other files in this folder. This
file documents `od_client.py` only.

The module is imported by explicit file path. There is no package
`__init__.py` here and no Work Stack core import.

Frozen upstream inspected for this lane:
`C:/ws-orca/workstack-pilot/.artifacts/opendocuments-review-20260908/upstream`
at `f3aba15f0161f1730e746de47a2ffd1e53cbea44`
(`packages/server/src/http/routes/chat.ts`,
`packages/server/src/http/middleware/auth.ts`,
`packages/server/src/http/workspace.ts`,
`packages/client/src/index.ts`,
`packages/core/src/rag/profiles.ts`).

## Trust boundary

`post_opendocuments_chat(query, config)` is the only public call.

- **Caller-supplied input** is the query string alone.
- **Trusted operator configuration** pins base origin, API key, upstream
  workspace id, corpus-only profile, and the overall timeout. The function
  accepts no request-provided URL, route, workspace, profile, conversation,
  or collection override.
- Distinct access domains require distinct trusted workspace-bound keys and
  client instances. This client does not fake collection filtering:
  `POST /api/v1/chat` has no collection field, and this transport will not
  emit one.
- Key-to-workspace binding and whether the pinned profile stayed corpus-only
  are **operator configuration**. A chat JSON body cannot prove them. This
  client does not treat `response.profile`, `route`, or returned sources as
  proof of key binding or of web-search being off.

## Wire contract

One request, never retried:

- Method and path: `POST /api/v1/chat` only.
- Header: `X-API-Key` from trusted config. No `Cookie`, no ambient
  `Authorization`, no document/admin/conversation/collection/stream calls.
- JSON body keys, exactly: `query`, `profile`, `workspaceId`.
- `profile` is always the pinned corpus-only value. On frozen upstream
  `f3aba15f`, that value is `fast` (`webSearch: false` in
  `packages/core/src/rag/profiles.ts`). `balanced` uses web fallback and
  `precise` enables web search; both are refused as config.
- `workspaceId` is the pinned trusted workspace. The caller cannot override
  it per query.

Upstream team mode authenticates with `X-API-Key` (or a session cookie this
client never sends). Workspace resolution prefers the API key record's
workspace over a body `workspaceId`. Sending both the pinned key and the
pinned workspace is still operator configuration, not a proof.

## Origin policy (HTTPS default)

- `https` origins are allowed with certificate verification
  (`ssl.create_default_context()`, hostname checks on).
- `http` is allowed only when the host is the **literal** loopback address
  `127.0.0.1` or `::1` (not `localhost`, not other `127.0.0.0/8` hosts).
- Userinfo, query, fragment, non-root path, and params are refused.
- Redirects (`300`–`399`) are refused. The client uses `http.client` and
  does not follow `Location`. The API key is not forwarded to a second hop.
- Transport is `HTTPConnection` / `HTTPSConnection` to the parsed host and
  port. It does not use `urllib.request`, does not read `HTTP_PROXY` /
  `HTTPS_PROXY` / `ALL_PROXY`, and does not forward ambient proxy
  credentials.
- API keys are not read from the environment. Real credentials are never
  loaded by this module.

`TrustedBackendConfig` redacts `api_key` in `repr` and `str`. Error objects
use a closed `{code, message}` pair and do not include the raw HTTP body,
query text, response chunk, URL path, or API key.

## Bounds

| Limit | Value |
|---|---|
| Query | 1..1000 characters, trimmed, no C0 controls |
| Origin / API key / workspace id | bounded config strings (see module constants) |
| Timeout | one overall monotonic deadline for connect, request send, status/header reads, and body reads |
| Response body | 1 MiB transport cap. This is **not** a Capture cap |
| JSON | no NaN/Infinity, no duplicate keys, depth ≤ 32; parser/walker RecursionError is closed `malformed_response` |
| `sources` | at most 100 entries when the field is present |

Timeouts and ambiguous connection loss after the origin TCP/TLS handshake
has completed return `outcome_unknown`. This includes
`HTTPConnection.request` raising `BrokenPipeError` (or another `OSError`)
after some POST bytes may have reached the peer, even when that call never
returns. A refusal that happens before `connect()` succeeds remains
`origin_unreachable`. This client does **not** retry. A later owner may
query or ask a human rather than duplicate generation.

The deadline is not an inactivity timer on a single buffered read. After
`connect()`, the connected socket is wrapped so each `recv` waits at most a
short slice (or the remaining deadline, whichever is smaller), then Python
rechecks the overall deadline. Status/header reads (`getresponse`) and body
reads share that path because both makefile the same socket. A peer that
sends another byte before each inactivity timeout therefore cannot stretch
header or body reading past the deadline. Slice `recv` retries are the same
read, not POST retries. There is no background reader thread. A `recv`
already inside the kernel is bounded by that slice, not by a watcher.

## Ephemeral upstream payload

A successful call returns:

```text
{"ok": True, "outcome": "received", "response": EphemeralChatResponse(...)}
```

`EphemeralChatResponse.take_for_mapper()` detaches the parsed JSON **once**
for a trusted in-process mapper. `repr` / `str` are redacted. The object is
not JSON-serializable, so accidental persistence as JSON fails closed.

This is **not** a promise that Python memory can be securely erased.

This transport does not print or write the payload. It does not construct a
Capture, does not assert title or source freshness, and does not implement
the Adapter/corpus ledger.

## Upstream query retention

Frozen upstream `POST /api/v1/chat` calls `persistQueryLog` on every
successful non-stream query (`packages/server/src/http/routes/chat.ts`).
That helper swallows database errors, so this client cannot prove whether a
log row was stored.

**This client cannot claim end-to-end no-retention.** Upstream may retain
queries. Query-log policy is outside this module. The adapter does not send
a suppression flag (none exists on the frozen route).

## Error codes

Closed `error.code` values: `invalid_query`, `invalid_config`,
`origin_refused`, `origin_unreachable`, `redirect_refused`,
`response_too_large`, `malformed_response`, `auth_refused`,
`upstream_refused`, `outcome_unknown`.

## Signatures

```python
@dataclass(frozen=True)
class TrustedBackendConfig:
    origin: str
    api_key: str
    workspace_id: str
    profile: str
    timeout_seconds: float

class EphemeralChatResponse:
    def take_for_mapper(self) -> dict: ...

def post_opendocuments_chat(
    query: str,
    config: TrustedBackendConfig | Mapping[str, Any],
) -> dict:
    ...
```
