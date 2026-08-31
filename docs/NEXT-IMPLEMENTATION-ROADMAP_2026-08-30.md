# Work Stack 다음 구현 로드맵

Date: 2026-08-30

Status: `READY_FOR_DOGFOOD_FEEDBACK`

Product baseline: `codex/workstack-ui-actions-20260830` at
`9e69fa332d9a5a98244674549ee2f6eaaa9df917`

Baseline tree: `d9df13869cfe80e50de959955b2c99014b491e4c`

Latest strict CI: `33304961422` (`PASS`)

## 1. 목표

다음 단계의 목표는 기능 수를 늘리는 것이 아니라, 현재 제품을 실제 업무에 매일 사용할 수 있는
상태로 닫고 Work Stack의 가장 큰 차별점인 Outlook/Teams 맥락 수집과 명시적 회신 흐름을 실제
OOB 환경에서 여는 것이다.

우선순위는 다음 네 가지 기준으로 결정한다.

1. 사용자가 지금 수행하는 핵심 업무 흐름을 막는가.
2. 실제 Outlook/Teams 입력을 안전한 Task 근거로 바꾸는가.
3. 제품 설치·복구·업데이트를 비개발자 수준으로 낮추는가.
4. 이미 합의된 Conduit docking 경계를 보존하면서 향후 Taskroom 소비를 가능하게 하는가.

## 2. 현재 출발점

- Graph, Board, Treemap, Table, Focus, Inbox, Daily Review, Objective Hub가 하나의 planning SSOT를
  사용한다.
- Task 선택·해제, revision-guarded 편집, idempotent 생성, append-only planning status,
  relationship cycle 방지, unified search와 saved views가 구현되어 있다.
- Windows one-file setup, 검증된 백업·복원·이전, 유지관리 launcher가 구현되어 있다.
- Chromium 32개 제품/접근성 시나리오와 Firefox/WebKit 4개 호환성 시나리오가 원격 CI에서
  통과했다.
- Outlook/Teams OOB UI와 strict packet/reply contract는 fixture 기준 구현되어 있지만 실제
  provider Gate 0는 통과하지 않았다. 네 개 provider build flag는 모두 기본 `false`다.
- 사용자의 기존 OOB 인증 agent가 provider를 호출한다. Work Stack은 OAuth token을 받거나 저장하지
  않으며 Outlook/Teams 데스크톱 앱 설치도 요구하지 않는다.

## 3. 변경할 수 없는 경계

- Work Stack은 유일한 planning-state authority다.
- Conduit는 유일한 execution-state authority다.
- Work Stack은 명시적 disclosure 확인 뒤 한 Task의 한 revision을 canonical 단일 파일로만
  export한다. Preview, 취소, 거절, export는 planning state를 변경하지 않고 Conduit에 접속하지 않는다.
- Work Stack에 Conduit client, watcher, relay, cloud transport, back-sync, bulk import, agent start,
  room creation을 추가하지 않는다.
- Microsoft 호출은 기존 인증 OOB agent가 수행한다. Work Stack에 OAuth token placeholder나 provider
  credential 저장소를 만들지 않는다.
- 자동 전송, 자동 재시도, background polling, provider health 추정은 허용하지 않는다.

동결 좌표:

- Contract Revision 4:
  `cb0a41f250ad1a4bff287a8268cb49e805770cf94682647fbac0e89375102f70`
- Safety Policy Revision 5 root:
  `sha256:48dfd17ec86942e44692a26e7d43b88387c2c97f208c4d1081878a9318440148`
- Shared conformance-kit root:
  `sha256:a18627e56e49bf0f488a4e42962faeb559e8d9e99ec4c27e0b389c2a075ae2b9`

## 4. 실행 순서

### N0 — 실사용 결함 폐쇄

목적: 현재 `8771` 데모에서 사용자가 발견하는 마찰을 가장 먼저 제거한다.

구현 범위:

- 사용자 관찰을 `재현 경로 → 기대 결과 → 실제 결과 → 영향도`로 기록한다.
- 데이터 손상, 저장 실패, 잘못된 상태 표시, 죽은 버튼, 선택/해제 불일치, 키보드 접근 불가를
  P0/P1로 분류한다.
