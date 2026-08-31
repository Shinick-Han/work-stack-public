# Work Stack Microsoft OOB Native Integration Plan

Date: 2026-08-30

Status: `DRAFT_FOR_SCOPE_APPROVAL`

Product baseline: `codex/workstack-ui-actions-20260830` at
`9e69fa332d9a5a98244674549ee2f6eaaa9df917`

Planning-doc baseline: local branch at `a1d899d0545cc4330f281d7bde8db848fc9dfee6`

## 1. 결정 요약

목표는 기존의 `요청 복사 → agent 실행 → JSON 가져오기` handoff를 넘어, 사용자가 Work Stack
화면 안에서 Outlook mail과 Teams message를 검색·열람하고 Task의 근거로 연결하며, 명시적으로
승인한 작성 동작도 실행할 수 있게 하는 것이다.

권장 구조는 다음과 같다.

```text
Work Stack browser (loopback)
  -> Work Stack Microsoft API (same-origin + CSRF)
  -> local Microsoft Capability Broker
  -> authenticated OOB capability
  -> Outlook / Teams
```

OOB token은 Microsoft Graph token이라고 가정하지 않는다. 첫 Gate에서 token이 의미하는 실제
endpoint, callable surface, expiry/renewal, tenant binding, scopes/capabilities를 측정한다. Work Stack은
token을 source repository, `.env`, JSON store, backup, log, browser storage에 저장하지 않는다. Windows
Credential Manager 또는 OOB broker가 token을 소유하고 Work Stack은 credential reference와 실제로
검증한 capability projection만 유지한다.

이 계획의 v1에서 “실제 기능”은 Work Stack의 업무관리 목적에 직접 필요한 다음 범위다.

- Outlook Mail: mailbox/folder 탐색, 검색, message/thread 열람, attachment metadata와 명시적 download,
  draft, 새 메일, reply, reply-all, forward, send, read/unread, flag, category, move/archive, delete.
- Teams Messaging: team/channel/chat 탐색, message/thread 검색·열람, reply, 새 chat/channel message,
  own-message edit/delete, reaction, mention, linked file metadata와 명시적 download.
- 공통: 선택한 원문을 sanitized Capture로 만들기, Task 생성/연결 근거로 사용하기, Task별 source watch,
  attention/activity, deep link.

Outlook Calendar/Contacts, Teams meeting/call, tenant administration, Planner, SharePoint file editing은
별도 product surface와 별도 permission model이므로 이 v1에 넣지 않는다. 사용자가 “Outlook 전체”에
Calendar까지 포함하기를 원하면 Mail v1 뒤의 독립 확장 lane으로 추가한다.

## 2. feasibility 판정

현재 판정은 `CONDITIONALLY FEASIBLE`이다.

현재 Work Stack 코드에는 fixture-backed OobRequest, Capture Packet, ReplyCommand, ReplyReceipt와
provider build gates가 이미 있다. 그러나 현재 Codex task의 callable inventory에는 Outlook/Teams
search/read/write tool이 없고, 공개 OpenAI 문서에서도 이 환경의 “OOB token”을 application-callable
API로 정의한 계약을 확인하지 못했다. 따라서 token만으로 direct integration이 가능하다고 먼저
주장할 수 없다.

Gate 0 결과는 세 가지 중 하나여야 한다.

1. `DIRECT_BROKER`: token과 공식 endpoint/SDK로 로컬 프로세스가 OOB capability를 호출할 수 있다.
2. `SESSION_BRIDGE`: token은 agent-session bound지만 지원되는 local CLI/IPC bridge가 존재한다.
3. `NO_CALLABLE_PATH`: token은 특정 host session 안에서만 유효하고 외부 호출 계약이 없다.

