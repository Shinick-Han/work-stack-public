# Work Stack 1.0.7 설치·사용 안내서

> **문서 상태.** Work Stack **1.0.7** 제품 기준입니다. 이 저장소의 GitHub Releases에는 아직 v1.0.7 설치 파일이 없습니다. 지금 공개된 설치 파일은 [Releases](https://github.com/Shinick-Han/work-stack-public/releases)에서 받고, 옆의 `.sha256` 사이드카로 검증하세요. 검증된 `WorkStack-Setup-1.0.7.ps1`과 사이드카를 따로 가진 경우에만 아래 1.0.7 절차를 따르세요.

---


## 1. 설치 전에 알아둘 것

Work Stack은 목표(Objective)·핵심 결과(Key Result)·작업(Task)·일일 작업 기록(Worklog)·노트를 하나의 워크스페이스에 두는 로컬 우선(local-first) 도구입니다. **프로그램 설치 폴더와 작업 데이터 폴더는 다릅니다.** 프로그램을 다시 설치하거나 지우는 일과 워크스페이스 데이터를 지우는 일은 별개의 작업입니다.

- Windows 배포판(`WorkStack-Setup-1.0.7.ps1`)에는 64비트 Python 3.12.10 임베디드 런타임과 잠금된(hash-locked) 의존성이 포함됩니다. 대상 PC에 Python이나 Node.js를 따로 설치하지 않으며, 설치 중 네트워크에 접속하지 않습니다.
- 데스크톱 창은 Microsoft Edge WebView2 Runtime이 있어야 열립니다. 최신 Windows 10/11에는 대개 포함되어 있습니다. 이 1.0.7 설치본은 64비트 Windows 11에서 WebView2와 함께 창이 열리는 것을 확인했습니다. 그 밖의 OS/WebView2 조합 목록은 이 문서에 없습니다.
- 설치 파일은 코드 서명되어 있지 않습니다. 체크섬은 "받은 바이트가 사이드카와 같은가"를 확인할 뿐, 게시자 신원을 증명하지 않습니다.
- 이 안내서는 Linux 데스크톱 GUI를 약속하지 않습니다. Linux는 SSH로 연결되는 원격 SSOT 서버와 명령행(CLI)·에이전트 실행 환경으로만 다룹니다.
- 소스 버전 리터럴은 `1.0.7`입니다(`workstack/__init__.py`). 설치 파일 이름은 `WorkStack-Setup-1.0.7.ps1`, 사이드카는 `WorkStack-Setup-1.0.7.ps1.sha256`입니다. 설치된 호스트의 파일 버전은 `1.0.7.0`, 제품 버전은 `1.0.7`입니다. **특정 빌드 SHA-256은 이 문서에 적지 않습니다.** 권위는 항상 같은 배포의 사이드카입니다.

이전 공개 버전은 1.0.6입니다. 참고로 1.0.6의 게시 자산은 `WorkStack-Setup-1.0.6.ps1`(26,402,269바이트, SHA-256 `5a41a4d542ce40662d73d1a769f1c6f5b311da5008a9da7296ed6795008420da`)과 사이드카 `WorkStack-Setup-1.0.6.ps1.sha256`(92바이트)이었고, 공개 저장소 `Shinick-Han/work-stack-public`에는 v1.0.0부터 v1.0.6까지의 릴리스가 있습니다. 1.0.7 자산도 같은 이름 규칙을 따릅니다.

## 2. 설치 파일을 받고 체크섬을 검증하기

같은 버전의 **두 파일**을 함께 받습니다: `WorkStack-Setup-1.0.7.ps1`와 `WorkStack-Setup-1.0.7.ps1.sha256`.

지금은 다음 중 하나입니다.

- **소스에서 빌드한 배포 묶음**: 체크아웃에서 `npm --prefix frontend run build` 뒤 `scripts\windows\Build-WindowsInstaller.ps1`을 실행하면 `.artifacts\WorkStack-Setup-1.0.7.ps1`과 사이드카가 생깁니다.
- **이미 받은 1.0.7 설치 파일**: 파일명과 사이드카가 위와 같으면 같은 절차로 검증합니다.
- **이후 공개 릴리스**: 자동 업데이트 검사기가 받아들이는 주소 형식은 `https://github.com/Shinick-Han/work-stack-public/releases/download/v1.0.7/WorkStack-Setup-1.0.7.ps1` 입니다. 공개 저장소의 릴리스 목록은 `https://github.com/Shinick-Han/work-stack-public/releases` 입니다. **v1.0.7 태그와 그 자산은 아직 없습니다.** 게시되기 전에는 이 URL을 열어 설치하지 마세요.

사이드카의 내용은 정확히 한 줄입니다: `<소문자 64자리 SHA-256>` + 공백 두 개 + `WorkStack-Setup-1.0.7.ps1` + 줄바꿈. (1.0.6 사이드카도 이 형식이었습니다.)

### 2.1 PowerShell로 검증 (사이드카 기준, 대소문자 무시)

다운로드한 두 파일이 있는 폴더에서 실행합니다. 아래 비교는 설치본의 검증기 `Test-WorkStackSetup.ps1`과 같은 규칙(파일명은 대소문자 무시, 다이제스트는 소문자로 맞춰 비교)입니다.

```powershell
$Setup = Join-Path $PWD 'WorkStack-Setup-1.0.7.ps1'
$Sidecar = "$Setup.sha256"
$Text = [IO.File]::ReadAllText($Sidecar, [Text.UTF8Encoding]::new($false))
$Match = [regex]::Match($Text, '\A(?<digest>[0-9a-fA-F]{64})  (?<name>[^\r\n\\/]+)\r?\n?\z')
if (-not $Match.Success) { throw 'Checksum sidecar must contain exactly one SHA-256 line with two spaces before the setup filename.' }
if (-not [string]::Equals($Match.Groups['name'].Value, [IO.Path]::GetFileName($Setup), [StringComparison]::OrdinalIgnoreCase)) { throw 'Setup filename mismatch.' }
$Expected = $Match.Groups['digest'].Value.ToLowerInvariant()
$Actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $Setup).Hash.ToLowerInvariant()
if ($Actual -ne $Expected) { throw "Setup hash mismatch. Expected $Expected, got $Actual." }
"VERIFIED SHA-256 $Actual  $([IO.Path]::GetFileName($Setup))"
```

### 2.2 사이드카 다이제스트만 직접 비교

릴리스 페이지에 적힌 해시가 아직 없을 때는 사이드카 한 줄의 64자리만 비교합니다.

```powershell
$Expected = ((Get-Content -Raw .\WorkStack-Setup-1.0.7.ps1.sha256).Split(' ', 2)[0]).ToLowerInvariant()
$Actual = (Get-FileHash -Algorithm SHA256 -LiteralPath .\WorkStack-Setup-1.0.7.ps1).Hash.ToLowerInvariant()
if ($Actual -ne $Expected) { throw "Setup hash mismatch. Expected $Expected, got $Actual." }
```

### 2.3 Linux / Git Bash

```sh
sha256sum -c WorkStack-Setup-1.0.7.ps1.sha256
```

성공하면 `WorkStack-Setup-1.0.7.ps1: OK`가 출력됩니다. 공개된 1.0.6 사이드카도 같은 형식입니다.

### 2.4 이미 설치된 Work Stack의 검증기 사용

이미 설치된 PC에서는 설치본의 엄격 검증기를 쓸 수 있습니다. 사이드카가 옆에 없으면 `-ChecksumPath`로 지정합니다.

```powershell
& "$env:LOCALAPPDATA\Programs\WorkStack\scripts\windows\Test-WorkStackSetup.ps1" -SetupPath .\WorkStack-Setup-1.0.7.ps1
```

성공 출력은 `VERIFIED SHA-256 <digest>  WorkStack-Setup-1.0.7.ps1` 한 줄입니다. 실패 메시지는 다음 중 하나입니다: `Setup artifact does not exist: ...`, `Checksum sidecar does not exist: ...`, `Checksum sidecar must contain exactly one SHA-256 line with two spaces before the setup filename.`, `Setup filename mismatch. Sidecar names '...' but selected artifact is '...'.`, `Setup hash mismatch. Expected ..., got ....`

검증이 실패하면 **설치하지 말고** 공식 릴리스 페이지에서 다시 받습니다.

## 3. Windows 새 설치

검증이 끝난 폴더에서 실행합니다. 실행 정책 우회는 이 프로세스에만 적용됩니다.

```powershell
powershell -ExecutionPolicy Bypass -File .\WorkStack-Setup-1.0.7.ps1
```

설치 파일은 다음 매개변수를 받습니다. 지정하지 않으면 기본값을 씁니다.

| 매개변수 | 기본값 | 뜻 |
| --- | --- | --- |
| `-InstallRoot` | `%LOCALAPPDATA%\Programs\WorkStack` | 프로그램 설치 폴더 |
| `-StateRoot` | `%LOCALAPPDATA%\WorkStack` | 설정·로그·백업·연결 프로필 등 상태 폴더 |
| `-DataDir` | `%LOCALAPPDATA%\WorkStack\data` | 워크스페이스 데이터 폴더 |
| `-BackupDir` | `<StateRoot>\backups` | 자동 백업 폴더 |
| `-Port` | `8765` | 로컬 서버 루프백 포트 |
| `-BackupRetention` | `14` | 보관할 최신 백업 개수 |
| `-NoShortcut` | 없음 | 바로가기 생성 생략 |

설치 중 동작:

- 설치 폴더·상태 폴더·데이터 폴더·백업 폴더는 서로 겹칠 수 없습니다. 겹치면 `Unsafe path overlap (...)` 오류로 중단합니다. 데이터는 절대 설치 폴더 안에 두지 않습니다.
- `-NoShortcut` 없이 실행하는 대화형 설치는 `%LOCALAPPDATA%\Programs` 아래에만 설치합니다. 다른 위치를 지정하면 `The default interactive installer only writes under LOCALAPPDATA\Programs.` 오류로 중단합니다.
- 번들 Python 런타임을 스테이징 폴더(`<InstallRoot>.staging-<PID>`)에서 먼저 스모크 테스트합니다. 실패하면 `Bundled 64-bit Python 3.12 runtime smoke test failed.`
- 지정한 포트가 사용 중이면 최대 +100 범위에서 빈 포트를 고르고 `Port 8765 is already in use; Work Stack will use <포트> instead.` 경고를 냅니다.
- 상태 폴더에 `config.json`을 씁니다. 필드는 `version`, `install_dir`, `data_dir`, `backup_dir`, `backup_retention`, `port`입니다. 같은 내용을 설치 폴더의 `runtime-config.json`에도 복사합니다.
- 성공하면 다음 세 줄을 출력합니다: `Work Stack installed at <InstallRoot>`, `Planning data remains at <DataDir>`, `Local endpoint: http://127.0.0.1:<포트>/`.

바로가기:

| 링크 | 위치 | 대상 |
| --- | --- | --- |
| `Work Stack.lnk` | 시작 메뉴 `Programs`, 바탕 화면 | `<InstallRoot>\WorkStack.exe` + 인수 `"<InstallRoot>\desktop\python-webview-shell\workstack_desktop.py" --install-root "<InstallRoot>" --state-root "<StateRoot>"` |
| `Work Stack Maintenance.lnk` | 시작 메뉴 `Programs` | `powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "<InstallRoot>\scripts\windows\Maintain-WorkStack.ps1" -InstallRoot "<InstallRoot>" -StateRoot "<StateRoot>"` |

아이콘은 `<InstallRoot>\desktop\python-webview-shell\assets\WorkStack-Mark-Lime-v2.ico`입니다. 설치 후 시작 메뉴의 **Work Stack**을 열어 창이 뜨고 원하는 워크스페이스가 보이는지 확인하세요. 창이 잠깐 유지되었다는 사실만으로 데이터 상태가 검증된 것은 아닙니다.

### 3.1 첫 실행

완전히 비어 있는 데이터 폴더의 첫 실행에서 데스크톱은 연결 레지스트리를 준비하기 **전에** 번들 런타임으로 `maintenance initialize`를 한 번 실행합니다. 비어 있지 않은 폴더는 초기화하지 않습니다. 실패하면 `Work Stack could not create its first workspace: ...`로 중단되며, 상세는 `%LOCALAPPDATA%\WorkStack\logs\initialize.err.log`에 남습니다.

### 3.2 데스크톱 사용

입력란·대화상자가 아닌 곳에서 숫자 키는 다음 화면으로 이동합니다.

| 키 | 화면 | 하는 일 |
| --- | --- | --- |
| `1` | Workspace · **Graph** | 목표·핵심 결과·작업의 맞춤과 의존 관계 |
| `2` | Workspace · **Board** | 상태 흐름. 행에서 작업 상태를 바꿈 |
| `3` | Workspace · **Treemap** | 목표별 규모 |
| `4` | Workspace · **Table** | 표로 훑고 정렬·상태 변경 |
| `5` | **Focus** | 지금 할 후보 |
| `6` | **Context Inbox** | 정제된 캡처. 기본 빌드에서 Microsoft 연동 레인은 꺼져 있음 |
| `7` | **Daily Review** | 체크인·Done/Next/Blockers |
| `8` | **Objective Hub** | 목표와 핵심 결과 추가·진척 |

- 상단 **Search or jump**(`Ctrl+K` 또는 `⌘K`)는 Task·Objective·노트·캡처·활동으로 점프합니다. Workspace 검색창은 `/`로 포커스합니다. 보이는 Task 사이에서 `j`/`k`로 이동합니다.
- 사이드바 **Configure SSOT connections**가 연결 설정입니다(6절).
- **Work Stack updates**에서 설치 버전을 봅니다. 명령행에 `--version`은 없습니다.

**핵심 결과(Key Result)를 어디에 보여 주는가.** Graph/Board/Treemap/Table **위**에 Outcomes 텍스트 카탈로그를 반복하지 않습니다.

- **Graph**: 핵심 결과는 노드입니다. 대상(Target)·상태(Status)·기록된 진척(Recorded progress)·연결된 작업 수가 노드에 있고, 목표·작업과 선으로 이어집니다.
- **Board / Table**: 각 작업에 outcome 칩이 있습니다. 칩을 누르면 그 쌍으로 걸러집니다(`Filter by outcome …`). 연결이 없으면 `Unassigned outcome`.
- **필터**: 검색, 상태, 우선순위, **Filter by objective**, **Filter by outcome**, 준비도(Ready to act / Blocked work), 기한(Overdue / Due today / Due soon / No due date).
- Task도 핵심 결과도 없으면 안내 문구 `Start with an outcome—or capture the first task.`와 **Define an objective** / **Create first task**가 나옵니다. 핵심 결과만 있고 Task가 없으면 Graph 등 뷰가 열리고 배너 `No Tasks yet. Your outcomes are shown below.`가 붙습니다.

목표·핵심 결과의 측정값 편집은 **Objective Hub**에서 합니다. 일일 기록은 **Daily Review**에서 합니다. 상세 화면 절차는 같은 `docs/`의 영문 기능 안내서를 보되, 단축키는 **이 절의 표**가 현재 제품입니다(`1`–`8`).

## 4. 실행 방식과 프로세스 식별

1.0.7의 데스크톱 실행 파일은 설치 폴더 루트의 **`WorkStack.exe`** 입니다. 이 실행 파일은 별도 런처가 아니라 **같은 프로세스 안에서** 번들 `runtime\python312.dll`을 불러 데스크톱 엔트리(`desktop\python-webview-shell\workstack_desktop.py`)를 실행합니다. `pythonw.exe`를 자식으로 띄우지 않고, 이름이 바뀐 복사본으로는 실행을 거부합니다.

소스가 보장하는 프로세스 구성은 다음과 같습니다.

| 역할 | 이미지 | 근거 |
| --- | --- | --- |
| 데스크톱 창(호스트) | `<InstallRoot>\WorkStack.exe` | 바로가기 대상; 설치기는 `WorkStack.exe`가 없으면 설치를 거부 |
| 로컬 서버(자식) | `<InstallRoot>\runtime\python.exe` + `run_work_stack.py --data-dir <데이터> graph serve --host 127.0.0.1 --port <포트>` | 데스크톱이 서버를 직접 띄움 |
| WebView2 렌더러 | `msedgewebview2.exe` (여러 개) | WebView2 런타임이 생성 |

`Stop-WorkStack.ps1`은 정확히 이 설치본의 `WorkStack.exe`(새 방식) 또는 `runtime\pythonw.exe`(이전 버전 방식) 데스크톱 프로세스와, 이 설치본의 `run_work_stack.py`를 실행 중인 `runtime\python.exe` 서버만 종료합니다. 다른 Python 프로세스는 건드리지 않습니다.

> **작업 관리자/`Get-Process`에서 보이는 이름.** 1.0.7 설치본에서 데스크톱 호스트는 `WorkStack.exe`입니다. `Get-Process WorkStack`이 그 이미지를 반환하고, 파일 버전은 `1.0.7.0`, 제품 버전은 `1.0.7`입니다. 같은 관찰에서 데스크톱 `pythonw.exe`는 없었습니다. 1.0.7 이전 설치본에서는 데스크톱이 `pythonw.exe`로 보였습니다. 번들 `runtime\pythonw.exe`는 예전 바로가기를 깨지 않으려고만 남아 있고, 새로 쓰는 바로가기는 `WorkStack.exe`만 가리킵니다.

데스크톱 창을 열 때의 동작:

- 로컬 모드에서는 데이터 폴더에 `workspace.json`이 있으면 서버를 띄우기 **전에** 자동 백업(`maintenance backup`)을 만들고, 백업 폴더의 `workstack-backup-*.zip` 중 최신 `backup_retention`개만 남깁니다. 백업이 실패하면 서버를 시작하지 않고 `Automatic pre-launch backup failed: ...`를 표시합니다.
- 창을 닫으면 그 창이 띄운 로컬 서버(또는 SSH 세션)를 종료합니다.
- 시작 실패는 `Work Stack could not start` 대화상자로 표시되고, 상세는 `%LOCALAPPDATA%\WorkStack\logs\desktop-startup.log`에 기록됩니다.

## 5. 데이터 위치, 워크스페이스 식별, 로그 위치

### 5.1 기본 경로

| 항목 | Windows 기본값 | `LOCALAPPDATA`가 없을 때 |
| --- | --- | --- |
| 데이터 폴더 | `%LOCALAPPDATA%\WorkStack\data` | `~/.local/share/workstack` |
| 실행 시 메타데이터 | `%LOCALAPPDATA%\WorkStack\runtime\<데이터 경로 해시 20자>` | `~/.local/state/workstack/<해시>` |

- `--data-dir`(전역 옵션, 명령 영역보다 앞에 둠) 또는 환경 변수 `WORK_STACK_HOME`이 데이터 폴더를 바꿉니다. `WORK_STACK_RUNTIME`은 실행 시 메타데이터 폴더만 바꿉니다.
- 데이터 폴더의 핵심 파일은 `workspace.json`, `store-meta.json`, `backlog.json`, `activity.json` 등이며, 쓰기 lease는 `.workstack.lock`, 복구 저널은 `.workstack-journal.json`입니다.
- 실행 시 메타데이터 폴더에는 실행 중 서버가 `.workstack-server.json`(소유자 광고)과 `.workstack-capture-token`을 둡니다. CLI는 이 파일로 "GUI 서버가 실행 중인가"를 판단합니다(8.2절).
- 워크스페이스에는 고유 UID(`workspace.json`의 `id`)가 있습니다. 이름이나 경로가 같아도 UID가 다르면 다른 워크스페이스입니다. UID나 내부 JSON을 손으로 고쳐 검사를 통과시키지 않습니다.

> **주의 — 없는 경로를 지정하면 새 워크스페이스가 생깁니다.** `--data-dir`에 오타가 있어도 오류가 나지 않고, 폴더가 만들어진 뒤 비어 있으면 **새 UID의 빈 워크스페이스로 초기화**됩니다. 빈 목록이 나오면 먼저 경로를 의심하세요.

### 5.2 상태 폴더(`%LOCALAPPDATA%\WorkStack`)의 파일

| 경로 | 내용 |
| --- | --- |
| `config.json` | 설치·데이터·백업 경로, 보관 개수, 포트 |
| `connection-registry.json` | **연결 프로필 레지스트리(권위 있는 설정)** — 6절 |
| `remote-connection.json` | 레지스트리의 활성 프로필에서 **생성되는 하위 호환 미러**. 읽어 들이거나 병합하지 않음 |
| `update-settings.json` | 자동 업데이트 설정(`auto_check`, `auto_download`, `install_on_exit`) |
| `updates\` | 내려받은 업데이트, `last-update.json` 영수증, 적용 중 임시 폴더 |
| `backups\` | 자동·수동 백업 `workstack-backup-*.zip` |
| `logs\desktop-startup.log` | 데스크톱 시작 실패 추적 |
| `logs\initialize.out.log`, `logs\initialize.err.log` | 첫 워크스페이스 `maintenance initialize` 출력/오류 |
| `logs\server.out.log`, `logs\server.err.log` | 로컬 서버 표준 출력/오류 |
| `logs\backup.err.log` | 실행 전 자동 백업 오류 |
| `logs\desktop-update.log` | 종료 후 업데이트 적용 로그 |
| `logs\microsoft-webview.log` | Context Inbox의 Microsoft WebView 진단(내용은 기록하지 않음) |
| `desktop-launch\remote-ssh.log` | SSH 터널 표준 출력/오류 |
| `diagnostics\ssh-network-<시각>.json` | `Test-WorkStackRemoteNetwork.ps1` 영수증 |
| `desktop-webview-profile\`, `desktop-microsoft-profile\` | WebView2 프로필 |

## 6. 연결 설정: 로컬 SSOT와 Remote SSH SSOT

### 6.1 연결 레지스트리가 설정의 권위입니다

1.0.7 데스크톱은 시작할 때 연결 레지스트리(`connection-registry.json`)를 준비하고 그중 **활성 프로필**을 골라 로컬 서버를 띄우거나 SSH 터널을 엽니다. 레지스트리 시작은 기본으로 켜져 있고, 환경 변수 `WORKSTACK_CONNECTION_REGISTRY_V1=0`일 때만 꺼집니다. 활성 프로필이 정해지면 그 내용을 `remote-connection.json`에 **한 방향으로** 내보냅니다. 이 미러 파일은 읽히거나 병합되지 않습니다.

레지스트리를 지우거나 손으로 편집해서 연결을 강제로 바꾸지 않습니다. 시작 시 레지스트리가 변하면 `Connection registry changed while the active workspace was verified` 오류로 중단됩니다.

### 6.2 연결 설정 화면 순서

데스크톱 사이드바의 **Configure SSOT connections** 버튼이 **SSOT connections** 대화상자를 엽니다(이 화면은 데스크톱 호스트 안에서 기본 활성이며 일반 브라우저에서는 보이지 않습니다).

1. **Workspace profiles** 목록에서 **Add local** 또는 **Add SSH**를 누릅니다.
2. **Profile label**을 적고, 로컬은 **Local SSOT directory**(또는 **Browse…**), SSH는 **SSH host alias**(**Refresh SSH aliases**로 `~/.ssh/config`의 `Host` 별칭을 가져올 수 있음), **Remote app directory**, **Remote SSOT directory**, 필요하면 **Advanced ports**의 **Preferred local port**(기본 18765)와 **Remote port**(기본 8765)를 입력합니다.
3. **Test connection**을 누르고 **Detected workspace identity**로 표시되는 UID가 기대한 워크스페이스인지 확인합니다. 저장된 프로필의 UID와 감지된 UID가 다르면 `These identities differ. Activation is blocked ...` 경고와 함께 활성화가 막힙니다. 이때는 **Review workspace synchronization**으로 동기화 상태를 먼저 검토합니다.
4. 저장만 할 때는 **Save profile**, 다음 시작부터 사용할 때는 **Save and activate after restart**를 누릅니다. 첫 프로필은 저장과 활성화를 함께 해야 합니다(`Your first profile must be saved and activated together.`). 활성화는 현재 레지스트리 상태에 대해 **방금 테스트한** 프로필에만 허용됩니다(`Test this exact profile against the current registry before scheduling activation.`).
5. **Work Stack을 다시 시작**해야 활성 연결이 바뀝니다. 저장 또는 테스트 성공만으로는 현재 연결이 바뀌지 않습니다. 재시작 후 서버/터널이 준비되고 선택한 워크스페이스와 동기화 상태가 맞아야 활성화가 확정됩니다(맞지 않으면 `Connection activation remains pending because the running server is not in sync with the selected workspace. ...`).

이 화면은 SSOT 디렉터리를 삭제·복사·병합하지 않습니다(대화상자 설명 그대로).

### 6.3 Remote SSH SSOT의 전제 조건

SSH 연결은 Linux에 Work Stack이나 Python을 설치해 주지 않습니다. 관리자가 먼저 준비합니다.

- **Windows**: OpenSSH 클라이언트(`ssh.exe`)가 PATH에 있어야 합니다. 없으면 `OpenSSH client was not found. Enable the Windows OpenSSH Client feature first.` 오류입니다. SSH 별칭은 `%USERPROFILE%\.ssh\config`에 둡니다. 비밀번호 입력 없이 접속되는 인증(에이전트/키)과 검증된 호스트 키가 필요합니다. Work Stack은 비밀번호·개인 키·토큰·호스트 키 우회를 저장하지 않습니다.
- **Linux**: 원격 앱 폴더(예: `/srv/workstack/app`)에 릴리스와 같은 버전의 Work Stack 소스와 빌드된 `frontend/dist`, 잠금된 의존성(`python3 -m pip install --require-hashes -r requirements.txt`)이 있어야 합니다. 원격 데이터 폴더(예: `/srv/workstack/ssot`)는 이미 초기화된 워크스페이스여야 하며 `store-meta.json`과 `workspace.json`이 존재해야 합니다. 비대화형 SSH 명령에서 `python3`가 올바른 환경을 가리켜야 합니다(데스크톱은 가상환경 활성화 스크립트를 대신 실행하지 않습니다).

데스크톱이 실제로 실행하는 SSH 명령의 형태는 다음과 같습니다(옵션은 고정이며 사용자가 바꿀 수 없습니다).

```text
ssh -T -o BatchMode=yes -o ExitOnForwardFailure=yes -o StrictHostKeyChecking=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -L 127.0.0.1:<로컬포트>:127.0.0.1:<원격포트> -- <별칭> "test -f <원격데이터>/store-meta.json && test -f <원격데이터>/workspace.json && cd -- <원격앱> && exec python3 <원격앱>/run_work_stack.py --data-dir <원격데이터> graph serve --host 127.0.0.1 --port <원격포트> --public-port <로컬포트> --exit-with-parent"
```

- 양쪽 끝 모두 루프백에만 바인딩됩니다. Linux 서버는 SSH 세션이 끝나면 함께 종료됩니다(`--exit-with-parent`).
- **Preferred local port**는 선호값일 뿐입니다. 이미 사용 중이면 그 실행에서만 OS가 고른 빈 포트를 쓰고 저장값은 바꾸지 않습니다. 점유 프로세스를 종료하지 않습니다.
- 원격 서버가 보고하는 워크스페이스 UID가 프로필의 UID와 다르면 `Remote Work Stack workspace identity <실제> does not match remote-connection.json (<기대>). Verify the remote directory and update the saved Workspace ID.` 로 실패합니다. 원격 프로토콜 버전이 지원 범위 밖이거나 세션 중 바뀌면 `Verify or upgrade remote_app_dir, then restart Work Stack.` 안내와 함께 중단됩니다.
- 창이 열려 있는 동안 상태 모니터가 SSH 자식 프로세스와 헬스 엔드포인트를 감시합니다. 헬스 실패 2회 연속(또는 SSH 종료) 뒤 최대 3회 재연결을 시도하고, 실패하면 **Disconnected** 상태와 함께 `Remote SSOT reconnection was exhausted. Review SSH diagnostics, then use Reconnect now.`를 표시합니다.

접속 전에 Windows에서 직접 확인할 수 있는 읽기 전용 명령(데스크톱 점검 명령과 같은 옵션):

```text
ssh -T -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10 -- <별칭> "command -v python3"
```

### 6.4 이전 방식 스크립트에 대한 주의

`scripts\windows\Configure-WorkStackRemote.ps1`(`-SshHostAlias`, `-RemoteAppDir`, `-RemoteDataDir`, `-WorkspaceId`, `-LocalForwardPort`, `-RemotePort`, `-UseLocal`, `-StateRoot`, `-Check`)은 **`remote-connection.json`만** 쓰는 이전 방식 도구입니다. 1.0.7 데스크톱에서 이 파일은 활성 레지스트리 프로필에서 다시 생성되는 미러이므로, 이 스크립트를 실행해도 **활성 프로필은 바뀌지 않습니다.** `-Check`는 방금 쓴 미러 파일을 대상으로 `--check-remote-connection`을 실행하는 점검이지, 현재 활성 프로필의 확인도 읽기 전용 명령도 아닙니다. 현재 프로필을 바꾸려면 6.2절의 화면을 쓰세요.

같은 이유로 `Test-WorkStackRemoteNetwork.ps1`(`-StateRoot`, `-Samples`, `-OutFile`)도 `remote-connection.json`을 읽습니다. 데스크톱이 한 번 정상 시작한 뒤라면 이 파일은 활성 프로필의 미러이므로 진단에 쓸 수 있고, 결과는 `diagnostics\ssh-network-<시각>.json`에 남습니다.

## 7. 각 SSH 에이전트 호스트에 CLI와 Skill 설치

에이전트 Skill은 **에이전트가 실행되는 쪽**에 설치합니다. Windows에서 에이전트가 돌면 Windows 사용자 영역에, SSH로 접속한 Linux에서 돌면 **그 Linux 사용자**의 영역에 설치합니다. Windows GUI의 SSH 연결 설정은 원격 Skill 설치를 대신하지 않습니다.

### 7.1 Skill 파일과 설치 위치

1.0.7 소스 트리의 `integrations/agent-skill/work-stack`에는 정확히 세 파일이 있습니다: `SKILL.md`, `references/commands.md`, `references/journal-policy.md`. 스크립트나 실행 파일은 없습니다. 저장소가 안내하는 Codex 사용자 범위 설치 위치는 다음입니다.

| 호스트 | 위치 |
| --- | --- |
| Linux | `$HOME/.agents/skills/work-stack` |
| Windows | `%USERPROFILE%\.agents\skills\work-stack` |

다른 에이전트 제품을 쓰면 그 제품이 실제로 읽는 Skill 위치를 확인해야 합니다. 파일 복사가 성공했다고 에이전트가 Skill을 읽었다고 볼 수는 없습니다. (저장소 루트의 `SKILL.md`는 이름이 `portable-work-stack`인 별개의 일반 Skill이며, 이 절의 대상이 아닙니다.)

기존 설치가 있으면 덮어쓰지 말고 먼저 비교합니다. 아래 예시는 대상이 없을 때만 복사합니다. `<checkout-root>`는 검증한 공개 소스 체크아웃입니다.

```sh
SkillDest="$HOME/.agents/skills/work-stack"
test ! -e "$SkillDest" || { echo "existing skill: review first"; exit 1; }
mkdir -p "$(dirname "$SkillDest")"
cp -R "<checkout-root>/integrations/agent-skill/work-stack" "$SkillDest"
```

```powershell
$SkillDest = Join-Path $env:USERPROFILE '.agents\skills\work-stack'
if (Test-Path -LiteralPath $SkillDest) { throw 'existing skill: review first' }
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $SkillDest) | Out-Null
Copy-Item -LiteralPath '<checkout-root>\integrations\agent-skill\work-stack' -Destination $SkillDest -Recurse
```

### 7.2 무해한 사후 검증

체크아웃 루트에서 저장소가 고정한 검증기를 실행합니다. 네트워크·시계·프로필 상태를 쓰지 않는 결정적 검사입니다.

```text
python -I quality/agent-p0-oracle/validate_skill.py "$HOME/.agents/skills/work-stack"
python -I quality/agent-p0-oracle/validate_skill.py "%USERPROFILE%\.agents\skills\work-stack"
```

성공은 종료 코드 0과 정확히 `{"skill":"work-stack","valid":true,"violations":[]}` 출력입니다. 종료 코드 2는 위반, 3은 사용법·IO 오류입니다. 이 검사는 복사된 트리의 유효성만 증명하며, 에이전트 호스트가 Skill을 발견·로드했는지는 증명하지 않습니다.

### 7.3 CLI 접두사와 세 가지 명시 입력

Skill은 파일을 탐색하거나 권위(authority)를 만들지 않습니다. 에이전트의 실행 정책에 다음 세 값을 **명시적으로** 둡니다. 공유 Skill 본문에 개인 경로나 인증 정보를 넣지 않습니다.

```text
command prefix: <pfx>        예) python -I <checkout-root>/run_work_stack.py
data directory: <data-dir>   예) /srv/workstack/ssot
expected workspace UID: <ws-uid>
```

Linux 소스 실행의 접두사 `python3 -I <checkout-root>/run_work_stack.py`는 런처가 자기 체크아웃 폴더를 `sys.path` 앞에 넣기 때문에 격리 모드(`-I`)에서도 동작합니다. 먼저 `--help`로 읽기 전용 확인을 합니다.

### 7.4 지원 범위

Skill의 권한은 **명시적으로 선택한 Task 하나의 읽기**와 **제한된 Daily Review 체크포인트 추가**뿐입니다. Task 상태 변경, Objective 편집, 관계 편집, 동기화 채택·복원·마이그레이션·리바인드, 메시지 전송, JSON/NDJSON/DB/SSOT 파일 직접 편집, SSH 자격 증명·브라우저 프로필·토큰 접근은 금지입니다. 소스 문서의 P0 경계는 다음과 같습니다.

| 기능 | P0 |
| --- | --- |
| 사용자 범위 수동 Skill 설치 | 지원 |
| 검증된 소스 런처 접두사 | 지원 |
| 명시적 v3 경로와 워크스페이스 UID | 필수 |
| 자동 Skill 업데이트/제거, 패키지 런처/PATH | P0b(미지원) |
| 워크스페이스 자동 발견 | 보류 |
| 데스크톱 SSH/Linux 레지스트리 연동 | P0b |
| 설치기/업데이터/공개 브라우저 매트릭스 | 공개 릴리스 게이트 |

## 8. CLI

### 8.1 접두사, 도움말, 읽기 명령

Windows 설치본의 접두사입니다. `$DataDir`은 `config.json`의 `data_dir`(또는 활성 로컬 프로필의 경로)로 바꿉니다.

```powershell
$InstallRoot = Join-Path $env:LOCALAPPDATA 'Programs\WorkStack'
$Python = Join-Path $InstallRoot 'runtime\python.exe'
$Entry = Join-Path $InstallRoot 'run_work_stack.py'
$DataDir = Join-Path $env:LOCALAPPDATA 'WorkStack\data'
& $Python $Entry --help
& $Python $Entry backlog --help
& $Python $Entry agent --help
& $Python $Entry --data-dir $DataDir backlog list --status active
& $Python $Entry --data-dir $DataDir backlog show T-0001
& $Python $Entry --data-dir $DataDir okr list --status active
& $Python $Entry --data-dir $DataDir okr rollup
& $Python $Entry --data-dir $DataDir worklog list --date 2026-09-04
& $Python $Entry --data-dir $DataDir weekly --days 7
& $Python $Entry --data-dir $DataDir graph export --out graph-data.json
```

Linux에서는 `python3 -I /srv/workstack/app/run_work_stack.py --data-dir /srv/workstack/ssot ...` 처럼 같은 규칙을 씁니다. 전역 `--data-dir`은 항상 명령 영역(`backlog`, `okr`, `worklog`, ...)보다 앞에 둡니다.

- 명령행에 `--version` 옵션은 **없습니다.** 버전은 릴리스 설명과 앱의 **Work Stack updates** 대화상자의 **Installed** 항목으로 확인합니다.
- 일반 명령의 결과는 JSON으로 출력됩니다. 도메인 오류는 `error: <코드>: <메시지>`, 그 외 값·IO 오류는 `error: <메시지>` 형식으로 표준 오류에 나오며 **종료 코드 2**입니다. `agent` 명령은 별도 계약(0 성공, 1 명령 실패, 2 사용법 오류)을 따릅니다.
- 명령 이름·플래그는 아래 표 그대로입니다. 여기 없는 플래그는 소스에 없습니다.

| 명령 | 인수·옵션 |
| --- | --- |
| `backlog add <title>` | `--detail`, `--priority {P0,P1,P2,P3}`(기본 P2), `--due`, `--tag`(반복), `--objective`(반복), `--parent`, `--depends-on`(반복) |
| `backlog list` | `--status`(기본 `active`) |
| `backlog show <id>` | |
| `backlog start|done|drop|reopen <id>` | |
| `backlog note <id> <text>` | |
| `backlog subtask <add|start|done|drop|reopen> <task> <subtask_or_title>` | `--priority {P0..P3}` |
| `okr add-objective <text>` | `--quarter` |
| `okr add-key-result <objective> <text>` | `--target` |
| `okr list` | `--status`(기본 `active`) |
| `okr link <objective> <task>` | |
| `okr progress <objective> <key_result> <value:int>` | |
| `okr rollup` | |
| `worklog checkin` | `--time`, `--date` |
| `worklog add <task>` | `--done`(반복), `--next`(반복), `--blocker`(반복), `--date` |
| `worklog checkpoint-state <checkpoint>` | `--stdin`(필수), `--idempotency-key`(필수) |
| `worklog list` | `--date` |
| `weekly` | `--end`, `--days`(기본 7) |
| `note <text>` | `--link`(반복) |
| `agent [--workspace-uid <uid>] status|context --task <id>|checkpoint --intent-id <id> --stdin|apply --stdin --intent-id <id>` | |
| `maintenance backup --out <dir>` / `verify <archive>` / `restore <archive> --to <dir> [--replace] [--safety-backups <dir>]` / `relocate --to <dir>` / `initialize` | `initialize`는 비어 있거나 없는 데이터 폴더에만 새 워크스페이스를 만듦 |
| `graph export [--out <file>]` / `graph serve [--host] [--port] [--public-port] [--exit-with-parent] [--seed-demo]` | |

### 8.2 GUI가 실행 중일 때와 아닐 때

CLI는 명령을 실행하기 전에 그 데이터 폴더의 실행 시 메타데이터 폴더에 `.workstack-server.json`(소유자 광고)이 있는지 봅니다.

| 소유자 광고 상태 | CLI 동작 |
| --- | --- |
| **없음(absent)** | 로컬 배타 경로. Store lease를 잡고 직접 씁니다. |
| **있음(present, 일반 파일)** | 아래 표의 명령은 실행 중인 서버(GUI가 띄운 서버)로 전달됩니다. 전달 전에 세션·저장소·동기화 사전 점검을 하고, 보내기 직전에 광고가 그대로인지 다시 확인합니다. |
| **잘못됨(invalid: 디렉터리·심볼릭 링크 등)** | `Work Stack server runtime metadata is not a readable regular file`로 거부합니다. 로컬 쓰기로 넘어가지 **않습니다.** |

광고가 있는데 서버가 응답하지 않거나, 사전 점검 중 광고가 사라지거나 바뀌면 각각 `Work Stack server runtime metadata is not available`, `Work Stack server runtime metadata changed before the <작업> was sent` 로 거부합니다. 광고 파일을 지우거나 고쳐서 쓰기를 강행하지 않습니다.

명령군별 전달 방식과 결과가 불명확할 때의 정책:

| 명령군 | 실행 중 서버가 있을 때 | 응답 유실 등으로 결과가 불명확할 때 |
| --- | --- | --- |
| `note`, `okr add-objective`, `okr add-key-result`, `backlog note`, `backlog subtask add` | 실행마다 새로 만든 멱등 키를 붙인 POST 한 번 | 같은 키·같은 본문으로 **내부에서 정확히 한 번** 재전송. 그래도 불명확하면 `... commit is unknown; inspect the ... before retrying`로 종료. 명령을 다시 실행하면 **새 키**이므로 재시도가 아니라 새 요청입니다. |
| `backlog start/done/drop/reopen`, `backlog subtask start/done/drop/reopen` | PATCH 한 번 | 재전송·재조회·로컬 우회 없이 불명확한 결과로 종료 |
| `worklog checkin`, `worklog add`, `backlog add`, `okr link`, `okr progress` | 키 없는 POST 한 번(요청 본문 1 MiB 제한) | 재전송 없이 `... commit is unknown; inspect the ... before retrying`로 종료 |
| `worklog checkpoint-state` | **실행 중 서버가 반드시 필요.** 사용자가 지정한 키로 POST 한 번 | 자동 재전송 없음. 같은 본문·같은 키를 보존한 **명시적** 재시도만 가능. 로컬 형식 없음(서버가 없으면 `Work Stack server is not running for this data directory`) |
| `agent checkpoint` | Agent 계약에 따라 실행 중 서버 또는 배타 로컬 경로 | 내부 동일 재전송 한 번 뒤에도 불명확하면 `commit_unknown`. Skill은 즉시 중단 |
| 그 밖의 명령(`backlog list/show`, `okr list/rollup`, `worklog list`, `weekly`, `graph export`, `snapshot`, `maintenance`) | 전달되지 않고 로컬 경로를 씁니다 | 해당 없음 |

서버가 확정적인 HTTP 오류를 돌려주면 `the running Work Stack server refused the <작업> (HTTP <코드>)`로 종료하며 재시도하지 않습니다.

> **로컬 경로 명령과 실행 중 GUI.** 전달되지 않는 명령은 초기화 단계에서 데이터 폴더의 `.workstack.lock` lease를 잠시 잡습니다. GUI가 띄운 서버가 그 lease를 쥐고 있는 동안에는 `the Work Stack data directory is already owned by another writer` 오류(종료 코드 2)로 거절합니다. 백업·복원·이동은 서버를 먼저 멈춰야 합니다(10절).

### 8.3 명령 예시

플래그와 위치 인수는 소스 파서와 같습니다. **운영 중인 워크스페이스에 붙여 넣지 마세요.** 연습은 비어 있는 별도 `--data-dir`에서만 합니다. ID는 명령이 돌려준 값을 쓰고 다음 ID를 추측하지 않습니다.

Task:

```powershell
& $Python $Entry --data-dir $DataDir backlog add '릴리스 체크리스트 작성' --priority P1 --due 2026-09-10 --tag release
& $Python $Entry --data-dir $DataDir backlog show T-0001
& $Python $Entry --data-dir $DataDir backlog note T-0001 '체크리스트 초안 검토 완료'
& $Python $Entry --data-dir $DataDir backlog subtask add T-0001 '체크섬 절 검토' --priority P2
```

상태:

```powershell
& $Python $Entry --data-dir $DataDir backlog start T-0001
& $Python $Entry --data-dir $DataDir backlog done T-0001
& $Python $Entry --data-dir $DataDir backlog reopen T-0001
& $Python $Entry --data-dir $DataDir backlog drop T-0001
```

체크인·일일 기록:

```powershell
& $Python $Entry --data-dir $DataDir worklog checkin --time 09:00 --date 2026-09-04
& $Python $Entry --data-dir $DataDir worklog add T-0001 --done '체크섬 절 검토' --next '설치 절 검토' --blocker '릴리스 URL 미확정' --date 2026-09-04
& $Python $Entry --data-dir $DataDir worklog list --date 2026-09-04
& $Python $Entry --data-dir $DataDir weekly --end 2026-09-04 --days 7
```

Backlog 조회:

```powershell
& $Python $Entry --data-dir $DataDir backlog list --status active
```

OKR:

```powershell
& $Python $Entry --data-dir $DataDir okr add-objective '배포 품질 개선' --quarter 2026-Q3
& $Python $Entry --data-dir $DataDir okr add-key-result O-1 '릴리스 회귀 0건' --target '0건'
& $Python $Entry --data-dir $DataDir okr link O-1 T-0001
& $Python $Entry --data-dir $DataDir okr progress O-1 KR-1 40
& $Python $Entry --data-dir $DataDir okr list --status active
& $Python $Entry --data-dir $DataDir okr rollup
```

- `okr progress`의 값은 정수입니다. 서버 전달 경로에서는 0–100으로 잘라 반영하고 100이면 상태가 `done`이 됩니다. 값 `0`은 기록한 값이며 미기록과 같지 않습니다.
- Objective ID(`O-1`), Key Result ID(`KR-1`)는 소스 예시의 형식입니다. Key Result ID는 Objective 안에서만 유일합니다.
- 노트: `& $Python $Entry --data-dir $DataDir note '교차 관찰' --link T-0001 --link O-1`

Agent 계약(자동화용):

```powershell
$Uid = 'REPLACE_WITH_VERIFIED_WORKSPACE_UID'
& $Python $Entry --data-dir $DataDir agent --workspace-uid $Uid status
& $Python $Entry --data-dir $DataDir agent --workspace-uid $Uid context --task T-0001
```

`agent status`가 종료 코드 0으로 `contract: workstack.cli.v1`, 기대 UID와 같은 `actual_workspace_uid`, `storage_format: v3`, `capability_supported: true`, `ready: true`를 돌려줄 때만 진행합니다. `meta.transport`는 `running-server` 또는 `exclusive-local`입니다. 거부 코드에는 `invalid_authority`, `workspace_mismatch`, `owner_unavailable`, `capability_not_enabled`, `invalid_body`, `context_too_large`, `commit_unknown`, `internal_error`가 있습니다.

## 9. 체크포인트·충돌·재시도 원칙

### 9.1 Agent 체크포인트

```text
<pfx> --data-dir <data-dir> agent --workspace-uid <ws-uid> checkpoint --intent-id <STABLE_INTENT_ID> --stdin
```

표준입력의 JSON 필드는 **정확히** `task_id`, `date`, `done`, `next`, `blockers`입니다.

```json
{"task_id":"T-0001","date":"2026-09-04","done":["체크섬 절 검토"],"next":["설치 절 검토"],"blockers":[]}
```

- 입력 전체 32 KiB 이내, 각 목록 최대 20개, 항목은 공백 정리 후 비어 있지 않은 1,000자 이내, 세 목록 중 하나 이상에 항목이 있어야 합니다. 날짜는 정규 `YYYY-MM-DD`, Task ID는 `T-` 뒤 4자리 이상 숫자, intent ID는 8–128자의 `[A-Za-z0-9._:-]`입니다.
- 파일은 UTF-8로 준비합니다. Windows PowerShell 5.1의 기본 파이프 인코딩이 한글을 바꿀 수 있으므로, 소스 문서가 권하는 방식처럼 파일 바이트를 그대로 표준입력에 넘기는 도우미를 쓰거나 인코딩을 확인하세요.
- 같은 intent ID + 같은 본문은 같은 논리적 체크포인트이며 `meta.replayed: true`로 멱등 재생될 수 있습니다. 같은 ID에 다른 내용을 쓰지 않습니다.
- `commit_unknown`(`error.retryable: false`, `meta.commit_state: "unknown"`)이 오면 **중단하고 같은 intent ID를 보존**합니다. 새 ID로 다시 쓰거나, `worklog list`의 문장이 같다는 이유로 기록되었다고 추정하지 않습니다.

### 9.2 체크포인트 상태 전환 명령

```text
worklog checkpoint-state <CHECKPOINT_ID> --stdin --idempotency-key <KEY>
```

- 이 명령은 1.0.7 소스 CLI에 **있습니다.** `--stdin`과 `--idempotency-key`가 모두 필수입니다. 상태·리비전·사유를 명령줄 옵션으로 주는 형식(`--state`, `--revision`, `--reason`)은 **없습니다.**
- 본문은 정확히 `state`, `revision`, `reason`이고 `reason`은 정확히 `code`, `explanation`입니다. 예: `{"state":"superseded","revision":0,"reason":{"code":"<정책이 정한 코드>","explanation":"..."}}`. 허용되는 `state`·`reason.code` 값은 서버 측 정책 모듈이 정하며 이 문서에 나열하지 않습니다.
- `CHECKPOINT_ID`는 `CP-` 뒤 64자리 16진수이고, 키는 8–128자의 `[A-Za-z0-9._:-]`입니다. 응답 이벤트의 `revision`은 요청 `revision + 1`이어야 하며 짝수는 `active`, 홀수는 `superseded`와 짝을 이룹니다.
- 실행 중 서버가 반드시 필요합니다. 재시도는 **같은 본문과 같은 키**를 그대로 보내는 명시적 재시도뿐이며, 키를 바꾸거나 리비전을 자동으로 올려 보내는 것은 재시도가 아니라 새 요청입니다.
- 이 명령을 운영 자동화에 넣기 전에 그 설치본의 `worklog --help`와 서버 정책을 확인하세요. 허용 `state`·`reason.code` 값은 이 문서에 나열하지 않습니다.

### 9.3 리비전 충돌과 외부 변경

- 리비전 충돌은 다른 변경이 있었다는 뜻입니다. 최신 상태를 다시 읽고 사용자가 새 의도를 결정합니다. 오래된 리비전을 강제로 적용하거나 리비전만 바꿔 재전송하지 않습니다.
- 외부에서 SSOT 파일을 직접 고치면 앱은 쓰기를 멈추고 검토를 요구합니다. 자동 병합·복제 기능이 아닙니다. 잠금 파일이나 소유자 광고를 지워 쓰기를 강행하지 않습니다.

## 10. 백업·검증·복원·이동

### 10.1 시작 메뉴의 Work Stack Maintenance

**Work Stack Maintenance**는 Backup / Verify / Restore / Relocate를 안내 창으로 제공합니다. Backup·Restore·Relocate는 Work Stack이 꺼져 있어야 하며, 실행 중이면 이 설치본의 프로세스만 종료할지 묻습니다. Restore와 Relocate는 명시적 확인이 필요합니다.

> **대상 범위.** 이 도구는 **`config.json`에 적힌 `data_dir`·`backup_dir`** 만 다룹니다. 연결 설정 화면에서 선택한 다른 로컬 프로필이나 SSH 원격 워크스페이스를 자동으로 백업하지 않습니다. 원격 데이터의 백업은 원격 관리자가 그 환경에서 명시적인 데이터 경로로 수행합니다. Windows의 로컬 백업을 원격 데이터 백업으로 간주하지 않습니다.

비대화형 형식: `Maintain-WorkStack.ps1 -Action <Backup|Verify|Restore|Relocate> [-BackupPath <zip>] [-Destination <dir>] [-Confirm]`.

### 10.2 명령행

서버를 멈춘 뒤, 실제 데이터 경로를 `--data-dir`로 명시합니다. 아래 경로는 자리표시자입니다.

```powershell
$BackupDir = Join-Path $env:LOCALAPPDATA 'WorkStack\backups'
& $Python $Entry --data-dir $DataDir maintenance backup --out $BackupDir
# 위 명령이 JSON으로 출력한 실제 백업 파일 경로를 지정합니다.
$Archive = 'REPLACE_WITH_ACTUAL_BACKUP_ZIP'
& $Python $Entry maintenance verify $Archive
```

복원은 먼저 **비어 있는 별도 목적지**에 합니다.

```powershell
& $Python $Entry maintenance restore $Archive --to 'REPLACE_WITH_EMPTY_DESTINATION'
```

기존 워크스페이스를 교체하려면 `--replace`와 `--safety-backups`가 **둘 다** 필요합니다. 없으면 `destination already contains a Work Stack store` 또는 `a safety backup directory is required when replacing`으로 거부됩니다.

```powershell
& $Python $Entry maintenance restore $Archive --to $DataDir --replace --safety-backups (Join-Path $BackupDir 'pre-restore')
```

이동은 원본을 지우지 않고 빈 목적지로 복사·검증합니다. Maintenance 창은 성공 후 `config.json`의 `data_dir`을 바꿔 주지만, 명령행으로 이동했다면 `config.json`(과 연결 프로필)을 직접 갱신해야 합니다.

```powershell
& $Python $Entry --data-dir $DataDir maintenance relocate --to 'REPLACE_WITH_EMPTY_DESTINATION'
```

- 백업 파일 이름은 `workstack-backup-<워크스페이스>-<시각>.zip` 형식이고 검증 시 128 MiB 상한이 있습니다. 복원 전에 아카이브 전체를 검증하며, 복원 뒤 워크스페이스 UID가 아카이브와 다르면 실패합니다.

## 11. 업데이트

### 11.1 자동 업데이트(앱 내)

데스크톱은 시작할 때 한 번 `https://github.com/Shinick-Han/work-stack-public/releases/latest/download/workstack-update.json`을 읽습니다. 매니페스트는 `schema_version` 1, `channel` `stable`이어야 하고, 설치 파일과 사이드카의 **이름·URL·SHA-256·크기**가 정확히 맞아야만 받아들입니다(설치 파일 100 MiB, 사이드카 1 KiB 상한, 설치 버전보다 오래된 버전은 거부). 내려받은 파일은 `%LOCALAPPDATA%\WorkStack\updates`에 두고 다이제스트를 다시 확인합니다.