- 각 수정은 먼저 실패하는 unit 또는 browser regression을 만든 뒤 최소 변경으로 닫는다.
- Graph/Board/Treemap/Table의 공유 Task Drawer, Quick Add, status 변경/Undo, Objective 이동,
  Capture-to-Task를 핵심 수동 acceptance flow로 유지한다.
- 미관 개선은 핵심 흐름 결함과 같은 파일을 건드리지 않는 경우에만 병렬로 진행한다.

완료 gate:

- 보고된 P0가 0이고, 진행을 막는 P1에는 재현 test와 닫힌 결과가 있다.
- backend, frontend, build, source audit, Chromium, Firefox/WebKit 독립 CI step이 모두 통과한다.
- 새 기능으로 우회하지 않고 기존 planning semantics와 docking bytes를 보존한다.

Work packet: 사용자 피드백 한 건당 독립 RED test + bounded fix + evidence commit.

### N1 — Outlook read Gate 0

목적: 사용자가 가장 자주 쓰는 “메일 제목을 알려주고 내용을 가져와 정제된 action item을 만드는”
흐름을 첫 실제 provider 가치로 연다.

실행 범위:

- 기존 인증 OOB agent의 Outlook search/read capability와 반환 형태를 비민감 테스트 메일로 확인한다.
- stable message/thread/version reference, mailbox-move 제한, allowlisted Microsoft deep link를 기록한다.
- raw body, header, address, quoted reply, HTML, attachment, source canary가 Capture Packet, Work Stack
  store, log, browser storage, export에 들어오지 않는지 확인한다.
- adapter/model/prompt/redaction-policy version과 redacted tool-trace digest를 evidence에 남긴다.
- evidence가 통과한 정확한 build에서만 `VITE_WORKSTACK_OUTLOOK_READ_VERIFIED=true`로 만들고 Teams와
  두 reply flag는 계속 `false`로 둔다.

완료 gate:

- Release Checklist의 Outlook per-provider read gate가 비민감 evidence로 전부 충족된다.
- restart 뒤에도 Capture와 Task link가 유지된다.
- UI release label이 `Microsoft 365 read-only dogfood (Outlook)`보다 강한 주장을 하지 않는다.
- Gate 실패 시 제품 코드를 우회하지 않고 flag를 닫은 상태로 유지한다.

Work packet: provider capability probe, sanitized fixture/evidence, exact flagged build, regression gate의
네 묶음.

### N2 — Teams read Gate 0

목적: Teams chat/channel/thread 맥락을 Outlook과 같은 안전 수준으로 Task 근거로 사용할 수 있게 한다.

실행 범위:

- N1의 증거를 재사용하지 않고 Teams search/read capability를 독립 검증한다.
- stable chat/channel/thread/message path와 allowlisted deep link를 검증한다.
- Teams 특유의 mention, participant, quoted content, card/attachment가 sanitized projection 밖으로
  새지 않는지 별도 canary로 확인한다.
- 통과한 정확한 build에서만 `VITE_WORKSTACK_TEAMS_READ_VERIFIED=true`를 추가한다.

완료 gate:

- Teams read checklist가 독립 evidence로 통과하고 Outlook evidence와 섞이지 않는다.
- 두 provider Capture가 Inbox에서 구분되고 각각 Task 근거로 연결된다.
- reply control은 여전히 비활성이고 background sync/health 표현이 없다.

Work packet: N1과 같은 네 묶음이며 Outlook 변경과 별도 commit/evidence를 사용한다.

### N3 — Outlook 명시적 회신 dogfood

목적: 가장 가치가 큰 첫 출력 흐름을 한 provider에만 좁혀 실제로 검증한다.

실행 범위:

- linked Outlook Capture가 있는 Task에서만 ReplyCommand를 작성한다.
- target은 Capture의 immutable locator에서만 가져오고 브라우저에서 recipient/target을 바꿀 수 없게
  유지한다.
- 사용자가 exact body와 target을 보고 승인한 후 OOB agent가 canonical plain-text thread reply를
  한 번 호출한다.
