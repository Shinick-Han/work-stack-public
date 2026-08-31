# Work Stack 현재 구현 상태

기준 시점: **2026-08-30**
체크포인트: **Docking v1 WS1–WS4 + Daily Review + Objective/KR 편집 + local continuity UI 구현·검증 완료**
제품 단계: **실사용 가능한 local-first prototype, Microsoft 365 경로는 fixture-backed 수동 OOB handoff**

## 1. 한 줄 요약

Work Stack은 목표와 Task를 Graph·Board·Treemap·Table·Focus로 관리하고, 정제된 외부 맥락을
Context Inbox에서 Task의 근거로 연결하며, Task 생성·상태 변경·상세 편집을 실제로 수행할 수
있는 웹 제품 상태다. Outlook/Teams 입출력 계약과 UI는 구현되어 있지만 현재 production build의
실제 provider Gate 0는 비활성이고, Work Stack 자체가 Microsoft 365를 background sync하는
형태는 아니다.

## 2. 지금 사용할 수 있는 제품 기능

### Workspace

- 동일한 Task 모델을 **Graph / Board / Treemap / Table** 네 화면으로 탐색한다.
- Graph에는 objective, task, note와 alignment/dependency/parent/reference 연결선이 보인다.
- Graph의 Task·Objective node는 pointer뿐 아니라 명시적 포커스와 Enter/Space로 같은 Drawer·Hub를 연다.
  Note node는 실행 가능한 척하지 않고 정보로 남는다.
- status, priority, objective, 검색어를 URL 상태로 유지하며 deep link와 뒤로가기를 지원한다.
- 30건 synthetic demo fixture와 5개 objective를 안전하게 seed할 수 있다.
- 실제 빈 workspace는 filter-empty 문구 대신 Objective 정의와 첫 Task 생성으로 이어지는
  outcome-first 시작 화면을 제공한다.
- Task를 선택하면 공통 Task Drawer가 열리고 URL에 Task ID가 남는다.
- Graph/Board/Treemap에서 선택한 Task를 다시 누르면 선택과 회색 강조가 해제된다.
- Table에서 Task를 정렬·필터링하고 inline status를 변경하며, 같은 행을 다시 누르면 선택이 해제된다.
  정렬 필드·방향과 Comfortable/Compact 행 밀도는 strict local preference로 복구되고, 좁은 화면에서는
  ID·Context·Revision 같은 기술 열을 접어 핵심 계획 열을 먼저 보여준다.
- Board 카드와 Table 행의 Enter/Space는 해당 항목 자체에 포커스가 있을 때만 동작해, 내부 Objective·
  blocker·status control의 키 입력이 Task 선택까지 중복 실행되지 않는다.
- Board와 Table은 전체 Workspace를 기준으로 dependency readiness를 계산해 blocker를 표시하고,
  존재하는 prerequisite Task Drawer를 바로 연다. 표시는 advisory이며 명시적 상태 변경은 막지 않는다.
- `Ready to act`와 `Blocked work` readiness 필터로 활성 Task를 추리고, 선택은 URL deep link와
  local saved view v1에 함께 보존한다. 기존 saved view에는 `All readiness`가 안전하게 기본 적용된다.
- dependency readiness는 Workspace당 ID index를 한 번만 만들고 Board·Focus·Objective·필터가
  재사용한다. 10,000 Task synthetic readiness filter gate를 선형 범위로 통과했다.
- 활성 필터가 있으면 Workspace 요약은 전체 수를 반복하지 않고 `결과 수 of 전체 수`를 표시해
  Graph·Board·Treemap·Table에 실제로 투영된 범위를 즉시 확인할 수 있다.
- Workspace 핵심 지표는 전체 active Task 중 blocked 수를 바로 표시하고, 기존 P0 active 수는
  같은 카드의 보조 신호로 유지한다.
- subtask가 있는 Task는 Board 카드와 Table Steps 열에서 `완료/전체` 진행을 표시한다. 이 값은
  기존 planning subtask의 read projection이며 status를 자동 변경하지 않는다.
