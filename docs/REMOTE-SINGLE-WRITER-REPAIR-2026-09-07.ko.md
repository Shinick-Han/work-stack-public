# Windows GUI / Linux SSOT 단일 writer 긴급 수정

사용자 사내 재현 보고: 2026-09-07. Linux 로그인 셸은 `/bin/csh`, SSOT는 사용자 홈 아래의 `workstack/ssot` 디렉터리다. 이 문서는 사내 현장 보고와 로컬 통합 소스 관찰을 구분한다. 사내 호스트에 직접 접속하거나 데이터를 수정하지 않았다.

## 수용 조건

GUI가 Linux loopback 8765의 유일한 HTTP writer를 소유한다. Linux ws/agent는 그 owner의 정상 클라이언트이며, GUI를 닫거나 JSON을 직접 수정하지 않고 읽기와 Task/KR 변경을 수행한다. Windows 포워딩 포트 28765를 Linux 클라이언트 주소로 오인하지 않는다. CAS, CSRF, Origin 및 동일 intent의 불확정 커밋 처리 규칙을 유지한다.

## 긴급 순서 및 작업 경계

1. 원격 수명과 자기 세션 판별: csh 중간 프로세스 때문에 PDEATHSIG가 sshd를 추적하지 못하는 경로를 수정한다. 정상 종료뿐 아니라 터널 프로세스 급사도 재현한다. 세션 토큰이 일치하는 probe와 타 세션 잠금을 구분한다. 타 owner 종료는 금지한다.
2. Linux CLI 읽기: backlog list/show, okr list/rollup, worklog list, weekly를 owner HTTP로 전달한다. GUI projection을 그대로 CLI 결과로 간주하지 않고 로컬 CLI와 출력/필터 동등성을 검증한다.
3. agent apply의 key_result_refs: 지원 필드와 검증을 확장하고 scoped KR, 부모 objective 정합성, CAS 및 명시적 빈 목록으로 해제하는 동작을 검증한다.
4. Windows activation: 실패 후 재시도가 중복 pending receipt를 만들지 않게 한다. 정확한 registry digest와 시도 식별에 결합하며, 다른 시도나 불확정 결과의 증거는 임의 삭제하지 않는다.
5. stale owner / legacy 경로: 실제 lease, PID 및 호스트 식별을 근거로 회수 가능한 상태를 구분한다. NFS의 타 호스트 owner, 일시적인 HTTP 장애, commit_unknown은 죽은 owner의 증거가 아니다. 저장소 내부의 우회 JSON writer와 문서를 조사하고 관리 가능한 경로부터 차단한다. 외부의 임의 스크립트까지 advisory lock으로 차단할 수 있다고 주장하지 않는다.

## 최초 로컬 확인 (fb6bee9)

- remote_command_contract.serve_tokens에 exec 접두사가 없다. 관련 테스트도 exec python3 부재를 기대한다. bare python3 복구가 아니라 검증된 절대 interpreter 경로와 셸 안전성을 유지해야 한다.
- cli_reads는 여섯 읽기의 owner parity를 빈 집합으로 두고 의도적으로 거부한다. 이것은 해결해야 할 클라이언트 기능 공백이다.
- 현재 workstack_desktop는 generate_session_token을 호출하고 _stop_owned_remote_connection에서 stop-owned를 먼저 호출한다. 사내 설치본에서는 미호출이라는 현장 보고와 다르므로 설치된 Windows/Linux payload의 버전 및 파일 해시를 확인해야 한다.
- agent_runtime는 server_info 존재를 근거로 running-server backend를 선택한다. stale metadata 처리에 대한 별도 검수가 필요하다.

## 독립 조사로 좁힌 수정 범위

- Linux server_info에는 실제 listen port가 기록된다. Windows 포워딩 포트를 Linux owner 주소로 잘못 게시하는 결함은 현재 소스와 격리 HTTP 시험에서 발견되지 않았다.
- 일반 CLI는 실제 writer lease를 한 번 획득하고 유지하지만 agent_runtime는 metadata만 보고 backend를 선택한다. agent도 동일한 lease 수명 원칙을 적용해야 한다. HTTP 실패 후 로컬로 우회하는 fallback은 추가하지 않는다.
- 원격 owner receipt에 host/boot 식별이 없어 공유 NFS에서 로컬 PID 정보로 타 호스트 owner를 오판할 수 있다. 새 receipt의 식별과 기존 receipt의 불확실성을 구분하고, 타 호스트 또는 미확인 owner에 신호를 보내거나 증거를 삭제하지 않아야 한다. 실제 두 호스트 NFS 잠금 시험은 아직 수행하지 않았다.
- 이 PC의 설치된 1.0.8 Python host에도 token 생성과 stop-owned 호출 코드가 있다. 이 사실은 사내 배포본의 코드나 실제 종료 시 호출 여부를 증명하지 않는다. 버전 문자열만으로 동일 빌드라고 판단하지 않는다.
- 저장소에는 backlog.py 및 BACKLOG_FILE 구현이 없다. 별도 legacy 스크립트는 이 제품 패치로 차단할 수 없다. 지원 CLI/HTTP 경로로 이관하거나 별도 계정·권한 경계를 적용해야 직접 파일 쓰기를 강제 차단할 수 있다. 실제 사내 alias/스크립트는 검사하거나 수정하지 않았다.
- agent apply의 intent ID는 상관관계 식별자이며 checkpoint의 idempotency key와 다르다. apply의 불확정 응답 뒤에는 새 intent 또는 변경된 packet으로 재시도하지 않는다. 이전 revision의 중복 제출은 추가 변경을 만들면 안 된다.