- agent는 전송 전 body/target digest를 다시 계산하고 terminal ReplyReceipt를 반환한다.
- matching receipt만 Task Activity에 한 번 반영하며 unconfirmed result는 `unknown`으로 끝낸다.
- `unknown`을 자동 retry하거나 외부 exactly-once라고 주장하지 않는다.

완료 gate:

- Outlook read gate가 먼저 통과했다.
- release checklist의 Outlook reply gate가 전부 통과한다.
- mismatch, duplicate receipt, transport-loss/unknown 시나리오가 fail closed다.
- 정확한 artifact에서만 `VITE_WORKSTACK_OUTLOOK_REPLY_VERIFIED=true`이며 Teams reply는 `false`다.

Work packet: command preview, one approved tool call, terminal receipt, failure matrix, exact artifact evidence.

### N4 — Microsoft Activity & Attention 최소 화면

목적: 실제 read/reply dogfood에서 생긴 상태를 사용자가 놓치지 않게 하되, queue나 background worker를
만들지 않는다.

선행 조건: N1과 N3의 실제 evidence가 존재하고 사용자가 활동 이력을 찾기 어렵다는 dogfood 관찰이
최소 한 건 있어야 한다. 조건이 없으면 이 단계는 건너뛴다.

구현 범위:

- 기존 Capture, ReplyCommand, ReplyReceipt, Task Activity의 allowlisted projection만 읽는다.
- `검토 대기`, `승인 후 receipt 대기`, `unknown`, `terminal`을 명확히 구분한다.
- 항목을 누르면 원래 Task/Capture로 이동한다.
- provider polling, retry center, health dashboard, raw message preview는 만들지 않는다.

완료 gate:

- 화면 자체가 외부 호출이나 planning mutation을 하지 않는다.
- attention count와 detail이 같은 authoritative local facts에서 계산된다.
- raw source, target locator, recipient, body, credential이 aggregate/diagnostic에 노출되지 않는다.

Work packet: read model, read-only API projection, UI, privacy negatives, browser acceptance.

### N5 — Teams 명시적 회신 여부 결정

목적: Outlook reply dogfood 결과가 좋을 때만 두 번째 provider 출력을 연다.

진행 조건:

- N3에서 승인 UX, `unknown` 처리, receipt 복구가 실제 사용에 충분했다.
- Teams connector가 canonical chat/channel thread reply action을 제공한다.
- Teams reply가 Outlook과 다른 target semantics를 독립 fixture와 evidence로 표현할 수 있다.

조건을 만족하면 N3와 동일한 원칙으로 Teams reply gate를 수행한다. 만족하지 않으면 Teams는 read-only로
유지하며, 이는 제품 실패가 아니라 의도적인 release label이다.

### N6 — 설치 가능한 release candidate

목적: provider flag가 확정된 동일 commit을 사용자가 한 파일로 설치·업데이트할 수 있게 한다.

구현 범위:

- 최신 production frontend를 빌드하고 one-file setup과 SHA-256 sidecar를 다시 생성한다.
- 깨끗한 disposable Windows 사용자 경로에서 install, first launch, version 표시, upgrade,
  pre-upgrade backup, restore, relocation, uninstall-with-data-preserved를 검증한다.
- support summary에 release version, schema, provider flag matrix를 정확히 표시한다.
- code-signing certificate가 준비되면 publisher signing을 추가한다. 인증서가 없으면 unsigned임을 명시하고
  checksum을 publisher authentication으로 표현하지 않는다.

완료 gate:

- 설치 artifact hash와 source commit/tree, provider flags, 모든 verification 결과가 한 receipt에 묶인다.
- 기존 사용자 planning data를 fixture 검증에 사용하지 않는다.
- 설치 후 화면의 release label이 실제 통과한 Gate 0보다 강하지 않다.

Work packet: artifact build, clean install smoke, upgrade/recovery smoke, release evidence.

### N7 — Conduit consumer docking acceptance

목적: Work Stack 변경 없이 frozen snapshot을 Conduit가 수동 import하고 Taskroom 후보를 만드는 실제
cross-product acceptance를 끝낸다.

소유권:

