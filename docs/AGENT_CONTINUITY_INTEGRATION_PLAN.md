# Work Stack × Agent 연속성 통합 계획

- 상태: 구현 제안서
- 작성일: 2026-09-01
- 범위: 사람의 계획 상태와 에이전트 실행 사이의 검증 가능한 인수인계

## 1. 제품 목표

Work Stack은 범용 지식 저장소나 에이전트의 무제한 장기 기억이 아니라, 다음 역할을 맡는다.

> 사람이 관리하는 작업 상태를 제한된 범위로 에이전트에 전달하고, 에이전트의 실행 결과를 증거와 함께 안전하게 되돌려 받는 신뢰 계층.

사용자 경험의 목표는 단순하다.

> 사용자가 `T-1042 이어서 해줘`라고 말하면, 에이전트가 같은 Task를 정확히 식별하고 현재 상태, 이전 시도, 막힌 지점, 허용된 범위와 최신성 경고를 받은 뒤 재설명 없이 작업을 시작한다.

Task 번호만으로 모호성이 생기면 Work Stack이 활성 workspace를 우선 사용하고, 여전히 후보가 둘 이상이면 사용자에게 workspace를 한 번 선택하게 한다. 내부에서는 Task 번호만 신뢰하지 않는다.

## 2. 설계 원칙

1. **사람이 트리거한다.** 에이전트는 Work Stack 전체를 상시 구독하지 않는다. 사용자가 재개 또는 내보내기를 요청한 시점에 한정된 패킷을 생성한다.
2. **정체성은 전역적으로 명확해야 한다.** 모든 교환은 workspace profile ID, workspace UID, planning Task UID, legacy Task ID와 revision을 함께 사용한다.
3. **기존 계약을 조용히 확장하지 않는다.** 현재 `workstack.planning-task-snapshot.v1`은 그대로 유지하고, 더 풍부한 문맥은 별도의 `ResumePacket v1`에 담는다.
4. **내보낸 범위를 설명한다.** 포함 항목, 제외 항목, 잘린 항목, 생성 시각과 출처를 manifest로 명시한다.
5. **사실과 추론을 구분한다.** Work Stack 상태, Git 증거, 에이전트 추론과 사용자 확인 사항은 서로 다른 provenance로 기록한다.
6. **변경 경로는 위험도에 따라 나눈다.** 제한된 Task 필드 갱신은 revision guard를 통과한 경우 직접 적용할 수 있고, 조사 결과·불확실성·외부 증거는 review inbox를 거친다.
7. **자동화는 조언으로 시작한다.** 유사 Task, 구현 완료 추정, 충돌 징후는 제안하거나 경고할 뿐 자동 완료나 자동 병합을 하지 않는다.
8. **실패를 숨기지 않는다.** handoff 누락, stale context, revision 충돌, 불완전한 증거를 명시적인 상태로 보여준다.

## 3. 기존 기반과 유지할 경계

현재 제품에 이미 존재하는 기반은 재사용한다.

- planning Task snapshot의 canonical JSON, 크기 제한, digest, workspace/task UID와 revision 검증
- `agent apply`의 허용 필드 제한과 expected revision 검사
- Capture의 fingerprint, retrieved-at 최신성, 중복 및 충돌 처리
- work session의 실행 상태와 done/next/blockers 기록
- 로컬 및 Remote SSH workspace profile과 SSOT 동기화 경계

현재 snapshot v1이 의도적으로 제외하는 objectives, dependencies, subtasks, notes, tags는 v1에 추가하지 않는다. 필요한 관계 정보는 `ResumePacket v1`이 별도 스키마와 제한을 갖고 참조한다.

## 4. 핵심 계약

### 4.1 ResumePacket v1

재개 시점에 생성되는 읽기 전용, 일회성 패킷이다.

