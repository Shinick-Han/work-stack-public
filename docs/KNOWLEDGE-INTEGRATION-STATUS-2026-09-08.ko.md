# Knowledge 통합 현황 — 공개 1.0.8과 최신 소스 비교

기준일: 2026-09-08. 비교 대상: 공개 저장소 `de7d764`의 1.0.8 snapshot과 이 문서가 포함된 최신 소스 snapshot.

이 문서는 OpenDocuments 통합 제안의 기존 구현 재사용 범위를 설명한다. OpenDocuments 연동이 완료되었다는 선언이나 새 설치기 배포 기록이 아니다. 공개 소스와 설치 파일의 버전은 [공개 manifest](../RELEASE-MANIFEST.md)가 있을 경우 그 기록으로 구분한다.

## 결론

현재 Work Stack에는 Task에서 문서 검색, 직접 문서 연결, 근거 확인, 선택한 근거로 재개용 문서 만들기가 구현되어 있다. 실제 연결 가능한 source는 **Local Markdown**이다. Obsidian vault도 그 폴더 형식으로 사용할 수 있다. 공통 참조 UI는 source별 표현을 분리하지만, Notion·NAS·OpenDocuments backend를 이미 지원하는 것은 아니다.

또한 **Knowledge Reference와 Capture는 다른 계약**이다. Reference는 선택한 로컬 문서를 읽고 발췌를 제시한다. Capture는 정제된 외부 제안을 Context Inbox에 저장한다. Reference 구현을 근거로 Capture의 provider/URL/본문 제한이 해제되었다고 판단하면 안 된다.

## 이미 추가된 기능과 적용 한계

| 고객 제안과 관련된 항목 | 공개 1.0.8 이후의 현재 구현 | 재사용 범위 / 남은 일 |
|---|---|---|
| P1-1 Task에서 검색 | 편집 가능한 질의, 사용자가 선택한 source, 명시적 Search 동작 | UI와 요청 처리 재사용. OpenDocuments 전송은 추가 필요 |
| P0-2 검색 요청의 Task 결박 | workspace UID, Task UID/ID/revision을 묶은 native bridge 요청과 늦은 응답 폐기 | 재사용 가능. 제안서의 만료 있는 KnowledgeRequest·서버 요청 ledger는 아직 없음 |
| P0-5 source 범위 | 사용자가 고른 Markdown root와 호스트가 발급한 vault ID의 registry | 로컬 root 범위만 해당. OpenDocuments workspace/corpus 권한 mapping은 없음 |
| 외부 RAG 교체 | 신뢰된 로컬 executable의 stdin/stdout 검색 provider 계약, 제한 시간·응답 검증 | 외부 엔진 adapter의 기반. 현재 출력은 Markdown 경로·줄·hash 후보 형식 |
| P0-4 근거 검토 | 원문 발췌 preview, source/document/위치/연결 이유, 명시적 연결·선택 | Task References에 구현됨. RAG Capture의 evidence/confidence Inbox UI는 별도 |
| P0-7 신선도 | 로컬 문서 hash와 읽기 시점 검증, 변경·누락·범위 밖 후보 제외, 변경 근거의 재확인 | 로컬 reader에서 재사용. Notion 권한 취소·NAS offline·삭제 전파는 미구현 |
| P0-8 안전한 경로 처리 | root 경계, traversal·ADS·symlink/reparse 등 검증과 제한된 Markdown 읽기 | 코드/테스트 패턴 재사용. NAS 임의 파일을 OS 앱으로 여는 resolver는 없음 |
| P1-6 근거 기반 업무 재개 | 사용자가 고른 최대 8개 근거와 저장된 Task/진행 기록으로 32 KiB 이내 resume brief 준비·복사 | 구현됨. 모든 Daily/Weekly Report 주장에 최신 외부 evidence를 강제하는 정책은 없음 |
| 범용 UI | source 표시와 공통 Reference row를 분리하고 Local Markdown 설명을 connector 영역에 한정 | 새 connector를 표시할 기반. 임의 SaaS 연결 설정이나 인증 UI가 완성된 것은 아님 |

## 공개본에 이미 있었던 기능

다음은 새로 만들어야 하는 기능도, 이번 공개 갱신에서 처음 추가되는 기능도 아니다.

- 정제된 Capture v1 입력 검증과 64 KiB capture endpoint 제한.
- Context Inbox의 사용자 검토, 기존 Task 연결, 선택 action의 Task 생성.
- Capture 수신용 좁은 bearer 경로와 사용자 mutation의 Origin/CSRF 경계.
- Capture 관련 idempotency와 변경 충돌 처리, 감사 이벤트의 기본 구조.