- Board와 Table의 Objective ID, Graph의 Objective 노드, Treemap의 Objective navigator는 같은
  Objective Hub로 바로 이동한다. 이 탐색은 Task 선택과 충돌하지 않고 planning state를 변경하지 않는다.
- Board와 Table의 active Task due는 Focus와 같은 local-calendar 계산으로 연체·오늘·7일 이내를
  상대 문구로 표시한다. 완료·드롭된 Task의 과거 날짜를 현재 연체로 오인하지 않는다.
- `Overdue`·`Due today`·`Due soon`·`No due date`로 active Task를 필터링하고 URL deep link 및
  bounded local saved view에 보존한다. 기존 saved view는 `All due timing`으로 안전하게 읽힌다.
- 현재 view와 검색·상태·우선순위·readiness·due timing·Objective 조합을 이름이 있는 최대 12개의
  local saved view로 보존한다. 적용한 view는 조건 변경분 갱신, 이름 변경, 삭제까지 같은 ID에서 수행한다.
- 활성 검색·상태·우선순위·readiness·Objective 칩을 각각 눌러 다른 조건은 유지한 채 하나만 해제한다.
- Objective와 Task에 연결되는 Graph note를 production UI에서 추가할 수 있다.

### Focus

- started, overdue, due today, due soon, P0/P1 근거를 조합해 오늘 볼 Task를 결정론적으로 고른다.
- 같은 Task에 여러 근거가 있어도 한 번만 표시하고 이유 badge를 함께 보여준다.
- focus candidate의 dependency가 아직 `done`이 아니거나 누락됐으면 실행 가능 항목 뒤에 배치하고
  blocker ID·제목을 표시하며 Start/Done을 잠근다. 이 판단은 planning state를 변경하지 않는다.
- 존재하는 blocker badge는 해당 선행 Task Drawer를 바로 열어, Focus를 벗어나지 않고 막힘의
  원인을 검토하고 작업할 수 있다.
- Workspace와 동일한 Task Drawer/deep link를 재사용한다.
- Focus 행에서 open Task를 **Start**, started Task를 **Mark done**으로 전환할 수 있다.
- inline 전환은 기존 revision guard를 재사용하며 최신 알림의 **Undo**는 이전 fact를 지우지 않고
  새 planning-status transition을 추가한다.

### Board와 Task 상태 변경

- `done`이 아닌 dependency는 카드와 표의 readiness에 보이며, 활성 필터 밖의 완료 Task도 전체
  Workspace 기준으로 올바르게 충족 처리한다.
- blocker ID는 해당 Task Drawer로 이동하고, 누락된 dependency ID는 거짓 navigation을 만들지 않는다.
- select와 drag가 하나의 Task 단위 mutation lock을 공유한다.
- revision을 포함한 optimistic update를 적용하고 409에는 authoritative Workspace를 다시 읽는다.
- 동시 변경에서 실패한 한 Task만 조건부 rollback하며 다른 성공 결과를 되감지 않는다.
- 전체 pending mutation이 끝날 때 한 번만 최종 Workspace resync를 수행한다.

### Task Drawer

- title, status, priority, due, parent, objectives, detail, tags, dependencies를 편집한다.
- 한 Task에는 PATCH를 하나만 전송하고 뒤의 변경은 field-level last-write-wins queue로 직렬화한다.
- 다음 PATCH는 직전 성공 revision을 사용하며 detail/Workspace cache를 revision 단조 증가로 유지한다.
- 409·네트워크 실패에는 미저장 의도를 보존하고 자동 재전송하지 않는다.
- 최신 서버값 위에서 사용자가 명시적으로 `Retry save`할 수 있다.
- 저장 실패나 비어 있는 제목이 남아 있으면 Drawer 닫기·관계 이동·Objective 이동·Export·Task actions를
  잠그고, `Retry save` 또는 `Discard unsaved changes`를 명시적으로 선택하게 한다.