`DIRECT_BROKER`와 `SESSION_BRIDGE`만 native integration으로 진행한다. `NO_CALLABLE_PATH`이면 token만으로
Work Stack 내부 UX를 완성하는 것은 불가능하므로, 지원되는 local broker가 제공될 때까지 기존
copy/import handoff를 유지한다. DOM automation, Teams/Outlook desktop automation, token reverse
engineering으로 우회하지 않는다.

## 3. Microsoft 공식 API가 주는 설계 제약

- Outlook mail API는 적절한 delegated/application permission으로 draft, read, reply, forward, send,
  update, delete를 지원한다. Reply는 `Mail.Send` permission을 사용한다.
- Teams chat/channel send는 정상 사용자 동작에 delegated permission이 필요하다. Application permission
  send는 migration 용도로 제한된다.
- Teams는 chat 목록, chat message, channel message, channel reply가 서로 다른 resource path와 permission을
  가진다. 하나의 `teams.readwrite=true` boolean으로 정직하게 표현할 수 없다.
- Microsoft change notification은 subscription endpoint, expiry renewal, lifecycle notification, 경우에 따라
  public HTTPS webhook과 application permission을 요구한다. OOB token 하나만으로 background push sync가
  자동으로 생기지 않는다.

따라서 구현은 provider별 boolean 대신 operation-level capability matrix를 사용하고, 처음에는 on-demand
read/write를 완성한 뒤 background update는 별도 gate로 연다.

Primary references:

- Microsoft Graph Outlook mail overview:
  <https://learn.microsoft.com/en-us/graph/api/resources/mail-api-overview?view=graph-rest-1.0>
- Outlook reply:
  <https://learn.microsoft.com/en-us/graph/api/message-reply?view=graph-rest-1.0>
- Microsoft Graph permissions reference:
  <https://learn.microsoft.com/en-us/graph/permissions-reference>
- Teams chat/channel message send:
  <https://learn.microsoft.com/en-us/graph/api/chatmessage-post?view=graph-rest-1.0>
- Teams chat messages:
  <https://learn.microsoft.com/en-us/graph/api/chat-list-messages?view=graph-rest-1.0>
- Teams channel messages:
  <https://learn.microsoft.com/en-us/graph/api/channel-list-messages?view=graph-rest-1.0>
- Teams message change notifications:
  <https://learn.microsoft.com/en-us/graph/teams-changenotifications-chatmessage>

## 4. 사용자 경험 목표

### 4.1 Microsoft Center

Work Stack sidebar에 `Microsoft` surface를 추가한다.

- 연결 setup과 factual capability 상태
- Outlook Inbox/Search/Folders
- Teams Chats/Teams/Channels/Search
- 연결된 source와 Task
- draft/approved/unknown/failed external action
- source watch에서 새로 들어온 attention

`Connected`는 capability probe가 방금 성공하고 token expiry가 확인된 경우에만 표시한다. 그 외에는
`Expired`, `Insufficient permission`, `Unavailable`, `Unknown`처럼 관찰된 상태를 표시한다. Build flag는
release evidence gate로 유지하되 runtime health로 사용하지 않는다.

### 4.2 원문 보기와 Task 근거 만들기

- 메시지 목록은 subject/title, sender display name, timestamp, unread/flag, bounded preview만 가져온다.
- message body/thread는 사용자가 항목을 열 때 fetch한다.
- 원문 viewer는 `Cache-Control: no-store`이며 memory TTL과 item/byte limit를 가진다.
- 원문 HTML은 sanitize 후 isolated renderer에서 표시한다. embedded instruction은 data일 뿐 agent/tool
  instruction이 될 수 없다.
- `Attach to Task` 또는 `Create Task`를 누를 때만 기존 Capture Packet sanitizer가 persistent projection을
  만든다.
- Task에는 stable opaque locator, version/fingerprint, sanitized summary/context/action item, deep link만
  남긴다. raw body, headers, recipient list, quoted replies, HTML, attachment bytes는 planning store에 넣지
  않는다.

### 4.3 외부 write

