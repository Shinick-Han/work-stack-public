# Read-only Focus Experiment 구현 계획

상태: **IMPLEMENTED — 실용 적대적 리뷰 통과**
작성일: 2026-08-29
구현 시작 조건: 독립 적대적 리뷰가 `GO` 또는 `GO_WITH_CUTS`이고 hard blocker가 없을 것

리뷰 정책은 완벽성보다 빠른 ROI를 우선한다. 보안/프라이버시 누출, data 손상,
외부 side effect, 비가역 migration, 기존 핵심 flow의 치명적 회귀만 hard blocker다.
그 밖의 UX polish, 측정 한계, 추가 edge test, 리팩터링 필요는 기술부채로 기록하고
timebox 안에서 구현을 진행한다.

## 1. 결정

이 증분은 완성형 Today page가 아니다. 현재 Task data 위에 **읽기 전용 Focus
projection 실험** 하나만 추가한다.

> active Task 중 기한·진행 상태·우선순위 때문에 지금 다시 볼 이유가 있는 항목을
> 중복 없는 단일 목록으로 보여주고, 이유를 모두 badge로 설명한다.

사용자는 목록에서 기존 Task drawer를 열거나 새 Task를 만들 수 있다. `Start`,
`Mark done`, drag, bulk action, saved focus는 없다.

## 2. 적대적 리뷰로 바뀐 전제

이전 Today 계획은 다음 이유로 거절됐다.

- local 31 Task 중 30개는 tracked synthetic seed와 같은 계보라 ROI 증거가 아니다.
- 기존 exclusive section 규칙에서는 12 focus 후보 중 10개가 `In progress`로 들어가
  `Due soon`, `P0/P1` 이유를 가렸다.
- 빠른 상태 변경은 현재 공통 mutation의 전체 workspace rollback 및 동시 mutation
  문제를 그대로 노출한다.
- Today를 첫 nav로 두는 것은 사용 가설이 검증되기 전 정보 구조 변경이다.

따라서 이번 판단을 “즉시 ROI가 입증됐다”가 아니라 다음 **낮은 비용의 가설**로
낮춘다.

> 한눈에 설명 가능한 Focus 후보 목록이 Board/Graph를 탐색하는 반복 비용을 줄일 수
> 있다. frontend 한정 실험으로 먼저 확인한다.

현재 seed는 다양한 상태·due·priority 조합을 렌더하고 회귀 테스트하는 데만 사용한다.
실사용 수요의 증거로 취급하지 않는다.

## 3. ROI와 timebox

| 항목 | 계약 |
| --- | --- |
| 구현 범위 | frontend pure projection + page + URL/nav wiring |
| backend/store/API | 변경 없음 |
| 새 dependency | 없음 |
| mutation | 없음 |
| elapsed 목표 | 병렬 agent 기준 4~8시간 |
| stop-loss | frontend 작업 1일을 넘기거나 공통 domain 변경이 필요하면 중단 |
| 유지 판단 | 3~5일 dogfood 후 |

이 실험보다 큰 Table, calendar, AI ranking, daily-plan persistence를 함께 만들면 ROI
검증이 불가능해지므로 금지한다.

## 4. 사용자 여정

### 4.1 진입

기존 nav 순서와 기본 진입점을 보존하고 `Focus`를 Workspace 바로 아래에 추가한다.

```text
Workspace
Focus
Context Inbox
```

- default/root surface는 계속 `Workspace / Graph`다.
- 기존 Workspace의 `<kbd>1</kbd>` 표시는 그대로 유지한다.
- 새 숫자 shortcut을 만들지 않는다.
- URL은 `?surface=focus`와 `?surface=focus&task=T-0001`을 사용한다.

Focus 사용 빈도가 확인되기 전에는 첫 nav/default landing으로 승격하지 않는다.

### 4.2 상단

다음만 표시한다.

- local calendar date
- unique Focus candidate 수
- 전체 active Task 수
- `New task`
- 기존 workspace query의 명시적 `Refresh`

평가 점수, productivity, health, on-track 문구는 없다. Focus 후보가 active Task 전체가
아님을 설명한다.

### 4.3 단일 목록

section으로 나누지 않는다. Task는 최대 한 row만 가진다. 해당 Task가 목록에 나타난
모든 이유를 badge로 표시한다.

예:

```text
T-0001  Release decision log
[Overdue 2d] [In progress] [P0]
```

이 방식은 `started`, `due soon`, `high priority`가 겹칠 때 어느 이유도 숨기지 않는다.

### 4.4 row action