- 같은 잠금은 다른 Task 선택과 SPA 뒤로가기를 현재 Task URL로 복원하며, 새로고침·탭 닫기는
  browser `beforeunload` 경계를 사용한다.
- linked Capture context와 Activity, 승인된 reply command/receipt 흐름을 표시한다.
- 상단의 Objective 배지를 누르면 현재 Task 선택과 Drawer를 정리하고 해당 Objective Hub로 이동한다.
- 실제 Workspace에 존재하는 parent·dependency·child·dependent는 관계 요약에서 양방향으로
  다른 Task Drawer를 바로 연다. 이 탐색은 planning state를 변경하지 않는다.
- parent 계층과 dependency 그래프는 여러 Task를 거쳐 자기 자신으로 돌아오는 순환 변경을
  서버 transaction 안에서 거부하고, UI는 거부된 draft를 명시적으로 폐기할 수 있다.
- Parent 선택기는 순환을 만들 descendant를 미리 제외하고, dependency는 Task 선택기와 제거 가능한
  관계 칩으로 편집한다. 순환 후보는 한 번의 선형 그래프 순회로 계산하며 10,000 Task gate를
  통과했다. 서버의 순환 검사는 최종 안전망으로 유지된다.
- 별도 Task actions에서 revision-guarded note와 subtask를 추가하고 subtask 상태를 바꾼다.
- Unicode 17과 frozen safety policy를 적용한 단일-revision planning snapshot을 명시적 확인 뒤
  파일로 export한다. Export는 Work Stack을 변경하거나 Conduit에 접속하지 않는다.

### Quick Add

- Workspace, Focus, Context Inbox의 `New task` 또는 command palette에서 빠르게 Task를 만든다.
- `POST /api/v1/tasks`와 한 intent에 고정된 Idempotency-Key를 사용한다.
- 생성 응답을 `Task` schema로 검증하고 성공 즉시 해당 Task Drawer를 연다.
- Workspace/Focus에서는 현재 surface를 유지하고, Inbox에서는 URL 규칙에 맞춰 Workspace로 이동한다.
- 생성 뒤 Workspace refresh는 background로 실행되어 이미 성공한 POST를 UI 실패로 되돌리지 않는다.
- 같은 tick의 중복 submit을 ref gate로 막고 pending 동안 모든 입력을 잠근다.
- submit 직후 pending 화면이 반영되기 전에도 같은 동기 gate가 입력 변경과 Cancel, Escape,
  backdrop, 닫기 버튼을 차단한다. 실패하면 원래 draft와 오류를 보존한 채 다시 편집할 수 있다.
- native Escape/cancel은 controlled Dialog 상태를 우회하지 못한다.
- 2xx 뒤 응답 schema만 깨진 경우는 일반 실패가 아니라 `commit unknown`으로 처리한다. 성공 알림이나
  Drawer를 만들지 않고 dialog를 닫은 뒤 Workspace에서 생성 여부를 확인하도록 경고한다.
- bounded strict local draft가 새로고침·닫기 뒤에도 계획 필드를 복구하며 성공 또는 명시적 Clear에서 삭제된다.
- 확인된 Task 생성 성공만 local draft를 비우며, Objective나 Graph note 생성은 보존 중인 Quick Add
  초안을 변경하지 않는다.

### Local continuity와 제한적 Undo

- 성공한 browser mutation은 내용 없는 version/source/nonce/time 신호만 다른 탭에 보내 active query를 갱신한다.
- 교차 탭 신호가 늦거나 실패해도 서버 revision 검사가 stale writer를 최종적으로 거부한다.
- Board/Table 상태 변경의 최신 알림 1건만 Undo할 수 있으며, 기존 fact를 지우지 않고 새 revision-guarded
  planning-status transition을 추가한다.
- **More workspace actions → Local continuity**에서 content-free 저장소 readiness, workspace UUID,
  파일 수와 총 크기를 확인하고 명시적 확인 뒤 전체 verified ZIP backup을 내려받는다.
