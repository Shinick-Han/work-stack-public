# Windows 데스크톱에서 Markdown·Obsidian 문서를 Task에 연결하기

이 안내는 Windows 데스크톱 Work Stack에서, 이미 있는 로컬 Markdown(`.md`) 파일을 Task에 **읽기 전용으로 연결**하는 현재 화면 동작을 설명합니다. Obsidian vault는 일반 폴더와 같습니다. Vault 전체를 색인하거나, 문서를 추론하거나, Obsidian과 양방향으로 맞추는 기능이 아닙니다.

예시는 합성 경로와 합성 Task만 씁니다. vault 루트는 `C:/ExampleVault`, 문서는 `projects/review.md`, Task는 **T-0001**입니다. 실제 개인 vault 경로를 적지 않습니다.

화면의 버튼·필드·상태 문구는 제품 UI와 같이 **영문 그대로** 적습니다.

## 이 기능이 하는 일

Work Stack은 고른 폴더, 그 안의 상대 경로, 줄 범위, 연결 이유, 그리고 연결 순간에 읽은 파일의 지문만 이 Windows 장치에 보관합니다. `.md` 원문은 `C:/ExampleVault`에 그대로 둡니다. 연결을 저장해도, 다시 읽어도, 연결을 지워도 원문 파일은 바뀌지 않습니다.

미리보기와 다시 읽기는 그때그때 폴더에서 해당 줄만 가져옵니다. 가져온 글은 화면과 준비한 스냅샷에서 **External reference · not Task permission · external reference** 및 **Read-only**로 표시됩니다. Task 권한이나 에이전트 지시로 승격되지 않습니다.

## 이 기능이 하지 않는 일

- LLM Wiki Compiler, vault 검색, 자동 색인, 모델 추론
- Obsidian·Markdown으로의 쓰기 또는 양방향 동기화
- vault 전체를 한 번에 가져오기, 숨은 파일 탐색, 이동한 폴더를 알아서 찾기
- 브라우저만으로 이 PC의 폴더를 읽거나 연결을 고정하기
- 워크스페이스 동기화·백업에 연결 기록을 넣기
- Conduit 스냅샷의 정식 Task 내용으로 넣거나, 에이전트에 자동 전송하기

Wiki 컴파일러가 있는 환경이어도, 이 Task 연결은 그 컴파일러 없이 `.md` 파일만 명시적으로 가리킵니다.

## 브라우저와 데스크톱

폴더를 고르고 파일을 읽으려면 Windows 데스크톱 앱이 필요합니다. 브라우저로 `http://127.0.0.1:8765/`만 연 경우에는 섹션 **Local Markdown references**에 다음이 보입니다.

- **Desktop knowledge host unavailable**
- *Open this Task in the Work Stack desktop app to choose a local vault. The browser build cannot read or pin Markdown on this device.*
- 배지 **Local device only**

같은 Task의 **Reference context**를 브라우저에서 열면, 내보내기는 *Open this Task in the Work Stack desktop app to export local references.* 로 거절됩니다.

## 연결하기

1. 데스크톱에서 합성 Task **T-0001**을 엽니다.
2. Task 상세의 **Context** 탭으로 갑니다. 섹션 제목은 **Local Markdown references**입니다.
3. 안내 문구: *Links are saved on this device. They are not included in workspace sync or backups.*
4. **Connect vault**를 선택합니다. Windows 폴더 선택 창의 설명은 **Choose a local Markdown vault**입니다. 이 창에서는 새 폴더를 만들지 않습니다. 이미 있는 폴더만 고릅니다.
5. `C:/ExampleVault`를 고르고 확인합니다. **Vault** 목록에는 폴더 이름 **ExampleVault**가 나타납니다. 같은 폴더를 다시 고르면 목록이 늘어나지 않고 기존 항목을 씁니다.
6. 폴더를 선택하고 확인하면 해당 vault가 선택됩니다. 폴더를 고르지 않고 선택 창을 취소하거나 닫았을 때만 *Vault picker closed without choosing a folder.*가 표시됩니다.

고른 폴더가 vault 루트입니다. 그 밖의 위치는 이 연결에서 읽지 않습니다.

## 상대 경로와 줄 범위 미리보기

