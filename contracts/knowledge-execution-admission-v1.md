# Knowledge execution admission v1

Status: **trusted internal read-only composition.** `workstack/knowledge_execution_admission.py`
decides whether one already-issued KnowledgeRequest is still a current owner
authorization. It is not an HTTP route, not a token issuer, not an instance
guard, not a child transport and not an executor. No existing issuer, ledger,
store, schema, server, route or frontend module is edited by this slice.

**Caller / trust boundary.** The caller is an in-process owner composition that
already holds Store access. It must not pass a caller-constructed authority or
Task. It holds one outer reentrant `Store.transaction` across admission and a
later guard consume, **releases before child I/O**, then calls admission **again**
before returning a proposal. A passing snapshot never guarantees the policy,
Task or window still hold. No bearer is added; nothing here returns a token.

## Frozen API

`admit_execution_request(store, request_document, *, now: str) -> AdmittedExecution`

| Field | Source |
| --- | --- |
| `document` | closed projection from `validate_knowledge_request` (copied, `repr=False`) |
| `request_digest` | stored ledger digest after `compare_digest` |
| `connection_alias` | stored ledger record |
| `policy_revision` | stored ledger record |
| `upstream_workspace_uid` | current roster row for that alias |

`now` is the caller's clock. The function reads `workspace.json`, `knowledge.json`
and, when the stored binding names a Task, `backlog.json`. It never saves, issues,
claims, guards, opens a network or reads a wall clock. It never calls
`plan_request_issue` (that planner would accept an unissued id as a new grant).

## Sequence

1. Dict-shape guard, then `canonical_uuid` on `request_id`.
2. Under `store.transaction`, load and `validate_knowledge_document`.
3. Locate the record or `KnowledgeLedgerError unknown_request`.
4. `state` must be `pending` or `request_not_pending`.
5. `request_authority_is_current` or `policy_revision_changed` (policy replace or
   revoked connection).
6. Active Task from the **ledger** binding's `task_id` via `_active_task_from_store`.
7. `owner_request_authority` from the record's connection alias, actual workspace/Task,
   and `now`; then `validate_knowledge_request`. Wire type/grammar/`unknown_field`
   refusals propagate from that validator. No query, alias or path is echoed.
8. `request_digest(projection)` vs stored digest; mismatch is `request_digest_mismatch`.

## Refusals (before any executor)

`unknown_request`, `request_not_pending`, `policy_revision_changed`,
`request_digest_mismatch`, `unknown_task`, and unchanged
`KnowledgeRequestError` codes (`request_expired`, `workspace_mismatch`,
`task_binding_mismatch`, `unknown_field`, …). Canonical projection (sorted
keys, trimmed query) is digested, not raw JSON spelling.

This slice does not execute a provider, persist a claim, or change CSRF,
thresholds or schema 6 / `knowledge.json` v1.
