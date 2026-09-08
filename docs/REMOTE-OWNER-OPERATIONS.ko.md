# Windows GUI + Linux `/bin/csh` 단일 writer 운영 안내

이 문서의 명령은 로컬 파서(`--help`·argv)와 합성 JSON으로 확인했다. 사내 SSH·NFS·실 SSOT에서는 실행하지 않았다. 경로·UID·Task ID는 실제 환경에 맞게 바꿀 자리표시자다. 토큰·refresh token·CSRF·owner receipt 내용을 출력하지 않는다.

이 문서는 검수된 CLI 소스 베이스 `47ca9e0`의 운영 계약을 기록한다. 2026-09-07 통합 라운드에서 검수 지적 여섯 건을 반영했다. 후속 원격 수정의 실제 설치 여부는 해당 릴리즈 검증 기록으로 확인한다.

**현재(이 베이스에 포함).** `agent apply`의 scoped `key_result_refs`, writer lease를 한 번 잡아 backend를 고르는 agent(복구 저널이 있거나 존재 여부를 모르면 거절), 인증된 owner CLI 읽기 여섯 가지(`backlog list`/`show`, `okr list`/`rollup`, `worklog list`, `weekly`).

**대기(검수 후보, 미출고).** 데스크톱 원격 수명·자기 probe, 활성화 복구 GUI, pidfd로 묶는 stop과 host/boot fencing. 이들을 설치된 동작으로 안내하지 않는다. 호스티드 소스 수정은 이미 설치된 EXE를 바꾸지 않는다.

## 정상 운영

Windows GUI가 Linux loopback **실제 8765**의 유일한 HTTP writer를 소유한다. Windows가 포워드하는 **28765**는 Linux `ws`/`agent` 주소가 아니다. Linux 클라이언트는 GUI 화면값을 복사하지 않고, 지원 CLI가 세션·Origin·CSRF를 붙인다. raw curl 우회를 쓰지 않는다.

GUI가 owner인 정상 `agent status`(exit 0)는 `ready: true`, `running_server_available: true`, `exclusive_local_available: false`, `meta.transport: "running-server"`다. local 플래그를 true로 만들려고 GUI를 닫지 않는다. status/context/checkpoint 거절은 exit 1과 `error.code`다. 흔한 값: `owner_unavailable`(점유된 lease·무응답 owner, 로컬 우회 없음), `workspace_mismatch`, `invalid_authority`. 파서 오류는 exit 2이며 agent 봉투가 없다. `agent apply`는 status/context/checkpoint의 canonical agent 봉투가 아닌 기존 `{data,meta}` receipt 경로다. 성공은 stdout과 exit 0이고, owner HTTP non-2xx는 서버 JSON을 stdout에 내고 exit 2이며, admission·local·transport 예외는 `error: ...`를 stderr에 내고 exit 2다.

```json
{
  "contract": "workstack.cli.v1",
  "data": {
    "actual_workspace_uid": "11111111-1111-4111-8111-111111111111",
    "capability_reason": null,
    "capability_supported": true,
    "contract": "workstack.cli.v1",
    "data_dir_available": true,
    "exclusive_local_available": false,
    "expected_workspace_uid": "11111111-1111-4111-8111-111111111111",
    "ready": true,
    "running_server_available": true,
    "storage_format": "v3"
  },
  "meta": {
    "command": "agent.status",
    "transport": "running-server",
    "workspace_uid": "11111111-1111-4111-8111-111111111111"
  }
}
```

아래 `<pfx>`는 셸 alias가 아니라 문서 치환이다. `/bin/csh`에서는 한 줄로 `/placeholder/python3 -I /placeholder/app/run_work_stack.py`를 넣는다. `-I`는 Python 옵션이며, 실행된 `run_work_stack.py`가 자신의 checkout을 모듈 경로에 넣는다. Linux 읽기·agent는 Linux 실제 8765 owner를 사용한다. Windows의 28765 터널을 Linux owner 주소로 입력하지 않는다.

## 지원 읽기 CLI

