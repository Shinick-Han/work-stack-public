# Today / Focus 구현 계획

상태: **적대적 리뷰 REJECT — 구현 보류**
작성일: 2026-08-29
구현 시작 조건: 이 문서에 대한 독립 적대적 리뷰가 `APPROVE`이고 blocker가 0건일 것

> 2026-08-29 리뷰 결과: runtime의 대부분이 synthetic seed이고 exclusive section이
> 여러 focus 이유를 숨기며, 계획이 가정한 quick status mutation의 동시 rollback
> 계약도 현재 코드에 없다. 이 범위는 구현하지 않는다. 더 작은 읽기 전용 단일 목록
> 실험은 `FOCUS-EXPERIMENT-IMPLEMENTATION-PLAN.md`에서 별도로 재심사한다.

## 1. 결정

다음 제품 증분은 기존 우선순위 3위였던 `Today/Table + 전역 검색` 전체가 아니다.
그중 매일의 실행에 직접 필요한 **Today / Focus projection 하나만** 구현한다.

> 지금 시작했거나, 기한이 임박했거나, 우선순위가 높은 active Task만 모아
> 오늘 무엇을 움직여야 하는지 결정하고 기존 Task drawer에서 실행하는 화면

이번 증분은 새 Task 모델, 저장소, backend endpoint, scheduling engine을 만들지
않는다. 이미 로드하는 `WorkspaceProjection.tasks`를 deterministic하게 분류하는
client-side projection이다.

## 2. 왜 지금 필요한가

현재 Work Stack은 구조를 이해하는 Graph, 흐름을 바꾸는 Board, 목표별 workload를
보는 Treemap을 제공한다. 하지만 앱을 열었을 때 “오늘 먼저 무엇을 해야 하는가”에
답하는 landing surface는 없다.

2026-08-29 현재 실행 중인 local workspace를 내용 없이 집계하면 다음과 같다.

| 항목 | 수 |
| --- | ---: |
| 전체 Task | 31 |
| active (`open` 또는 `started`) | 21 |
| `started` | 10 |
| due가 있는 active Task | 20 |
| 7일 이내 due | 10 |
| active P0/P1 | 9 |
| objective 미연결 active Task | 1 |

Microsoft handoff record나 background integration이 없어도 이 데이터는 이미 존재하고
매일 사용 가능하다. 따라서 Gate 0 이후에만 가치가 생기는 integration status UI보다
즉시 ROI가 높다.

## 3. 범위 축소 판단

| 후보 | 이번 판단 | 이유 |
| --- | --- | --- |
| Today / Focus projection | **구현** | 기존 21 active Task에 즉시 적용 |
| TanStack Table | 제외 | 12~20개 focus 후보에는 native list가 충분 |
| 전역 검색 / command palette | 제외 | Workspace `/` 검색과 Quick Add가 이미 있음 |
| saved daily plan / pinning | 제외 | 새 persistence와 ritual 가정이 필요 |
| calendar / time blocking | 제외 | 외부 일정과 시간 추정 모델이 필요 |
| AI 자동 우선순위 | 제외 | 설명 불가능한 ranking과 agent dependency 증가 |
| notification / reminder | 제외 | background scheduler가 필요 |

이 화면은 “Today라는 이름의 새 task manager”가 아니라 동일 Task data의 네 번째
얇은 projection이다.

## 4. 사용자 여정

### 4.1 진입

sidebar의 첫 항목에 `Today`를 추가한다.

```text
Today
Workspace
Context Inbox
```

초기 release에서 기존 기본 진입점은 `Workspace / Graph`로 유지한다. Today의 실제
사용 빈도를 dogfood로 확인하기 전에는 root/default route를 바꾸지 않는다.

URL은 기존 query-state 구조를 유지한다.

```text
?surface=today
?surface=today&task=T-0001
```

새 router dependency나 path migration은 없다. browser back/forward와 refresh에서
선택 surface/task가 복원돼야 한다.

### 4.2 화면 상단

Today page는 local calendar date와 네 개의 사실만 표시한다.

- In progress 수
- Overdue 수
- Due today 수
- Due in next 7 days 수

