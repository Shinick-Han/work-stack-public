# Practical Adversarial Review Policy

작성일: 2026-08-29
목표: blocker 0이나 설계 완벽성이 아니라 **검증 가능한 ROI를 가장 빨리 안전하게 전달**한다.

## 판정 원칙

리뷰는 다음 질문 순서로 진행한다.

1. 이 증분이 지금 실제 사용 흐름의 비용·오류·마찰을 줄이는가?
2. 더 작은 범위로 같은 가치를 낼 수 있는가?
3. 아래 hard blocker가 있는가?
4. hard blocker가 없다면 나머지는 구현을 막지 않고 기술부채로 기록할 수 있는가?

### Hard blocker

- credential, 원문, 개인정보가 승인되지 않은 경계로 누출됨
- 사용자 data 손상·유실 또는 다른 Task의 확정 결과를 되감을 수 있음
- 승인되지 않은 외부 전송·결제·삭제 등 side effect가 생김
- 비가역 migration 또는 안전한 rollback 부재
- 현재 Graph/Board/Treemap/Focus/Inbox/OOB 등 핵심 flow의 치명적 회귀

### 기본적으로 허용하는 기술부채

- telemetry와 장기 ROI 증거 부족
- 추가 browser/timezone/gesture matrix
- animation, copy, visual polish
- helper 일반화와 구조 리팩터링
- cross-tab/offline/대규모 scale 대응
- dogfood 후 조정할 ranking 또는 정보 구조

허용한다는 것은 숨긴다는 뜻이 아니다. owner 없이 남기지 않도록 계획 또는 구현 결과에
debt와 재평가 조건을 기록한다.

## 결론 형식

- `GO`: hard blocker가 없고 timebox 안에서 ROI를 낼 수 있다.
- `GO_WITH_CUTS`: bounded한 필수 수정 또는 범위 삭제 후 즉시 구현한다.
- `STOP`: hard blocker가 있거나 같은 ROI를 훨씬 작은 증분으로 낼 수 없다.

`GO`를 위해 blocker 0, 완전한 telemetry, 모든 edge case, 장기 확장 설계를 요구하지 않는다.
반대로 보안·data 무결성·외부 side effect·핵심 회귀는 “빠른 시제품”이라는 이유로 debt로
내리지 않는다.

## 실행 규칙

- 각 증분은 4~8시간 기본 timebox와 stop-loss를 둔다.
- 리뷰는 구현 전에 한 번, 실브라우저/실행 검증 뒤 한 번 수행한다.
- 리뷰 중 나온 soft finding은 구현을 멈추지 않고 debt 목록으로 이동한다.
- 실제 사용 증거가 필요한 기능은 얇은 experiment로 만들고 3~5일 dogfood 후 유지한다.
- 테스트는 synthetic data로 correctness를 증명할 수 있지만 product demand를 증명했다고
  주장하지 않는다.