루트 플래그는 `--data-dir`뿐이다. `backlog`/`okr`/`worklog`/`weekly`에 `--workspace-uid`를 붙이면 파서가 exit 2로 거절한다. workspace 신원은 데이터 디렉터리의 `workspace.json`에서 읽힌다. `agent`의 `--workspace-uid`는 파서 문법상 optional이어서 생략 argv 자체는 파싱되지만, 런타임 admission은 canonical non-nil UID를 요구하고 `workspace.json`의 실제 UID와 exact match하지 않으면(생략 포함) `workspace_mismatch`로 exit 1 한다.

```sh
<pfx> --data-dir /placeholder/ssot backlog list
<pfx> --data-dir /placeholder/ssot backlog list --status active
<pfx> --data-dir /placeholder/ssot backlog show T-0001
<pfx> --data-dir /placeholder/ssot okr list
<pfx> --data-dir /placeholder/ssot okr list --status active
<pfx> --data-dir /placeholder/ssot okr rollup
<pfx> --data-dir /placeholder/ssot worklog list
<pfx> --data-dir /placeholder/ssot worklog list --date 2026-09-07
<pfx> --data-dir /placeholder/ssot weekly
<pfx> --data-dir /placeholder/ssot weekly --end 2026-09-07 --days 7
```

파서 기본값: `list`의 `--status`는 `active`, `weekly`의 `--days`는 `7`. `--date`/`--end`는 생략 가능하다. 결과는 owner GET 패리티이지 GUI projection 복제가 아니다.

## agent status / context / apply (KR)

```sh
<pfx> --data-dir /placeholder/ssot agent --workspace-uid 11111111-1111-4111-8111-111111111111 status
<pfx> --data-dir /placeholder/ssot agent --workspace-uid 11111111-1111-4111-8111-111111111111 context --task T-0001
```

`--view`를 생략하면 `core-v1`이다. 알 수 없는 view는 파서가 exit 2로 거절한다.

`apply`는 `--stdin`과 `--intent-id`가 필수다. stdin JSON 최상위는 정확히 `workspace_id`, `task_id`, `expected_revision`, `changes` 네 키다. `changes`에 허용되는 필드: `title`, `detail`, `status`, `priority`, `due`, `scheduled`, `estimate_minutes`, `tags`, `objective_ids`, `parent_id`, `dependencies`, `key_result_refs`. 비밀을 넣지 않는다.

KR 패치 예(자리표시자). 임시 파일로만 넘기고, 파일이나 환경에서 토큰을 출력하지 않는다.

```json
{"workspace_id":"11111111-1111-4111-8111-111111111111","task_id":"T-0001","expected_revision":4,"changes":{"key_result_refs":[{"objective_id":"O-1","key_result_id":"KR-1"}]}}
```

```sh
<pfx> --data-dir /placeholder/ssot agent --workspace-uid 11111111-1111-4111-8111-111111111111 apply --stdin --intent-id agent.kr.apply.0001 < /placeholder/tmp/task-patch.json
```

`key_result_refs`를 빼면 기존 값을 유지하고, `"key_result_refs": []`는 명시적 해제다. 항목은 `objective_id`/`key_result_id` scoped 쌍만 쓴다. 유효한 non-empty `key_result_refs`를 적용하면 빠진 부모 Objective ID는 결과 Task의 `objective_ids`에 자동으로 한 번만 추가된다. 같은 패킷에 `objective_ids`를 명시해도 빠진 KR 부모는 결과 목록 끝에 한 번만 추가되므로, 부모를 수동으로 중복 추가하지 않는다. `expected_revision`이 현재와 다르면 CAS가 거절하고 쓰지 않는다.

apply intent는 상관관계 ID이며 checkpoint idempotency key가 아니다. 결과가 `agent apply commit is unknown; inspect the Task revision before retrying`이면 **중단**한다. 새 intent·바뀐 패킷·다른 revision 추측으로 재시도하지 않는다.

checkpoint는 POST가 서버에 도달했을 수 있는데 응답을 잃은 경우에만 CLI 내부에서 **같은 intent + 같은 canonical body**를 최대 한 번 자동 재생한다. 운영자가 명령 전체를 다시 보내라는 일반 권고는 없고, `commit_unknown`이면 intent와 증거를 보존한 채 멈춘다. Worklog 문구가 같아도 커밋 증명이 아니다.