- live backup은 서버가 보유한 writer lease 안에서 일관된 9개 store 파일과 SHA-256 manifest를 만들고,
  생성 전후 계획 store를 변경하지 않는다. Restore는 의도적으로 앱 종료 후 offline CLI에서만 수행한다.

### Command palette와 키보드 탐색

- `Ctrl/Cmd+K`에서 Task·Objective·note·sanitized Capture·minimal activity를 통합 검색하고,
  Graph/Board/Treemap/Table·Focus·Inbox 이동, Quick Add,
  sanitized Capture import를 실행한다.
- 검색 projection은 reply body/target, raw locator, recipient, credential field를 노출하지 않는다.
- search index는 process-local allowlisted projection만 보유하며 store generation이 증가할 때 폐기한다.
  10,000 Task synthetic gate에서 warm search 중앙값 약 5ms를 기록했고 Command palette DOM은
  초기 30개 이하, 검색 중 최대 50개 결과로 제한한다.
- `1/2/3/8`은 Graph/Board/Treemap/Table, `4/5/6/7`은 Focus/Inbox/Daily Review/Objective Hub,
  `J/K`는 현재 필터의 다음/이전 Task다.
- 입력 control이나 열린 dialog에서는 전역 단축키를 실행하지 않는다.

### Daily Review

- `6` 또는 sidebar에서 Daily Review를 열어 날짜별 check-in과 Task별 Done/Next/Blocker를 기록한다.
- versioned API의 모든 writer는 intent idempotency와 atomic replay record를 사용한다.
- 선택 날짜를 끝으로 하는 deterministic 7-day Task/Objective roll-up을 제공한다.
- review evidence는 Task revision/status를 자동 변경하지 않고 Conduit 실행 상태를 추정하지 않는다.

### Objective/KR Hub

- `7` 또는 sidebar에서 Objective Hub를 열어 목표, 평균 KR 진척도, 연결 Task를 함께 본다.
- Objective status와 KR progress/status를 명시적으로 변경하며 objective revision으로 stale write를 막는다.
- KR 생성은 intent idempotency와 restart-safe replay를 사용하고 변경은 append-only activity로 남는다.
- 기존 revision 없는 Objective는 read projection에서 revision 0으로 호환하고 첫 변경부터 단조 증가시킨다.
- Objective 제목·quarter와 KR 설명·target label을 progress/status와 함께 편집할 수 있다.
- 자유 텍스트 변경 내용은 activity에 복제하지 않고 변경 필드명과 revision만 기록한다.
- 연결 Task 영역의 `Create aligned task`에서 Quick Add를 열면 현재 Objective만 미리 선택한다.
  기존 제목·상세·우선순위·태그·기한 draft는 그대로 유지한다.
- Objective별 연결 Task를 Actionable·Blocked·Done·Dropped로 나누고, 막힌 Task 카드에는 아직
  완료되지 않은 dependency ID를 표시한다. 카드를 누르면 해당 Task Drawer가 열린다.
- 이 상태는 Work Stack의 계획 사실이며 Conduit 실행 상태를 추정하거나 변경하지 않는다.

### Context Inbox와 외부 맥락

- 정제된 Capture Packet v1을 import하고 Capture를 기존 Task에 link한다.
- 추출된 action item을 Task로 전환하거나 source 전체에서 새 Task를 만들 수 있다.
- Task와 Capture와 Activity 변경은 journal transaction으로 함께 복구된다.
- raw message body, recipient, header, attachment, credential-shaped material은 저장 경계에서 거부한다.

### Outlook / Teams OOB handoff

- Outlook/Teams read request를 좁은 범위로 만들고 이미 인증된 agent session에 복사하는 UI가 있다.
- agent가 반환한 sanitized Capture를 import해 Task 생성 근거로 연결한다.
- linked source에 대한 plain-text reply를 Work Stack에서 작성·명시 승인하고, 고정된 target을 가진
  ReplyCommand를 agent에 전달할 수 있다.
