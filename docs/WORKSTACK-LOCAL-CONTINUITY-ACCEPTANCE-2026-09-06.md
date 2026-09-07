# Work Stack local continuity acceptance — 2026-09-06

이 문서는 **U-03 (Agent Continuity roundtrip)** 를 향한 **좁은 checkpoint CLI 하위흐름
증거**다. U-03 전체 인수가 아니다.

**U-01은 이 왕복이 아니며, 별도로 미완(pending)이다.** U-01은 현재 제품·운영 좌표를 가리키는
포인터다. `docs/CURRENT-IMPLEMENTATION-STATUS.md`는 **아직 그 좌표를 담고 있지 않다** — 설치된
HEAD, 배포 asset, profile, workspace UID 같은 운영 좌표는 그 문서에 없다. 최종 wave release
좌표는 통합 이후 root에서 따로 기록한다. 이 문서는 U-01의 수령증이 아니고, 아래 실행이 U-01을
종결하지도 않는다.

## 0. 이 실행이 U-03의 어디까지인가

U-03의 정식 인수 범위는 다음 사슬 전체다.

1. 실제 Task
2. goal / source / relationships / freshness / omission 맥락
3. 실행(execution)
4. result / Next / Blocker / conflict
5. 다음날 resume

아래 실행이 **덮은 구간은 1, 2의 일부, 4의 일부, 5의 기계적 부분**뿐이다. 구체적으로:

- 맥락(2)은 **의도적으로 좁다.** `agent context`는 `omitted`에
  `objectives`, `relationships`, `captures`, `work_sessions`를 스스로 명시하며 반환한다.
  즉 goal·relationships·source 맥락은 이 응답에 **들어 있지 않고**, freshness 판단 근거도
  Task revision과 최근 worklog로 한정된다.
- **실행(3)은 전혀 수행하지 않았다.** 이 왕복은 계획 사실을 읽고 checkpoint를 덧붙일 뿐이다.
- **conflict(4의 일부)는 발생시키지도 검증하지도 않았다.** `commit_unknown`, revision 충돌,
  경쟁 writer는 이 실행의 경로에 등장하지 않았다.
- **다음날 resume(5)** 은 같은 fixture 안에서 **새 CLI 호출이 직전 checkpoint를 읽는다**는
  기계적 성질만 보였다. 실제 사람이 하루 뒤 이어받는 인수는 하지 않았다.

따라서 아래의 어떤 통과 표시도 **"U-03 CLI 계약 통과"로 읽으면 안 된다.**

## 1. 결론 요약

| 항목 | 상태 |
|---|---|
| checkpoint CLI 하위흐름 (status → context → checkpoint → replay → context, exclusive-local) | **실행됨** — §3 |
| 동일 intent·동일 body 재전송의 멱등성 | **확인됨** — `replayed: true`, 중복 기록 없음 |
| 새 CLI 호출이 직전 checkpoint를 읽음 | **확인됨** — `recent_worklog` 1건 |
| goal / source / relationships 맥락 | **미검증** — 응답에서 의도적으로 omitted (§0) |
| 실행(execution) 단계 | **미검증** — 이 왕복에 없음 (§0) |
| conflict / `commit_unknown` 처리 | **미검증** — 발생시키지 않음 (§0) |
| running-owner transport (owner/server metadata 존재 시) | **미검증** — §5 |
| human next-day acceptance (실사용자 다음날 인수) | **미완 (pending)** — §5, §6 |
| **U-03 전체 인수** | **미완.** 위 미검증 항목이 남아 있다 |

## 2. 범위와 안전 경계

- agent Skill의 정본은 `integrations/agent-skill/work-stack/SKILL.md`이며 명령 표면은
  `integrations/agent-skill/work-stack/references/commands.md`가 소유한다. 이 문서는 그
  계약을 **복제하지 않고 실행 결과만** 기록한다.
- 실행은 임시 디렉터리 안에 새로 만든 **합성 v3 authority** 하나만 사용했다. `--data-dir`는
  그 fixture를 직접 가리켰고, `WORK_STACK_RUNTIME=<FIX>/runtime`으로 runtime 상태도 같은
  fixture 안으로 고정했다(§3의 환경 전문). 따라서 이 실행이 접근할 수 있었던 authority는
  fixture 하나뿐이며, 실제 사용자 계획 데이터는 읽지도 쓰지도 않았다.
