# Microsoft Handoff Activity & Attention 구현 계획

상태: **적대적 리뷰 REJECT — 구현 보류**
작성일: 2026-08-29
구현 시작 조건: 이 문서에 대한 독립 적대적 리뷰가 `APPROVE`일 것

> 2026-08-29 리뷰 결과: 실제 Gate 0/Reply data가 없는 상태에서 기존 Inbox와
> 중복되는 화면이 되며, 해소 상태가 없는 `attention` 의미도 안전하지 않다는
> blocker가 확인됐다. 제품 코드는 구현하지 않는다. 최소 한 provider의 실제
> Gate 0와 handoff dogfood 증거가 생긴 뒤 범위를 새로 작성해 재심사한다.

## 1. 결정

이 증분은 이전 우선순위 2위였던 `통합 상태·감사·재시도 UI`를 그대로
구현하지 않는다. 현재 Work Stack의 Microsoft 365 경로는 background connector가
아니라 사용자 매개 OOB agent handoff이기 때문이다.

이번에 구현할 것은 다음 한 가지다.

> Context Inbox 안에서 Outlook/Teams의 **로컬에 기록된 handoff 사실**과
> **안전한 후속 확인이 필요한 항목**을 한눈에 발견하는 읽기 전용 패널

제품명은 `Microsoft Handoff Activity & Attention`으로 고정한다. 이 화면은
provider의 연결 상태나 health를 추측하지 않는다. `Capture`, `ReplyCommand`,
`ReplyReceipt`, `Activity`, build-time Gate 0 flag에 이미 기록된 사실만 표시한다.

## 2. 왜 지금 필요한가

OOB prototype은 Task 안에서 승인된 reply와 receipt를 안전하게 다룰 수 있다.
하지만 reply 상태는 해당 Task drawer를 다시 열어야만 발견할 수 있다.

- `approved`: command가 승인됐지만 matching receipt가 아직 기록되지 않음
- `failed`: agent receipt가 실패를 기록함
- `unknown`: delivery가 불명확하며 자동 재전송이 금지됨
- `sent`: matching receipt가 전송 성공을 기록함

Task가 늘어나면 사용자는 어떤 Task를 다시 확인해야 하는지 기억해야 한다.
이 문제는 방금 조립한 Outlook/Teams 입출력 흐름의 실사용성을 직접 제한한다.

반대로 일반적인 integration dashboard는 아직 필요하지 않다.

- 실제 background sync, job, queue, worker가 없다.
- Outlook/Teams의 네 Gate 0 capability는 기본적으로 모두 비활성이다.
- build flag는 release evidence gate이지 runtime connection/health가 아니다.
- 외부 reply의 `failed`와 `unknown`은 retry 대상이 아니라 terminal local fact다.

따라서 지금의 최대 ROI 범위는 새 자동화 계층이 아니라 기존 사실을 안전하게
집계해 보여주는 것이다.

## 3. 후보 비교와 ROI 판단

| 후보 | 즉시 사용자 가치 | 구현/운영 비용 | 현재 판단 |
| --- | --- | --- | --- |
| 전체 integration health/retry center | 낮음. 표시할 실제 health/job이 없음 | 매우 높음 | 거절 |
| Microsoft Handoff Activity & Attention | 높음. 흩어진 handoff 상태를 발견 | 낮음. 읽기 전용 projection 하나 | **이번 증분** |
| Today/Focus | 높음 | 낮음~중간 | 다음 후보. daily ritual 확인 후 |
| Table + global search | 중간~높음 | 중간 | 30건 규모에서 기존 검색/필터 우선 |
| SQLite/FTS5 | 낮음 | 중간~높음 | 동시 writer/검색 규모 증거 후 |
| 자동 분류/Task 추천 | 중간 | 높음 | 실제 Microsoft data 평가 후 |
| Command palette | 중간 이하 | 낮음 | 기존 `Ctrl/Cmd+K`, `/`가 있어 polish |

이 결정은 “가장 많은 기능”이 아니라 “가장 적은 새 구조로 다음 실제 작업을
놓치지 않게 하는가”를 기준으로 한다.

## 4. 사용자 여정

### 4.1 기본 상태

Context Inbox에 compact한 Microsoft handoff panel을 표시한다.

