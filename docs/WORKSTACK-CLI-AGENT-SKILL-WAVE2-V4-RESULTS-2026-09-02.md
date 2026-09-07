# Work Stack CLI + Agent Skill — Wave 2 DeepSeek V4 Results

Date: 2026-09-02  
Branch: `codex/workstack-python-desktop-20260831`  
Wave base: `b6d9b7fb57d6f9a6fffe407174b598a8889ee5bd`

## 1. Purpose

Wave 2 assigned the post-G10 implementation and independent conformance packets to headless
OpenCode workers using `openrouter/deepseek/deepseek-v4-flash:floor` with `high` effort. The run was
not designed as a benchmark. It reused the work that would otherwise have gone to the GLM
throughput pool so that ordinary orchestration evidence could guide the next routing decision.

Seven workers ran concurrently with 3 GiB of scheduling headroom each. Codex workers were reserved
for independent admission, failed-gate diagnosis and bounded replacement work. No OpenCode worker
could self-admit its own candidate.

## 2. Raw-run scorecard

| Metric | Result |
| --- | ---: |
| Packets | 14 |
| Concurrent workers | 7 |
| Dispatch-to-finish wall time | 1,790.10 s / 29:50 |
| Summed worker time | 10,822.89 s / 3:00:23 |
| Work/wall parallel factor | 6.05x |
| First terminal result | 6:34 |
| First `READY_FOR_REVIEW` result | 7:17 |
| Supervisor `READY_FOR_REVIEW` | 2 / 14 |
| Supervisor blocked or failed | 12 / 14 |
| Independently accepted without correction | 0 / 14 |

The two candidates reported ready by the supervisor were TA and TC2. Independent review rejected
both: TA targeted the wrong requester ABI and contained vacuous response-loss checks; TC2 targeted
the wrong backend method and asserted a non-contract command name. Most other workers changed only
their owned paths and produced useful implementation structure, but failed the terminal-record
protocol or the frozen semantics.

## 3. Accepted G21 result

Codex reviewers recovered useful candidates where possible, replaced invalid conformance suites,
and admitted only implementation/test pairs that passed in disposable composition. The following
seven gates are integrated:

| Gate | Main implementation | Main independent test | Result |
| --- | --- | --- | --- |
| G21-A transport | `f63a4e1` | `31c2957` | 15/15 |
| G21-B1 authority | `dc3bda5` | `5f3a3b7` | 18/18, 10 mutants |
| G21-B2 local backend | `5e49af5` | `65ea862` | 7/7 |
| G21-C1 status | `30fd9ea` | `ac355ed` | 16/16, 5 mutants |
| G21-C2 context | `492a188` | `fb701d0` | 25/25, 5 mutants |
| G21-C3 checkpoint | `d39f30f` | `8799722` | 15/15, 7 mutants |
| G21-D Skill | `f4f2ac5` | `44285be` | 6/6 plus pinned validator |

The composed branch passes:

- 148 focused agent contract/conformance tests;
- 97/97 pinned Agent P0 Oracle tests; and
- `PASS: 171 production files` from the architecture quality gate.

## 4. What V4 contributed

V4's useful contribution was throughput. It sustained a 6.05x parallel factor, produced bounded
one-file implementations quickly, and generally respected path ownership. Its structural drafts
reduced the amount of blank-page implementation work for authority, local backend, command-handler
and Skill lanes. The D production Skill candidate was retained and admitted unchanged after a new
independent TD suite proved it.

The main cost was semantic correction. Recurring errors included invented ABI names, wrong command
or envelope fields, invalid fixtures, vacuous tests, and failure to emit the required single JSON
terminal record. Safety-sensitive transport, authority, idempotency and exact conformance work all
needed Codex intervention before admission. A passing self-authored test was not a useful confidence
signal by itself.

One failure was orchestration infrastructure rather than model quality: a long generated Git ref
exceeded Windows path limits. The local OpenCode supervisor now derives short packet/ref names and
its 15 tests pass.

## 5. Natural comparison with GLM Wave 1

The workloads were different, so the following is an operational judgment rather than a controlled
model benchmark.

| Observation | GLM 5.3 Flash Wave 1 | DeepSeek V4 Flash Wave 2 |
| --- | --- | --- |
| Best use observed | bounded read-only investigation | fast structural scaffolding |
| Raw authoring acceptance | 0/3 | 0/14 |
| Read-only evidence | 4/4 useful | not the focus of this wave |
| Parallel factor | 2.10x | 6.05x |
| Main weakness | slow critical-path authoring | frozen-contract precision and terminal protocol |

V4 was more productive than GLM as a code-producing throughput worker, but neither model earned
authority to own acceptance-sensitive work. V4 should be preferred for narrow, mechanically
checkable one-file scaffolds when an independent executable contract already exists. GLM remains
better evidenced for read-only characterization. Codex remains the default for contracts, Oracle
policy, transport/authority decisions, adversarial conformance, failed-gate rescue and integration.

The practical routing rule is therefore:

1. send bounded investigation to the cheapest proven evidence worker;
2. send narrow mechanical production work to V4 only when a blind independent test lane exists;
3. route contract, authority, response-loss and idempotency work directly to Codex; and
4. stop same-model retries after one semantic rejection.

## 6. Exit result

Wave 2 closes all seven G21 implementation/conformance pairs. Runtime composition (`I1`, `I2`,
`I3`) and the independent CLI end-to-end lane (`TE`) remain later gates; they were deliberately not
mixed into this model run before the G21 receipts existed.
