# Work Stack 1.0.8 설치·기존 설치 업그레이드 안내

작성일: 2026-09-07. Windows x64용 **시험 릴리스(pre-release)**. 설치 파일은 현재 검증된 1.0.8 통합본이다. 전체 자동 릴리스 게이트 완료 기록이 없어 안정 버전 자동 업데이트에는 올리지 않았다.

- 공개 소스: https://github.com/Shinick-Han/work-stack-public
- 1.0.8 다운로드: https://github.com/Shinick-Han/work-stack-public/releases/tag/v1.0.8
- 이전 1.0.7: https://github.com/Shinick-Han/work-stack-public/releases/tag/v1.0.7

## 1. 무엇을 받으면 되나

릴리스 페이지의 Assets에서 다음 두 파일을 **같은 폴더**에 받는다.

1. `WorkStack-Setup-1.0.8.ps1`
2. `WorkStack-Setup-1.0.8.ps1.sha256`

설치기는 약 25.1 MiB의 PowerShell 파일이다. 실행하면 Python과 제품 런타임을 설치하고 `WorkStack.exe`와 바로가기를 만든다. 별도 Python/Node 설치나 `Source code.zip` 다운로드는 필요 없다. Windows x64 및 Microsoft Edge WebView2 Runtime이 필요하다. WebView2가 없는 회사 PC에서는 승인된 설치 경로로 먼저 준비한다.

회사 PC에서는 회사가 허용한 소프트웨어·스크립트 실행 정책을 따른다. 이 배포물은 회사 보안 승인을 의미하지 않으며, 실행이 차단되면 정책이나 보안 제품을 비활성화하지 않는다. 개인 PC의 데이터·프로필·자격증명을 회사 PC로 복사할 필요가 없다.

## 2. 다운로드 무결성 확인

다운로드 폴더에서 PowerShell을 열고 실행한다.

```powershell
$setup = (Resolve-Path '.\WorkStack-Setup-1.0.8.ps1').Path
$expected = '9299b4d349f43615df791cc66095a75ba3821bf7f025a30a56c2975e37b08a65'
if ((Get-FileHash -LiteralPath $setup -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expected) {
    throw '설치 파일 해시 불일치: 실행하지 마세요.'
}
'SHA-256 확인 완료'
```

SHA-256은 파일 일치 여부를 확인한다. 이 설치기는 서명된 게시자 인증서를 제공하지 않으므로 해시 확인을 게시자 서명과 동일하게 해석하지 않는다.

## 3. 기존 설치가 있다면: 먼저 지우지 않는다

**기존 프로그램을 삭제하거나 데이터 폴더를 비우지 않고, 같은 설치 경로와 StateRoot에 업그레이드한다.** 이미 1.0.8이면 설치할 필요는 없다. 손상 복구가 목적일 때만 백업 후 동일 버전을 재설치한다.

기본 위치는 다음과 같다.

| 구분 | 기본 경로 |
|---|---|
| 실행 파일 | `%LOCALAPPDATA%\Programs\WorkStack\WorkStack.exe` |
| 설정·연결 프로필 | `%LOCALAPPDATA%\WorkStack` |
| 기본 작업 데이터 | `%LOCALAPPDATA%\WorkStack\data` |
| 기본 백업 | `%LOCALAPPDATA%\WorkStack\backups` |

사용자 지정 위치나 Connection Center의 선택된 프로필을 사용하면 실제 데이터 경로가 다를 수 있다. **업그레이드 전에 앱에서 활성 로컬 프로필 경로와 작업 공간 이름을 기록한다.** `config.json`의 `data_dir`만으로 활성 프로필이라고 단정하지 않는다. 원격 SSH 프로필을 선택한 경우 이 안내는 원격 서버 업그레이드를 수행하지 않는다. 로컬 설치를 위한 적절한 로컬 프로필을 선택하고 종료한다. 실험적 schema 4 저장소도 이 안내의 자동 업그레이드 대상이 아니다.

### 3-1. 저장 후 종료하고 백업

편집을 저장하고 Work Stack과 같은 데이터에 쓰는 에이전트를 종료한다. 기본 설치라면 다음으로 설치 경로와 설정을 확인할 수 있다. 사용자 지정 StateRoot는 첫 줄을 실제 위치로 바꾼다.

```powershell
$stateRoot = Join-Path $env:LOCALAPPDATA 'WorkStack'
$config = Get-Content -LiteralPath (Join-Path $stateRoot 'config.json') -Raw | ConvertFrom-Json
$installRoot = [string]$config.install_dir
$dataDir = [string]$config.data_dir
# Connection Center에서 기록한 활성 로컬 경로가 다르면 다음 단계 전에 $dataDir를 그 경로로 바꾼다.
& (Join-Path $installRoot 'scripts\windows\Stop-WorkStack.ps1') -InstallRoot $installRoot
```

백업은 기존 데이터·프로그램 폴더 바깥에 만든다. 아래는 기본 문서 폴더를 사용한다. 회사에서 별도 백업 위치를 지정했다면 그 경로로 바꾼다. `state`에는 연결 정보 등이 들어갈 수 있으므로 백업을 공개 저장소에 올리지 않는다.

```powershell
$backupRoot = Join-Path ([Environment]::GetFolderPath('MyDocuments')) ('WorkStack-before-1.0.8-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $backupRoot -ErrorAction Stop | Out-Null
Copy-Item -LiteralPath $stateRoot -Destination (Join-Path $backupRoot 'state') -Recurse -ErrorAction Stop
Copy-Item -LiteralPath $dataDir -Destination (Join-Path $backupRoot 'active-data') -Recurse -ErrorAction Stop
Copy-Item -LiteralPath $installRoot -Destination (Join-Path $backupRoot 'previous-runtime') -Recurse -ErrorAction Stop
& (Join-Path $installRoot 'runtime\python.exe') (Join-Path $installRoot 'run_work_stack.py') --data-dir $dataDir maintenance backup --out (Join-Path $backupRoot 'verified-backup')
if ($LASTEXITCODE -ne 0) { throw '검증 백업 실패: 설치를 진행하지 마세요.' }
$backupRoot
```