모든 외부 write는 Work Stack planning mutation과 분리한다.

- reversible triage action: read/unread, flag, category, reaction
- content write: draft/update draft, send, reply, reply-all, forward, new Teams message, edit own message
- destructive write: move/archive, delete mail, delete own Teams message

content/destructive write는 exact target, rendered content, attachment list, participant/recipient set, operation을
확인하는 approval dialog를 통과한다. Token permission prompt가 추가로 나타나면 그 prompt도 보존한다.
Transport loss 뒤 성공 여부가 불명확하면 `unknown`이며 자동 retry하지 않는다. Provider가 stable remote
request identifier로 안전한 reconciliation을 제공한다고 Gate에서 입증할 때만 `Check outcome`을 제공한다.

## 5. 네 개의 데이터 구역

### Zone A — credential

- Owner: Windows Credential Manager 또는 OOB broker.
- Contains: opaque OOB token/refresh material.
- Forbidden: repository, `.env`, command line, browser storage, Work Stack JSON, backup, diagnostics, support
  summary, logs.
- Work Stack stores only a non-secret credential reference and tenant/account fingerprint.

### Zone B — ephemeral source

- Owner: in-memory broker cache.
- Contains: raw mail/chat body and remote response required for the open source viewer.
- Bounds: short TTL, LRU item/byte cap, no disk cache, no search index, no backup, no support export.
- Cleared on disconnect, token change, process exit, or explicit `Clear source cache`.

### Zone C — planning evidence

- Owner: Work Stack.
- Contains: existing strict sanitized Capture Packet, stable opaque locator, provenance, Task links.
- Searchable and backed up, because raw/token/recipient/attachment fields have already been excluded.
- Remains subordinate evidence for planning; Outlook/Teams remain source authority.

### Zone D — external intent and receipt

- Owner: Work Stack integration state, not planning state.
- Contains: user-authored outgoing content, immutable operation/target digest, approval time, idempotency key,
  minimal result/receipt.
- Does not contain OOB token, raw inbound content, connector dumps, or hidden recipient expansion.
- Does not cause automatic Task status, priority, due date, Objective, KR, or docking mutation.

## 6. capability contract

The broker returns factual, operation-level capabilities rather than a generic healthy flag.

```text
connection
  provider_family: microsoft
  transport: direct_broker | session_bridge
  tenant_fingerprint
  account_fingerprint
  token_expires_at?
  checked_at

capabilities
  outlook.mail.list
  outlook.mail.search
  outlook.mail.read
  outlook.mail.attachment.read
  outlook.mail.draft
  outlook.mail.send
  outlook.mail.reply
  outlook.mail.reply_all
  outlook.mail.forward
  outlook.mail.mark_read
  outlook.mail.flag
  outlook.mail.category
  outlook.mail.move
  outlook.mail.delete
  teams.chat.list
  teams.chat.message.read
  teams.chat.message.send
  teams.chat.message.reply
  teams.chat.message.edit_own
  teams.chat.message.delete_own
  teams.chat.message.react
  teams.team.list
  teams.channel.list
  teams.channel.message.read
  teams.channel.message.send
  teams.channel.message.reply
  teams.file.read
```

Each capability records `available`, `unavailable`, or `unknown`, plus bounded reason code and observation time.
The UI hides or disables only the exact unsupported operation. One provider result never enables another operation.

## 7. local broker boundary

Define a narrow `MicrosoftCapabilityBroker` interface before provider implementation.

```text
probe() -> connection + capabilities
list(resource, cursor, fields, limit) -> bounded metadata page
search(resource, query, cursor, fields, limit) -> bounded metadata page
get(reference, fields) -> source item/thread
execute(approved_action) -> minimal receipt | unknown
clear_ephemeral_cache()
disconnect()
```

Rules:

- The Work Stack browser never calls Microsoft or an OOB endpoint directly.
- Browser requests use the existing loopback Host/Origin/CSRF boundary.
- The local server allowlists every operation and outbound host/IPC endpoint.
- The browser cannot supply arbitrary URL, Graph path, tool name, connection ID, tenant, target, recipient, or
  capability string.
- Pagination cursors are opaque, bounded, connection-bound, and never placed in Task/Capture state.
- Broker response schemas reject unknown fields and strip connector diagnostics before returning to the browser.
- Access logs record correlation ID, operation, provider, duration, result class, and bounded reason code only.
- Token rotation/disconnect invalidates every in-memory cursor and raw-source cache entry.

If Gate 0 exposes only a supported local CLI/IPC agent, implement the same broker interface over that transport.
Do not let UI or domain code depend on transport choice.

## 8. API and persistence outline

Proposed loopback APIs:

```text
POST /api/v1/integrations/microsoft/connect
POST /api/v1/integrations/microsoft/disconnect
GET  /api/v1/integrations/microsoft/status
GET  /api/v1/integrations/microsoft/outlook/messages
POST /api/v1/integrations/microsoft/outlook/search
GET  /api/v1/integrations/microsoft/outlook/messages/{opaque_ref}
GET  /api/v1/integrations/microsoft/teams/navigation
POST /api/v1/integrations/microsoft/teams/search
GET  /api/v1/integrations/microsoft/teams/messages/{opaque_ref}
POST /api/v1/integrations/microsoft/captures
POST /api/v1/integrations/microsoft/actions/preview
POST /api/v1/integrations/microsoft/actions/{intent_id}/approve
POST /api/v1/integrations/microsoft/actions/{intent_id}/execute
POST /api/v1/integrations/microsoft/actions/{intent_id}/check-outcome
```

`connect` is loopback-only and hands an entered token directly to the secure credential provider. Request/response
logging and crash dumps must redact the field by construction. A safer native setup prompt may replace this route if
the installed launcher can write Credential Manager without passing the token through browser JavaScript.

New local integration stores, if required:

- `microsoft-connection.json`: non-secret credential reference, tenant/account fingerprints, last factual capability
  projection.
- `microsoft-actions.json`: append-only intent/approval/receipt projection with bounded user-authored outgoing text.
- `microsoft-watches.json`: explicit Task-source watch definitions and non-content cursors after the watch gate opens.

These stores are not part of Work Stack → Conduit export and cannot mutate Task planning fields implicitly. Backup
policy must declare whether outgoing drafts/actions are included; credential and Zone B data are always excluded.

## 9. 실행 파동

### G0 — OOB token/callable-path spike

No product code is implemented in this gate.

1. Obtain the OOB token through a secure local prompt; never paste it into chat, source, shell history, or a task file.
2. Identify the authoritative endpoint/CLI/IPC contract and vendor/version.
3. Probe identity, expiry/renewal, tenant binding, scopes/tool inventory, rate limits, pagination, error shapes, and
   user approval behavior with non-sensitive data.
4. Prove one Outlook list/search/read and one Teams chat/channel list/read without persisting source content.
5. Do not perform any write.
6. Produce a redacted capability receipt and choose exactly one outcome: `DIRECT_BROKER`, `SESSION_BRIDGE`, or
   `NO_CALLABLE_PATH`.

Gate: native implementation starts only with a supported callable contract and reproducible non-sensitive read.

### W1 — secure broker and connection UX

- Implement broker interface, Windows credential provider, token redaction, disconnect/rotation, factual capability
  probe, correlation IDs, and fail-closed schemas.
- Add Microsoft Center setup/status UI.
- Preserve existing manual OOB handoff as fallback behind an explicit `Manual handoff` action.

Gate: token never appears in repo/runtime stores/logs/backups/browser storage/support summary; expiry and insufficient
permission are distinguished; restart reconnects through credential reference without exposing token.

### W2 — native read surfaces

Parallel lanes after W1 contract freeze:

- Outlook lane: folders, bounded list/search, message/thread fetch, attachment metadata/download.
- Teams lane: chats, teams, channels, bounded search/history, message/reply thread, linked file metadata/download.
- UI lane: Microsoft Center navigation, virtualized lists, loading/empty/error/offline states, deep links.
- Safety lane: HTML sanitizer, ephemeral cache, raw-content leak scans, prompt-injection fixtures.

Gate: real non-sensitive Outlook and Teams records can be found and viewed entirely inside Work Stack, while a full
disk/runtime/browser audit proves that Zone B source content is not persisted.

### W3 — source-to-Task integration

- `Create Task from source`, `Attach to existing Task`, and sanitized Capture preview.
- Keep existing Capture fingerprint/version/stale-update/idempotency semantics.
- Show source title/provider/time/deep link and sanitized context in Task Drawer.
- Let the user refresh one source explicitly and review the sanitized diff before updating Capture.

Gate: source basis survives restart; raw body/recipients/attachments do not; refresh cannot silently change planning
status or overwrite a newer Capture projection.

### W4 — reversible external actions

- Outlook read/unread, flag, category.
- Teams reaction where capability exists.
- Explicit action preview and operation-level capability checks.
- Append-only action receipt; local optimistic UI rolls back on failure.

Gate: exact replay does not duplicate local intent; remote state is reread after response loss; unsupported operations
never appear active.

### W5 — content writes

Provider sub-gates close independently in this order:

1. Outlook reply to existing message.
2. Teams reply to existing chat/channel message.
3. Outlook draft and new send.
4. Outlook reply-all and forward.
5. Teams new chat/channel message.
6. Own-message edit/delete, mail move/archive/delete.

Every operation has operation-specific target schema and approval copy. Arbitrary Graph URL/tool-name execution is
never accepted. Attachments require an explicit file picker, size/type audit, exact manifest preview, and a separate
approval gate; they may be deferred without blocking text actions.

Gate: one non-sensitive real action per enabled operation returns a minimal receipt, mismatch/expiry/transport-loss
tests fail closed, and `unknown` never causes automatic resend.

### W6 — Task source watch and attention

Start with local pull while Work Stack is running.

- User explicitly watches a linked thread/message/query for a specific Task.
- Broker polls with bounded interval, cursor, backoff, and rate-limit handling only while the local app is running.
- New source revisions create attention/evidence facts, not Task status/priority/due-date mutations.
- User reviews a sanitized diff and chooses whether to update Capture or create an action item.
- Add integration Activity & Attention based on real facts.

Cloud change notifications are a separate optional design because Microsoft documents webhook, subscription renewal,
lifecycle, and permission requirements beyond a token-only local app. Do not add a cloud relay merely to claim sync.

Gate: restart/replay is idempotent, deletion/edited-message cases are explicit, rate limit does not lose the last safe
cursor, and no raw source enters the watch store.

### W7 — recovery, release, and installer

- Include non-secret connection/action/watch stores in backup semantics; always exclude credentials and raw cache.
- Add integration reset that disconnects, clears cache/cursors, and preserves planning evidence unless explicitly
  removed.
- Build the exact provider-capability artifact, one-file installer, sidecar, clean install/upgrade/restore evidence.
- Update release label from manual handoff only for capabilities that passed real operation-level gates.

Gate: install, token setup, read, source-to-Task, approved write, restart, backup/restore, disconnect, and uninstall are
reproducible without secret or raw-content leakage.

## 10. 병렬 구현 지도

After G0 and the broker contract freeze, use disjoint task-scoped branches/worktrees.