- Work Stack: 이미 구현된 canonical single-task export와 non-mutation evidence만 제공한다.
- Conduit: import, display/review/storage, execution confirmation, orchestration template 선택, Taskroom
  생성과 실행을 소유한다.

완료 gate:

- 동일 Task revision export가 동일 bytes를 생성한다.
- export 전후 Work Stack planning store가 byte-identical하다.
- Conduit가 imported description을 자동 agent prompt로 실행하지 않고 사용자 confirmation을 요구한다.
- Work Stack에 transport, watcher, link table, back-sync를 추가하지 않는다.

Work packet: Conduit-owned importer packet, cross-product fixture run, bilateral acceptance receipt.

## 5. 실사용 중 인터럽트 규칙

사용자 테스트 결과는 다음 순서로 현재 단계보다 우선한다.

1. P0 — 데이터 손실·잘못된 외부 전송·보안/개인정보·planning authority 위반: 해당 lane 즉시 중지.
2. P1 — 핵심 흐름을 완료할 수 없음, 저장 결과 오표시, 죽은 주요 action: 다음 feature 전에 수정.
3. P2 — 우회 가능한 UX 마찰: 같은 surface의 다음 bounded packet에 묶는다.
4. P3 — 미관·선호도: dogfood에서 반복될 때만 승격한다.

리뷰는 blocker 0을 목표로 하지 않는다. P0와 normative docking 위반만 즉시 정지 조건이고, P1/P2는
사용자 가치와 회수 가능한 기술 부채를 비교해 bounded follow-up으로 등록한다.

## 6. 병렬화 원칙

- 사용자 dogfood triage와 Microsoft capability evidence 수집은 병렬로 진행할 수 있다.
- Outlook과 Teams evidence는 병렬 수집할 수 있지만 release flag 활성화와 reply gate는 N1 → N2 → N3
  순서로 닫는다.
- frontend UI와 backend contract는 먼저 실패하는 공유 fixture를 고정한 뒤 서로 다른 task-scoped
  branch/worktree에서 구현한다.
- installer artifact는 provider flag와 release commit이 확정되기 전에 만들지 않는다.
- 같은 persistence/schema 파일을 여러 worker가 동시에 수정하지 않는다.
- 모든 merge 후보는 독립 CI와 diff/privacy audit를 거친다.

## 7. 증거가 생길 때까지 예약하지 않는 항목

다음 항목은 현재 구현 순서에 넣지 않는다.

- Work Stack 서버가 Microsoft를 직접 호출하는 continuous sync
- polling, background queue/worker, retry daemon, integration health 추정
- Activepieces embedding
- SQLite/Postgres migration
- persisted FTS 또는 server-side saved-filter sharing
- real-time multi-user editing
- 자동 priority 변경, 개인화 Focus scoring, calendar auto-scheduling
- Work Stack 내부 Conduit client, Taskroom creation 또는 execution back-sync

Persisted FTS는 10,000-Task process-local 검색의 cold-start 또는 memory 비용이 실사용 문제로 측정될
때만 다시 검토한다. 데이터베이스 전환은 JSON planning SSOT의 durability/scale가 실제 blocker라는
증거가 있을 때 별도 migration contract로 다룬다.

## 8. 다음 행동

1. 사용자는 현재 `http://127.0.0.1:8771/`의 synthetic workspace에서 핵심 흐름을 직접 시험한다.
2. 관찰된 P0/P1은 N0 work packet으로 즉시 변환한다.
3. 동시에 기존 인증 OOB agent에서 Outlook read capability와 비민감 Gate 0 fixture를 준비한다.
4. N0가 green이면 N1을 닫고 Outlook read-only artifact를 만든다.
5. N2와 N3를 차례로 통과한 뒤에만 N4/N5의 실제 필요성을 판단한다.
6. provider flag가 확정된 commit으로 N6 설치 artifact를 한 번 빌드한다.
7. Work Stack release candidate가 안정된 뒤 N7 Conduit consumer acceptance로 넘어간다.

현재 `8771` localhost 서버와 synthetic runtime은 사용 편의를 위한 임시 dogfood 환경이며 release
evidence나 설치 artifact가 아니다.