`.workstack-journal.json`이 있거나 `lstat`가 실패하면 agent는 저널을 재생하지 않고 거절한다. 저널을 지우지 않는다.

## 진단

| 우선 | 관찰 | 지금 | 금지 / 대기 |
|---|---|---|---|
| 1 | 터널이 끊겨도 Linux writer가 남음 | GUI owner 유지, 증거 보존 | 포트/PID만으로 kill. 원격 수명을 출고로 단정하지 않음 |
| 2 | Linux가 28765 또는 raw curl 사용 | 실제 8765 + 지원 CLI | 토큰/CSRF/receipt 내용 출력 |
| 3 | stale `.workstack-server.json` + 실제 lease 비어 있음 | lease-first가 local을 고를 수 있음 | 메타데이터 삭제. HTTP 실패를 회수 근거로 사용 |
| 4 | lease 점유 또는 owner 무응답 | `owner_unavailable`로 중단 | 로컬 우회, lock 삭제 |
| 5 | 타 호스트이거나 host/boot/machine-id가 없는 구 receipt | 불확실. 신호·삭제 금지 | 파일 삭제 레시피로 이관 |
| 6 | 구 `backlog.py` / alias / symlink writer | 지원 CLI로 이관 | advisory lock이 임의 JSON writer를 막는다고 주장 |
| 7 | 활성화 재시도가 꼬임 | 증거 보존 | 복구 GUI를 출고 기능으로 안내 |

세 상태를 같은 “죽은 owner”로 묶지 않는다. (1) 광고 파일만 남고 `.workstack.lock`이 비어 있으면 이 베이스의 agent는 lease를 한 번 잡아 local을 고를 수 있다. (2) lease가 잡혀 있는데 HTTP가 죽으면 `owner_unavailable`이며 회수가 아니다. (3) receipt의 호스트가 다르거나 host/boot/machine-id가 없으면 불확실이며, 로컬 PID로 신호를 보내지 않는다.

실제 writer 권한은 데이터 디렉터리의 `.workstack.lock`이다(실험 경로 `writer.lock`과 바꾸지 않는다). `.workstack.lock`, `.workstack-server.json`, `.workstack-remote-owner.json`, `.workstack-journal.json`을 수동으로 제거하지 않는다. 포트나 PID만 보고 프로세스를 죽이지 않는다.

advisory `flock`은 협조하는 제품 경로만 조정한다. `vim`/`jq`/임의 스크립트의 JSON 덮어쓰기를 막지 못한다. 저장소에는 `backlog.py`/`BACKLOG_FILE`이 없다. 옛 alias·symlink writer는 지원 CLI로 옮긴다.

과거 CLI 베이스 `47ca9e0`의 remote receipt는 PID/start 등이며 host/boot가 없다. 현재 통합 소스에는 host/boot fencing이 포함되지만, 설치본에 반영됐는지는 해당 빌드의 해시와 검증 기록으로 확인한다. 공유 경로에서 로컬 `/proc`으로 타 호스트를 오판할 수 있다. machine-id 부재는 **불확실**이지 죽은 owner의 증거가 아니다. 구 receipt 이관은 범위가 명시된 복구 설계가 필요하며 파일 삭제 절차가 아니다.

## 미래 pidfd stop 게이트 (미출고)