**공개 채널에 v1.0.7이 올라가기 전에는** 이 검사기가 1.0.7 설치 파일을 내려주지 않습니다. 그때는 11.2절의 검증된 설치 파일로 올립니다.

**Work Stack updates** 대화상자(사이드바의 업데이트 상태 항목)에는 **Installed / Latest / Status**, 버튼 **Check now**, **Download update**(available 상태), **Install and restart**(ready 상태), **Release notes**, 그리고 **Automatic updates** 설정 **Check automatically**, **Download automatically**, **Install when Work Stack closes**가 있습니다. 세 설정의 기본값은 모두 켜짐이며 `update-settings.json`에 저장됩니다.

검증된 업데이트는 **Work Stack이 닫힐 때** 적용됩니다. 종료 시 `scripts\windows\Apply-WorkStackUpdate.ps1`이 숨김 창으로 실행되어:

1. 데스크톱 프로세스 종료를 최대 90초 기다립니다.
2. 현재 설치 폴더를 `updates\.rollback-<버전>-<PID>`로 복사하고 `config.json` 바이트를 보존합니다.
3. 설치본의 `Update-WorkStack.ps1`·`Test-WorkStackSetup.ps1` 복사본으로 검증 후 설치합니다(이 단계는 바로가기를 쓰지 않습니다).
4. 새 `WorkStack.exe`를 실행하고 **1.5초 안에 종료하지 않으면** 커밋으로 간주합니다. 그 뒤에야 바로가기를 갱신합니다.
5. 영수증 `updates\last-update.json`의 `status`는 `installed`, `rolled-back`, `recovery-required`, `failed` 중 하나이고 로그는 `logs\desktop-update.log`입니다.

