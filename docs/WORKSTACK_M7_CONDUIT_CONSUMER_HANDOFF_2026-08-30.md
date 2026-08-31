# Work Stack M7 → Conduit consumer handoff

Date: 2026-08-30
Status: `WORKSTACK_PRODUCER_READY / CONDUIT_CONSUMER_NOT_IMPLEMENTED_HERE`

## Producer coordinate

- repository: `https://github.com/Shinick-Han/work-stack`
- branch: `codex/workstack-ui-actions-20260830`
- product commit: `1dad3bc63e97acc0281444a96533af87f2cb6220`
- product tree: `9d1782ca172f4fd0cbd942effe56e78afd3fba4b`

## Frozen inputs

- Contract Revision 4 SHA-256:
  `cb0a41f250ad1a4bff287a8268cb49e805770cf94682647fbac0e89375102f70`
- Safety Policy Revision 5 root:
  `sha256:48dfd17ec86942e44692a26e7d43b88387c2c97f208c4d1081878a9318440148`
- Conformance-kit root:
  `sha256:a18627e56e49bf0f488a4e42962faeb559e8d9e99ec4c27e0b389c2a075ae2b9`

The consumer must use these exact bytes. Any normative mismatch stops the docking lane
and returns to bilateral amendment; it is not repaired by normalizing or widening input.

## Exact synthetic producer artifact

- path relative to the reviewed Work Stack worktree:
  `.artifacts/m7-handoff-20260830-123512/0c273163-439f-5c62-8d86-01b009d71805.workstack-task.json`
- bytes: 503
- SHA-256:
  `350c752338852485dd78beffee25dc635dfcbc93b51eb3fe546e3f5d07cc309f`
- contract digest label:
  `sha256:350c752338852485dd78beffee25dc635dfcbc93b51eb3fe546e3f5d07cc309f`
- planning revision: 0
- final byte: LF (`0x0A`)

This is disposable synthetic evidence, not a committed repository fixture and not user
planning data. A second independently delivered file was byte-identical. All ten Work
Stack store files, including the empty product lock, retained the same SHA-256 before and
after both exports.

## Required consumer work packets

1. **Pure ingestion gate** — read exact selected bytes, enforce the frozen manifest,
   Unicode 17 behavior, safety policy, canonical reserialization, and digest. No Core
   mutation, ticket, provider action, prompt construction, or Taskroom action occurs.
2. **Review ticket** — store one-use bounded review state, display exact imported title
   and description plus classification/omissions, and let cancellation delete or expire
   only the ticket. Imported description remains display/review/storage content.
3. **Explicit confirmation** — one confirmation owns one trusted attempt identity and at
   most one atomic Core Task creation attempt. Snapshot digest and `origin_ref` are not
   idempotency keys.
4. **Ambiguous recovery** — converge only through the attempt-keyed Core query and exact
   identity/intent-owned field match. Never infer commit from UI state and never resubmit
   automatically.
5. **Taskroom proposal** — after confirmed Core creation, propose the appropriate
   Taskroom/orchestration setup in Conduit-owned state. Import itself must not start a
   provider, process, session, Seat, Run, room, or agent.
6. **Cross-product acceptance** — perform the frozen P4 matrix in both supported Conduit
   windows: success, cancellation, refusal, malformed/unsafe/stale input, duplicate
   origin, response loss, restart, and absence of Work Stack mutation/back-sync.

## Authority and nonclaims

- Work Stack remains the sole planning-state authority.
- Platform Core remains the sole shared execution Task-state writer.
- Conduit owns execution state, review tickets, confirmations, recovery, and Taskrooms.
- The transport is an explicit user-carried file. There is no Work Stack Conduit client,
  watcher, loopback transport, relay, cloud sync, back-sync, or bulk import.
- Export does not prove Conduit ingestion, Core mutation, Taskroom creation, execution, or
  provider readiness.
- No push or release was performed.