```text
identity
  packet_id
  workspace_profile_id
  workspace_uid
  planning_task_uid
  legacy_task_id
  task_revision

task
  immutable planning-task-snapshot.v1

scope
  selected relationships
  included notes/worklog/handoff references
  file and byte limits
  omissions and truncation reasons

continuity
  latest accepted handoff
  unresolved uncertainties
  blockers and next actions
  active advisory claim, if any

freshness
  generated_at
  source revisions and timestamps
  stale flags and reasons
  Git evidence summary, if explicitly connected

provenance
  authoritative Work Stack facts
  advisory external evidence
  agent-authored statements
```

규칙:

- 패킷은 canonical serialization과 digest를 가진다.
- 비밀값, 첨부파일 원문, 숨겨진 페이지 내용은 기본적으로 제외한다.
- 관계 탐색은 깊이, 항목 수, 전체 바이트 수를 제한한다.
- 오래된 context는 삭제하지 않고 stale reason과 함께 제공한다.
- 같은 legacy Task ID가 여러 workspace에 있으면 자동 선택하지 않는다.

### 4.2 Resume Briefing

ResumePacket을 사람이 읽기 쉬운 순서로 렌더링한 결정론적 briefing이다.

1. 지금 해야 할 일
2. 현재 상태와 revision
3. 마지막으로 확인된 진행 상황
4. 막힌 지점과 미해결 불확실성
5. 관련 관계와 코드 위치
6. stale 또는 동시 작업 경고
7. 추천 시작점
8. 포함·제외된 context manifest

초기 버전은 규칙 기반으로 생성한다. LLM 요약은 이후 선택 기능으로만 추가하며 원본 packet digest와 근거 링크를 항상 유지한다.

### 4.3 AgentResultPacket v1

에이전트가 실행을 마치거나 정상적으로 중단할 때 반환하는 패킷이다.

```text
identity
  result_id
  input_resume_packet_id
  input_resume_packet_digest
  workspace and task identity
  expected_task_revision

execution
  outcome: completed | partial | blocked | cancelled
  summary
  changed_files
  commands/tests and outcomes
  commit/PR references, when available

continuity
  done
  next
  blockers
  deliberately_not_done
  unresolved_uncertainties

requested_changes
  bounded Task field patch
  proposed completion/status change
  proposed relations/objective changes
  review inbox captures
```

반환 결과는 세 경로로 나눈다.

| 결과 종류 | 처리 |
| --- | --- |
| 허용된 일반 Task 필드의 제한적 수정 | expected revision 일치 시 직접 적용 |
| 조사 결과, 외부 자료, 불확실성, 긴 서술 | review inbox에 제안으로 적재 |
| 완료, 삭제, Objective·관계 변경, 충돌 해결 | 명시적 사용자 확인 후 적용 |

### 4.4 Handoff Receipt

정상 종료 또는 정상 중단에서는 AgentResultPacket을 수락한 뒤 receipt를 발행한다.

- result digest
- 적용된 변경과 review 대기 항목
- 새 Task revision
- handoff 저장 시각
- 거절 또는 충돌 항목과 이유

프로세스 강제 종료, 장치 종료, 네트워크 단절에서는 handoff 생성을 보장할 수 없다. 이 경우 다음 재개 화면에 `handoff missing` 또는 `session abandoned` 상태와 마지막 확인 시점을 표시한다. “항상 handoff가 남는다”는 거짓 보장을 하지 않는다.

## 5. 기능 설계

### 5.1 정확한 Task 재개

- 사용자는 Task 번호, 검색 결과 또는 현재 열린 Task에서 재개를 시작한다.
- Work Stack은 workspace profile과 SSOT identity를 먼저 확정한다.
- Task revision과 packet digest를 고정한 뒤 briefing을 만든다.
- 에이전트가 오래된 packet으로 변경을 시도하면 재생성 또는 수동 병합을 요구한다.

### 5.2 Stale Context 경고

단순히 “마지막 수정 후 N일”만 보지 않고 다음 신호를 조합한다.

- Task revision 또는 상태 변경
- Objective, dependency 또는 parent revision 변경
- 마지막 handoff 이후 worklog 변경
- 연결된 Git branch/commit의 이동
- Remote SSH workspace의 SSOT generation 변경

경고는 `fresh`, `possibly stale`, `stale`, `conflicted`로 구분하고 이유를 표시한다.