> 영문 `docs/WORKSTACK_WINDOWS_INSTALL_BACKUP_USER_GUIDE_2026-08-30.md`는 같은 설치기의 짧은 영문 짝입니다. 1.0.7의 연결 레지스트리·프로세스 이름·수동 업그레이드 주의는 **이 한국어 안내서**가 더 깁니다.

### 11.2 수동 업데이트(데이터 보존)

검증된 새 설치 파일과 사이드카를 같은 폴더에 둡니다.

**권장.** 연결 레지스트리에 활성 **로컬** 프로필이 있으면 설치 파일을 `-InstallRoot`와 `-StateRoot`만 넘겨 다시 실행합니다. `-DataDir`을 주지 않으면 설치기가 활성 로컬 프로필의 데이터 경로를 씁니다.

```powershell
powershell -ExecutionPolicy Bypass -File .\WorkStack-Setup-1.0.7.ps1 `
  -InstallRoot "$env:LOCALAPPDATA\Programs\WorkStack" `
  -StateRoot "$env:LOCALAPPDATA\WorkStack"
```

`config.json`의 `data_dir`이 활성 로컬 프로필과 다를 때 `-DataDir`를 넘기면 `Explicit DataDir conflicts with the selected local connection profile.`로 거절됩니다. 설치된 `Update-WorkStack.ps1`은 `config.json`의 `data_dir`을 **항상** `-DataDir`로 넘기므로, 두 경로가 어긋난 설치본에서는 이 업데이터가 같은 오류로 멈춥니다. 그때는 위의 권장 명령을 씁니다.

