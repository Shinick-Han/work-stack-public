# agent apply와 scoped `key_result_refs`

합성 식별자만 사용한다. 실제 개인/사내 SSOT 경로, 자격 증명, CSRF 토큰은 이 문서에 넣지 않는다.

## 무엇을 허용하는가

`agent apply` stdin 패킷의 `changes`는 기존 Task 필드와 함께 optional `key_result_refs`를 받을 수 있다. 패킷 최상위는 그대로 `workspace_id`, `task_id`, `expected_revision`, `changes` 네 키만 허용한다. 32 KiB 한도와 기존 필드(`title`, `detail`, `status`, `priority`, `due`, `scheduled`, `estimate_minutes`, `tags`, `objective_ids`, `parent_id`, `dependencies`)는 유지된다.

admission은 필드 이름만 연다. scoped 쌍 모양, 부모 Objective 정합, 미지 Objective/KR 거절은 이미 있는 `WorkStack.patch_task` 도메인이 수행한다. 더 약한 검사를 admission에 복제하지 않는다.

## 생략과 빈 목록

- `changes`에서 `key_result_refs`를 빼면 기존 값을 유지한다.
- `"key_result_refs": []`는 명시적 해제로 저장된다. Objective 링크는 지우지 않는다.

항목은 정확한 scoped 쌍만 허용한다.

```json
{"objective_id": "O-1", "key_result_id": "KR-1"}
```

같은 표시 ID라도 Objective가 다르면 다른 참조다. 부모 Objective가 `objective_ids`에 없으면 도메인이 한 번 정렬해 넣는다.

## 실행 예시 (stdin JSON, curl/CSRF 없음)

owner HTTP가 떠 있으면 CLI가 세션과 CSRF를 붙인다. 호출자는 JSON만 표준입력으로 넣는다. exclusive-local이면 같은 패킷이 보유 중인 Store에 적용된다.

아래 경로와 식별자는 예시다. 선택한 workspace와 Task의 현재 revision 및 실제 Objective/KR 식별자로 바꾼다. `run_work_stack.py`는 자신의 검증된 checkout 경로를 등록하므로 `-I` 실행을 지원한다.

PowerShell:

```powershell
@'
{"workspace_id":"11111111-1111-4111-8111-111111111111","task_id":"T-0001","expected_revision":0,"changes":{"key_result_refs":[{"objective_id":"O-1","key_result_id":"KR-1"}]}}
'@ | python -I "C:/path/to/app/run_work_stack.py" --data-dir "C:/path/to/ssot" agent --workspace-uid 11111111-1111-4111-8111-111111111111 apply --stdin --intent-id agent.kr.apply.0001
```

Linux bash/csh/tcsh: 위 JSON 객체를 UTF-8 파일 `task-patch.json`에 저장한 뒤 표준입력으로 전달한다. 아래 Python 경로도 실제 검증된 interpreter 경로로 바꾼다.

```sh
/path/to/python3 -I /path/to/app/run_work_stack.py --data-dir /path/to/ssot agent --workspace-uid 11111111-1111-4111-8111-111111111111 apply --stdin --intent-id agent.kr.apply.0001 < task-patch.json
```

성공 응답의 `data`가 정규화된 Task다. `expected_revision`이 현재와 다르면 CAS가 거절하고 쓰지 않는다. 같은 intent를 같은 stale 패킷으로 다시 넣어도 두 번째 쓰기는 생기지 않는다.

apply의 intent ID는 상관관계 식별자이며 checkpoint의 idempotency key와 다르다. `commit is unknown`이면 재전송을 중단하고 원래 intent와 패킷을 보존한다. revision을 다시 읽는 것만으로 미반영이 증명되거나 재시도가 허용되지는 않는다. 결과를 조정·확인하기 전에는 새 intent, 새 revision 또는 바뀐 패킷으로 재시도하지 않는다.

응답 유실 후 확인 로직은 반환된 Task와 제출한 변경값을 정확히 비교한다. 서버가 참조 순서 등을 정규화하여 값이 달라지면 성공을 추측하는 대신 불확정으로 남길 수 있다. 이 경우에도 위의 중단 규칙을 적용한다.
