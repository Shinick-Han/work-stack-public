# Work Stack 1.0.14

Windows 설치본과 SSH로 연결한 Linux 서버의 업데이트 경로를 연결하고, 원격 종료·재접속과 중단 후 복구를 개선했습니다.

- Windows 설치기에 같은 소스와 UI로 만든 Linux 원격 번들을 포함합니다. Linux 번들은 CPython 3.12 / x86-64 / manylinux_2_17 대상이며 원격 Python은 별도로 필요합니다.
- `Update this PC`와 `Update connected server`를 구분하고 Desktop, Remote server, 제공 UI 식별 정보를 따로 표시합니다. Windows 업데이트만으로 Linux 앱이 바뀌었다고 표시하지 않습니다.
- 원격 업데이트의 대상 확인, 새 앱 파일 준비, 기존 owner 종료, 백업, 검증, 연결 전환과 재확인을 기존 기능에 연결했습니다.
- 원자적 디렉터리 게시가 지원되지 않는 파일시스템에서 검증된 새 디렉터리 unpack 경로를 제공합니다. 기존 앱이나 SSOT를 덮어쓰는 방식이 아닙니다.
- 원격 종료 시 SSH가 유지하던 HTTP 연결 때문에 종료 확인이 지연되던 문제를 수정했습니다. 연결 정리 뒤 같은 권한으로 종료 상태를 다시 확인하며, 동시 호출이 확인된 결과를 덮어쓰지 않게 했습니다.
- 완료된 업데이트 기록이 다음 업데이트를 막는 문제와 중단·응답 유실 후 복구 경로를 수정했습니다.

## 설치와 업데이트

[v1.0.14 릴리즈](https://github.com/Shinick-Han/work-stack-public/releases/tag/v1.0.14)에서 `WorkStack-Setup-1.0.14.ps1`과 체크섬을 받으세요. 기존 설치를 삭제하거나 데이터를 옮길 필요 없이 같은 설치 위치에 실행합니다. 설치기는 기존 설정과 데이터 보존 및 백업 경로를 사용합니다.

PowerShell에서 다운로드 파일의 SHA-256을 먼저 확인하세요.

```powershell
Get-FileHash -Algorithm SHA256 -LiteralPath "$env:USERPROFILE\Downloads\WorkStack-Setup-1.0.14.ps1"
```

정상 SHA-256: `3a65bf5802e822e55dbea50dab60b3509342360a1024720aa75acbc1acb476d2`

검증 후 실행합니다. 별도 경로를 쓰는 기존 설치는 기존 설치·설정 경로를 유지하세요.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$env:USERPROFILE\Downloads\WorkStack-Setup-1.0.14.ps1"
```

SSH 사용자는 Windows 업데이트 후 원격 업데이트 화면에서 `Preview server update`로 대상 앱·데이터·workspace를 확인하고, 화면이 제공하는 다음 작업을 진행합니다. SSOT 경로나 workspace ID를 새로 만들지 마세요. 기존 owner가 살아 있거나 결과가 불명확하면 표시된 복구 절차를 사용합니다.

## 검증 범위

- 통합 소스: `6a797d5242a53274a8e9eea98b12bdc16f506392`.
- 제품 파일 581개의 구조 검사 통과; 부채 허용치 증가 없음. 원격 종료 수정의 독립 테스트 197개와 후속 경합 수정 집중 테스트 61개 통과. 서로 다른 후보에 대한 검증이며 하나의 전체 스위트 실행으로 합산하지 않습니다.
- 실제 공개 1.0.13 설치기(SHA-256 `954df6199d61997972827f6fe2701283681a1f73e33991243fbc1aedec9bf009`)에서 이 최종 설치기로 업그레이드하여 Task, 연결된 Note, workspace ID, 설정 보존과 백업을 확인했습니다.
- 최종 Windows 패키지의 변경 코드 5개, UI 파일 15개 및 동봉 Linux 번들이 검증된 원본과 일치합니다.
- 실제 Windows GUI/WSL SSH에서 종료·재접속 2회, 제품의 `stop_confirmed_already_exited; owner=dead; exit=verified`, 데이터 보존과 관찰된 프로세스·포트 정리를 확인했습니다. 시험 도구의 성공 코드 분류 오류는 원본 기록을 보존한 별도 판정으로 정정했습니다.
- 앞선 후보에서 실제 SSH 업데이트·연결 전환과 다음 업데이트 사전 확인도 검증했습니다. 최종 후보에서 그 전체 흐름을 다시 실행했다는 주장은 하지 않습니다.

## 제한

서명되지 않은 `WorkStack.exe`를 Windows 앱 제어 정책이 차단하는 환경에서는 자동 재시작을 보장하지 않습니다. 이번 릴리즈는 코드 서명이나 OS 보안 정책 승인을 제공하지 않습니다. 회사 NFS/csh 환경 실기 검증은 별도로 필요합니다.

GitHub Actions는 계정 결제/사용 한도 사유로 시작하지 못했습니다. 이번 배포는 로컬에서 검증한 산출물의 수동 게시이며 전체 자동 릴리즈 워크플로의 성공을 의미하지 않습니다.

공개 소스 스캐너의 44개 경고는 기존에 독립 검수한 가상 테스트 데이터 27개 파일과 바이트가 같았습니다. 스캐너를 완화하거나 무조건 자동 통과로 처리하지 않았습니다. 개인 SSOT·런타임 상태·자격 증명·비공개 Git 이력은 공개 소스에 포함하지 않습니다.
