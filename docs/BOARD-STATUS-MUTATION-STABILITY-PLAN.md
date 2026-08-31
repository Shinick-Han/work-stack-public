# Board 상태 변경 안정화 구현 계획

상태: **IMPLEMENTED — 독립 실용 적대적 리뷰 2건 통과**
작성일: 2026-08-29
예상 범위: frontend 한정 4~8시간

## 1. 왜 지금 하는가

Board는 현재 Workspace에서 Task 상태를 빠르게 바꾸는 유일한 실행 surface다. 그러나
표면의 계약과 App 연결이 서로 다르다.

- `BoardView`는 `onChangeTaskStatus()`가 반환한 Promise를 기다리고, 실패하면 카드 위치를
  복원하도록 구현되어 있다.
- `App`은 `statusMutation.mutate()`를 호출한 뒤 `void`를 반환한다.
- 따라서 Board의 `Saving…`, 동일 Task 잠금, 실패 catch가 실제 네트워크 요청보다 먼저
  끝난다.
- App의 실패 복구는 전체 Workspace snapshot을 되돌리므로 서로 다른 Task의 동시 변경이
  있을 때 한 Task의 실패가 다른 Task의 성공을 되감을 수 있다.

이 문제는 새 기능 가설이 아니라 현재 핵심 실행 flow의 신뢰성 결함이다. Table/global
search는 기존 검색·필터와 겹치고, Microsoft activity/retry는 실제 provider Gate 0가 아직
닫혀 있으며 retry가 외부 중복 전송 위험을 만든다. 따라서 다음 ROI는 Board 안정화가 가장
높다.

## 2. 목표

사용자가 Board select 또는 drag로 상태를 바꿀 때 다음 계약을 만족한다.

1. 해당 Task만 즉시 새 column에 보인다.
2. 요청이 끝날 때까지 해당 Task만 `Saving…` 및 이동 불가 상태다.
3. 같은 Task의 빠른 중복 제출은 하나의 PATCH만 만든다.
4. 성공하면 서버가 반환한 revision을 Workspace cache에 반영한다.
5. 실패나 revision conflict면 해당 Task의 local override를 버리고 서버 기준 상태를
   다시 표시하며 이유를 알린다.
6. 서로 다른 Task A/B가 동시에 변경될 때 A 실패가 B 성공을 되감지 않는다.

## 3. 최소 구현 범위

### App mutation bridge

- `statusMutation`에 안정적인 mutation key를 둔다.
- Board callback은 `mutate()`가 아니라 `await mutateAsync()`를 사용해 `Promise<void>`를
  반환한다.
- callback 시점의 Task `revision`을 mutation 변수에 함께 고정한다. `mutationFn`은
  optimistic cache update 후 revision을 다시 읽지 않는다.
- `onMutate` context에는 전체 Workspace가 아니라 `previousTask`, 요청한
  `optimisticStatus`만 저장한다.
- optimistic cache update는 대상 Task에만 적용한다.
- `onError` rollback은 현재 cache의 대상 Task가 아직 같은 optimistic status이고 이전
  revision인 경우에만 대상 Task snapshot을 복원한다. 이미 더 최신 revision이 반영됐다면
  건드리지 않는다.
- `onSuccess`는 서버가 반환한 Task만 교체한다.
- App의 동기 counter로 status mutation 수를 추적하고 모두 settle되어 counter가 0이 된
  시점에 한 번 Workspace를 invalidate한다. 먼저 끝난 요청의 refetch가 아직 pending인
  다른 Task의 optimistic state를 덮지 않게 한다.

### Board 잠금

- callback type을 `Promise<void>`로 고정한다.
- state render 이전의 rapid duplicate event도 막도록 Task ID별 동기 lock을 둔다.
- select와 drag는 같은 `changeStatus` 경로와 같은 lock을 사용한다.
- lock과 `Saving…`은 Promise settle 후에만 해제한다.
- 실패 시 Board local optimistic override를 해당 Task만 삭제해 최종 refetch의 서버 기준
  상태가 보이게 하고 기존 inline alert를 쓴다.

### 타입 경계

다음 callback 선언을 같은 계약으로 맞춘다.

- `frontend/src/features/workspace/WorkspacePage.tsx`
- `frontend/src/features/workspace/views/types.ts`
- `frontend/src/features/workspace/views/BoardView.tsx`
- `frontend/src/types/workspace-views.d.ts`

## 4. 명시적 비범위

