# Quick Add 생성 후 즉시 이어서 편집 계획

상태: **IMPLEMENTED — 최종 실용 적대적 재검토 GO, hard blocker 0건**
작성일: 2026-08-29
예상 범위: frontend 한정 2~4시간

## 1. 왜 지금 하는가

Quick Add는 `Ctrl/Cmd+K`, Workspace, Focus에서 반복 노출되는 핵심 진입점이지만 지금은 생성
응답의 Task ID를 버리고 `Task created` 알림만 보여준다. 사용자는 방금 만든 일을 다시 검색하고
열어야 한다. 반면 Capture → Task 흐름은 생성 직후 기존 Task Drawer를 여는 패턴이 이미 있다.

한 번의 작은 변경으로 모든 Quick Add 진입점의 후속 클릭·검색을 없앨 수 있으므로 다음 증분으로
선택한다.

## 2. 목표와 성공 기준

1. 성공 응답을 기존 `taskMutationSchema`로 검증한 `Task`로 반환한다.
2. 성공 직후 생성된 ID의 기존 Task Drawer를 연다.
3. Workspace와 Focus에서는 현재 surface를 유지한다.
4. Inbox에서 단축키로 생성했다면 URL 규칙에 맞춰 Workspace로 이동해 Drawer를 연다.
5. 생성 요청은 사용자 제출당 정확히 한 번만 전송한다.
6. 추가 검색·클릭 0회로 생성된 Task의 상세 편집을 시작할 수 있다.

## 3. 최소 구현 범위

### API 경계

- `api.createTask()`의 반환형을 `Promise<Task>`로 바꾼다.
- legacy `POST /api/tasks`의 raw JSON 응답을 기존 `taskMutationSchema`로 검증한다.
- 서버 endpoint, payload, CSRF 재발급 동작은 바꾸지 않는다.

### 생성 후 이동

- 검증된 POST 응답을 생성 성공의 경계로 삼고 즉시 후속 UI를 진행한다.
- Workspace query 갱신은 비차단 background 작업으로 실행한다. 이 refetch가 실패해도 이미 commit된
  생성을 mutation error로 바꾸거나 dialog를 다시 열지 않는다.
- Quick Add dialog를 닫고 생성된 `taskId`로 Drawer를 연다.
- Workspace/Focus는 현재 surface와 view/filter를 보존한다.
- Inbox는 `surface=workspace`, `captureId=null`, `taskId=created.id`로 전환한다.
- 성공 알림에는 생성된 Task ID를 포함한다.

### 실패

- HTTP 실패에는 Drawer를 열지 않고 기존 dialog error를 표시한다.
- 2xx 뒤 Task schema 검증만 실패하면 이미 commit됐을 가능성이 있는 별도 오류로 분류한다.
  성공 알림/Drawer는 열지 않되 dialog를 닫고 Workspace로 이동해 background refresh하며,
  `Task may have been created` 경고를 표시한다. 같은 열린 폼에서 즉시 재POST할 수 없게 한다.
- 자동 retry나 자동 재POST는 추가하지 않는다.
- pending 동안 기존 submit/close 비활성화를 유지한다.

### 단일 제출과 draft 보호

- render의 `pending`보다 먼저 닫히는 동기 ref gate를 두어 같은 tick의 Enter/click 중복 제출도
  한 번만 `onSubmit`한다.
- pending 동안 title, detail, priority, due, objective, tags 입력을 모두 잠가 제출 뒤 바뀐 값이
  저장된 것처럼 보였다가 사라지는 일을 막는다.
- generic controlled `Dialog`의 native `cancel` 기본 동작을 `preventDefault()`한 뒤 `onClose`로
  위임한다. 부모가 pending close를 거부하면 Escape 뒤에도 dialog와 draft가 그대로 보인다.

## 4. 명시적 비범위

- Quick Add 폼 재설계 또는 새 필드
- objective 자동 추론, AI 분류, optimistic append
- backend/API v1/idempotency migration
- `Create another`, inline Focus 편집, Table/global search
- 일반화된 post-create navigation framework
- 응답 유실 뒤 서버 생성 여부를 판별하는 복구 프로토콜

마지막 항목은 로컬 legacy POST의 기존 기술부채다. 이번 변경은 자동 재전송을 만들지 않으며,
이를 해결하려고 backend idempotency migration까지 확장하지 않는다.

## 5. 검증

### API test

- raw Task 응답을 `Task`로 반환한다.
- malformed 응답을 거부한다.
- 2xx+malformed 응답은 일반 HTTP 실패와 구분 가능한 commit-unknown 오류가 된다.
- 정상 제출의 `POST /api/tasks` 횟수는 1회다.

### App integration test

- Workspace에서 생성하면 dialog가 닫히고 생성 ID의 Drawer/URL이 열린다.
- Focus에서 생성하면 Focus surface를 유지한다.
- Inbox 단축키에서 생성하면 Workspace로 이동하고 Capture drawer state를 제거한다.
- Workspace projection은 생성 뒤 한 번 다시 읽힌다.
- POST 성공 뒤 Workspace refetch가 실패해도 성공 알림과 생성 ID Drawer를 유지한다.
- 실패에는 Drawer를 열지 않고 dialog에 오류가 남는다.
- 2xx+malformed 응답에는 POST 1회, Drawer/성공 알림 없음, dialog 닫힘, Workspace refresh와
  생성 여부 경고를 보장한다.