- 고정 mode: `Manual agent handoff · no background sync`
- Outlook/Teams 각각의 read/reply **Gate 0 evidence 상태**
- provider별 sanitized Capture 수와 마지막 기록 시각
- provider별 reply outcome 수와 마지막 기록 시각
- 확인이 필요한 reply 목록
- 최근 sanitized handoff activity

Gate가 false일 때는 `Not verified` 또는 `Gate 0 pending`이라고 표시한다.
`Disconnected`라고도 표현하지 않는다. 연결 시도 자체를 관찰하지 않기 때문이다.

### 4.2 새 read handoff 시작

패널의 `New read request`는 기존 `MicrosoftOobDialog`를 연다. 별도의 request
작성기나 OobRequest 저장소를 만들지 않는다. 해당 provider의 read gate가 false면
기존과 동일하게 disabled 상태와 사유를 표시한다.

### 4.3 확인이 필요한 reply

각 항목은 `Open task`만 제공한다.

- `approved`: `Matching receipt not recorded`라고 표시한다. 전송 여부를 단정하지
  않으며 command를 자동 복사하지 않는다.
- `failed`: `Review the failed attempt and approve a new reply if needed`라고 표시한다.
- `unknown`: `Check the original Microsoft thread · do not resend automatically`라고
  표시한다.
- `sent`: attention 목록에는 넣지 않고 recent activity에만 표시한다.

패널에는 `Retry`, `Resume sending`, `Resend`, `Copy command` 버튼을 두지 않는다.
실제 body/target/approval context는 기존 Task drawer 안에서만 다룬다.

### 4.4 Capture 확인

최근 Capture activity의 `Open capture`는 기존 Capture drawer를 연다. linked/converted
Capture에서 Task가 명확한 경우에도 새 mutation을 실행하지 않고 기존 drawer 또는
Task drawer로 이동만 한다.

### 4.5 새로고침

사용자가 누르는 `Refresh recorded activity`만 제공한다. polling, focus interval,
background refresh, next-run ETA는 추가하지 않는다.

## 5. 데이터 계약

### 5.1 신규 read-only endpoint

```text
GET /api/v1/microsoft-handoffs
```

이 endpoint는 한 Store transaction 안에서 다음 세 파일을 읽는다.

- `captures.json`
- `replies.json`
- `activity.json`

쓰기, migration, 새 store는 없다.

### 5.2 응답 초안

```json
{
  "data": {
    "schema_version": "1.0",
    "mode": "manual_agent_handoff",
    "providers": [
      {
        "provider": "microsoft-outlook",
        "capture_counts": {
          "inbox": 0,
          "linked": 0,
          "converted": 0,
          "dismissed": 0
        },
        "reply_counts": {
          "approved": 0,
          "sent": 0,
          "failed": 0,
          "unknown": 0
        },
        "last_capture_at": null,
        "last_reply_at": null
      }
    ],
    "attention": [
      {
        "reply_id": "R-0001",
        "task_id": "T-0001",
        "capture_id": "C-0001",
        "provider": "microsoft-outlook",
        "state": "approved",
        "recorded_at": "2026-08-29T00:00:00Z"
      }
    ],
    "recent_activity": [
      {
        "event_id": "E-000001",
        "type": "capture.ingested",
        "capture_id": "C-0001",
        "task_id": null,
        "reply_id": null,
        "provider": "microsoft-outlook",
        "recorded_at": "2026-08-29T00:00:00Z"
      }
    ]
  }
}
```

### 5.3 정규화 규칙

- provider는 `microsoft-outlook`, `microsoft-teams` 두 값만 허용한다.
- 두 provider row는 data가 없어도 항상 같은 순서로 반환한다.
- count key는 모든 Capture/Reply state를 항상 포함한다.
- `last_capture_at`은 provider Capture의 최신 `updated_at`이다.
- `last_reply_at`은 provider ReplyCommand의 최신 `updated_at`이다.
- attention은 `approved`, `failed`, `unknown`만 최신순으로 최대 20건이다.
- recent activity는 `capture.*`, `reply.*`만 최신순으로 최대 50건이다.
- Activity에 provider가 없으면 capture/reply의 allowlisted provider를 역참조한다.
- 깨진 참조는 provider를 추측하지 않고 해당 activity를 제외한다.
- 저장 파일을 읽는 도중 schema 위반이 있으면 endpoint는 fail closed한다.