Capture 구현 일부가 별도 서비스·route·storage 파일로 분리되었으므로 파일 크기나 위치 변경을 새 기능으로 세면 안 된다. 특히 **Capture provider allowlist의 OpenDocuments 미지원은 여전히 유효한 지적**이다.

## 여전히 필요한 OpenDocuments 작업

1. `opendocuments` provider/resource/tool 계약과 provider별 URL 정책.
2. 기존 v1 호환을 보존하는 다중 evidence·retrieval 계약 및 저장/API projection.
3. 검색 결과 identity와 근거 문서 identity 분리. 같은 문서/버전으로 다른 질문의 결과가 공존해야 한다.
4. 서버가 소유한 corpus 정책과 request-result binding, 만료/재전송/commit-unknown 흐름.
5. 전용 Adapter 인증·자격 증명 관리 및 실제 OpenDocuments 응답 변환.
6. Context Inbox의 confidence·근거·현재 확인 상태 UI.
7. 원본 삭제·접근 취소·offline 검증, 안전한 NAS/Notion source resolver.
8. OpenDocuments 실제 설치 commit과 Notion/NAS 수집 경계·보존 정책의 실행 검증.

현재 `capture.py`는 schema `1.0`을 검증한다. 제안의 `capture-packet/1.1`, `agent`, `tools` 등을 그대로 제출하면 지원되는 packet이 아니다. `Knowledge Reference`의 존재를 이유로 기존 validator를 우회하지 않는다.

## 저장·보안 경계

- 연결 registry는 이 기기의 StateRoot 아래에 저장하며 선택한 로컬 root와 reference metadata를 보관한다. 발췌 원문은 registry에 저장하지 않는다.
- Reference는 workspace SSOT 동기화·일반 백업·기본 agent context에 자동 포함되지 않는다. 원문 발췌는 사용자가 명시적으로 준비·복사하는 보충 문서에 포함될 수 있다.
- 따라서 고객이 제안한 “SSOT의 opaque ID만으로 NAS/Notion을 관리”하는 구조와 현재 로컬 Reference 저장은 동일하지 않다.
- 외부 검색은 Task 선택만으로 실행되지 않으며, 검색 결과로 Task를 자동 변경하지 않는다.
- 실제 지원되는 connector만 표시한다. Notion/Gmail/Teams 등 미래 연결을 작동하는 것처럼 보여주지 않는다.
- 기존 [SECURITY.md](../SECURITY.md)의 같은 OS 사용자·침해된 Adapter·upstream retention에 관한 한계는 유지된다.

## 현재 UI 사용 순서

1. 데스크톱 앱에서 Task를 열고 **Resume**에서 **View all context** 또는 **Prepare resume brief**로 들어간다.
2. **Knowledge sources → Choose folder**로 Markdown 폴더를 등록한다.
3. **Find or link a document**에서 경로로 문서를 연결하거나, 설정된 검색 provider로 질의한다.
4. Preview와 연결 이유를 확인한 뒤 Link한다. 참조는 자동 선택되지 않는다.
5. 필요한 참조를 선택하고 **Prepare resume brief → Copy resume brief**를 사용한다. JSON은 보조 형식으로 유지된다.
6. **Record progress**는 해당 Task를 Daily Review 대상으로 넘긴다. 저장한 진행 기록은 Resume에 다시 반영된다.

호스트가 없는 일반 웹 브라우저에는 로컬 폴더 연결을 지원한다고 표시하지 않는다. 검색 provider가 없으면 수동 문서 연결을 사용할 수 있다.

## 구현 위치

- [검색 호스트 계약](KNOWLEDGE-SEARCH.md)
- [Markdown connector와 저장 경계](KNOWLEDGE-CONNECTOR-V1.md)
- [Task/Knowledge 사용 가이드](TASK-KNOWLEDGE-WORKFLOW.ko.md)
- [소스 독립 참조 표시](../frontend/src/features/knowledge/knowledgeSourceView.ts)
- [외부 검색 실행·검증](../desktop/python-webview-shell/knowledge_search.py)
- [Native 검색 요청 처리](../desktop/python-webview-shell/knowledge_host_search.py)
- [기존 Capture validator](../workstack/capture.py)
- [Capture 서비스](../workstack/service_captures.py)

설치·native 검증과 브라우저·단위 검증은 구분한다. 새 소스 snapshot의 공개 자체가 고객 환경이나 새 installer의 통과를 의미하지 않는다.