`Productive`, `On track`, `Healthy` 같은 평가 문구나 허위 점수는 표시하지 않는다.

### 4.3 Focus sections

active Task를 아래 순서로 정확히 한 section에만 배정한다. 먼저 일치한 section이
Task를 소유한다.

1. `Overdue`: `due < today`
2. `Due today`: `due == today`
3. `In progress`: `status == started`
4. `Due soon`: `today < due <= today + 7 calendar days`
5. `High priority next`: 아직 배정되지 않은 `P0` 또는 `P1`

`done`, `dropped`는 항상 제외한다. 어느 조건에도 맞지 않는 open Task는 Today에
억지로 노출하지 않고 Workspace에 남긴다.

section이 비면 row를 만들지 않는다. 단, Overdue와 Due today가 모두 비어 있으면
`Nothing overdue or due today`라는 중립적인 설명을 상단 summary 아래에 표시한다.

### 4.4 section 내부 정렬

동일 section 안에서는 다음 comparator를 사용한다.

1. priority: `P0`, `P1`, `P2`, `P3`
2. due: 이른 날짜 우선, due 없음은 마지막
3. status: `started`, `open`
4. Task ID `localeCompare`

정렬은 설명 가능하고 deterministic해야 한다. 최근 수정 시각, context 수, Objective
진행률을 숨은 score에 사용하지 않는다.

### 4.5 Task row

각 row는 다음만 표시한다.

- ID와 title
- status
- priority
- due와 상대 표현 (`Today`, `2 days`, `3 days overdue`)
- 연결 Objective chip
- sanitized context count

row 또는 `Open task`를 누르면 기존 `TaskDrawer`를 연다. Today 전용 상세 화면은
만들지 않는다.

### 4.6 빠른 상태 전이

현재 존재하는 status mutation을 재사용한다.

- `open` row: `Start`
- `started` row: `Mark done`

새 mutation endpoint는 없다. 기존 revision/idempotency/optimistic rollback 규칙을
그대로 사용한다. 상태 전이 성공 후 같은 `workspace` query data에서 Focus section이
즉시 재계산된다.

`Mark done`은 명시적인 텍스트 버튼으로 제공하고 destructive icon-only action으로
숨기지 않는다. dropdown, bulk action, drag-and-drop은 추가하지 않는다.

### 4.7 새 Task

Today heading의 `New task`는 기존 `QuickTaskDialog`를 연다. Today 전용 field나 기본
due 값은 강제하지 않는다.

## 5. 날짜와 시간대 계약

Task due는 date-only ISO `YYYY-MM-DD`이다. 비교 기준은 브라우저의 local calendar
date다. UTC `toISOString().slice(0, 10)`을 사용하면 한국 자정 부근에 날짜가 어긋날
수 있으므로 사용하지 않는다.

신규 helper는 local date component를 조합한다.

```text
YYYY = date.getFullYear()
MM   = date.getMonth() + 1
DD   = date.getDate()
```

projection 함수는 test에서 기준 날짜를 명시할 수 있어야 한다.

```ts
buildFocusProjection(tasks, today: '2026-08-29')
```

날짜 비교는 valid Task contract의 ISO date string을 lexicographic하게 비교한다.

`today + 7`은 local calendar arithmetic으로 계산한다. 24시간 millisecond 덧셈으로
DST 경계를 계산하지 않는다.

## 6. Frontend 구조

신규 파일:

```text
frontend/src/features/today/focusModel.ts
frontend/src/features/today/focusModel.test.ts
frontend/src/features/today/TodayPage.tsx
frontend/src/features/today/TodayPage.test.tsx
```

기존 파일 변경:

```text
frontend/src/domain/types.ts
frontend/src/app/urlState.ts
frontend/src/app/App.tsx
frontend/src/components/Icon.tsx        # Today icon이 정말 필요할 때만
frontend/src/styles.css
frontend/src/app/App.test.tsx
```

backend, API contract, store, fixture data, package dependency는 변경하지 않는다.

### 6.1 model 경계

`focusModel.ts`는 React를 import하지 않는 pure module이다.