### 5.3 구조화된 불확실성

에이전트가 추측한 내용을 자유 텍스트 안에 숨기지 않고 다음 형태로 남긴다.

- 질문 또는 불확실한 주장
- 추측이 필요했던 이유
- 사용한 임시 가정
- 영향받는 Task/파일/결정
- 해결 상태와 확인자

미해결 항목은 다음 Resume Briefing에 포함하되, 사용자가 해소한 항목은 history로 보존하고 기본 화면에서는 접는다.

### 5.4 동시 작업 안내

분산 잠금 대신 advisory Task claim을 사용한다.

- claim owner, session ID, 시작 시각, heartbeat, TTL을 기록한다.
- 살아 있는 claim이 있으면 재개 전에 경고하고 읽기 전용 보기, 공동 진행, 인계 요청 중 하나를 선택하게 한다.
- heartbeat가 끊긴 claim은 자동 만료하지만 기록은 남긴다.
- claim은 SSOT 수정의 유일한 안전장치가 아니다. 실제 쓰기는 계속 revision guard와 동기화 충돌 검사를 통과해야 한다.

### 5.5 Git 증거 연결

명시적으로 연결된 repository에 한해 commit, branch, PR, 변경 파일과 테스트 결과를 수집한다.

- “이미 구현되었을 가능성”을 제시할 수 있다.
- Task 자동 완료는 하지 않는다.
- 증거가 가리키는 repository, revision, 수집 시각과 매칭 이유를 표시한다.
- 제목 유사도만으로 완료 가능성을 높게 판단하지 않는다.

### 5.6 유사 과거 Task 탐색

초기 구현은 결정론적 신호를 사용한다.

- 같은 파일 또는 디렉터리
- 같은 Objective, dependency, parent
- 동일한 에러 fingerprint
- 공통 tag와 source reference
- 같은 component 또는 repository

벡터 검색은 실제 검색 실패 사례와 평가 데이터가 쌓인 뒤 별도 실험으로 추가한다. 벡터 결과 역시 근거를 보여주고 자동 연결하지 않는다.

## 6. 멀티 워크스페이스와 Remote SSH

Work Stack은 여러 로컬·Remote SSH SSOT를 하나의 화면에 투영할 수 있지만 각 저장소의 권위 경계를 섞지 않는다.

- 각 workspace profile은 별도 profile ID, SSOT workspace UID, 연결 방식, 경로와 generation을 가진다.
- Task의 표시 ID가 같아도 내부 UID와 workspace UID가 다르면 다른 Task다.
- 통합 검색과 Focus view는 projection이며, 원본 수정은 해당 workspace의 동기화·revision 규칙을 따른다.
- Remote SSH 변경 감지 후 pull 후보와 local 미적용 변경을 비교하고, 자동 덮어쓰기 대신 충돌 상태를 만든다.
- ResumePacket은 어느 workspace generation에서 생성됐는지 기록한다.
- 연결이 끊기면 마지막 cache를 읽기 전용으로 보여줄 수 있지만 변경 적용은 재검증 전까지 막는다.

## 7. UX 흐름

### 재개

1. 사용자가 `T-1042 재개`를 누른다.
2. 중복 ID가 없으면 바로 briefing이 열리고, 있으면 workspace 선택기가 한 번 표시된다.
3. stale, missing handoff, active claim이 있으면 시작 전에 짧은 경고가 보인다.
4. 사용자가 에이전트 실행을 승인하면 packet digest와 함께 세션이 시작된다.

### 종료 및 인수인계

1. 에이전트가 done/next/blockers/uncertainties와 증거를 제출한다.
2. Work Stack이 revision과 packet digest를 검증한다.
3. 안전한 필드만 적용하고 나머지는 review inbox에 분리한다.
4. 사용자는 변경 요약과 handoff receipt를 확인한다.
5. 다음 재개에서는 승인된 최신 handoff가 briefing의 중심이 된다.

### 비정상 종료