`config.json`과 활성 로컬 프로필의 데이터 경로가 **같을 때**는 설치된 업데이터를 쓸 수 있습니다.

```powershell
& "$env:LOCALAPPDATA\Programs\WorkStack\scripts\windows\Update-WorkStack.ps1" -SetupPath 'C:\path\to\WorkStack-Setup-1.0.7.ps1'
```

매개변수: `-SetupPath`(필수, `.ps1`이어야 함), `-ChecksumPath`(사이드카가 다른 곳에 있을 때), `-InstallRoot`, `-StateRoot`, `-NoShortcut`. 업데이터는 **먼저** 파일명·다이제스트를 검증하고, 그다음 `config.json`을 읽어 `data_dir`·`backup_dir`·`port`·`backup_retention`을 설치 파일에 넘깁니다. 설정이 없으면 `Work Stack is not configured. Install it before updating.`로 중단합니다.

설정이 없는 새 설치 파일을 다시 실행하면 기존 `config.json` 값을 유지합니다. 활성 로컬 프로필이 있으면 그 데이터 경로가 우선입니다.

업데이트 중 설치기의 동작(실제 구현 순서):

1. 스테이징 폴더에 새 페이로드를 풀고 번들 Python을 스모크 테스트합니다.
2. `Stop-WorkStack.ps1`로 **이 설치본의** 프로세스만 종료합니다.
3. 데이터 폴더에 `workspace.json`이 있으면 **스테이징된 새 런타임으로** 업그레이드 전 백업을 만듭니다. 실패하면 `Pre-upgrade backup failed; installation was not changed.`
4. 기존 설치 폴더를 `<InstallRoot>.rollback-<PID>`로 옮기고 스테이징을 제자리로 옮깁니다(교체).
5. `config.json`을 쓰고 바로가기를 갱신합니다.
6. 이후 단계에서 예외가 나면 rollback 폴더를 되돌리고 원래 `config.json` 바이트를 복원합니다. 이전 런타임이 아직 잠겨 있으면 `Work Stack was installed, but the previous runtime is still locked and could not be removed: ... It is safe to remove this rollback directory after Work Stack processes have exited.` 경고를 남깁니다.