row container는 상호작용 요소가 아닌 `<article>`이다.

- title은 하나의 명시적 button: accessible name `Open T-0001 · <title>`
- title button은 기존 `TaskDrawer`만 연다.
- container에 `onClick`, `role=button`, `tabIndex`를 넣지 않는다.
- 내부에 상태 변경 button은 없다.
- nested button과 event propagation workaround가 없다.

Focus가 완전히 비면 기존 `EmptyState`와 `New task`만 제공한다. Workspace 이동은
항상 보이는 sidebar를 사용하므로 별도 prop/action을 만들지 않는다.

## 5. Focus reason 계약

대상은 `status`가 `open` 또는 `started`인 Task뿐이다.

각 Task의 reason set은 서로 독립적으로 계산한다.

| reason | 조건 | 표시 |
| --- | --- | --- |
| `invalid_due` | due가 null이 아니고 canonical date가 아님 | `Due date needs review` |
| `overdue` | canonical due `< today` | `N days overdue` |
| `due_today` | canonical due `== today` | `Due today` |
| `due_soon` | `today < due <= today + 7 calendar days` | `Due in N days` |
| `in_progress` | `status == started` | `In progress` |
| `high_priority` | priority가 `P0` 또는 `P1` | `P0` 또는 `P1` |

candidate는 reason이 하나 이상인 active Task다. `done`, `dropped`는 reason이 있어도
제외한다.

Task 하나가 `due_soon + in_progress + high_priority`를 모두 가질 수 있지만 row는
하나만 렌더링한다.

canonical due가 아니면 날짜 reason을 만들지 않고 `invalid_due`만 만든다. status와
priority reason은 계속 계산하므로 손상된 due가 다른 focus 이유를 숨기지 않는다.

## 6. Canonical civil date 계약

### 6.1 입력 검증

Focus model은 shared API schema를 이번 증분에서 바꾸지 않는다. 자체 boundary에서
due를 다음과 같이 검증한다.

1. 정규식 `^\d{4}-\d{2}-\d{2}$`
2. year/month/day를 숫자로 분리
3. `Date.UTC(year, month - 1, day)`로 civil ordinal을 계산
4. UTC component round-trip이 원래 year/month/day와 같은지 확인

`Date.UTC`는 문자열을 UTC 날짜로 해석하거나 browser today를 얻는 데 사용하지 않는다.
입력 component의 Gregorian 유효성 및 날짜 간 정수 차이를 계산하는 데만 사용한다.

backend가 허용하는 compact/week ISO due가 있더라도 Focus에서는 canonical date가
아니므로 `Due date needs review`로 명시한다. 조용히 no-date로 취급하지 않는다.

### 6.2 local today

browser today는 한 번의 계산에서 다음 component로 만든다.

```text
date.getFullYear()
date.getMonth() + 1
date.getDate()
```

`new Date().toISOString().slice(0, 10)`은 금지한다.

### 6.3 자정 rollover

Focus page는 data polling 없이 date만 갱신한다.

- mount에서 local today 계산
- 다음 local midnight 직후 한 번 timer로 재계산
- document가 다시 visible이 될 때 local today 재계산
- timer cleanup

날짜가 바뀌면 같은 in-memory workspace Task로 projection을 다시 계산한다. API refetch를
자동 실행하지 않는다.

## 7. 정렬 계약

Task마다 가장 높은 urgency reason rank를 계산한다.

```text
0 invalid_due
1 overdue
2 due_today
3 due_soon
4 high_priority
5 in_progress
```

`invalid_due`를 첫 순서에 두는 이유는 silent data corruption을 숨기지 않기 위해서다.

전체 comparator:

1. minimum reason rank
2. canonical due civil ordinal — 없음/invalid는 마지막
3. priority `P0`, `P1`, `P2`, `P3`
4. status `started`, `open`
5. Task ID ASCII code-point order

Task ID 비교는 locale에 따라 달라질 수 있는 `localeCompare`가 아니라 `<`/`>` 기반
code-point comparator를 사용한다.

입력 Task array와 Task object를 mutate하지 않는다.

## 8. summary 의미

중복 가능한 raw reason count를 metric strip으로 노출하지 않는다. 오해를 피하기 위해
다음 두 숫자만 표시한다.

- `Focus candidates`: reason이 하나 이상인 unique active Task 수
- `Active tasks`: `open|started` 전체 수

reason별 빈도는 각 row badge 자체로만 보인다. analytics가 필요하면 dogfood 이후
별도 근거로 추가한다.