1. claim heartbeat 또는 세션 종료 이벤트가 사라진다.
2. Work Stack은 세션을 `abandoned` 후보로 표시한다.
3. 다음 재개에서 마지막 확인된 상태와 handoff 누락을 알린다.
4. 사용자가 복구, 폐기 또는 새 세션 시작을 선택한다.

## 8. 구현 단계와 완료 조건

### Phase 0 — 계약 고정과 평가 fixture

- ResumePacket v1, AgentResultPacket v1, receipt JSON Schema 작성
- canonical bytes, digest, 크기·깊이·개수 제한 정의
- identity ambiguity, stale packet, revision conflict, interrupted handoff fixture 작성

완료 조건: 스키마 golden test, invalid packet corpus, backward compatibility test가 CI에서 통과한다.

### Phase 1 — ResumePacket과 결정론적 Briefing

- 기존 snapshot v1을 내장하는 packet builder 구현
- notes/worklog/relations를 제한된 scope로 수집
- manifest와 provenance 렌더링
- CLI/API/UI에서 명시적 export 및 재개 진입점 제공

완료 조건: 동일 상태에서 동일 digest가 생성되고, 포함·제외·절단 항목을 UI와 CLI 모두 설명한다.

### Phase 2 — AgentResultPacket과 Handoff Receipt

- 결과 검증, idempotency, expected revision 처리
- 위험도별 direct apply/review/confirm 라우팅
- 정상 종료 receipt와 비정상 종료 missing-handoff 상태 구현

완료 조건: 응답 유실, 중복 제출, stale revision, 부분 적용에서 데이터가 이중 반영되거나 조용히 손실되지 않는다.

### Phase 3 — Freshness와 구조화된 불확실성

- Task·관계·worklog·workspace generation의 freshness 계산
- uncertainty 생성, 해결, history와 briefing 연동

완료 조건: 경고마다 기계 판독 가능한 reason과 사용자가 이해할 수 있는 설명이 함께 존재한다.

### Phase 4 — Advisory Task Claim

- claim 생성, heartbeat, TTL, release, takeover audit 구현
- 로컬·Remote SSH의 단절 및 재연결 시나리오 테스트

완료 조건: claim은 협업 경고를 제공하되 stale claim이 작업을 영구 차단하지 않고, revision guard를 우회하지 못한다.

### Phase 5 — Git Evidence Adapter

- repository 연결과 명시적 권한 설정
- commit/PR/test evidence 수집 및 provenance 표시
- 구현 완료 가능성 경고

완료 조건: 증거가 없는 경우 `unknown`으로 남고, Work Stack이 Task를 자동 완료하지 않는다.

### Phase 6 — 결정론적 유사 Task 검색

- 파일, error fingerprint, graph 관계 기반 후보 생성
- 매칭 이유와 confidence band 표시
- 사용자의 유용/무용 피드백 수집

완료 조건: 고정 evaluation set에서 precision 기준을 통과하고, 결과가 없어도 재개 흐름이 저하되지 않는다.

### Phase 7 — 선택적 의미 검색 실험

- 충분한 실제 평가 데이터가 있을 때만 벡터 index를 실험한다.
- local-only index, rebuild, deletion propagation과 privacy 경계를 검증한다.

완료 조건: 결정론적 baseline보다 유의미하게 개선되고 오탐·운영비용이 수용 가능한 경우에만 기본 제품에 포함한다.

## 9. Release Regression Flow

각 release는 다음 검사를 통과해야 한다.

1. schema compatibility와 canonical digest golden tests
2. packet size/depth/count 및 malicious input tests
3. workspace/task identity ambiguity tests
4. stale revision, duplicate submit, lost response와 idempotency tests
5. local/Remote SSH generation drift와 reconnect tests
6. normal, cancelled, crashed session handoff tests
7. direct apply/review/confirmation routing tests
8. privacy omission과 redaction tests
9. UI keyboard, screen reader, light/dark theme tests
10. installer upgrade/rollback 및 이전 packet 읽기 tests

Release artifact에는 지원하는 packet schema 버전, migration 정책, 알려진 제한과 검사 결과 요약을 포함한다.