- 임시 fixture 경로와 workspace UID는 실행 후 폐기되는 합성 값이다. 이 문서에는 기계
  사용자명·개인 runtime 경로를 남기지 않으며, 아래에서 fixture 루트는 `<FIX>`로 쓴다.
- 실행에 사용한 intent ID `agent:u01:checkpoint-0001`은 실행 당시의 값 그대로다. 기록된
  증거이므로 사후에 고쳐 쓰지 않는다. 이 문자열은 위의 U-01/U-03 구분과 무관하다.

## 3. 실행한 명령과 결과 (exclusive-local transport)

준비 단계는 계약 검증 대상이 아니다. agent CLI는 authority를 만들지 않으므로, 앱이 첫
실행에서 하는 것과 같은 방식으로 제품 store seam(`workstack.service.WorkStack` +
`workstack.store.Store`)을 한 번 호출해 빈 v3 authority만 물리적으로 만들었다. 그 뒤
**기존 Task 생성부터 모든 단계는 지원되는 CLI로만** 수행했다.

아래 모든 명령은 하나의 환경 전문(preamble) 아래에서 실행했다. 값은 fixture 루트를 `<FIX>`로
치환한 것 외에는 실행 당시 그대로다.

```text
# 환경 전문: runtime 상태도 같은 fixture 안으로 고정한다
export WORK_STACK_RUNTIME=<FIX>/runtime
```

```text
# 준비: 기존 Task 하나 (지원되는 CLI)
python run_work_stack.py --data-dir <FIX>/data \
  backlog add "Synthetic continuity fixture task" --priority P1 --detail "Contained U01 fixture"
```

`<WS_UID>`는 이 fixture의 `workspace.json`에서 한 번 읽은 값
`1ad37782-a6c9-440c-a75b-68ecb3f7f214`이며, 폐기된 임시 fixture의 식별자다.

### 3.1 status

```text
python run_work_stack.py --data-dir <FIX>/data agent --workspace-uid <WS_UID> status
```

`exit=0`. 핵심 필드:

```json
{
  "ready": true,
  "storage_format": "v3",
  "capability_supported": true,
  "data_dir_available": true,
  "exclusive_local_available": true,
  "running_server_available": false
}
```

`meta.transport`는 `exclusive-local`이다. 즉 이 전체 실행은 **owner/server metadata가 없는
상태**의 경로다.

### 3.2 context (checkpoint 이전)

```text
python run_work_stack.py --data-dir <FIX>/data agent --workspace-uid <WS_UID> context --task T-0001
```

`exit=0`. `data.recent_worklog`는 `[]`이고, `data.task`는 `status: "open"`,
`revision: 0`의 allowlisted projection이다. `data.omitted`는
`["attachments","captures","objectives","relationships","work_sessions"]`로 무엇이
의도적으로 제외됐는지 스스로 밝힌다.

### 3.3 checkpoint (최초 commit)

```text
python run_work_stack.py --data-dir <FIX>/data agent --workspace-uid <WS_UID> \
  checkpoint --intent-id agent:u01:checkpoint-0001 --stdin
```

stdin (5-field UTF-8 JSON 한 개):

```json
{
  "blockers": [],
  "date": "2026-09-06",
  "done": ["Read the selected Task context through the agent CLI."],
  "next": ["Confirm the identical replay stays idempotent."],
  "task_id": "T-0001"
}
```

`exit=0`, `meta.commit_state = "committed"`, `meta.replayed = false`.

### 3.4 동일 replay (같은 intent ID, 같은 body)

같은 명령·같은 stdin을 그대로 한 번 더 실행했다.

`exit=0`, `meta.commit_state = "committed"`, **`meta.replayed = true`**.
`data`는 3.3과 동일하다.

### 3.5 fresh context (새 세션이 이어받는다)

```text
python run_work_stack.py --data-dir <FIX>/data agent --workspace-uid <WS_UID> context --task T-0001
```

