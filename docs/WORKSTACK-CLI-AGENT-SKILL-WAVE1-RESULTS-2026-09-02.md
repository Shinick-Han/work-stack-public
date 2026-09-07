# Work Stack CLI + Agent Skill — Wave 1 Results

Date: 2026-09-02
Branch: `codex/workstack-python-desktop-20260831`
Savepoint: `1d378f517866ec0c73387851d8349dc511fefd61`

## 1. Purpose

Wave 1 tested whether a large pool of headless GLM workers could implement and characterize the
Agent P0 bootstrap faster than a small fixed worker pool, while path ownership and independent
admission remained strict.

The run used seven simultaneous OpenCode workers with
`openrouter/z-ai/glm-5.3-flash:floor`. Each worker was charged 3 GiB of scheduling headroom. Actual
average worker RSS was about 634 MiB, so memory was not the limiting resource.

## 2. Valid-run scorecard

| Metric | Result |
| --- | ---: |
| Dispatch spread across seven workers | 0.423 s |
| First completed result | 299.17 s / 4:59 |
| Valid-run wall time | 3587.25 s / 59:47 |
| Summed worker time | 7529.69 s / 2:05:30 |
| Work/wall parallel factor | 2.10x |
| Effective seven-slot utilization | 30.0% |

Per-packet duration:

| Packet | Duration | Initial supervisor state |
| --- | ---: | --- |
| X2 store/admission characterization | 4:59 | evidence ready |
| X3 context/projection characterization | 5:54 | evidence ready |
| X1 HTTP characterization | 6:24 | evidence ready |
| Q0 quality topology | 11:20 | blocked by test environment |
| R0 adversarial review | 11:37 | evidence ready |
| M0 interface manifest | 25:29 | ready for review |
| O1 Oracle seed | 59:47 | ready for review |

O1 consumed 99.99% of valid-run wall time and ran alone for the final 2057.66 seconds, or 57.36% of
the whole wave. It was the critical-path bottleneck.

## 3. Outcome quality

- Bounded read-only investigation was effective: X1, X2, X3 and R0 produced usable evidence in four
  of four attempts.
- Initial authoring acceptance without Codex rescue was zero of three. M0, Q0 and O1 all produced
  commits, but semantic review found defects that their local tests did not expose.
- Q0's reported test failure was not a product defect. The disposable checkout lacked ignored
  frontend dependencies; the same candidate passed `PASS: 164 production files` after the existing
  dependency directory was exposed safely.
- M0 required Codex correction for canonical bytes, exact ABI order, gate identifiers, envelope
  semantics, cross-list non-empty rules and a precomputable contract-fixture projection.
- O1's initial 83 passing tests did not prove the real contract. The runner lacked executable G10,
  did not validate M0, hashed source bytes instead of `contract_fixture_bytes()`, and applied
  probe-only synthetic seams to prospective product candidates.
- The first Codex rescue also remained untrusted until an independent Codex audit found three real
  composition defects: CRLF checkout bytes broke manifest loading on Windows, lane-array order was
  treated as semantic, and a wrong conformance lane label was accepted. O1-R3 fixed all three.
- The accepted bootstrap passes 97 Oracle tests and the 164-file architecture quality gate. An
  independently built O2/T0 pair then passed all 19 executable G10 checks against the actual M0
  manifest, including exact ABI, fixture bytes and receipt digests.

## 4. Orchestration failures

These failures are infrastructure defects and are not charged to the model:

1. The first run used a 263-character checkout path and failed under legacy Windows path handling.
2. Q0's isolated checkout lacked ignored `frontend/node_modules`, causing a false required-test
   failure.
3. The valid batch persisted its final result, then CP949 console encoding failed while rendering an
   em dash.

Corrections already made locally include a short run root, Git `core.longpaths=true`, 3 GiB worker
headroom, UTF-8 Python/test environments, UTF-8 result rendering and progressive batch receipts.
Ignored dependency staging and base-test preflight remain mandatory before another batch.

## 5. Mixed-worker decision

GLM/OpenCode remains the throughput pool for bounded evidence, mechanical one-file changes, narrow
fixtures and independently testable conformance packets. Codex is the rescue and critical-path pool
for contract/Oracle decisions, safety-sensitive authority or transport logic, failed-gate diagnosis,
cross-candidate semantic review and integration.

Direct Codex assignment is required when a packet:

- owns contract or Oracle policy;
- spans more than eight files or two subsystems;
- decides authority, no-fallback, response-loss or idempotency behavior; or
- blocks at least three downstream packets.

An active GLM packet receives read-only Codex review at 75% of timeout. One semantic rejection ends
same-model retries. A new Codex attempt may author only after the old attempt is rejected or expired.
No model can self-admit a candidate.

The Codex pool is recorded as three explicit profiles rather than one opaque fallback:

| Profile | Model | Assignment |
| --- | --- | --- |
| `rescue-deep` | `gpt-5.6-sol` | contract/Oracle conflicts, adversarial review, multi-module critical path |
| `rescue-balanced` | `gpt-5.6-terra` | candidate audit, gate triage, bounded corrective implementation |
| `rescue-volume` | `gpt-5.6-luna` | repetitive receipt checks, reproduction and evidence extraction |

There is no silent model substitution. An unavailable profile leaves the packet queued until the
orchestrator records a new attempt and explicit model choice.

## 6. Oracle regeneration split

The original O1 packet combined 32 files and should not be repeated. A regeneration uses four
parallel GLM packets for schema/validator, pinned directives, probes/fixtures and mutants/golden
vectors. A Codex-only O1-RUNNER packet then owns the runner plus ownership/import/E2E tests. M0 stays
one Codex-owned file because splitting a single contract manifest creates write contention rather
than useful parallelism.

## 7. Exit result

The accepted bootstrap was composed on the private branch as:

- M0 contract manifest: `5435e18`;
- Q0 quality topology: `0f56d26`;
- O1 trusted Oracle bootstrap: `6178a95`.

The composed tree passes the 97-test Oracle suite, the 164-file architecture quality gate, exact
7,242-byte contract fixture verification, Windows CRLF regression and independent synthetic G10
composition. This tree is the sole admissible base for O2/T0 dispatch after its final push.
