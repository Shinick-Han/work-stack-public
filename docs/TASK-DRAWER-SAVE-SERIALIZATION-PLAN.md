# TaskDrawer 저장 직렬화 구현 계획

상태: **IMPLEMENTED — 최종 독립 실용 적대적 리뷰 2건 GO**
작성일: 2026-08-29
예상 범위: frontend 한정 4~8시간

## 1. 왜 지금 하는가

현재 `TaskDrawer`는 저장 중이면 뒤이어 들어온 `save()`를 아무 표시 없이 버린다. 대부분의
입력은 `Saving…` 동안 disabled지만 Tags와 Dependencies는 예외이고, blur와 다른 control의
change가 같은 interaction에 겹치면 React render 전에 두 저장이 같은 revision으로 시작할
수도 있다.

첫 요청의 응답은 draft를 서버 값으로 덮고 `Saved`를 표시한다. 따라서 사용자가 방금 입력한
값이 사라졌는데도 저장 성공처럼 보일 수 있다. 이는 polish가 아니라 핵심 Task 편집 flow의
입력 유실과 거짓 성공 상태다.

## 2. 목표

1. 한 Task에는 PATCH를 최대 하나만 in-flight로 둔다.
2. 저장 중 들어온 변경은 버리지 않고 field별 last-write-wins patch로 합친다.
3. 다음 PATCH는 직전 성공 응답의 새 revision을 사용한다.
4. in-flight와 대기 patch가 모두 끝나기 전에는 `Saved`를 표시하지 않는다.
5. 성공 응답을 Task detail과 Workspace cache에 같은 Task 값으로 반영한다.
6. 실패나 409에서는 자동 재전송하지 않고 `Not saved`를 표시한다. 서버 최신값을 다시 읽되
   사용자의 미저장 의도는 화면에 남겨 검토 후 명시적으로 재시도할 수 있게 한다.
7. 늦은 응답이 더 높은 revision의 Task cache를 되감거나 현재 Drawer의 draft를 덮지 않는다.

## 3. 최소 구현 범위

### 동기 저장 gate와 직렬 queue

- `saveState` render 값 대신 ref 기반 gate를 사용해 같은 tick의 중복 event도 즉시 직렬화한다.
- `queuedPatch`는 새 patch를 shallow merge한다. 같은 field는 가장 마지막 값이 이긴다.
- drain loop는 한 번에 한 PATCH만 보내고, 성공 응답 Task를 다음 요청의 revision 기준으로 쓴다.
- queue가 빌 때까지 `Saving…`을 유지한다.

### draft와 cache 수렴

- 각 성공 응답은 해당 Task의 detail cache와 Workspace cache만 갱신한다.
- cache에 이미 더 높은 revision이 있으면 늦게 도착한 낮은 revision 응답은 무시한다.
- 다음 patch가 대기 중이면 성공 응답 위에 대기 field를 다시 얹어 draft가 뒤로 돌아가지 않게
  한다.
- Tags와 Dependencies를 포함한 모든 editable control에 같은 pending 정책을 적용한다.
- 마지막 성공 뒤 Task detail과 Workspace를 한 번씩 invalidate하여 서버 Activity, parent,
  dependency, objective projection을 동기화한 후 `Saved`를 표시한다.

### 실패와 Task 전환

- 실패한 patch와 아직 대기 중인 patch를 `{ ...failedPatch, ...queuedPatch }` 순서로 합쳐
  마지막 field 값이 이기게 보존하고 drain을 중단한다.
- 409 및 일반 네트워크 실패 모두 자동 retry하지 않는다. authoritative detail을 refetch하고,
  성공하면 최신 Task 위에 미저장 patch를 다시 얹어 사용자가 차이를 검토할 수 있게 한다.
- inline error에 명시적 `Retry save`를 제공한다. 재시도 직전 이미 서버와 같은 field는 제거하고,
  남은 field만 최신 revision으로 다시 queue에 넣는다.
- App의 기존 `key={taskId}` unmount 경계와 mounted guard를 사용한다. 늦은 응답은 revision이
  더 최신일 때만 원래 Task cache에 반영하고, unmount된 Drawer state는 갱신하지 않는다.
- 저장 중에는 Drawer close를 비활성화한다. Drawer를 강제로 전환하거나 앱을 종료한 뒤의
  실패 draft를 영속 복구하는 전역 저장소는 이번 범위에 넣지 않는다.

## 4. 명시적 비범위

- backend/API/schema 변경
- offline queue, background sync, generic autosave framework
- 자동 409 merge 또는 네트워크 실패 자동 retry
- undo history, form library 도입, Drawer UI 재설계
- Board mutation helper와의 일반화
- cross-tab 동시 편집의 완전한 해결
- unmount/app 종료를 넘는 미저장 draft persistence

## 5. 테스트

### Component/state machine