| Lane | Owns | Starts after | Merge gate |
| --- | --- | --- | --- |
| Broker | transport, credential provider, capability probe | G0 | contract fixtures + leak negatives |
| Outlook | mail adapter and operation schemas | broker contract | real read before any write |
| Teams | chat/channel adapter and operation schemas | broker contract | real read before any write |
| Backend | loopback APIs, intent/receipt stores, journal recovery | broker contract | idempotency + restart |
| Frontend | Microsoft Center, source viewer, action approvals | API fixtures | Chromium + Firefox/WebKit |
| Safety | sanitizer, cache, redaction, source/export audit | G0 | independent adversarial review |
| Release | installer, backup/restore, capability receipt | W5/W6 freeze | exact artifact verification |

Do not parallelize edits to the store manifest/journal or shared API schemas without a single owning lane. Real
provider writes remain serial and user-approved even when implementation work is parallel.

## 11. test and evidence matrix

### Contract/fixture

- Opaque cursor/reference tampering, unknown field, oversized response, wrong tenant/account, expired connection.
- Raw header, recipient, HTML, quote, attachment, token, connector dump, malicious source instruction.
- Per-operation capability unavailable/unknown/available.
- Exact action replay, conflicting idempotency, changed target/body/attachment manifest.

### Persistence/recovery

- Credential never enters any tracked/runtime/backup file.
- Zone B cache is absent after process exit and never indexed.
- Journal interruption replays action intent/receipt once.
- Restore onto a clean install reconnects only after credential setup and does not restore a secret.

### Real Gate 0/operation evidence

- Use only synthetic or explicitly non-sensitive tenant records.
- Record tool/endpoint version, capability, stable identifier shape, permission outcome, redacted request/response
  digest, and observed retention boundary.
- Exercise reads first, then one approved write at a time.
- Do not reuse Outlook evidence for Teams, chat evidence for channel, or reply evidence for new send.

### Browser/release

- Loading, empty, expired, insufficient-permission, rate-limit, offline, transport-loss, and unknown-result UI.
- Keyboard/focus, 200% reflow, forced colors, and three-browser bounded flows.
- Source selection and Task selection must remain independent and understandable.
- Release UI names exact enabled capabilities rather than generic `Microsoft connected`.

## 12. stop conditions and acceptable partial releases

Stop only the affected lane for:

- unsupported or undocumented token invocation;
- credential/raw-content leakage;
- target/recipient substitution;
- unapproved external write;
- ambiguous write that auto-retries;
- planning mutation caused by integration refresh;
- frozen Work Stack → Conduit contract drift.

Acceptable releases:

- Outlook read-only with Teams disabled.
- Outlook + Teams read-only.
- Outlook full mail actions plus Teams read-only.
- Text-only writes with attachment operations disabled.
- On-demand access without background watch.

These are honest capability releases, not failures. The UI must name the exact available operations.

## 13. deferred until evidence

- Public webhook/cloud relay for Microsoft change notifications.
- Tenant-wide application permissions or admin-wide message ingestion.
- Automatic send/retry/reply.
- Automatic Task status, priority, due date, KR, or Objective updates from source messages.
- Raw mailbox/chat mirror, local full-text indexing of raw messages, or attachment archive.
- Calendar, contacts, meeting/call, Planner, SharePoint editing.
- Work Stack-side Conduit client, Taskroom creation, execution back-sync.

## 14. next decision and first executable packet

Before implementation, confirm whether “Outlook 전체” includes Calendar in the first release. Recommendation:
ship Mail + Teams Messaging first and add Calendar only after those paths are green.

The first executable packet is G0 only. It needs:

1. the OOB token entered through a secure local prompt, not pasted into chat;
2. the token issuer/product name and supported endpoint/CLI/IPC contract, if known;
3. one non-sensitive Outlook test mail;
4. one non-sensitive Teams chat message and one channel message;
5. permission to perform reads only during G0.

G0 performs no product implementation and no provider write. Its durable output is a redacted capability matrix and
an architecture verdict. The implementation plan is reviewed adversarially against that measured result before W1
starts. If the token has no supported application-callable path, the result must say so directly rather than building
a fake broker around an agent-only session.