데이터 폴더는 설치 폴더 밖에 있으므로 업데이트로 옮겨지거나 지워지지 않습니다. 업데이트 전에는 앱과 그 워크스페이스를 쓰는 CLI·에이전트를 종료하세요.

## 12. 제거

```powershell
& "$env:LOCALAPPDATA\Programs\WorkStack\scripts\windows\Uninstall-WorkStack.ps1"
```

매개변수: `-InstallRoot`(기본 `%LOCALAPPDATA%\Programs\WorkStack`), `-StateRoot`(기본 `%LOCALAPPDATA%\WorkStack`), `-RemoveData`.

- 설치 폴더가 `%LOCALAPPDATA%\Programs` 아래가 아니면 `Refusing to uninstall outside LOCALAPPDATA\Programs.`로 중단합니다.
- 이 설치본의 프로세스를 종료하고, **소유가 확인된** 세 바로가기(`Work Stack.lnk` 시작 메뉴·바탕 화면, `Work Stack Maintenance.lnk`)만 지웁니다. 소유를 확인할 수 없는 링크는 `Preserving shortcut whose Work Stack ownership could not be verified: ...` 경고와 함께 남깁니다.
- 기본 동작은 **프로그램 폴더만** 지우고 `Work Stack was removed. Planning data and backups remain under <StateRoot>.`를 출력합니다.
- `-RemoveData`를 붙이면 상태 폴더(`StateRoot`) 전체 — 데이터·백업·설정·연결 프로필·로그 — 를 지웁니다. 되돌릴 수 없습니다. 지우기 전에 `StateRoot`가 `%LOCALAPPDATA%` 아래인지 검사하며, 아니면 `Refusing to remove data outside LOCALAPPDATA.`로 중단합니다. 성공 메시지는 `Work Stack and local planning data were removed.`입니다.
- `-RemoveData`는 `StateRoot` **바깥**에 둔 사용자 지정 데이터·백업 폴더나 SSH 원격 워크스페이스는 건드리지 않습니다. 그 폴더들은 남습니다.