책임:

- local ISO date 생성
- plus-calendar-days 계산
- active Task 분류
- section deduplication
- deterministic ordering
- summary counts
- relative due label에 필요한 day distance

UI copy, mutation, navigation은 model에 넣지 않는다.

### 6.2 TodayPage props

```ts
interface TodayPageProps {
  workspace: WorkspaceProjection
  today?: string
  isRefreshing: boolean
  onRefresh: () => void
  onCreateTask: () => void
  onSelectTask: (taskId: string) => void
  onChangeTaskStatus: (taskId: string, status: TaskStatus) => void
  changingTaskId?: string | null
}
```

실제 `today`는 생략하고 local browser date를 사용한다. test만 고정 값을 주입한다.

### 6.3 기존 query 재사용

Today surface도 App의 기존 `['workspace']` query를 그대로 사용한다. 추가 fetch, query
key, polling은 없다. App이 이미 사용하는 loading/error/retry state를 공유한다.

## 7. URL state 변경

`AppUrlState.surface`를 다음 union으로 확장한다.

```ts
'today' | 'workspace' | 'inbox'
```

`readUrlState`는 정확히 세 값만 허용한다. unknown surface는 기존 default인
`workspace`로 fallback한다.

surface 전환 시 stale drawer ID 규칙:

- Today 진입: `captureId`를 null로 만든다.
- Workspace 진입: `captureId`를 null로 만든다.
- Inbox 진입: `taskId`를 null로 만든다.
- Today row 선택: `taskId`를 설정하고 `captureId`를 null로 만든다.

기존 workspace view/filter/search query는 Today에 진입해도 URL에서 보존할 수 있다.
Today projection에는 적용하지 않는다. 다시 Workspace로 돌아가면 기존 view/filter를
복구한다.

## 8. UX와 접근성

- page는 하나의 `h1`과 section별 `h2`를 가진다.
- 각 section에 Task 수를 텍스트로 표시한다.
- row 전체 click과 내부 button이 중복 실행되지 않도록 event propagation을 고정한다.
- action button accessible name에 Task ID를 포함한다.
- focus outline을 제거하지 않는다.
- status/priority는 색상만으로 구분하지 않고 text를 함께 표시한다.
- mobile 768px 이하에서 metadata는 wrap되고 action은 최소 44px target을 유지한다.
- empty state에서도 Workspace와 New task로 이동할 수 있다.
- loading/error는 App의 기존 공통 state를 재사용한다.

## 9. 상태 mutation 규칙

기존 `statusMutation`은 App에서 공유한다. 추가할 것은 UI의 per-task pending 표시뿐이다.

- mutation 중 해당 row action만 disabled한다.
- optimistic update로 section이 바뀌거나 사라질 수 있다.
- 실패 시 기존 rollback 후 toast를 표시하고 row가 원래 section으로 돌아온다.
- 연속 double-click을 막는다.
- 다른 Task row는 계속 열 수 있다.

만약 현재 mutation이 per-task pending ID를 노출하지 못하면 App의 mutation variables에서
현재 task ID만 읽어 전달한다. 새 mutation hook/framework는 만들지 않는다.

## 10. 테스트 계획

### 10.1 Pure model

- `done`, `dropped` 제외
- overdue/today/started/due-soon/high-priority first-match와 중복 없음
- started이 overdue이면 Overdue에만 속함
- P0 due today가 Due today에만 속함
- due +8일 P1이 High priority next에 속함
- open P2 due +8일은 Focus에 포함되지 않음
- priority → due → status → ID 정렬
- date 없는 Task 정렬과 high-priority 분류
- leap day와 month/year rollover에서 plus 7 calendar days
- UTC 날짜가 local date로 잘못 사용되지 않음
- 입력 Task array를 mutate하지 않음

### 10.2 TodayPage

- summary count와 section heading/row 표시
- Task가 section 사이에 중복 렌더링되지 않음
- empty day 설명과 Workspace/New task action
- row click이 Task drawer callback 호출
- `Start`와 `Mark done`이 정확한 status를 요청
- action click이 row open을 함께 실행하지 않음
- pending row만 disabled
- Objective/context/due metadata의 text fallback
- keyboard navigation과 accessible names