- matching ReplyReceipt를 import해 `sent`, `failed`, `unknown` terminal fact를 기록한다.
- arbitrary recipient 입력, credential 저장, automatic resend, provider health 가장은 하지 않는다.
- **현재 실제 Outlook/Teams read/reply Gate 0는 모두 unverified이고 build flag도 false다.** 따라서
  기본 production UI의 Microsoft 전용 control은 disabled이며 generic manual Capture import만 열린다.
- 2026-08-30 M4 preflight에서도 현재 Codex task에 Outlook/Teams connector tool이 노출되지 않아
  실제 tenant evidence를 만들지 않았고 네 flag를 모두 false로 유지했다.
- Outlook/Teams desktop app 설치는 필요하지 않다.

### Windows 설치·백업·복구

- **More workspace actions → Local continuity**에서 실행 중인 Work Stack 제품 버전과 store schema를
  확인하고, 자동 업데이트가 아닌 `.sha256` 검증·pre-upgrade backup 경계를 읽을 수 있다. Workspace
  ID·Task/Objective/Capture·경로·수신자를 제외한 allowlisted support summary를 복사할 수 있다.
- 단일 `WorkStack-Setup-1.0.0.ps1`에 hash-verified 64-bit Python 3.12.10 runtime과
  Unicode 17 dependency가 포함되므로 대상 PC에 Python이나 Node.js를 미리 설치할 필요가 없다.
- 빌드 시 설치 파일명과 SHA-256을 고정한 `.sha256` sidecar를 함께 만들고, strict verifier가
  filename mismatch와 hash mismatch를 모두 거부한다. 이는 전송 무결성 증거이며 코드 서명은 아니다.
- 앱은 `%LOCALAPPDATA%\Programs\WorkStack`, 계획 데이터와 backup은
  `%LOCALAPPDATA%\WorkStack` 아래에 분리한다.
- launcher는 서버 시작 전에 검증된 versioned backup을 만들고 retention을 적용한다.
- upgrade는 matching Work Stack process만 중지하고 pre-upgrade backup 뒤 staged install을 교체한다.
- updater는 installed configuration을 읽거나 setup을 실행하기 전에 adjacent `.sha256`의 정확한
  filename과 digest를 검증하며 mismatch에는 아무 setup code도 실행하지 않는다.
- backup archive는 exact member allowlist, size, SHA-256 manifest, workspace/store semantic validation을
  통과해야 restore할 수 있다.
- 실행 중인 UI에서도 명시적 확인 뒤 동일한 verified backup format을 다운로드할 수 있다.
- 기존 store restore에는 별도 safety backup이 필수이며 relocation은 원본을 삭제하지 않는다.
- Start menu의 **Work Stack Maintenance** 창에서 verified backup 생성·검증, offline restore,
  workspace relocation을 수행한다. 실행 중인 Work Stack은 명시적 동의 없이 중지하지 않으며,
  relocation 설정은 검증 복사 성공 뒤에만 바뀐다.
- uninstall은 기본적으로 앱만 제거하고 계획 데이터와 backup을 보존한다.

## 3. 데이터와 안전 경계

- 기본 SSOT는 `%LOCALAPPDATA%\WorkStack\data`의 local JSON store다.
- 단일 writer lease, process lock, atomic replace, recovery journal을 사용한다.
- 서버는 loopback bind만 허용하고 browser mutation에는 Origin + CSRF 경계를 적용한다.
- 인식되는 legacy browser writer 5종은 유효한 browser 경계 뒤에도 HTTP 410으로 종료되며,
  실제 제품 mutation은 versioned `/api/v1` 경로만 사용한다.