제거 전 순서: (1) 앱·CLI·에이전트 종료 → (2) `maintenance backup`과 `maintenance verify` → (3) 실제 데이터 폴더 경로 기록 → (4) 제거. 제거 후 남은 폴더가 보여도 경로를 확인하기 전에는 재귀 삭제하지 않습니다.

## 13. 롤백: 자동 실패 복구와 의도적 다운그레이드는 다릅니다

**자동 실패 복구**: 설치기는 설치 중 예외가 나면 이전 프로그램 폴더와 `config.json`을 되돌립니다. 종료 후 적용기(`Apply-WorkStackUpdate.ps1`)는 새 런타임이 즉시 종료하는 등 커밋 전 실패에서 스냅샷을 되돌리고 `rolled-back` 영수증을 남기며, 되돌리기까지 실패하면 `recovery-required`와 함께 `recovery_path`를 기록합니다. 릴리스 게이트의 `Test-WorkStackUpgrade.ps1`은 **주입된 설치 실패**와 **새 런처 시작 실패**에서 이전 버전 페이로드·설정 바이트·SSOT 바이트·사용자 지정 백업 폴더가 보존되는지를 검사합니다.

**의도적 다운그레이드**: 정상 설치된 새 버전 위에 이전 설치 파일을 실행하는 절차는 위 검사가 증명하지 않으며 릴리스 정책에도 없습니다. 자동 업데이트 검사기는 설치본보다 오래된 버전을 `update version must not be older than the installed version`으로 거부합니다. 이전 설치 파일의 체크섬이 맞다는 사실은 새 버전 데이터와의 호환성을 증명하지 않습니다. **이 문서는 다운그레이드 명령을 제공하지 않습니다.** 릴리스가 정확한 이전 버전·지원 절차·검증 결과를 명시할 때만 그 절차를 따르세요.

