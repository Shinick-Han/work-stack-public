# Reviewed-query bridge

One seam joining the released chat transport of `CLIENT.md` to the frozen
manual-import composer of `MANUAL-IMPORT.md`. It is a **bridge and nothing
else**: not an automated Adapter, not a runtime, not a claim loop, not a
credential loader, not a CLI, not a submitter and not source verification.

Owned files: `manual_query.py`, this `MANUAL-QUERY.md`. There is no
`__init__.py` here. `od_client.py`, `retrieval_mapper*.py` and
`manual_import.py` are read-only from this lane and are called unedited.

## Settled call

```python
from integrations.opendocuments.manual_query import run_reviewed_query

result = run_reviewed_query(
    reviewed_query,
    config=trusted_backend_config,
    request_id=ledger_request_id,
    item_id=caller_item_id,
    result_limit=limit,
    source_catalog=operator_catalog,
)
```

| Result | Shape |
| --- | --- |
| composed | `{"ok": True, "outcome": "composed", "envelope": {...}}` |
| refused | `{"ok": False, "error": {"code": ..., "message": ...}}` |

`envelope` is the released `workstack.knowledge-import.v1` document that
`build_manual_import` returned, unchanged. Nothing is sent to Work Stack: the
owner still carries it by hand into
`POST /api/v1/knowledge/captures/import` under the browser session they
already have.

## Who may call this

**A trusted internal caller only.** The contract this function is written
against is:

- a human operator has **already reviewed** `query`; this module does not
  review, rewrite, classify or filter it,
- `config` is an operator-pinned `TrustedBackendConfig` — origin, API key,
  workspace and the corpus-only profile — read from operator state,
- `source_catalog` is the operator's own trusted document map. **A catalog can
  never be built from a chat body.** Admission of evidence comes only from this
  map, which is why it is a required argument with no default.

There is **no default config, no environment variable, no secret file, no
endpoint override and no CLI** in this lane. The transport's own origin policy
is the only route policy, and it refuses an origin that carries a path, query,
fragment or userinfo, so no caller-supplied endpoint can be reached.

Exposing `run_reviewed_query` on a network port would publish an
unauthenticated OpenDocuments query relay. **This function is not a wire
boundary and implies no public authentication.**

## What it does not assert

It does **not** assert that `request_id` names an owner-issued knowledge
request, that the request is still open, or that the caller is authorised to
complete it. No owner check happens here and none is duplicated here: those
questions belong to the released importer, which answers them inside its own
transaction when the envelope is submitted. A composed envelope is a proposal,
not an authorisation.

Nothing is minted and nothing is persisted. `item_id` is the caller's, exactly
as `MANUAL-IMPORT.md` settled it; no identifier, timestamp, version or origin
is allocated here.

## One invocation, at most one POST

One call issues **at most one** upstream `POST /api/v1/chat` and never retries
it. The body is the transport's own pinned `{query, profile, workspaceId}` and
nothing else; the path is the transport's own `/api/v1/chat`.

A repeated invocation issues a further POST — that is the caller's repetition,
not a retry added here. **No across-restart exactly-once guarantee exists**:
this module holds no state, no ledger and no idempotency key.

When the transport cannot tell whether the upstream saw the request, it answers
`outcome_unknown`, and that object is returned **unchanged**. It is never
retried, re-composed, reclassified or swallowed by a broad `except`.

## Order of admission

1. `canonical_uuid` on `request_id` and `item_id`, then `admit_result_limit`
   and `admit_catalog` — the composer's and mapper's **existing** helpers, run
   before any socket is opened. A malformed structural input costs **zero**
   upstream requests.
2. `post_opendocuments_chat(query, config)` — the transport owns the query
   bound and the trusted-config binding. Neither policy is restated here.
3. `build_manual_import(body, ...)` over the transport's parsed body — the
   mapper owns catalog admission, the caps, the byte bounds and the
   `answer_scope`; the composer owns the title, the summary and the context.

No title, path, evidence or display-text policy is duplicated in this module.

## Refusal codes

| Stage | Codes | Message |
| --- | --- | --- |
| transport | the transport's own `ERROR_CODES`, including `outcome_unknown` | the transport's own, returned unchanged |
| compose | the mapper's / composer's own closed code, e.g. `invalid_uuid`, `invalid_result_limit`, `invalid_catalog`, `invalid_chat`, `invalid_query_id`, `invalid_confidence`, `no_admitted_evidence`, `mixed_evidence_not_representable`, `extension_too_large`, `import_too_large` | the constant `the retrieval could not be composed` |

The compose message is a constant so that no chat body, response byte, API key,
config value, title or identifier can ride out on it.

**The query is never returned and never recorded.** It reaches the transport
and nothing else: it is not copied into the envelope, into a refusal, or into
any object this module returns, and this module writes no log.

## Imports

`manual_query.py` imports only its adapter siblings (`manual_import`,
`od_client`, `retrieval_mapper_fields`), `workstack.knowledge_request` for
`canonical_uuid`, and the standard library. That is the existing
`py_opendocuments_adapter` layer allowance; no store, task, packet, credential
or process surface is reachable from here.