## 9. URL state invariant

`AppUrlState.surface`:

```ts
'workspace' | 'focus' | 'inbox'
```

공통 normalizer를 `readUrlState`와 `update`의 write 직전에 모두 적용한다.

- `workspace|focus`: `captureId = null`
- `inbox`: `taskId = null`
- unknown surface: `workspace` fallback 후 같은 invariant 적용

직접 URL, refresh, popstate, programmatic update가 모두 같은 규칙을 사용한다.

Workspace의 `view`, `search`, `status`, `priority`, `objectiveId`는 Focus 진입 중에도
URL state에 보존하지만 Focus projection에는 적용하지 않는다. Workspace로 돌아가면
이전 filter가 복원된다.

App rendering은 `workspace`와 `focus`를 workspace query만으로 분기한 뒤에, `inbox`에서만
captures query loading/error를 요구한다. Capture fetch 오류가 Focus를 막지 않는다.

## 10. `/` shortcut

현재 전역 `/` handler는 Focus에 search input이 없어도 default를 막는다. 이번 wiring에서
다음으로 바로잡는다.

```text
현재 surface에 실제 search input이 있을 때만 preventDefault + focus
없으면 browser/default key behavior 보존
```

Focus 전용 search를 만들지는 않는다.

## 11. Frontend 구조

신규:

```text
frontend/src/features/focus/focusModel.ts
frontend/src/features/focus/focusModel.test.ts
frontend/src/features/focus/useLocalToday.ts
frontend/src/features/focus/useLocalToday.test.ts
frontend/src/features/focus/FocusPage.tsx
frontend/src/features/focus/FocusPage.test.tsx
```

기존 변경:

```text
frontend/src/domain/types.ts
frontend/src/app/urlState.ts
frontend/src/app/App.tsx
frontend/src/styles.css
frontend/src/app/App.test.tsx
```

기존 `Icon name="target"`, `Pill`, `Button`, `IconButton`, `EmptyState`, `TaskDrawer`,
`QuickTaskDialog`, `workspaceQuery`를 재사용한다. Icon, API, backend, package 파일은
변경하지 않는다.

### FocusPage props

```ts
interface FocusPageProps {
  workspace: WorkspaceProjection
  today: string
  isRefreshing: boolean
  onRefresh: () => void
  onCreateTask: () => void
  onSelectTask: (taskId: string) => void
}
```

mutation callback과 pending state는 없다.

## 12. dirty worktree 안전 경계

OOB 구현은 아직 uncommitted이고 `App.tsx`, `styles.css`, App test와 겹친다. 따라서
구현 시작 전 다음 read-only baseline을 기록한다.

- `git status --short`
- `git diff --stat`
- 변경 대상 파일의 현재 diff
- frontend/backend 현재 test 결과

새 구현은 기존 diff를 덮어쓰지 않고 additive patch로만 적용한다. rollback을 `git
revert`라고 부르지 않는다. Focus 관련 hunk와 신규 파일만 역패치하고 rebuild한다.
OOB 변경은 보존한다.

## 13. 테스트 계획

### 13.1 focusModel

- active만 대상, terminal 제외
- 하나의 Task에 reason 여러 개 보존
- candidate row는 Task당 하나
- invalid canonical form: compact date, ISO week date, impossible date, 잘못된 윤일
- valid leap day, month/year rollover
- overdue/today/+1/+7/+8 경계
- KST 및 DST timezone에서 component 기반 today와 civil day distance
- reason rank → due → priority → status → ASCII ID 정렬
- duplicate input Task ID는 조용히 dedupe하지 않고 명시적 error
- 입력 array/object byte-equivalent 불변

### 13.2 useLocalToday

- mount에서 local date
- 다음 local midnight 후 update
- hidden→visible 시 update
- 같은 date면 불필요한 state update 없음
- timer/listener cleanup
- data fetch/refetch를 호출하지 않음

### 13.3 FocusPage

- unique candidate/active count
- reason badge가 overlap을 모두 표시
- row가 중복 렌더링되지 않음
- title button accessible name과 Task callback
- non-clickable article, nested button/role-button 없음
- New task/Refresh callback
- 완전 empty state와 New task
- no status mutation controls
- mobile metadata wrap, visible focus outline, text+color semantics

### 13.4 URL/App