문제가 생기면: 설치 결과 출력, `updates\last-update.json`, `logs\desktop-update.log`, `logs\desktop-startup.log`를 보존하고, 앱 실행과 워크스페이스 연결을 각각 확인합니다. 바로가기 경고(`Shortcuts incomplete: ...`)는 런타임이 설치된 상태에서 나오는 파생 경고이며 롤백을 뜻하지 않습니다. 데이터까지 되돌려야 하면 검증된 백업을 **빈 새 위치**에 복원해 확인한 뒤 연결 설정에서 전환합니다.

## 14. 알려진 한계와 증상별 확인 순서

### 14.1 알려진 한계

- 설치 파일은 코드 서명이 없습니다. 체크섬은 전송 무결성 증거일 뿐입니다.
- Remote SSH 모드에는 오프라인 Windows 복제본, 필드 단위 자동 병합, 채택 이전 바이트 백업으로부터의 복원이 없습니다. Linux가 유일한 계획 상태 권위입니다.
- 원격 서버는 세션 동안만 존재하며 상주 데몬이 아닙니다. 창을 닫으면 SSH 세션과 원격 서버가 종료됩니다.
- `--data-dir` 오타는 새 빈 워크스페이스를 만듭니다(5.1절).
- 자동 업데이트는 정확히 일치하는 allowlist 자산만 받으며, 게시자 서명을 대신하지 않습니다.
- Microsoft(Outlook/Teams) 연동 레인은 기본 빌드에서 비활성입니다. 이 안내서의 범위 밖입니다.