- Capture/reply 및 Task-create POST는 Idempotency-Key와 canonical request digest를 사용한다.
- stable Workspace/Task UUID, monotonic revision, append-only planning-status fact를 유지한다.
- Docking v1 contract/kit/safety bytes는 `.gitattributes`로 platform newline 변환에서 보호한다.
- export audit가 source allowlist와 runtime tree에서 raw/credential/recipient leakage를 검사한다.
- Activepieces, Conduit runtime, 외부 DB, background Microsoft worker는 아직 제품 안에 넣지 않았다.

## 4. 현재 체크포인트의 검증 증거

- frontend: **35 test files / 177 tests 통과**
- backend: **145 tests 통과 / Windows symlink 권한 1건 skip**
- production build: 통과
  - 생성물: `frontend/dist`
  - M48 기준 초기 JS 440.01 kB이며 Workspace, Table, Task Drawer, Graph, Treemap은
    bounded lazy chunk로 분리돼 있다.
  - 알려진 경고: Zod annotation 제거 경고만 남았고 500 kB chunk 경고는 해소됐다.
- Playwright Chromium: **32 tests 통과** (기존 22개 제품 flow + forced-colors + 200%
  reflow-equivalent viewport + 독립 timeout을 가진 8개 surface axe scan)
- Playwright WebKit compatibility: **2 tests 통과** (Board 선택 해제·Graph keyboard Objective 탐색)
- Playwright Firefox compatibility: 로컬 host application-control policy는 다운로드한 실행 파일을
  product code 실행 전에 차단했지만 strict Windows remote CI에서 Firefox와 WebKit 합계 **4 tests**가
  독립적으로 통과했다.
- source export audit: **265 UTF-8 files 통과**
- disposable runtime tree audit: **10 UTF-8 files 통과**
- `git diff --check`: 통과
- historical remote CI run `33302786618` (commit `a98d9c9ef759f38398a03142a88da23be1396045`)은
  backend/frontend/build/source-audit와 첫 22개 Chromium scenario를 통과했지만 마지막 axe scan이
  30초 timeout으로 실패했다. M52는 이 aggregate timeout을 surface별 test로 분리했고 새 pushed
  run의 acceptance를 기다린다.
- remote run `33304031482`는 Chromium 32개와 Firefox/WebKit 4개를 통과했지만, 같은 PowerShell
  step 안의 frontend test 실패가 뒤의 build 성공으로 가려지는 CI 결함도 드러냈다. 이후 commit
  `291e08678a1b986112304c0710153484d79800f9`에서 모든 command를 별도 fail-closed step으로
  분리했고 초기 lazy Workspace test의 wait를 5초로 제한했다. 따라서 `33304031482`는 브라우저
  증거로는 유효하지만 최종 aggregate green claim으로 사용하지 않는다.
- strict remote run `33304485683` at
  `499ecc07d290a7fb54e17e438b797853f4e35a5b`는 분리된 Python/frontend install, backend 145,
  frontend 177, build, 265-file audit, Chromium 32, Firefox/WebKit 4개 단계를 모두 통과했다.
- disposable browser QA:
  - Graph/Board/Treemap 모두 같은 Task를 두 번 눌러 선택·Drawer·회색 강조가 해제됐다.
  - command palette, Task actions, Workspace actions, 숫자키와 J/K를 실제 production build에서 확인했다.
  - Objective Hub에서 Objective를 idempotent하게 직접 생성·선택하고 KR을 추가해 목록·revision 반영을 실제 production build에서 확인했다.
  - Table에서 P0 filter를 저장하고 즉시 다시 선택되는 것을 실제 production build에서 확인했다.
- M6 product branch/commit/tree:
  `codex/workstack-ui-actions-20260830` / `1dad3bc63e97acc0281444a96533af87f2cb6220` /
  `9d1782ca172f4fd0cbd942effe56e78afd3fba4b`
- M7 producer handoff: synthetic revision-0 snapshot 503 bytes, SHA-256
  `350c752338852485dd78beffee25dc635dfcbc93b51eb3fe546e3f5d07cc309f`, repeat bytes equal,
  ten store file hashes unchanged. Conduit consumer implementation은 이 repository의 완료 claim이 아니다.