- deferred 요청 중 double-submit에도 POST는 1회이고 모든 필드가 disabled다.
- pending 중 native cancel/Escape 후에도 dialog와 입력 draft가 남는다.

### 전체 검증

- frontend 전체 tests와 production build
- backend 전체 tests — backend 무변경 확인
- UTF-8 source/runtime audit와 `git diff --check`
- disposable fixture browser smoke: Workspace 생성 1건과 Inbox 생성 1건

## 6. 실용 적대적 승인 기준

### Hard blocker

- 한 번의 제출이 Task를 중복 생성함
- 잘못된 Task ID 또는 허용되지 않는 Inbox+Task URL 상태를 엶
- 기존 Workspace/Focus/Inbox 탐색 또는 Task Drawer를 깨뜨림
- malformed/실패 응답을 성공으로 표시함
- 새 보안·개인정보 누출, 데이터 유실, 승인되지 않은 외부 side effect가 생김

### 허용 가능한 기술부채

- legacy POST의 네트워크 응답 유실 복구/idempotency 부재
- 생성 직후 Workspace refetch 동안 짧은 로딩
- Quick Add 폼의 현재 UX와 validation copy
- 모든 브라우저 단축키 조합의 자동화 부재
- 공통 post-create helper 미추출

리뷰 결론은 `GO`, `GO_WITH_CUTS`, `STOP` 중 하나다. Hard blocker가 없다면 위 기술부채나
구조 개선 제안은 구현을 막지 않는다.

## 7. Stop-loss

- 4시간을 넘기거나 backend endpoint, idempotency 저장소, 새 form/navigation framework가
  필요해지면 이번 증분에서는 중단한다.
- 계획 범위를 넘는 문제는 문서화하되 생성 → 즉시 편집의 핵심 흐름을 먼저 출시한다.

## 8. 1차 리뷰 반영

결론: `GO_WITH_CUTS`

- 반영: POST 성공과 background refetch 실패를 분리한다.
- 반영: 동기 submit gate와 pending 전체 field lock을 추가한다.
- 반영: controlled Dialog가 native cancel로 React 상태와 분리되지 않게 한다.
- 부채 유지: `POST /api/v1/tasks`와 backend idempotency migration. 자동 retry를 추가하지 않고
  이번 증분의 2~4시간 stop-loss를 지킨다.

## 9. 2차 리뷰 반영

결론: `GO_WITH_CUTS`

- 반영: HTTP 실패와 2xx+schema 불일치를 분리한다.
- 반영: 후자를 commit-unknown으로 처리해 성공 UI 없이 Workspace 확인 흐름으로 이동하고
  같은 열린 폼의 즉시 재제출을 막는다.
- 부채 유지: 실제 네트워크 응답 유실의 서버 commit 여부 판별은 backend idempotency/API v1
  이관 전까지 완전히 해결하지 않는다.

## 10. 구현 및 최종 검증 결과

- `api.createTask()`가 legacy 성공 응답을 기존 Task schema로 검증해 반환한다.
- Workspace/Focus에서는 현재 surface를 유지하고 생성된 Task Drawer를 즉시 연다.
- Inbox에서는 Capture state를 제거하고 Workspace의 생성 Task Drawer로 이동한다.
- background Workspace refresh 실패는 이미 완료된 생성을 실패로 되돌리지 않는다.
- 2xx+schema 불일치는 commit-unknown으로 분리해 성공 UI와 즉시 재제출을 막는다.
- 동기 submit gate, pending field lock, controlled dialog cancel 경계를 구현했다.

최초 최종 리뷰에서는 submit 직후 TanStack pending render가 반영되기 전 Cancel/Escape가 draft를
지울 수 있는 경쟁 조건 1건이 `STOP` blocker로 발견됐다. 같은 동기 gate를 close, Cancel, Escape,
backdrop, 닫기 버튼과 모든 field change에도 공유하고, deferred POST에서 같은 turn의 submit→cancel→
HTTP 400 순서를 재현하는 App 회귀 테스트를 추가했다. 수정 뒤 독립 재검토는 `GO`였고 새 hard
blocker는 발견되지 않았다.

검증 결과:

- Quick Add 관련 테스트: **18/18 통과**
- frontend 전체: **18 test files / 92 tests 통과**
- backend 전체: **67 tests 통과 / Windows symlink 권한 1건 skip**
- production build: 통과
- source/runtime export audit와 `git diff --check`: 통과
- disposable browser QA: Workspace `T-0031`, Inbox `T-0032`를 각각 정확히 1건 생성하고 올바른
  Drawer/deep link를 열었으며 console error는 0건

출시를 막지 않는 잔여 부채는 legacy `POST /api/tasks`의 서버 idempotency와 실제 network response
loss 복구 부재다. 자동 retry는 추가하지 않았다.