### 14.2 증상별 확인 순서

| 증상·메시지 | 먼저 확인할 것 | 하지 말아야 할 것 |
| --- | --- | --- |
| `Setup hash mismatch` / `Setup filename mismatch` / 사이드카 형식 오류 | 두 파일을 공식 릴리스에서 다시 받고 2절 재검증 | 검증을 건너뛰고 실행 |
| `The default interactive installer only writes under LOCALAPPDATA\Programs.` | 기본 `-InstallRoot` 사용, 또는 의도적일 때만 `-NoShortcut` | 시스템 폴더에 강제 설치 |
| `Port 8765 is already in use; Work Stack will use N instead.` | 설치 출력의 `Local endpoint`와 `config.json`의 `port` 확인 | 점유 프로세스 임의 종료 |
| `Work Stack could not start` 대화상자 | `logs\desktop-startup.log`, WebView2 Runtime 설치 여부 | 런타임 파일 교체 |
| `Automatic pre-launch backup failed: ...` | `logs\backup.err.log`, 백업 폴더 권한·용량 | 데이터 폴더 삭제 |
| 앱은 뜨는데 워크스페이스가 비어 보임 | 활성 프로필의 데이터 경로와 UID, `--data-dir` 오타(새 워크스페이스 생성 여부) | UID·내부 JSON 직접 편집 |
| `OpenSSH client was not found. ...` | Windows OpenSSH 클라이언트 기능 활성화, PATH | — |
| `SSH connection check failed. Confirm the host alias, known-host key, SSH agent, remote paths, and python3.` | `ssh -T -o BatchMode=yes -o StrictHostKeyChecking=yes ... <별칭> "command -v python3"`, 원격 앱·데이터 경로, `store-meta.json`/`workspace.json` 존재 | 호스트 키 검사 끄기 |
| `Remote Work Stack workspace identity ... does not match ...` | 원격 데이터 폴더와 프로필의 Workspace ID 재확인 | 원격 `workspace.json` 수정 |
| `Remote SSOT reconnection was exhausted. ...` | `desktop-launch\remote-ssh.log`, 네트워크·VPN·bastion, **Reconnect now** | 상주 서버 가정 |
| `Connection activation remains pending because the running server is not in sync ...` | 동기화 검토 화면에서 상태 확인 후 다시 시작 | 레지스트리 파일 삭제 |
| `Work Stack server is not running for this data directory` (`checkpoint-state`, `capture`) | GUI(서버)가 그 데이터 폴더로 실행 중인지 | 로컬 우회 시도 |
| `Work Stack server runtime metadata is not a readable regular file` / `... changed before the ... was sent` | 실행 시 메타데이터 폴더 상태, 앱 재시작 후 재시도 | 메타데이터 파일 삭제 |
| `... commit is unknown; inspect the ... before retrying` / `commit_unknown` | 출력·키(intent ID)·입력 보존 후 중단, 최신 상태 재조회 | 새 키로 재기록, 무한 재전송 |
| `the running Work Stack server refused the ... (HTTP N)` | 입력값(ID·상태 전이)이 유효한지 | 같은 요청 반복 |
| `the Work Stack data directory is already owned by another writer` | GUI 종료 후 재시도, 또는 전달되는 명령군 사용 | `.workstack.lock` 삭제 |
| `Explicit DataDir conflicts with the selected local connection profile.` | `-DataDir`를 빼고 설치 파일을 다시 실행(11.2절). `config.json`과 활성 로컬 프로필이 같은 폴더를 가리키는지 | `config.json`을 손으로 고쳐 우회 |
| `Work Stack is not configured. Install it before updating.` | `config.json` 존재 여부, 새 설치 절차 | — |
| `last-update.json`의 `recovery-required` | `recovery_path`와 `logs\desktop-update.log` 보존, 설치 상태 확인 | 폴더 임의 삭제 |
| 에이전트가 Skill을 못 읽음 | 에이전트가 **실제 실행되는 쪽**의 `.agents/skills/work-stack`, 7.2절 검증기 | 다른 Skill·설정 덮어쓰기 |

### 14.3 증상 보고에 넣을 것

릴리스 버전, 사용한 데이터 위치의 식별 정보(UID), 종료 코드, 오류 시각, 관련 로그 파일 이름. 개인 경로·원격 주소·작업 내용·인증 정보·원시 요청은 필요한 범위 밖으로 노출하지 마세요. 먼저 검증된 백업과 원본을 보존하고, 데이터 삭제·잠금 우회·전체 프로세스 종료로 증상을 숨기지 않습니다.

## 15. 이 안내서가 다루지 않는 것

- 이 저장소의 GitHub Release로 게시된 v1.0.7 설치 파일·사이드카·SHA-256. 지금은 사이드카가 체크섬의 권위입니다.
- Windows 10을 포함한 더 넓은 OS/WebView2 조합. 확인한 환경은 64비트 Windows 11과 WebView2입니다.
- Task 상태 변경의 Undo는 방금 바꾼 사용자 의도에만 적용되며, 리비전을 자동으로 맞추지 않습니다.

같은 `docs/` 폴더의 영문 기능 안내서는 참고용입니다. Workspace 단축키는 이 문서 3.2절(`1`–`8`)이 1.0.7 기준입니다.

---

이 문서는 Work Stack **1.0.7** 기준입니다. 이 저장소의 GitHub Releases에 v1.0.7이 게시된 것은 아닙니다.