- M8 usability/continuity product commits:
  - Focus inline transition: `41a028f1908b85b5f0a1ca1291d349ad54cc34c4`
  - Objective/KR editing: `44ff7eb743b070bfe1e53877e50a164dca44ec70`
  - verified live backup UI: `f26d73c9244136d9027ba268dd365473e8547e0e`
  - bounded indexed search: `9ebfa8da72492422c18576262c46c01fb8e4b63b`
  - Objective Hub direct create: `fdc179dd07f848069a2b780ee94ddc22bb074da8`
  - Capture draft rebase guard: `3ed1bc993eb651cf4742a44e0f59bfb83aff9a6d`
  - disjoint-field Task rebase and cross-tab signal fix: `54204d7fe54e17e0967580e8c293a2dfdcd8b1b2`
  - stale Workspace continuity: `6e4900f79d894f7964659b92b245487e44cacac7`
  - large Graph viewport virtualization: `0da585e3baa2138cb15f8ca796846686296affb3`
  - Gate 0-aware Capture trust labels: `87e567059dab75b8e683c6ab1adb27c3202b1351`
  - self-contained Windows runtime setup: `47e9cdab11608466514b31f8a12c91b2ace43ee4`
  - reproducible optional QR transfer tools: `cbf5f7184b02ffef52918616db4097843872a8e6`
  - outcome-first empty Workspace: `84d116642623d138bc5ee9b0f4782f32c94e8fc9`
  - Objective-aligned Task creation and Quick Add draft ownership:
    `19181bb4712ae90eb7be03af3814a176b0062b46`
  - Task-to-Objective reverse navigation: `c8b67c18f091ede813d5136f26d91499e9fee487`
  - parent/dependency Task navigation: `d1817f5d0110214f5cd97a529662edd98825c330`
  - Windows setup SHA-256 sidecar and strict verifier:
    `5dc0ce63b0f80f974af3bad87ee9408795c96ab5`
  - explicit unsaved Task navigation guard: `43701f140191bcc071a43be9ed7076760c0633e3`
  - guarded browser history and unload boundary: `582c19a56fbcbd7e3c2b58695381a5c7aea0177f`
  - fail-closed Windows update checksum gate: `98593b562405805a2789390612c9f786bf81514d`
  - bidirectional Task relationship navigation: `9f1d55ccd68a6b72563979d94af1487ac73351c9`
  - cyclic Task relationship rejection: `8b932cb4461f236c2a7ac4dcc7116498ac8d825b`
  - guided safe Task relationship editor: `1be5f1ce27dc90fa7851e802b42ef51aebe6565e`
  - bounded relationship candidate filtering: `1a18336621d390541af1e4745770a26c7155fa52`
  - dependency-aware actionable Focus: `1a94bb8d2ec27f87933eec43fe5ef8a37df962b7`
  - direct Focus blocker navigation: `b929d3d0c969fd349de74db0b3b302fd432f5fae`
  - Objective execution readiness: `e35d94bf0e2712355fa55e9f7ba8ec8d086f809c`

## 5. 아직 구현하지 않았거나 의도적으로 보류한 것

- Work Stack 서버가 OOB connector를 직접 호출하는 continuous Outlook/Teams sync
- 실제 tenant를 대상으로 한 provider별 Gate 0 evidence와 production flag 활성화
- Microsoft background job/queue/worker, polling, retry center, integration health dashboard
- Conduit consumer import와 Task별 multi-agent Taskroom 자동 생성
- Activepieces embedding, SQLite/Postgres migration, indexed FTS and server-side saved-filter sharing
  (현재 검색과 saved filters는 bounded local implementation)
- 자동 우선순위 변경 또는 Focus 자체 점수의 사용자 설정
- Microsoft Handoff Activity & Attention 화면
  - 실제 Gate 0/reply dogfood evidence가 없어 현재 계획은 reviewer가 reject한 상태다.