백업 명령의 성공 결과와 ZIP 파일을 확인하고 이 폴더를 보관한다. 복사나 백업에서 오류가 나면 설치를 진행하지 않는다. 사용자 지정 백업 경로에 있는 과거 ZIP도 계속 보관한다.

### 3-2. 같은 경로에 설치

해시 확인한 다운로드 폴더에서, 위 변수들이 남아 있는 같은 PowerShell 창으로 실행한다.

```powershell
& $setup -InstallRoot $installRoot -StateRoot $stateRoot
```

기존 데이터·백업 경로, 백업 보관 수, 포트는 기존 설정에서 이어받는다. 선택된 로컬 프로필은 설치기의 별도 권한 경로 검사로 확인한다. 사용자 지정 경로를 썼는데 기본 설치 명령으로 새 폴더에 설치하는 실수를 피한다. `-DataDir`를 임의로 새 경로로 덮어쓰지 않는다.

설치기는 교체 직전에 사전 백업을 만들고, 설치 실패 시 이전 런타임·설정 복구를 시도한다. **성공한 설치 뒤 이전 런타임은 정리될 수 있으므로 3-1의 수동 보관본이 필요하다.**

## 4. 처음 설치하는 PC

해시 확인 후 다운로드 폴더에서 실행한다.

```powershell
& .\WorkStack-Setup-1.0.8.ps1
```

기본적으로 사용자 영역에 설치한다. 관리자 권한을 요구하는 방식으로 시작하지 않는다. 스크립트 실행 정책에 걸리면 해당 PC에서 승인된 실행 방법을 사용한다. 회사 정책 우회 명령은 이 안내에 포함하지 않는다.

## 5. 설치 후 확인

시작 메뉴 또는 바탕 화면의 Work Stack을 실행한다. 기본 EXE는 `%LOCALAPPDATA%\Programs\WorkStack\WorkStack.exe`이다.

1. 프로그램 버전이 **1.0.8**, EXE 파일 버전이 **1.0.8.0**인지 확인한다.
2. 이전 작업 공간과 활성 프로필 경로가 맞고, 연결 상태가 정상인지 확인한다.
3. 기존 Objective, Task, 노트, 업무 기록을 열어 보존 여부를 확인한다.
4. Graph/Board/일일 리뷰를 확인한 뒤 테스트용 Task를 생성·변경하고 앱을 다시 열어 저장을 확인한다.

기존 설치 후 목록이 비어 있다면 데이터를 삭제하거나 데모로 초기화하지 말고, 먼저 활성 프로필과 실제 데이터 경로를 확인한다.

1.0.8은 보고서 저장 기반을 위한 `reports.json`을 추가하며, 지원되는 기존 collection schema 3을 첫 사용 시 schema 5로 이행한다. 다른 로컬 프로필은 직접 열기 전까지 모두 이행된 것으로 간주하지 않는다. 자동 생성된 `.workstack-migration-backups`도 보관한다. 보고서 저장 UI와 원격 설치 UI 등은 아직 후속 작업이다.

## 6. 문제가 생겼을 때

- 설치 자체가 실패하면 오류 메시지를 보관하고 기존 EXE·설정이 복구됐는지 확인한다. 실패 상태에서 설치·삭제를 반복하지 않는다.
- 창이 열리지 않으면 WebView2 설치, EXE 버전, 활성 프로필을 확인한다.
- **1.0.8이 데이터를 연 뒤에는 EXE만 1.0.7로 바꿔도 데이터가 되돌아가지 않는다.**
- 다운그레이드가 필요하면 앱·에이전트를 종료한 뒤 3-1에서 보관한 **이전 런타임 + 이전 데이터 백업**을 함께 사용한다. 현재 데이터를 덮어쓰지 말고, 이전 런타임의 유지보수 도구로 검증된 schema 3 ZIP을 별도의 빈 폴더에 복원하여 확인한 뒤 해당 로컬 프로필을 선택한다. 1.0.8 복원 도구는 이전 백업을 schema 5로 이행할 수 있어 1.0.7용 다운그레이드 도구가 아니다.
- 제거만 필요하면 설치 폴더의 `scripts\windows\Uninstall-WorkStack.ps1`을 사용하되, 데이터 삭제 옵션을 선택하지 않는다. 업그레이드에는 사전 제거가 필요 없다.

## 7. 이번 배포의 검증 범위

2026-09-07에 배포할 설치 파일로 1.0.7 → 1.0.8 업그레이드, 사용자 지정 설정·백업 경로·기존 데이터 바이트 보존, 설치 실패 복구, 재시작 실패 복구를 시험했다. 설치 파일의 제품 소스·프런트 배포물 228개를 통합본과 비교했고, 포함된 의존성 배포물은 해시 잠금 목록과 비교했다. 설치기 계약 테스트 32개와 공개 소스 민감정보 검사를 통과했다.

기존 통합본의 백엔드·프런트·브라우저 검사 결과는 별도 검증 기록에 있다. 이를 이번 공개 커밋에서 전체 검사를 다시 실행한 결과로 표시하지 않는다. 전체 자동 릴리스 파이프라인, 모든 회사 환경, 실제 Microsoft 365 연동이 통과했다는 의미도 아니다. 이 때문에 안정 버전 자동 업데이트는 1.0.7로 유지하고 1.0.8은 수동 설치 시험 릴리스로 제공한다.