`exit=0`. `data.recent_worklog`가 정확히 **1건**이 되었다:

```json
[
  {
    "blockers": [],
    "date": "2026-09-06",
    "done": ["Read the selected Task context through the agent CLI."],
    "next": ["Confirm the identical replay stays idempotent."]
  }
]
```

두 번 보낸 checkpoint가 **1건으로만 남았다**는 점이 checkpoint 하위흐름의 멱등성과
기계적 인계를 동시에 보인다. 이는 U-03 사슬의 일부이며 전체가 아니다(§0).

### 3.6 진단용 worklog 읽기

```text
python run_work_stack.py --data-dir <FIX>/data worklog list --date 2026-09-06
```

`exit=0`, `entries` 1건. 이 legacy 읽기는 진단 증거일 뿐이며
`commit_unknown`을 해소하지 못한다(정본 참조: `references/commands.md`).

## 4. 자동 회귀 근거

```text
python scripts/run_backend_tests.py --pattern "test_agent_*.py" --quiet
```

`Ran 174 tests` / `OK` / `exit: 0`. 이 pattern에는 agent CLI 계약, 전송(transport),
status/context/checkpoint 개별 계약, 그리고 `integrations/agent-skill/work-stack`의 Skill
계약 검사(`tests/test_agent_skill_contract.py`,
`quality/agent-p0-oracle/validate_skill.py`)가 포함된다. 실행 중 보이는 `usage:` 줄은
argparse 거부를 검사하는 케이스의 정상 출력이다.

## 5. 세 가지 검증 층위는 서로 다른 것이다

`running-server`는 **GUI 화면이 떠 있는지**를 뜻하지 않는다. agent runtime은
`store.server_info_path`에 **running owner가 남긴 server metadata가 존재하는지**만 보고
전송을 고른다(`workstack/agent_runtime.py`의 `_owner_metadata_present`). 아래 셋을 섞지 않는다.

| 층위 | 조건 | 이번 상태 |
|---|---|---|
| **exclusive-local** | owner/server metadata 없음 → CLI가 authority를 직접 배타적으로 연다 | **§3에서 실제 실행함** (좁은 하위흐름 범위, §0) |
| **running-owner** | owner/server metadata 존재 → CLI가 그 owner에 요청을 보낸다 | **이번 수동 실행에서 미검증.** fixture에 metadata를 쓰지 않았다. 이 전송과 `commit_unknown` 경계는 §4의 자동 계약 테스트(`tests/test_agent_transport_contract.py`, `tests/test_agent_cli_e2e_contract.py`)가 담당한다 |
| **human next-day acceptance** | 실사용자의 실제 workspace에서 다음날 새 세션이 전날 checkpoint를 이어받음 | **미완 (pending).** 자동·합성 검증이 대신할 수 없다 |

## 6. U-03 전체 인수를 위해 남은 것

위 실행은 **합성 fixture에 대한 좁은 하위흐름 증거**다. 다음 항목은 여전히 미완이며, 이
문서의 어떤 확인 표시도 이를 대신하지 않는다.

1. **맥락 폭** — goal, source, relationships, freshness, omission을 실제로 판단에 쓰는
   경로. 현재 `agent context`는 objectives·relationships·captures·work_sessions를
   의도적으로 omit한다. U-03이 이 맥락을 요구한다면 무엇이 그 맥락을 공급하는지부터
   정해야 한다.
2. **실행(execution) 단계** — 읽기·기록만이 아니라 실제 작업 수행이 사슬에 포함될 때의 동작.
3. **result / Next / Blocker / conflict** — 특히 conflict와 `commit_unknown`이 실제로
   발생했을 때 사람이 어떻게 판단하는지에 대한 운영 확인.
4. **running-owner transport** — owner/server metadata가 존재하는 상태에서의 동일 왕복
   수동 확인.
5. **human next-day acceptance** — 실제 사용자의 실제 workspace에서 다음날 새 세션이
   전날 checkpoint를 맥락으로 이어받는 인수 시험.

이 다섯이 끝나기 전까지 U-03은 **"좁은 checkpoint 하위흐름만 검증됨, 전체 인수 미완"**
상태다.