- 첫 요청을 deferred한 상태에서 같은 interaction의 두 번째 field 변경이 사라지지 않음
- 동시 PATCH 최대 1개, 두 번째 PATCH는 첫 응답 revision 사용
- 대기 patch가 남아 있는 동안 `Saved`가 나타나지 않음
- Tags/Dependencies를 포함한 모든 editable control이 pending 중 disabled
- 마지막 성공 후 draft, detail cache, Workspace cache가 같은 최종 Task로 수렴
- 여러 PATCH 뒤 detail/Activity refetch는 마지막에 1회
- 409/네트워크 실패 시 `Not saved`, 자동 추가 PATCH 없음, authoritative refetch 수행
- 실패 뒤 최신 Task 위에 미저장 값이 남고 명시적 retry만 새 PATCH를 보냄
- 더 높은 revision의 detail/Workspace cache가 늦은 낮은 revision 응답으로 내려가지 않음
- dirty draft 중 detail refetch/cache update가 title, tags, dependencies의 미저장 값을
  덮지 않음

### 전체 검증

- frontend 전체 tests와 production build
- backend 전체 tests — backend 무변경 확인
- UTF-8 source/runtime audit와 `git diff --check`
- disposable fixture에서 두 field 연속 저장과 409 browser smoke
- Task drawer 진입과 인접 Workspace/Board/Focus 핵심 smoke

## 6. 실용 적대적 승인 기준

### Hard blocker

- 사용자 입력이 조용히 버려지거나 저장되지 않은 값에 `Saved`가 표시됨
- 한 Task에 두 PATCH가 동시에 전송되거나 다음 요청이 이전 revision을 재사용함
- 실패가 다른 Task 또는 이미 확정된 cache 값을 되감음
- 늦은 낮은 revision 응답이 최신 cache를 되감거나 dirty hydration이 미저장 값을 덮음
- 새 외부 side effect, 데이터 migration, 핵심 화면 회귀가 생김

### 허용 가능한 기술부채

- retry UI의 시각적 polish
- field-level dirty badge와 undo 부재
- cross-tab coordination 부재
- queue/helper 일반화 미실시
- 모든 브라우저의 gesture 조합 자동화 부재
- 강제 Task 전환/app 종료를 넘는 미저장 draft 복구 부재

리뷰 결론은 `GO`, `GO_WITH_CUTS`, `STOP` 중 하나다. hard blocker가 없다면 구조 일반화나
polish 제안은 구현을 막지 않고 기술부채로 남긴다.

## 7. Stop-loss

- 8시간을 넘기거나 backend batch endpoint, 새 form/state library, 범용 mutation engine이
  필요해지면 중단한다.
- 먼저 자를 것은 retry button polish, 세밀한 dirty badge, 광범위 browser matrix다.
- `single in-flight`, `no dropped patch`, `returned revision chain`, `truthful Saved`, 실패 시
  자동 retry 금지와 검증 test는 자르지 않는다.

## 8. 완료와 rollback

필수 테스트와 disposable smoke가 통과하고 독립 실용 적대적 리뷰가 hard blocker 없음으로
판정하면 완료다. rollback은 `TaskDrawer`와 신규/수정 test의 관련 hunk만 역패치한다.
backend나 data migration rollback은 없다.

## 9. 구현 및 검증 결과

- ref 기반 single-flight gate, field별 last-write-wins queue, 성공 응답 revision chaining을
  구현했다. 같은 field를 요청 중 원래 값으로 되돌리는 경우도 보상 PATCH가 누락되지 않는다.
- dirty field만 authoritative Task 위에 다시 얹으므로 conflict refetch가 title, tags,
  dependencies를 지우지 않으면서 다른 최신 field는 즉시 반영한다.
- 409/네트워크 실패는 `Not saved`와 명시적 `Retry save`로 멈춘다. 서버에 이미 반영된 field와
  사용자가 authoritative 값으로 되돌린 field는 retry 전에 제거되어 중복 PATCH가 나가지 않는다.
- detail/Workspace cache는 낮은 revision 응답을 무시하며, queue와 dirty field가 모두 해소된
  뒤 최종 resync를 거쳐야만 `Saved`가 보인다.
- 최종 리뷰 중 React StrictMode effect 재실행이 run을 detached 상태에 남기는 hard blocker를
  발견해 setup/cleanup을 대칭화했고 전용 회귀 test로 고정했다.
- TaskDrawer 집중 **10/10**, frontend 전체 **16 files / 81 tests**, backend **67 tests +
  Windows symlink 권한 1 skip**, production build, source/runtime audit, `git diff --check`를 통과했다.
- disposable browser에서 정상 저장의 detail/Workspace revision 2 수렴과, 외부 bump revision 3
  뒤 409 → dirty 보존/non-dirty rebase → 명시 retry revision 4 수렴을 확인했다. 브라우저 console
  error는 없었고 임시 runtime은 제거했다.
- 서로 독립적인 최종 리뷰 2건 모두 `GO`, hard blocker 0으로 판정했다.

허용한 기술부채는 빈 title의 별도 validation 안내, 강제 Task 전환·앱 종료를 넘는 draft
persistence, 모든 실제 브라우저 blur/gesture 조합 자동화다.