### 5.4 응답 allowlist

신규 endpoint가 반환할 수 있는 필드는 위 예시에 명시된 필드뿐이다.

반드시 제외한다.

- reply body
- reply target와 모든 locator
- body/target digest
- receipt 원문
- `remote_message_ref`, `web_url`
- error text 또는 connector diagnostics
- source title, summary, context, action item
- idempotency record와 request digest
- Activity의 arbitrary `details`
- recipient, raw message/chat, attachment, credential, token

전역 operational panel은 “어떤 내용이었는가”가 아니라 “어떤 안전한 로컬 상태가
기록됐는가”만 답한다.

## 6. Frontend 설계

### 6.1 배치

새 top-level surface나 sidebar item을 만들지 않는다. 패널은 Context Inbox의 heading과
기존 capture metrics 사이에 배치하거나, 데스크톱에서는 우측 compact region으로
배치한다. 모바일에서는 한 column로 접힌다.

### 6.2 컴포넌트 경계

신규:

```text
frontend/src/features/integrations/MicrosoftHandoffPanel.tsx
```

재사용:

- `Pill`, `Button`, `IconButton`, `ErrorState`
- existing timeline/list styles
- `MicrosoftOobDialog`
- `TaskDrawer`, `CaptureDrawer`
- React Query
- `providerGates`

추가 OSS dependency는 없다. Recharts, TanStack Table, Activepieces UI도 사용하지 않는다.

### 6.3 상태의 출처 분리

Frontend는 두 종류의 사실을 섞지 않는다.

| 표시 | 출처 | 의미 |
| --- | --- | --- |
| Gate 0 read/reply | `providerGates` | exact build가 해당 evidence gate를 통과했음 |
| Capture/Reply count와 시각 | 신규 endpoint | Work Stack에 local record가 존재함 |

Gate가 verified여도 `Connected`, `Healthy`, `Available now`라고 표현하지 않는다.
local record가 최근이어도 `Synced`라고 표현하지 않는다.

### 6.4 query와 invalidation

```text
query key: ['microsoft-handoffs']
```

다음 mutation 성공 후에만 invalidation한다.

- Capture import/update
- Capture link/convert/dismiss
- Capture에서 Task 생성
- ReplyCommand approval
- ReplyReceipt import

자동 refetch interval은 없다.

## 7. 예상 파일 변경

Backend/contract:

- `workstack/service.py`
- `workstack/server.py`
- `contracts/api-v1.md`
- `tests/test_api.py` 또는 전용 projection test

Frontend:

- `frontend/src/domain/types.ts`
- `frontend/src/domain/schemas.ts`
- `frontend/src/api/client.ts`
- `frontend/src/features/integrations/MicrosoftHandoffPanel.tsx`
- `frontend/src/features/integrations/MicrosoftHandoffPanel.test.tsx`
- `frontend/src/features/inbox/InboxPage.tsx`
- `frontend/src/app/App.tsx`
- 필요한 최소 CSS와 기존 integration test

예상 elapsed time은 병렬 agent 기준 8~14시간이다. 구현 중 새 persistence나 범용
integration framework가 필요해지면 stop-loss를 발동하고 계획 재심사로 돌아간다.

## 8. 구현 작업선

### Lane A — Backend projection

1. 필드 allowlist와 두 provider 정규화 helper를 먼저 작성한다.
2. 한 transaction 안에서 Capture/Reply/Activity를 읽는다.
3. count, last timestamp, attention, recent activity를 만든다.
4. cap과 deterministic ordering을 적용한다.
5. GET route를 추가한다.

### Lane B — Frontend panel

1. strict Zod schema와 API client를 추가한다.
2. 두 provider capability/evidence card를 만든다.
3. attention과 recent activity를 compact list로 만든다.
4. existing Task/Capture drawer로만 연결한다.
5. manual refresh와 truthful empty/error state를 만든다.

### Lane C — Security/contract/tests

1. sensitive field leakage negative test를 작성한다.
2. provider 역참조와 깨진 참조 fail-closed behavior를 검증한다.
3. `Retry`, `Resend`, health claim이 UI에 없음을 검증한다.
4. keyboard focus와 mobile layout을 검증한다.
5. source/runtime export audit와 release checklist를 갱신한다.