### 10.3 App/URL integration

- Today nav로 이동하면 `surface=today`
- refresh/back/forward로 Today와 selected Task 복원
- Inbox → Today 전환 시 Capture drawer 닫힘
- Today → Inbox 전환 시 Task drawer 닫힘
- unknown surface는 Workspace로 fallback
- Today에서 Quick Task와 기존 TaskDrawer가 재사용됨
- status mutation 성공/실패 후 projection 일관성
- Workspace Graph/Board/Treemap filter state 보존

### 10.4 전체 회귀

- frontend 전체 test
- frontend production build
- backend 전체 test — backend 무변경 확인
- source export audit
- runtime export audit
- 브라우저 desktop/mobile 시나리오
- Graph/Board/Treemap/Inbox/Task drawer/OOB 기존 흐름

## 11. 적대적 리뷰 승인 기준

다음을 모두 만족해야 구현에 들어간다.

1. 현재 실제 Task data에 즉시 사용할 row가 존재한다.
2. 새 backend/store/API/dependency가 없다.
3. 같은 Task가 Focus section에 두 번 나오지 않는다.
4. 분류와 정렬 규칙이 설명 가능하고 pure test로 고정된다.
5. Today가 기본 landing을 강제로 바꾸지 않는다.
6. saved focus, AI priority, calendar, Table, search로 범위를 넓히지 않는다.
7. 기존 TaskDrawer, QuickTaskDialog, status mutation을 재사용한다.
8. 날짜가 local calendar 기준이며 UTC/DST 경계 테스트가 있다.
9. optimistic rollback과 double-submit 방지 테스트가 있다.
10. 예상 변경은 frontend에 국한되고 단일 revert로 제거 가능하다.

## 12. 완료 기준

- Today nav와 deep link가 동작한다.
- 21개 active fixture/current Task 중 규칙에 맞는 focus subset이 deterministic하게 보인다.
- overdue/due-today/started/due-soon/high-priority가 중복 없이 분리된다.
- Task를 열고 `open → started`, `started → done`을 수행할 수 있다.
- 상태 변경이 Workspace의 Graph/Board/Treemap과 즉시 일치한다.
- 빈 화면, 오류, mutation 실패가 명확하다.
- frontend/backend test, build, source/runtime audit, browser QA가 통과한다.
- 구현 결과에 대한 최종 적대적 리뷰도 `APPROVE`다.

## 13. Stop-loss

다음 중 하나가 필요해지면 구현을 중단하고 계획을 재심사한다.

- 새로운 Task field 또는 daily-plan persistence
- backend projection/endpoint
- TanStack Table, router, date library 등 dependency 추가
- Objective/KR 자동 score
- calendar/OOB/Activepieces 호출
- notification/background scheduler
- bulk mutation 또는 drag-and-drop
- Workspace 기존 filter 의미 변경
- default landing surface 변경

## 14. Rollback

이 증분은 저장 data를 변경하지 않는다. rollback은 Today component/model/test/CSS를
제거하고 `surface` union과 sidebar wiring을 되돌린 뒤 frontend를 rebuild/restart하면
끝난다. data cleanup이나 migration rollback은 없다.

## 15. 후속 판단

Today를 3~5일 사용하며 다음만 기록한다.

- 사용자가 실제로 가장 많이 여는 section
- Start/Mark done을 Task drawer보다 Today에서 더 자주 쓰는가
- open high-priority와 due-soon 규칙이 실제 우선순위 ritual을 설명하는가
- Today에 없는 Task를 찾으러 Workspace search로 얼마나 자주 이동하는가

그 증거로 다음 후보를 선택한다.

- 목록 탐색이 병목이면 Table/global search
- focus 선택을 저장해야 하면 최소 `focus_date/focus_rank` 계약
- 외부 signal이 병목이면 실제 Gate 0/Activepieces
- plain note가 병목이면 Tiptap

dogfood 전에 이 후보들을 동시에 구현하지 않는다.