1. **Document path** (*vault-relative .md*)에 `projects/review.md`를 입력합니다. 실제 파일은 `C:/ExampleVault/projects/review.md`입니다. 드라이브 문자, `C:/...` 전체 경로, 백슬래시(`\`)는 넣지 않습니다.
2. **Start line** / **End line**은 선택입니다. 비우면 1행부터 40행까지입니다. 행 번호는 1부터의 정수이며, 한 번에 최대 80행입니다.
3. **Preview**를 선택합니다. 경로가 비어 있으면 미리보기를 할 수 없습니다. **Vault**가 없으면 입력칸이 비활성화됩니다.
4. 미리보기에는 문서 제목, **Read-only**, 신선도 배지, `projects/review.md · lines 1–40` 형식의 범위, 발췌가 보입니다. 첫 미리보기의 신선도는 **Source not compared**입니다. 발췌가 허용 길이를 넘으면 *Excerpt truncated to the permitted read window.* 가 붙습니다.

현재 vault·경로·줄 범위의 미리보기가 준비되고 **Reason**에 연결 이유를 입력해야 **Link**가 활성화됩니다. 경로나 범위를 바꿨다면 다시 **Preview**를 선택하세요.

## Task 연결 저장

1. **Reason** (*required to link*)에 이 문서가 **T-0001**에 필요한 이유를 적습니다. 비어 있으면 **Link**가 켜지지 않습니다. 최대 500자입니다.
2. **Link**를 선택합니다. 저장되는 것은 원문이 아니라 연결 기록입니다. 원문은 `C:/ExampleVault`에 남습니다.
3. 목록 제목 **Linked on this device** 아래에 경로, **Linked** 배지, `ExampleVault · lines 1–40`, 이유가 보입니다. 아직 없으면 *No local references are linked to this Task yet.* 입니다.

같은 폴더·같은 상대 경로·같은 줄 범위를 다시 연결하면 새 항목을 늘리지 않고, 그때 읽은 지문과 이유를 갱신합니다.

진행 중에는 **Cancel**로 그 요청을 멈출 수 있습니다.

## 다시 읽기와 원문 변경 표시

목록에서 **Read**를 선택하면 지금 디스크의 해당 줄을 다시 읽습니다. 연결할 때 저장한 지문과 비교합니다.

| 배지 | 뜻 |
| --- | --- |
| **Unchanged source** | 연결 이후 원문 바이트가 같습니다. |
| **Source changed** | 같은 경로의 원문이 연결 이후 바뀌었습니다. 발췌는 현재 파일 내용입니다. |
| **Source not compared** | 이번 읽기에는 저장된 지문과 비교하지 않았습니다. (미리보기가 해당합니다.) |

원문이 바뀐 뒤에도 연결 기록은 남아 있습니다. 지문을 지금 파일에 맞추려면 같은 경로·범위를 다시 **Preview**한 다음 **Link**합니다. 미리보기와 파일이 어긋나면 *The source document changed since the expected revision.* 과 **Refresh preview**가 나옵니다.

## 연결 제거

**Unlink**는 이 장치의 Task 연결 기록만 지웁니다. `C:/ExampleVault/projects/review.md`는 삭제·수정되지 않습니다. 확인 대화 상자는 없습니다.

## 선택한 문서를 Task 인계에 곁들이기

정식 Task 내보내기(**Export to Conduit**)와는 별개입니다. 이쪽은 이미 연결해 둔 Markdown 일부를, 사용자가 고른 뒤에만 복사·저장하는 보충 스냅샷입니다.

1. 같은 **Context** 탭에서 **Reference context**를 펼칩니다. 안내: *Selected external Markdown to accompany a Task handoff. Not canonical Task context, not agent instructions, and not sent to an agent.*
2. **Open reference context**를 선택합니다. (열려 있으면 **Close reference context**)
3. *Select at most eight linked documents. None are selected by default. Binding is this selected Task revision, not unsaved draft text.*
4. 포함할 `projects/review.md`만 선택합니다. 기본값은 모두 해제입니다. 연결이 없으면 *No linked references to include.* 입니다.
5. **Prepare snapshot**을 선택합니다. 준비 전에 지금 저장된 연결 목록을 다시 확인하고, 고른 범위를 다시 읽으며, 열린 워크스페이스와 저장된 Task 버전이 준비 전후에 같은지 검사합니다. 목록에서 사라진 항목이나 바뀐 기록은 전체를 거절하고 아무것도 내보내지 않습니다.
6. 안내: *Prepared snapshot only. Linked files can change after you copy or download. This is not canonical Task context and is not sent to an agent.*
7. 어떤 항목이 **Source changed**이면, 복사·저장 전에 각 항목의 *Source changed since it was linked. Include this prepared snapshot anyway.* 를 선택해야 합니다.
8. **Copy JSON** 또는 **Download JSON**을 선택합니다. 복사에 성공하면 버튼이 **JSON copied**로 바뀝니다. 받은 파일 이름은 예를 들어 `workstack-knowledge-context-T-0001-r3.json`처럼 Task 번호와 저장된 버전 숫자를 씁니다.

스냅샷은 최대 8개, UTF-8 기준 32KiB입니다. 넘으면 *The prepared JSON exceeds 32KiB. Select fewer references. Nothing was exported.* 입니다.

패널을 닫거나 Task나 저장된 버전을 바꾸거나 선택 항목을 바꾸면 준비 중인 작업은 버려집니다. 이미 클립보드에 들어간 내용은 회수되지 않습니다.

## 한도 (현재 데스크톱 동작)

| 항목 | 한도 |
| --- | --- |
| 이 장치에 연결할 폴더 | 최대 32 |
| Task당 연결 기록 | 최대 64 |
| 인계에 고를 문서 | 기본 0, 최대 8 |
| 한 번 읽는 줄 수 | 최대 80 (기본 비우면 1–40) |
| 문서 크기 | 512KiB |
| 발췌 글자 수 | 6,000 |
| 이유 | 1–500자 |
| 상대 경로 길이 | 1–1,024자, `.md`로 끝남 |
| 준비한 JSON | 32KiB |
| 폴더 선택 창 대기 | 최대 5분 |

점(`.`)으로 시작하는 경로 조각, 바로 가기나 연결로만 있는 경로, 일반 파일이 아닌 대상, 고른 폴더 밖 경로, Windows 예약 장치 이름은 거절됩니다. Obsidian 설정 폴더 `.obsidian` 안의 파일은 연결할 수 없습니다. 이 장치에 폴더가 32개를 넘거나 Task 연결이 64개를 넘으면 *Knowledge operation was refused.* 가 보일 수 있습니다.

## 문제 해결

**파일을 옮기거나 이름을 바꾼 경우.** **Read**는 *The referenced document is not available.* 또는 *The referenced document could not be read.* 를 보여 줍니다. 같은 이름의 다른 파일로 바꿔 연결하지 않습니다. 새 상대 경로를 입력해 **Preview** · **Link**한 뒤, 옛 항목은 **Unlink**합니다.

**폴더 자체를 옮긴 경우.** Work Stack은 예전 위치를 따라가지 않습니다. **Read**는 *The chosen vault is not available.* 일 수 있습니다. **Connect vault**로 새 위치(`C:/ExampleVault`가 옮겨진 폴더)를 다시 고르고, 상대 경로를 **Preview** · **Link**한 다음 동작하지 않는 옛 연결을 **Unlink**합니다.

**원문만 고친 경우.** **Read**와 인계 미리보기에 **Source changed**가 붙습니다. 연결 기록은 유지됩니다. 지문을 갱신하려면 다시 **Preview** · **Link**합니다. 인계에 넣으려면 변경 확인 칸을 선택한 뒤에만 **Copy JSON** / **Download JSON**이 켜집니다.

**워크스페이스가 다른 경우.** 데스크톱이 보고 있는 워크스페이스와 열린 Task의 워크스페이스가 다르면 *The knowledge request does not match the active workspace.* 입니다. 활성 워크스페이스가 없으면 *No active workspace is available for knowledge operations.* 입니다. 해당 Task가 속한 워크스페이스로 데스크톱을 연 뒤 다시 시도합니다. **Try again**으로 목록을 다시 불러올 수 있습니다.

**줄 범위.** *Line numbers must be whole numbers starting at 1.* 또는 *Use a one-based span of at most 80 lines.* / *The requested line range is not present in the document.*

**빈 문서·크기·형식.** *The referenced document is empty.* / *The referenced document exceeds the allowed size.* / *Only Markdown documents can be referenced.* / *The document path is not a permitted relative Markdown path.*

**인계 준비 실패.** 고른 연결이 사라진 경우 *A selected reference is no longer saved for this Task. Reload the linked references and select again. Nothing was exported.* 저장된 Task 버전이 바뀐 경우 *The live Task no longer matches this selected revision. Binding uses the selected Task revision, not unsaved draft content.* 목록을 **Try again**으로 다시 불러 고릅니다.