Lane A/B는 contract fixture가 고정된 뒤 병렬 진행하고 Lane C는 두 lane을 독립적으로
검증한다.

## 9. 테스트 계획

### Backend

- 두 provider가 data가 없어도 반환됨
- non-Microsoft Capture/Activity가 제외됨
- Capture/Reply state counts가 정확함
- attention state와 최신순/cap이 정확함
- recent activity가 allowlisted type만 포함함
- reply body/target/digest/receipt/remote ref가 직렬화되지 않음
- arbitrary Activity details가 직렬화되지 않음
- projection 호출 전후 저장 파일 byte가 동일함
- malformed provider/reference는 fail closed 또는 명시된 제외 규칙을 따름

### Frontend

- all-gates-false에서 `Gate 0 pending`과 manual mode가 표시됨
- verified flag는 capability evidence로만 표시되고 health 문구가 없음
- `approved`를 미전송으로 단정하지 않고 receipt 미기록으로 표시함
- `failed`, `unknown`에 Retry/Resend action이 없음
- `unknown`에 original thread 확인 경고가 있음
- `Open task`, `Open capture`, `New read request`, manual refresh가 기존 flow를 엶
- empty/loading/error state가 keyboard로 사용 가능함
- panel의 strict schema가 unknown/sensitive extra field를 거절함

### 전체 회귀

- backend 전체 test
- frontend 전체 test
- frontend production build
- source export audit
- 실제 runtime export audit
- 브라우저에서 Context Inbox 기본/empty/attention/mobile 흐름
- Graph/Board/Treemap/Task drawer/Capture ingest/Reply receipt 기존 흐름

## 10. 승인 기준

다음을 모두 만족해야 완료다.

1. 패널의 모든 값이 저장된 allowlisted fact 또는 exact build flag에 매핑된다.
2. provider connection/health/background sync를 암시하는 문구가 없다.
3. 외부 send를 retry/resume/resend/copy하는 전역 action이 없다.
4. `approved`는 matching receipt가 기록되지 않은 상태로만 설명된다.
5. `unknown`은 원문 확인을 요구하고 자동 재전송이 금지된다.
6. 새 store, migration, queue, worker, polling, dependency가 없다.
7. API가 reply body/target/source content/recipient/credential을 노출하지 않는다.
8. 기존 Task/Capture drawer와 OOB dialog를 재사용한다.
9. 전체 test/build/audit/browser 검증이 통과한다.
10. 별도 적대적 리뷰가 최종 구현도 `APPROVE`한다.

## 11. 강제 중단 조건

구현 중 다음 중 하나가 필요해지면 코드를 더 확장하지 않고 계획 재심사로 돌아간다.

- provider health를 얻기 위한 새 connector call
- request/job/attempt/retry persistence
- external send 재시도 API
- generic integration/action framework
- Activepieces 또는 background worker
- 새 database나 schema migration
- Gate 0 자동 활성화
- raw Microsoft content 또는 connector diagnostics 저장
- Today/Table/command palette/Tiptap 동시 구현

## 12. 비목표

- 실제 Outlook/Teams Gate 0 실행 또는 flag 활성화
- live connection 검사
- background capture/sync/polling
- OobRequest history 저장
- delivery exactly-once 보장
- failed/unknown 자동 reconciliation
- generic audit log viewer
- Integration/Automation 전용 top-level 화면
- Activity 상세 diff
- Today, Table, global search
- SQLite/FTS5
- Conduit/Taskroom

## 13. 후속 판단

이 증분이 완료된 뒤 실제 OOB handoff에서 다음을 기록한다.

- attention 항목을 통해 놓친 후속 처리를 발견했는가
- Task drawer까지 가는 동선이 충분한가
- provider별 실제 Gate 0 증거가 확보됐는가
- 20회 왕복 또는 5일 dogfood에서 background collection 필요가 반복됐는가

그 증거가 있을 때만 provider health, Activepieces, 자동 수집, retry orchestration을
다시 후보로 올린다. 그 전에는 `Today/Focus`를 다음 제품 가치 후보로 재평가한다.
