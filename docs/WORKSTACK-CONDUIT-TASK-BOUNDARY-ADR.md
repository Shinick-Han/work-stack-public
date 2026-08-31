# ADR: Work Stack PlanningTask와 Conduit Task 경계

- 상태: Accepted for prototype
- 날짜: 2026-08-29
- 적용 범위: Work Stack 72-hour prototype 및 향후 Conduit adapter

## Context

Work Stack의 기존 `Task`는 개인이 목표에 맞춰 해야 할 일을 계획하고 우선순위를
정하는 항목이다. Conduit의 canonical `Task`는 `CONDUIT_CONCEPTS.md`가 정의한
durable unit of intended work이며 goal, scope, risk, data requirements,
responsibility continuity, execution lifecycle, Runs, Outcomes, Evidence와 human
attention history를 소유한다.

두 entity를 같은 Task로 취급하면 planning status와 execution lifecycle가
충돌하고, 어느 저장소가 title/status와 실행 증거의 원본인지 불명확해진다.

## Decision

두 entity를 분리한다.

### Work Stack PlanningTask

소유 필드:

- immutable internal UUID와 사람이 읽는 display ID
- planning title/detail
- priority/due/tags
- objectives, parent, planning dependencies
- planning status: `open`, `started`, `done`, `dropped`
- 개인 note, sanitized external context와 source reference

Work Stack UI에서는 호환성과 간결성을 위해 `Task`라고 표시할 수 있지만 public
integration type은 `workstack.planning-task`다.

### Conduit canonical Task

소유 필드:

- execution goal/scope/risk/data requirements
- responsibility continuity와 Seat/authority 관계
- Conduit execution lifecycle
- Runs, Outcomes, Evidence, Gates, Attention Holds
- Taskroom과 execution provenance

Conduit의 기존 canonical vocabulary를 변경하지 않는다.

### 관계와 authority

- 한 PlanningTask는 0..N개의 Conduit Task 실행을 제안할 수 있다.
- 한 Conduit Task는 선택적으로 하나의 originating PlanningTask reference를
  가질 수 있다. 이 reference는 ownership이나 1:1 동기화를 뜻하지 않는다.
- reference 형식은
  `workstack://<workspace-uuid>/planning-tasks/<planning-task-uuid>`다.
- Work Stack display ID (`T-0001`)는 사람이 읽는 alias이며 canonical identity가
  아니다.
- Conduit Task 생성 시 PlanningTask snapshot을 초기 proposal로 복사할 수 있다.
  그 이후 양쪽 필드는 독립적으로 authoritative하다.
- Work Stack `done`과 Conduit `CLOSED` 사이에는 자동 상태 변환이 없다.
- Conduit Outcome/Evidence는 향후 Work Stack에 read-only execution summary로
  projection할 수 있지만 PlanningTask를 직접 수정하지 않는다.
- Work Stack은 Conduit Room/Run/Outcome/Evidence를 수정하지 않는다.

## Prototype consequence

첫 시제품은 PlanningTask internal UUID projection과 위 reference 생성 규칙만
준비한다. Conduit adapter, Task 생성, status sync, Taskroom UI는 구현하지 않는다.
향후 adapter는 PlanningTask snapshot과 execution proposal을 전달하고, 명시적
사용자 또는 policy 승인을 받아 Conduit Task를 생성한다.

## Rejected alternatives

- 같은 Task ID namespace 공유: lifecycle와 field authority가 충돌한다.
- Work Stack status를 Conduit lifecycle로 자동 mapping: 정보 손실과 잘못된
  자동 종료를 만든다.
- Conduit Task를 Work Stack의 직접 projection으로 사용: 개인 planning UI의
  독립성과 현재 JSON/CLI 호환을 깨뜨린다.