- 자동 우선순위 조정·Focus scoring 설정·calendar scheduling처럼 현재 명시적 Start/Done 범위를
  넘어서는 Today automation

## 6. 등록된 기술 부채와 성숙화 순서

1. Objective/Graph note/Task note/subtask 생성은 intent idempotency로 보호된다. Subtask status 응답이
   유실되면 authoritative Task를 다시 읽어 실제 commit 여부를 확인한다.
2. cross-tab refresh는 content-free best-effort 신호와 revision guard를 조합한다. 서로 다른 Task 필드는
   한 번만 자동 rebase하지만, 같은 필드 충돌은 명시적 검토가 필요하며 실시간 공동편집 제품은 아니다.
3. Graph model은 1,000 Task gate와 250-node 초과 viewport virtualization을 통과했고 unified search는
   10,000 Task process-local index gate를 통과했다. Persisted FTS는 현재 수치로는 hard blocker가 아니다.
   Dependency readiness도 10,000 Task에서 공용 index를 한 번만 만들며 Task별 전체 roster scan을 하지 않는다.
4. 실제 Microsoft provider capability는 fixture나 supplied provenance만으로 승인하지 않는다. Generic packet
   import는 manual provenance만 받고, OOB Capture는 provider Gate 0 전까지 unverified로 표시한다.
5. one-file installer는 official Python runtime까지 포함하고 launch/update, automatic backup,
   verified restore CLI와 GUI verified backup을 제공한다. 남은 release-hardening debt는 OS signing이다.

이 항목들은 현재 실사용을 막는 hard blocker로 판정하지 않았으며, 다음 계획에서 ROI와 실제 dogfood
증거를 기준으로 다시 순위를 정한다.

성숙화는 `docs/PRODUCT-MATURITY-EXECUTION-ROADMAP_2026-08-30.md`의 순서와 gate를 따른다.

## 7. 실행 상태와 저장소 상태

- 실행 중인 localhost 주소는 영속 제품 상태나 release evidence로 간주하지 않는다.
- 원격 branch `origin/codex/workstack-ui-actions-20260830`에는 strict acceptance를 통과한 aggregate
  commit `499ecc07d290a7fb54e17e438b797853f4e35a5b`까지 push되어 있다. 이후 successor는
  acceptance 결과와 최종 영수증만 갱신하는 documentation-only commit이다.
- 원 checkout의 기존 사용자 소유 dirty path 6개는 별도이며 docking/UI commit에 포함하지 않았다.

## 8. 관련 결정 문서

- `docs/PRACTICAL-ADVERSARIAL-REVIEW-POLICY.md`
- `docs/FOCUS-EXPERIMENT-IMPLEMENTATION-PLAN.md`
- `docs/BOARD-STATUS-MUTATION-STABILITY-PLAN.md`
- `docs/TASK-DRAWER-SAVE-SERIALIZATION-PLAN.md`
- `docs/QUICK-ADD-CREATE-CONTINUE-PLAN.md`
- `docs/OOB-OUTLOOK-TEAMS-IMPLEMENTATION-PLAN.md`
- `docs/MICROSOFT-HANDOFF-ACTIVITY-IMPLEMENTATION-PLAN.md`
- `docs/WORKSTACK_WINDOWS_INSTALL_BACKUP_USER_GUIDE_2026-08-30.md`

## 9. 다음 실행 순서

1. 새 one-file setup artifact와 sidecar는 release 후보를 실제 배포할 때 다시 빌드·검증한다.
2. 실제 Outlook/Teams lane은 connector capability와 비민감 Gate 0 evidence가 생길 때만 별도로 활성화한다.
3. OS publisher signing은 외부 인증서와 release identity가 준비될 때 수행한다.
4. persisted FTS는 dogfood에서 process-start cold search가 실제 문제가 될 때만 다시 검토한다.
5. Conduit 관련 작업은 consumer-side import/taskroom handoff로 제한하고 Work Stack의 단일 파일 export
   권한 경계를 변경하지 않는다.