## 10. 성공 지표

- 재개 후 첫 유효 작업까지 걸린 시간
- 사용자가 다시 설명해야 했던 횟수
- stale packet이 변경 전에 차단된 비율
- 정상 종료 세션의 handoff receipt 생성률
- 비정상 종료가 누락 상태로 정확히 표시된 비율
- agent result의 direct apply/review/거절 비율
- revision 충돌에서 조용한 데이터 손실 0건
- 유사 Task 제안 precision과 사용자 채택률

## 11. 비목표

- Work Stack 전체를 에이전트가 상시 크롤링하는 구조
- Task 번호만으로 workspace 경계를 무시하는 전역 쓰기
- Git 흔적만으로 Task를 자동 완료하는 기능
- 모든 agent output을 무조건 직접 적용하거나 모두 수동 review로 보내는 단일 정책
- 분산 잠금 서비스 수준의 강한 동시성 보장
- 초기 release부터 벡터 DB를 필수 인프라로 채택하는 것
- 비정상 종료에서도 handoff가 반드시 생성된다고 보장하는 것
- Obsidian/Notion의 범용 문서 편집 경험을 그대로 복제하는 것

## 12. 계획 완료 후의 Work Stack

계획이 완료되면 Work Stack은 단순한 Task 목록이나 “에이전트용 second brain”이 아니다. 여러 로컬 및 Remote SSH workspace에 흩어진 작업을 사람 중심으로 통합해 보여주면서, 각각의 SSOT 권위와 revision을 보존하는 실행 관제면이 된다.

사용자는 아침에 Focus에서 `T-1042`를 연다. Work Stack은 이 Task가 어느 회사 SSH workspace의 어떤 planning Task UID인지 정확히 확정한다. 전날 handoff, 미해결 질문, 관계된 dependency와 최근 변경을 짧은 briefing으로 보여주고, 밤사이 Objective와 branch가 바뀌었다면 무엇이 stale한지 설명한다. 다른 에이전트가 작업 중이라면 소유자와 claim 만료 시각도 보인다.

사용자가 “계속”을 누르면 에이전트는 Work Stack 전체가 아니라 이 Task에 필요한 제한된 packet만 받는다. 무엇이 포함됐고 무엇이 빠졌는지 알며, 어떤 revision을 기준으로 일하는지도 안다. 작업 중 발견한 추측은 uncertainty로, 코드 변경과 테스트는 evidence로, 다음 작업은 handoff로 구조화한다.

작업이 끝나면 안전한 Task 필드 수정은 revision이 맞을 때만 반영된다. 조사 메모와 외부 자료는 review inbox에 들어가고, 완료 처리나 관계 변경은 사용자 확인을 기다린다. 네트워크가 끊기거나 프로세스가 죽으면 Work Stack은 성공한 척하지 않고 마지막 확인 지점과 handoff 누락을 보여준다.

다음 사람이나 다음 에이전트는 같은 Task를 다시 열어도 처음부터 맥락을 재구성하지 않는다. 동시에, 과거 에이전트의 설명을 맹신하지도 않는다. Work Stack의 사실, Git에서 관찰한 증거, 에이전트의 판단과 사람의 승인이 구분되어 있기 때문이다.

결과적으로 Work Stack이 제공하는 핵심 가치는 “기억을 많이 저장하는 것”이 아니라 다음 세 가지가 된다.

1. **정확한 재개** — 어떤 workspace의 어떤 Task를 어느 revision부터 이어가는지 명확하다.
2. **안전한 왕복** — 제한된 context가 나가고, 증거와 revision을 갖춘 결과만 위험도에 맞게 돌아온다.
3. **검증 가능한 연속성** — 세션, 사람, 장치와 에이전트가 바뀌어도 무엇을 알고 무엇을 모르는지가 보존된다.

제품 문장으로 요약하면 다음과 같다.

> Work Stack은 사람의 계획과 에이전트의 실행 사이에서, 제한되고 검토 가능하며 다시 시작할 수 있는 작업 문맥을 운반하는 신뢰 계층이다.