WSL에는 배포판 tcsh 6.24.13을 별도 시험 폴더에 압축 해제했다. 패키지 SHA-256을 apt metadata와 대조했고 시스템 설치 및 로그인 셸 변경은 하지 않았다. 이 런타임을 쓰는 로컬 csh 계열 시험은 사내 실제 sshd/NFS 검증의 대체 증거가 아니다.

## 배포와 검수

### 통합된 수정 — 2026-09-07 16:23 KST

- `agent apply`의 `key_result_refs` 지원과 안내를 통합했다. KR과 부모 Objective 정합성, revision 충돌, 명시적 빈 목록을 통한 해제를 검수했다.
- 활성화 재시도는 같은 후보의 원본 receipt와 rollback 근거를 재사용한다. 기존에 쌓인 중복 기록을 정리하는 핵심 함수는 검수됐지만, 사용자가 명시적으로 실행하는 시작 화면 연결은 아직 작업 중이다.
- `agent status/context/checkpoint`는 실제 writer lease를 한 번 획득해 유지하는 방식으로 backend를 선택한다. stale/corrupt 서버 정보가 있어도 lease가 비어 있으면 안전한 local 경로를 선택하며, 다른 writer가 lease를 보유하면 HTTP 경로만 사용한다. HTTP 실패 후 local로 우회하지 않는다.
- 보류 중인 복구 저널이 있거나 존재 여부를 확인할 수 없으면 agent는 내용을 소비하지 않고 거부한다. lease 획득 전과 획득 후에 확인하여 저널 재생 경합을 차단한다. 유효한 저널의 바이트 보존과 owner 오류 시 무우회 동작을 독립 검수했다.
- 배포 스킬은 `running-server`, `ready: true`, `exclusive_local_available: false` 조합을 정상으로 안내한다. 스킬 계약 검사 11개와 통합 구조 검사 347개 production file 범위가 통과했다.

### CLI 읽기 통합과 추가 검수 — 2026-09-07 17:03 KST

여섯 CLI 읽기는 독립 재검수를 통과하여 통합했다. 각 HTTP 읽기를 하나의 Store transaction으로 묶어 Task와 Activity의 서로 다른 revision이 섞이지 않게 했다. 실제 writer가 읽기 중 lock에서 대기하는 시험, 전체 응답의 변경 전·후 동등성 검사, transaction 경계를 제거하면 기존 HTTP 400 오류가 재현되는 대조 시험을 통과했다. 독립 검수는 관련 59개 검사와 15개 owner 읽기 oracle을 실행했고, 통합본의 전체 CLI 회귀 검사는 진행 중이다. 클라이언트가 HTTP 서버 모듈을 가져오지 않도록 순수 계약 모듈을 분리했고 구조 검사는 350개 production file 범위에서 통과했다.

실제 WebView2로 복구 화면을 시험하여 `NavigateToString` 페이지의 발신 주소가 `about:blank`임을 확인했다. 이 화면에는 `crypto.randomUUID`가 없어 기존 복구 버튼이 비활성화된 뒤 메시지를 보내지 못했다. 시험 중 polyfill을 주입한 후의 왕복 성공은 기존 제품의 버튼 성공으로 인정하지 않는다. 외부 origin의 복구 메시지가 발신 화면 검사보다 먼저 처리되는 문제도 실제로 재현했다. 두 문제는 복구 UI 수정 및 별도 네이티브 재검증 대상이다.

원격 프로세스 수명·자기 probe와 활성화 복구 UI는 아직 최종 통합 검수 중이다. 안전한 PID handle을 지원하지 않는 Linux에서 `stop-owned`는 불확실한 PID에 신호를 보내지 않고 거부하도록 보완 중이다. Python의 `pidfd_open`은 Linux 5.3 이상, `pidfd_send_signal`은 Linux 5.1 이상이 필요하며, 버전만으로 실제 권한·API 사용 가능성을 보장하지 않는다. GUI가 소유한 SSH 종료와 exec/PDEATHSIG 정리는 별도 경로다. 실제 사내 커널 지원 여부는 확인하지 않았다. 근거: [Python os.pidfd_open](https://docs.python.org/3.12/library/os.html#os.pidfd_open), [Python signal.pidfd_send_signal](https://docs.python.org/3.12/library/signal.html#signal.pidfd_send_signal).

이 목록은 수정 소스의 상태이며 현재 설치된 EXE가 위 수정을 포함한다는 뜻은 아니다.

기존 로컬 1.0.9 후보는 원격 환경 수락 완료본이 아니다. 개인 설치본/공개판은 1.0.8 상태이며, 본 결함 수정과 독립 검수 전에는 새 배포하지 않는다. 진행 중인 Obsidian/보고서 검증 결과는 보존한다. 작업자는 분리 worktree에서 구현하고 gpt-5.6-sol high가 결과를 독립 검수한 후 coordinator가 순서대로 통합한다. 사내 csh/SSH/NFS에서 미재현한 사항은 로컬 테스트와 구분해 남긴다.