- Focus nav와 breadcrumb `Focus`
- default는 Workspace 유지
- direct `surface=focus&task=...` 복원
- direct `surface=focus&capture=...`은 capture 제거
- direct `surface=inbox&task=...`은 task 제거
- unknown surface fallback + invariant
- read/write/popstate/back/forward canonicalization
- Workspace filter/query 보존, Focus에는 미적용
- Capture query error가 Focus를 막지 않음
- `/`는 실제 search input이 있을 때만 prevent/focus
- Focus에서 기존 QuickTask/TaskDrawer 재사용
- OOB Inbox/provider gate 회귀

### 13.5 전체

- frontend 전체 test
- production build
- backend 전체 test — backend 무변경 확인
- source/runtime export audit
- browser desktop/mobile Focus → Task drawer → Workspace/Inbox navigation
- Graph/Board/Treemap/Inbox/OOB/Reply 기존 flow

## 14. 적대적 승인 기준

1. 읽기 전용이며 Task mutation을 추가하지 않는다.
2. single list + multi-reason badge로 overlap을 숨기지 않는다.
3. seed data는 renderability만 증명하고 ROI를 주장하지 않는다.
4. backend/store/API/package dependency를 변경하지 않는다.
5. canonical civil-date, invalid due, midnight rollover가 test로 고정된다.
6. default/nav first position을 바꾸지 않는다.
7. URL surface/drawer invariant가 read/write/history 전체에 적용된다.
8. 기존 OOB dirty diff를 보존한다.
9. 작업은 frontend 한정 1일 timebox 안에 끝난다.
10. hard blocker가 남으면 구현하지 않는다. soft finding은 debt로 기록하고 구현한다.

### Hard blocker

- credential/raw Microsoft content/privacy leak
- Task 또는 local store 손상 가능성
- 외부 message 전송 등 새 side effect
- 비가역 migration
- Graph/Board/Treemap/Inbox/OOB 핵심 flow를 깨뜨리는 회귀

### 허용 가능한 기술부채

- 사용 빈도 telemetry 부재
- 추가 timezone/browser 조합 test
- visual polish와 animation
- focus ranking을 실제 ritual에 맞게 조정할 필요
- dirty worktree 때문에 rollback이 hunk 단위인 점

## 15. 완료 기준

- `Focus` surface가 Workspace 아래에 추가되고 default는 유지된다.
- synthetic test set에서 candidate/reason/order가 deterministic하다.
- 같은 Task가 한 번만 보이고 모든 focus 이유가 badge로 보인다.
- 기존 Task drawer와 Quick Task만 재사용한다.
- invalid due는 숨지 않고 review reason으로 나타난다.
- local midnight 이후 date와 projection이 polling 없이 갱신된다.
- direct URL/history conflict가 canonicalize된다.
- 전체 test/build/audit/browser QA와 구현 적대적 리뷰가 통과한다.

## 16. Rollback

- Focus 신규 파일 제거
- App/types/url/CSS/test의 Focus hunk만 역패치
- frontend rebuild/restart

data migration이나 cleanup은 없다. 기존 OOB 변경은 그대로 남는다.

## 17. Dogfood 판정

3~5일 사용 후 다음을 수동 기록한다.

- Focus를 연 날 수
- Focus에서 Task drawer를 연 횟수와 이유 badge
- 곧바로 Workspace search/Board로 되돌아간 횟수
- 실제 우선순위 판단을 잘못 설명한 후보
- invalid due reason의 실제 유용성

Focus가 거의 사용되지 않거나 Board Started와 중복이면 제거한다. 유지가 확인된 뒤에만
빠른 상태 변경, saved daily plan, Table/global search 중 하나를 새 계획으로 심사한다.

## 18. 구현 결과

- frontend 전체: 15 files / 67 tests 통과
- backend 전체: 67 tests 통과, Windows symlink 권한 test 1건 skip
- production build 통과
- source/runtime export audit 통과
- desktop 및 390×844 mobile browser QA 통과
- 실제 local data에서 21 active 중 12 unique Focus candidate와 복수 reason badge 확인
- Focus title에서 기존 Task drawer 및 `surface=focus&task=...` deep link 확인
- Workspace/Context Inbox 전환과 URL conflict canonicalization 확인

구현 후 리뷰가 발견한 `Capture → 새 Task` drawer 회귀는 `inbox` surface에서 `taskId`를
지우는 새 invariant와 기존 callback의 충돌이었다. 생성 성공 후
`surface=workspace&task=<id>`로 함께 전환하도록 수정하고 App 회귀 test를 추가했다.

허용한 기술부채는 locale별 날짜 문구, passive row hover affordance, 사용하지 않는 Capture
query의 background 시작, ranking의 실제 ritual 적합성이다. dogfood를 막지 않는다.