- backend/API/store/schema 변경
- offline queue, retry queue, idempotency framework
- DnD library 교체 또는 Board 재작성
- TaskDrawer mutation 통합
- undo history, animation, toast redesign
- cross-tab coordination
- 실제 runtime Task를 사용한 destructive QA

브라우저 mutation smoke는 disposable fixture data directory에서만 수행한다.

## 5. 테스트

### Board component

- deferred success 동안 해당 select/drag handle만 disabled이고 `Saving…` 유지
- 같은 Task rapid duplicate 요청은 callback 1회
- 실패 시 카드가 원래 column으로 복원되고 alert 표시
- 서로 다른 Task A/B 동시 변경에서 A 실패 후 B 성공 상태 유지
- 성공 후 새 parent Task revision/status와 local optimistic state가 수렴
- select와 drag가 같은 mutation function을 호출

### App integration

- callback이 PATCH 응답까지 resolve되지 않음
- PATCH body가 click 시점 revision을 사용
- 성공 응답의 revision 반영
- HTTP 409/실패 시 대상 Task만 복원
- A/B 동시 mutation에서 A rollback이 B 성공을 보존
- 마지막 mutation settle 뒤 Workspace refetch

### 전체

- frontend 전체 tests
- production build
- backend 전체 tests — backend 무변경 확인
- source/runtime export audit
- disposable fixture에서 Board 성공/실패 browser smoke
- Graph/Treemap/Focus/Inbox/OOB/Reply 회귀 확인

## 6. 실용 적대적 승인 기준

### Hard blocker

- Task 또는 Workspace cache를 손상/유실할 수 있음
- 실패가 다른 Task의 확정 성공을 되감음
- 같은 Task 중복 PATCH를 막지 못함
- 새 외부 side effect나 비가역 migration이 생김
- Graph/Board/Treemap/Focus/Inbox/OOB 핵심 flow가 깨짐

### 허용 가능한 기술부채

- cross-tab 또는 TaskDrawer와 Board의 동시 편집
- offline retry/undo 부재
- animation과 toast 중복
- drag gesture별 추가 browser matrix
- mutation helper 일반화 미실시

리뷰 결론은 `GO`, `GO_WITH_CUTS`, `STOP` 중 하나다. 위 hard blocker가 없으면 polish나
일반화 제안 때문에 구현을 막지 않는다.

## 7. Stop-loss

- 8시간을 넘기거나 backend/queue/DnD 재작성이 필요하면 중단한다.
- 먼저 자를 것은 animation, toast polish, cross-tab 대응이다.
- `mutateAsync`, Task 단위 조건부 rollback, 같은 Task 동기 lock, 실패·동시성 test는
  자르지 않는다. 이 중 하나라도 충족하지 못하면 이 증분은 출시하지 않는다.

## 8. 완료와 rollback

완료는 모든 필수 테스트와 disposable browser smoke가 통과하고 실용 적대적 리뷰가
hard blocker 없음으로 판정할 때다.

rollback은 Board 안정화 관련 hunk와 신규 test만 역패치한다. data migration이나 runtime
cleanup은 없다. Focus와 기존 OOB 변경은 보존한다.

## 9. 구현 및 검증 결과

- `mutateAsync()` 기반 Promise 계약, 요청 시작 시 revision 고정, Task 단위 조건부 rollback,
  전체 status mutation이 끝난 뒤 한 번만 refetch하는 동기 counter를 적용했다.
- Board의 select와 drag가 Task 단위 동기 lock 및 단일 변경 경로를 공유한다.
- 실패 시 이전 local status를 억지로 덮어쓰지 않고 override를 삭제하도록 했다. disposable
  브라우저 검증에서 실제 409 뒤 서버가 가진 최신 상태가 노출되는 것을 확인했다.
- frontend 전체 **16 files / 74 tests**, backend **67 tests + Windows symlink 권한 1 skip**,
  production build, UTF-8 source/runtime audit, `git diff --check`를 통과했다.
- disposable fixture에서 성공 시 `revision 1` 반영과 stale revision 409 후 authoritative
  resync를 검증했다. 개인 runtime 데이터에는 mutation smoke를 수행하지 않았다.
- 최종 독립 리뷰 2건 모두 `GO`, hard blocker 0으로 판정했다.

출시를 막지 않고 등록한 기술부채는 실제 drag gesture 자동화 확대, TaskDrawer/cross-tab
동시 편집, toast와 inline alert 중복, mutation counter/helper 일반화다.