프로세스 핸들에 묶인 stop은 검수 대상이다. 공식 Python 3.12 문서 기준, Linux에서 `os.pidfd_open`은 커널 **≥ 5.3**, `signal.pidfd_send_signal`은 **≥ 5.1**이다: [pidfd_open](https://docs.python.org/3.12/library/os.html#os.pidfd_open), [pidfd_send_signal](https://docs.python.org/3.12/library/signal.html#signal.pidfd_send_signal). 실제 인터프리터의 API 제공 여부와 권한도 필요하다. 지원하지 않는 소스 후보는 종료 요청을 명시적으로 거부한다. `kill(pid)`로 우회하지 않는다.

## 읽기 전용 버전·해시 점검

버전 문자열만으로 동일 바이너리라고 보지 않는다. 로컬 `/bin/csh` 또는 격리 tcsh 증명은 사내 SSH/NFS 증명이 아니다. 자동 SSH를 열지 않는다. 해시는 셸 alias 대신 Python SHA-256을 쓴다. `/bin/csh` 한 줄:

```sh
/placeholder/python3 -c 'import sys; print(sys.executable); print(sys.version)'
```

```sh
/placeholder/python3 -c 'import hashlib, pathlib; print(hashlib.sha256(pathlib.Path("/placeholder/payload/usr/bin/tcsh").read_bytes()).hexdigest())'
```

```sh
/placeholder/python3 -c 'import hashlib, pathlib; print(hashlib.sha256(pathlib.Path("/placeholder/app/workstack/agent_runtime.py").read_bytes()).hexdigest())'
```

```sh
/placeholder/python3 -c 'import hashlib, pathlib; print(hashlib.sha256(pathlib.Path("/placeholder/app/desktop/python-webview-shell/remote_entry.py").read_bytes()).hexdigest())'
```

Windows 설치본도 같은 Python 한 줄이다(자리표시자 경로). 설치본 해시가 소스와 같아도 미출고 수명 수정이 들어 있다고 보지 않는다.

```powershell
& 'C:\placeholder\app\runtime\python.exe' -I -c "import hashlib, pathlib; print(hashlib.sha256(pathlib.Path(r'C:\placeholder\app\runtime\python.exe').read_bytes()).hexdigest())"
```

```powershell
& 'C:\placeholder\app\runtime\python.exe' -I -c "import hashlib, pathlib; print(hashlib.sha256(pathlib.Path(r'C:\placeholder\WorkStack-Setup-1.0.9.ps1').read_bytes()).hexdigest())"
```

```powershell
& 'C:\placeholder\app\runtime\python.exe' -I -c "import hashlib, pathlib; print(hashlib.sha256(pathlib.Path(r'C:\placeholder\app\desktop\python-webview-shell\workstack_desktop.py').read_bytes()).hexdigest())"
```

```powershell
& 'C:\placeholder\app\runtime\python.exe' -I -c "import hashlib, pathlib; print(hashlib.sha256(pathlib.Path(r'C:\placeholder\app\workstack\agent_runtime.py').read_bytes()).hexdigest())"
```

2026-09-07 로컬 시험용 참고 측정값(**사내 설치 증명이 아님**). 아래 1.0.9 설치 후보는 긴급 수정 전의 오래된 빌드이므로 설치 대상으로 쓰지 않는다. 같은 파일·같은 패키지를 비교할 때만 해시 차이를 판단한다. 소스의 CRLF/LF 차이도 바이트 해시를 바꾸므로 최종 배포 manifest와 대조한다.

| 대상 | SHA-256 |
|---|---|
| 격리 tcsh `.deb` 패키지 | `9ec0069a5ad829fb25e5fc5bf9f7ba3b01960bb1028011aa28cc33a79ed61169` |
| 그 패키지에서 꺼낸 `tcsh` 바이너리 | `3b660a10244cbf564a5cae251ee829cbbb52ac02ccbc11d48e4e19590cdf5f14` |
| 격리 1.0.9 `WorkStack-Setup-1.0.9.ps1` | `bf2b65ffe0f12f5b832ff4b705407d56d28f065e19255d3f9e02b3985b2b7125` |
| 그 설치본 `runtime/python.exe` | `4d6f5f81a4bca11191c4c7c6b43632694d0a4ce74e068619d8fdc161d469859a` |
| 1.0.9 설치본 `agent_runtime.py` | `c0f8d1883b1a59cb1aedb0000078d50d2c3e8e5ae6f940cf782410a53816d0cd` |
| 이 베이스 `agent_runtime.py` | `3fa195f00a628dfbca10eba022b3e8ea4053c49189bcff0823a76cb29ff13235` |
| 1.0.9 설치본·이 베이스 `workstack_desktop.py` | `355ca66a05a4b03797f1f15a6579589c5abfc258de3db8adf29c93f3e87da3e7` |

`agent_runtime.py` 해시가 갈리면 lease-first agent가 설치본에 없을 수 있다. `workstack_desktop.py`가 같아도 원격 수명 수정이 이 컷에 있다는 뜻이 아니다.

관련 배경: `docs/REMOTE-SINGLE-WRITER-REPAIR-2026-09-07.ko.md`.
