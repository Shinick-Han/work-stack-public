# Work Stack structural quality and release regression gate plan

Date: 2026-08-31
Repository: `https://github.com/Shinick-Han/work-stack.git`
Worktree: the dedicated `source-providers` quality worktree
Branch: `codex/workstack-python-desktop-20260831`
Measured HEAD: `fc3e64e84f8b4759d6143a792a865ff008544ba4`
Measured tree: `b1e1427b73f1cefda9713967d4521096bd177ca2`

## 1. Decision

Work Stack is **functionally well protected but not yet structurally regression-proof**.
The correct response is not a broad rewrite. The release process should immediately
freeze structural regressions against a measured baseline, then reduce the highest-risk
hotspots in small behavior-preserving packets.

The first refactoring target should be `WorkStack.patch_task`, not `do_POST`:

- `patch_task` has CCN 72 and only 62.2% measured branch coverage within its function
  range. It combines validation, graph integrity, diffing, status transitions, activity,
  and persistence inside the planning authority.
- `do_POST` has the higher CCN of 79, but its measured branch coverage is 80.5% and its
  primary responsibility is transport dispatch. It remains the second target.

This policy deliberately allows known debt to exist while making it impossible to add
new debt silently.

## 2. What “structurally robust” means

No single metric proves robustness. The release assessment uses six independent lenses:

1. **Control-flow complexity**: cyclomatic complexity, nesting/cognitive burden, function
   length, and parameter count identify code that is hard to reason about.
2. **Dependency architecture**: cycles, reverse layer dependencies, unresolved imports,
   and feature-to-shared-component inversions identify coupling risk.
3. **Test adequacy**: line coverage is supplemented by branch coverage, changed-code
   coverage, characterization tests, and targeted mutation testing.
4. **Change concentration**: complexity multiplied by repository churn identifies files
   where defects and merge conflicts are most likely to accumulate.
5. **Duplication and module size**: repeated logic and oversized modules identify likely
   divergence and ownership problems.
6. **Release reproducibility**: the same quality command must run locally, in pull requests,
   and immediately before producing an installer and update manifest.

Cyclomatic complexity is a hotspot signal, not a one-to-one required-test count and not
a proof of bad design. A function with CCN 79 does not mechanically require exactly 79
tests. Branch semantics, invariants, and mutation survival matter more than a raw case
count.

## 3. Authoritative method and tool choices

| Concern | Work Stack tool | Why |
| --- | --- | --- |
| Python complexity | Ruff `C901` plus Lizard JSON/CSV baseline | Ruff supplies a fast hard gate; Lizard supplies symbol-level deltas and NLOC. |
| TypeScript complexity | ESLint `complexity`, `max-depth`, and `max-lines-per-function` | Native AST-aware TypeScript linting avoids treating JSX wrapper callbacks as reliable architectural scores. |
| Python architecture | Import Linter contracts | Enforces explicit forbidden and layered imports, including indirect paths. |
| TypeScript architecture | dependency-cruiser | Enforces named boundaries, unresolved imports, production-to-test imports, and cycles. Madge is a migration cross-check only and is removed from the required path after parity. |
| Python coverage | coverage.py with branch measurement and JSON | Produces line and branch data suitable for baseline comparison. |
| Frontend coverage | Vitest V8 coverage with per-file and global thresholds | Uses the existing test runner and produces statement, branch, function, and line metrics. |
| Duplication | jscpd | Cross-language clone detection with a machine-readable report; report/scheduled at first, promoted only after the deliberate clone fixture proves deterministic. |
| Test strength | mutmut for Python and StrykerJS for selected TypeScript modules | Detects assertions that execute code but do not detect behavioral mutations. |
| Static security | CodeQL in a separate required check | Covers security/correctness analysis without confusing it with maintainability metrics. |

Primary references:

- SonarSource cyclomatic complexity: https://www.sonarsource.com/resources/library/cyclomatic-complexity/
- SonarSource cognitive complexity: https://www.sonarsource.com/resources/cognitive-complexity/
- Ruff McCabe complexity rule: https://docs.astral.sh/ruff/rules/complex-structure/
- coverage.py branch coverage: https://coverage.readthedocs.io/en/7.15.2/
- ESLint complexity rule: https://eslint.org/docs/latest/rules/complexity
- Vitest coverage thresholds: https://vitest.dev/guide/cli.html
- Import Linter contracts: https://import-linter.readthedocs.io/en/latest/index.html
- dependency-cruiser rules: https://github.com/sverweij/dependency-cruiser/blob/main/doc/rules-reference.md
- Madge: https://github.com/pahen/madge
- mutmut: https://mutmut.readthedocs.io/en/latest/
- StrykerJS: https://stryker-mutator.io/docs/stryker-js/introduction/
- GitHub CodeQL setup: https://docs.github.com/en/code-security/concepts/code-scanning/setup-types

## 4. Provisional measurement baseline

The following measurements were run from a clean tree and are sufficient to prioritize
work, but they are **not yet an authoritative release floor**. Analysis packages were
installed outside the repository. Frontend coverage was measured with an ephemeral Vitest
3.2.4 and `@vitest/coverage-v8` 3.2.4 pair before the dependency tree was restored with
`npm ci`; the current lockfile resolves Vitest 3.2.7 and does not contain the coverage
provider. Complexity, coverage, duplication, source-universe, exclusion, and function-range
commands are not yet committed.

The current commit/tree and all values in this section remain provisional. The authoritative
baseline is created once the repository-owned measurement command is reproducible. It is a
normal reviewed file, not a special two-commit protocol.

`quality/structural-baseline.json` records the measurement commit and its deterministic
`measurement_source_digest` for provenance, plus a `config_digest` over locks and quality
configuration. Each candidate emits its own `candidate_source_digest`; it is expected to change
with product code and is not compared for equality with the baseline. CI instead requires the
baseline `config_digest` to match the active configuration and requires source discovery to
classify every eligible file. The baseline file and generated reports are excluded from all
digests, which removes self-reference without constraining merge strategy or commit history.
Squash, rebase, merge, and ordinary refactoring remain available.

The baseline never updates automatically. A deliberate baseline change is reviewed like code,
shows metric deltas, and cannot weaken the non-waivable product invariants. This preserves a
trustworthy floor while allowing the gate to be installed incrementally instead of requiring
every future quality component to exist before useful measurement begins.

### 4.1 Existing functional gate

- Python: 213 tests passed, 1 skipped.
- Frontend: 230 tests passed across 46 files.
- The existing Windows GitHub Actions job runs backend tests, frontend tests, production
  build, export audit, Chromium accessibility/product checks, and Firefox/WebKit
  compatibility checks.
- The current CI does not measure complexity, coverage, dependency contracts, duplication,
  mutation score, or CodeQL results and is not reused by an automated release-publish job.

The earlier report's statement that Python tests appear to exist in one file is obsolete.
The current repository contains 24 Python test modules and 213 discovered test methods.

### 4.2 Python complexity

Measured using Lizard 1.21.0 and independently reproduced with Radon 6.0.1:

- 353 production Python functions/methods.
- Mean CCN: 6.72; median CCN is low, but the tail is heavy.
- 56 functions exceed CCN 10.
- 37 functions exceed CCN 15.
- 11 functions exceed CCN 25.
- Provisional summed excess above CCN 15, `sum(max(CCN - 15, 0))`: 424.
- Radon grades: 2 F, 8 E, 12 D, 35 C, 80 B, 247 A across all discovered blocks.

Highest-risk production functions:

| Function | CCN | NLOC | Measured line coverage | Measured branch coverage |
| --- | ---: | ---: | ---: | ---: |
| `workstack/server.py::do_POST` | 79 | 361 | 87.3% | 80.5% |
| `workstack/service.py::patch_task` | 72 | 152 | 68.2% | 62.2% |
| `workstack/maintenance.py::_read_verified_archive` | 40 | 85 | 64.3% | 57.5% |
| `workstack/server.py::do_GET` | 39 | 126 | 68.6% | 68.0% |
| `workstack/store.py::_validate_ready_state_locked` | 36 | 91 | 74.6% | 65.8% |
| `workstack/cli.py::main` | 34 | 154 | 28.0% | 19.0% |
| `workstack/service.py::search_projection` | 33 | 124 | 85.2% | 82.1% |

Large Python modules are also concentration points:

- `workstack/service.py`: 3,226 lines and 21 historical touches.
- `workstack/store.py`: 1,236 lines and 11 historical touches.
- `workstack/server.py`: 1,001 lines and 19 historical touches.

### 4.3 Coverage

Provisional coverage.py 7.10.6 measurement:

- Core `workstack` statements: 3,803/4,547 = 83.64%.
- Core `workstack` branches: 1,245/1,790 = 69.55%.
- Core combined statements/branches: 5,048/6,337 = 79.66%.
- Desktop shell coverage is only 24% when included, so it must be tracked as a separate
  product lane rather than averaged into the backend and hiding the core signal.

Provisional Vitest 3.2.4 with V8 coverage:

- Statements/lines: 8,095/9,165 = 88.32%.
- Branches: 2,420/2,967 = 81.56%.
- Functions: 525/725 = 72.41%.

The frontend test suite is strong overall. Its function coverage and several large
orchestrating components still justify a no-regression floor and targeted extraction.

### 4.4 Dependency structure

- Python: 13 production modules, 24 internal import edges, zero detected cycles.
- Frontend: 115 files processed by Madge 8.0.0, zero detected cycles.
- Frontend has one skipped directory-style import (`features/workspace/views`) that must
  be made explicit or allowlisted by exact path before dependency-cruiser becomes a hard
  gate.
- A real boundary inversion exists: shared `components/Dialog.tsx` imports the
  feature-specific `features/inbox/sourceHostBridge.ts`. The host suspend/resume concern
  should move to an app/host integration boundary or be injected through a neutral hook.

### 4.5 Duplication and change concentration

jscpd 4.0.5 over production code only, minimum 8 lines/50 tokens:

- 5 clones, 53 duplicated lines out of 19,168: 0.28%.
- Duplication is not a release risk at present; the five clones are cleanup candidates,
  not blockers.

Highest churn files over the available repository history:

| Touches | Path |
| ---: | --- |
| 46 | `frontend/src/styles.css` |
| 35 | `frontend/src/app/App.tsx` |
| 21 | `workstack/service.py` |
| 19 | `workstack/server.py` |
| 17 | `frontend/src/features/tasks/TaskDrawer.tsx` |
| 17 | `frontend/src/api/client.ts` |
| 13 | `desktop/python-webview-shell/workstack_desktop.py` |

This is useful for prioritization, but it should initially be reported rather than hard
gated because product history is still short and feature-heavy.

### 4.6 Frontend concentration

- `TaskDrawer.tsx`: 911 lines.
- `App.tsx`: 867 lines and 27 direct imports.
- `api/client.ts`: 651 lines.
- `domain/schemas.ts`: 648 lines.
- `GraphView.tsx`: 547 lines.
- `styles.css`: 991 lines; `workspace-views.css`: 713 lines.

The frontend is acyclic, but `App.tsx` and `TaskDrawer.tsx` are composition hotspots.
Raw Lizard scores for anonymous TSX callbacks are noisy and must not become a hard gate;
ESLint's AST-aware function rules and dependency contracts are the authoritative checks.

## 5. Diagnosis

### Strong

- Broad functional, API, persistence, browser, accessibility, and compatibility tests.
- Good frontend feature organization and zero circular dependencies.
- Very low production duplication.
- Explicit idempotency, revision, fail-closed storage, and release checklist semantics.
- Current CI already validates more product behavior than the original CCN report assumed.

### Material risks

1. Planning-state mutations are concentrated in `service.py`, particularly `patch_task`.
2. HTTP dispatch is concentrated in two methods in `server.py`.
3. Backend branch coverage is materially weaker than line coverage, especially in the
   highest-risk mutation and archive paths.
4. Structural boundaries are conventions, not executable contracts.
5. `App.tsx`, `TaskDrawer.tsx`, and global CSS are growing change-conflict hotspots.
6. Desktop shell behavior is well covered by contract tests but poorly covered as executed
   Python code; native host/window/session code needs its own test lane.
7. There is no release workflow that refuses to publish an artifact when structural quality
   worsens.

### Verdict

**Continue product releases, but immediately install a baseline-aware structural gate.**
Do not wait for every hotspot to be refactored, and do not allow the gate to be bypassed by
raising thresholds or regenerating a baseline automatically.

## 6. Gate policy

### 6.1 Baseline rule

Generate `quality/structural-baseline.json` from the repository-owned quality command. It
contains only the data needed for stable comparison:

- schema version, measurement commit, `measurement_source_digest`, and `config_digest`;
- exact tool versions and normalized command arguments;
- included production roots and explicit exclusions;
- global coverage covered/total counts plus separate critical-module floors;
- the current high-risk complexity debt set and dependency-cycle count;
- report schema version and approved temporary exceptions.

Detailed per-file/symbol reports remain CI artifacts rather than inflating the baseline. CI
emits `candidate_source_digest`, recomputes `config_digest`, and fails on missing eligible source,
unknown production roots, malformed reports, or unreviewed exclusion changes. Source digest
difference alone is normal and never blocks a code change. The baseline does not contain its own
tree or artifact identity.

Pin resolved Vitest and `@vitest/coverage-v8` to the same exact version in
`frontend/package-lock.json`. Lock Ruff, Lizard, coverage.py, Import Linter, mutmut, ESLint,
dependency-cruiser, jscpd, and StrykerJS through repository-owned development lockfiles. A
package installed outside those locks may not generate an authoritative measurement or receipt.

The baseline never updates automatically in CI. Improvements may be accepted directly. A
temporary regression follows the lightweight exception rule in section 6.5. Changes to tools,
source roots, or exclusions update `config_digest` in the same reviewed pull request and include
the before/after report; they do not require a special commit topology.

Average CCN remains informational. The gate tracks the named high-risk debt set and new/changed
functions in planning, storage, canonical serialization, security, and installer code. Other
areas report complexity trends without blocking ordinary product work.

### 6.2 Pull-request hard gates

1. Existing backend/frontend tests and the production build pass.
2. Python and frontend dependency cycles remain zero; no unresolved production import or
   forbidden layer direction is introduced.
3. Source discovery includes every tracked production file under declared roots. New roots or
   unclassified executable files fail until classified. Generated/vendor exclusions require an
   exact path and generated marker; reproducible regeneration is required only for canonical
   fixtures and release inputs, and is otherwise a scheduled check.
4. Coverage is risk-based. The core backend, desktop shell, and frontend keep separate global
   floors. Planning, storage, canonical/docking, capture privacy, updater, and installer modules
   also keep explicit floors. Changed executable lines target 80% line coverage and changed
   branches target 70%; a justified miss is warning-level outside critical modules. Ordinary
   per-file fluctuations do not block a pull request when global, critical, and changed-code
   requirements remain healthy.
5. Complexity is hard-gated only for new or materially changed functions in critical modules:
   CCN above 15 requires a scoped exception. Elsewhere CCN above 15, functions above 100 NLOC,
   modules above 500 NLOC, and growth of an existing hotspot are warnings with a report link,
   not automatic failures. CCN 10 remains the preferred target.
6. Git rename detection preserves file-level history. AST/body copy/split lineage, clone
   identity, parameter count, nesting, and exact per-symbol NLOC are scheduled diagnostics, not
   PR blockers. Removing a named critical hotspot requires a reviewer-visible explanation of
   whether behavior was deleted, moved, or split.
7. Duplication remains report-only. A materially large new clone in critical planning,
   persistence, security, or installer code requires review; global clone percentage and exact
   clone identity do not block normal feature development.
8. Missing required tool output, malformed reports, source/config digest mismatch, or failure of
   a non-waivable product invariant fails closed. Informational tools may report unavailable
   without blocking when the required test/build/integrity lanes are complete.

Floors rise through ordinary reviewed improvements and never update automatically. The purpose
is to prevent meaningful regression, not to freeze every local metric or force unrelated cleanup
into a product change.

### 6.3 Release-candidate gates

All pull-request gates plus a proportional release matrix:

1. Chromium product/accessibility coverage and a Windows first-launch smoke run for every public
   release. Firefox/WebKit run on browser/rendering changes, major releases, and scheduled CI.
2. Targeted mutation is required when planning, storage, canonical serialization, capture
   privacy, updater, or installer logic changes. Other releases consume the latest scheduled
   mutation result and do not rerun mutation solely to publish unchanged critical code.
3. Installer/update changes run isolated install, upgrade, uninstall-preserves-data, and SSOT
   preservation. Other public releases run install plus first-launch/update-manifest smoke.
4. CodeQL high/critical alerts block when CodeQL is available. Capability is established in
   repository configuration rather than re-litigated per release; scheduled/default-branch
   results may satisfy a release when they cover the candidate commit.
5. The workflow accepts a full candidate SHA and canonical version, verifies default-branch
   reachability and required checks, checks out exactly that SHA, records `HEAD^{tree}`, rejects
   dirty/submodule drift, and verifies the source version.
6. Release-build starts from a clean checkout with absent output directories, builds the
   frontend once, freezes a sorted path/size/SHA-256 manifest, and packages only those bytes.
   Checksum and update manifest are generated without rebuilding or mutating frozen output.
7. Installer, checksum, update manifest, frozen manifest, and a concise `build-receipt.json` are
   uploaded once as one immutable artifact. The job exports its numeric artifact ID and service
   digest. The receipt records source commit/tree and payload hashes but does not attempt to
   contain the artifact's own ID or digest.
8. Downstream smoke and publish jobs download that one artifact by numeric ID, verify the
   service digest where supported and every internal payload hash, and never rebuild it.
9. A read-only `release-policy` job runs with `if: always()` and has explicit `needs` to
   release-build plus the jobs selected by a committed fail-closed change classifier. Unknown
   paths select the conservative full matrix. It fails on failed, cancelled, skipped-required,
   incomplete, unknown, or missing results and emits `allow_publish=true` only when satisfied.
10. Publish depends directly on release-policy and release-build, runs only with
    `allow_publish=true`, repeats artifact verification, and contains no checkout or build step.
11. The target remains the exact allowlisted `Shinick-Han/work-stack-public`. Only publish
    receives a protected, target-scoped GitHub App or fine-grained credential; the private
    source repository's ordinary `GITHUB_TOKEN` is not treated as cross-repository authority.
12. The human release receipt is concise: candidate identity, selected gate matrix and result,
    immutable artifact ID/digest, installer/update hashes, target release, exceptions, and links
    to detailed CI artifacts. Raw per-file metrics, credential metadata, and full tool reports
    stay in protected CI evidence rather than being duplicated into every receipt.

### 6.4 Scheduled deep checks

- Weekly: CodeQL, dependency audit, Firefox/WebKit compatibility, and hotspot/churn report.
- Monthly or before a major release: full Python/TypeScript mutation campaign, duplication
  analysis, AST/body lineage diagnostics, and generated-file reproducibility checks.
- Scheduled diagnostics inform refactoring and may be promoted selectively when repeated defects
  justify the cost. They do not silently become universal PR blockers.
- jscpd remains report-only unless duplication becomes a demonstrated defect source.

On native Windows, mutmut should run in WSL or a Linux GitHub Actions job because its
execution model requires fork semantics.

Core mutation may run on Linux/WSL. Windows-native desktop and installer behavior is proved by
the Windows lane, not inferred from Linux mutation.

### 6.5 Exception policy

An exception contains only `id`, exact scope, reason, issue URL, and expiry date/release. A
compensating test or reviewer may be added when the risk warrants it; independent approval is
not mandatory for this primarily single-owner project. Wildcard scope, silent renewal, and
expired exceptions fail.

No exception is permitted for commit/tree mismatch, dirty release input, a missing report from
an available required tool, artifact hash mismatch, unresolved imports/cycles,
docking-manifest mismatch, canonical-byte drift, fail-closed storage tests,
planning-authority tests, or unverified Microsoft enablement. A repository capability record
may explain why CodeQL is `NOT_AVAILABLE`; it cannot relabel an attempted, incomplete, or
missing available CodeQL run as passing.

No exception may waive a new high/critical CodeQL alert, provider trust/evidence rule,
canonical/docking or storage/planning integrity invariant, source-discovery completeness, or
release artifact hash mismatch. CodeQL availability is recorded once in repository settings;
an attempted available scan that fails or is incomplete is `FAIL`, not `NOT_AVAILABLE`.

Changing an inclusion rule, exclusion, generated/vendor classification, or supported source
root is reviewed with its before/after inventory and config digest. It needs deliberate-red
evidence only when the change affects a critical invariant or release input.

## 7. Architecture contracts to encode

### Python

Every tracked production Python file belongs to a named layer through committed path rules. New
production roots are unclassified until reviewed. Ordinary allowed imports do not need a
per-edge registry.

- Foundation modules `workstack.__init__`, `planning_status`, `snapshot_safety`, `unicode17`,
  and `capture` import no other Work Stack module.
- Canonical/docking imports are exact: `snapshot` may import only `snapshot_safety` and
  `unicode17`; `snapshot_conformance` may import only `snapshot`, `snapshot_safety`, and
  `unicode17`; `snapshot_export` may import only `snapshot`.
- Storage imports are exact: `store` may import only `planning_status`; `maintenance` may import
  only `store` and package-version metadata from `workstack.__init__`.
- Application code may import foundation, canonical, and storage predecessors but no transport,
  CLI, frontend, or desktop code.
- Transport may import application/storage/foundation predecessors but no CLI or desktop code.
- `cli`, `run_work_stack.py`, and the desktop launcher are composition roots. Core modules do
  not import them. The desktop launcher may import package-version metadata and its sibling
  desktop-update module only.
- Only a necessary exception to the layer direction is registered by exact importer/imported
  module, rationale, and expiry. Unclassified modules, unapproved reverse edges, and cycles fail.

### Frontend

Every production `.ts`/`.tsx` file belongs to exactly one committed partition: `domain`,
`config`, `utils`, `integration`, `api`, `components`, `features/<name>`, `app`, or a root
composition entrypoint. An unknown production directory fails; moving code into an unclassified
directory is not an escape.

- `domain` imports only domain code and external libraries.
- `config` and `utils` may import `domain` but not `integration`, `api`, `components`,
  `features`, or `app`.
- Neutral `integration` may import only `domain`, `config`, and `utils`. It exposes host/event
  interfaces and adapters but contains no feature UI and imports no `api`, `components`,
  `features`, or `app` module.
- `api` may import `domain`, `config`, `utils`, and neutral `integration`; it may not import
  `app`, `components`, or a feature. The planning-change bus moves from `app/crossTabSync.ts`
  into neutral `integration`, resolving the current `api/client.ts -> app/crossTabSync.ts` edge.
- `components` may import `domain`, `config`, `utils`, and other components, but not
  `integration`, `api`, a feature, or `app`. Shared `Dialog` becomes host-agnostic. The app
  composition root injects a lifecycle interface/context whose default is a no-op; its concrete
  desktop adapter lives in neutral `integration`. This removes the current
  `components/Dialog.tsx -> features/inbox/sourceHostBridge.ts` inversion without replacing it
  with `components -> integration`.
- `features/<name>` may import shared lower layers and its own feature. Cross-feature page or
  composition imports are forbidden. Stable cross-feature contract imports are allowed through
  a small exact exception registry; ordinary same-feature and lower-layer imports need no entry.
- `app` and `main.tsx` are composition roots and may import inward; no lower partition imports
  them.
- Production-to-test imports, unknown partitions, unregistered cross-feature imports,
  unresolved imports, indirect forbidden paths, and cycles fail.

Before moving Dialog host behavior, characterize open/close, unmount while open, unrelated
rerender, host unavailable, and failed suspension. A successful suspension receives one matching
resume; unavailable/failed suspension receives none; cleanup cannot resurrect a deactivated
provider. Concurrency and unusual multi-dialog histories remain scheduled diagnostics unless a
real defect makes them release-critical.

### Non-waivable integrity invariants

Every pull request executes planning-status, store-identity/corruption, capture/privacy,
snapshot conformance/export, API boundary, and Microsoft provider-gate suites. Installer/update
suites follow the risk-based release matrix in section 6.3. The Work Stack-Conduit
`MANIFEST.sha256` and frozen bundle root are recomputed whenever docking or snapshot inputs
change and before a public release.

Refactors may not move planning writes into transport, frontend, or desktop code; make
Conduit a planning-state authority; repair corrupt storage silently; alter canonical snapshot
bytes; weaken revision, idempotency, conflict, or SSOT semantics; or enable an unverified
Microsoft lane. Work Stack remains the sole planning-state authority. These invariants cannot
be waived through the exception mechanism.

## 8. Implementation sequence

### QG-1: Reproducible measurement harness

- Add repository-owned locked development dependencies for Ruff, Lizard, coverage.py,
  Import Linter, mutmut, ESLint, dependency-cruiser, Vitest plus the exact matching V8
  coverage provider, jscpd, and StrykerJS.
- Add one cross-platform entry point: `python scripts/quality_gate.py`.
- Commit explicit source universes/exclusions and normalized command definitions. Generate
  JSON plus Markdown with deterministic ordering and no timestamps inside the comparison
  payload.
- Generate the compact baseline, emit candidate source provenance, and verify `config_digest` in
  CI.
- Add deliberate-red fixtures for missing or zero-coverage source, new production root,
  unclassified executable source, dependency cycle/reverse/unresolved import, critical-function
  complexity regression, tool/config drift, and malformed/mismatched baseline. Rename/split/copy
  lineage and denominator-gaming fixtures move to the scheduled diagnostic suite.

Acceptance: the command passes locally and in CI on ordinary merge, squash, and rebase histories;
each required red fixture fails; a reviewed baseline update shows before/after metrics without
requiring special commits or artifact envelopes.

### QG-2: Executable architecture contracts

- Add Import Linter and dependency-cruiser contracts.
- Encode Python/frontend layer direction and reject unclassified production paths. Register only
  reverse-layer or cross-feature contract exceptions by exact path and expiry.
- Remove both current inversions: move the planning-change bus below `api/client.ts`, and move
  embedded-source suspension below shared `Dialog.tsx` while preserving its exact host lifecycle.
- Resolve the skipped directory import before enabling the unresolved-import gate.

Acceptance: both `api -> app` and `components -> feature` inversions are removed rather than
permanently allowlisted; unknown directories, unapproved reverse imports, page/composition
cross-feature imports, and the core Dialog lifecycle violations fail.

### QG-3: Coverage and integrity lane

- Add branch coverage to both existing unit-test commands.
- Add global and critical-module coverage floors plus changed-code coverage; retain per-file
  detail as report-only evidence.
- Treat backend core, desktop Python, and frontend as separate populations.
- Wire the non-waivable integrity suites and frozen docking-manifest/bundle-root checks as
  named required results.

Acceptance: all reports are retained as CI artifacts; deliberate missing-source, reduced
coverage, canonical-byte, provider-flag, and docking-root violations each fail closed.

### Risk-based mutation and Windows evidence lanes

- Install targeted mutation for critical-module changes and a scheduled full campaign.
- Maintain Windows first-launch smoke for public releases. Run the extended
  install/upgrade/uninstall/SSOT matrix when desktop, updater, installer, or persistence-sensitive
  packaging changes.

Acceptance: the change classifier selects the expected jobs in deliberate-red path fixtures;
unknown paths select the conservative full matrix rather than silently skipping evidence.

### Characterization safety packet

Before either backend refactor, add table-driven behavior matrices for both
`patch_task` and the HTTP boundary. The HTTP matrix freezes route/method matching,
Host/Origin/CSRF/content-type/body-limit handling, status and error codes, response shapes,
idempotency, and commit-unknown behavior. This packet may add tests only; it does not move
production logic.

### QG-4: Atomic immutable release workflow enforcement

- Refactor `.github/workflows/ci.yml` into reusable locked quality jobs invoked by pull-request
  and release workflows.
- Add a protected-default-branch release workflow accepting a verified full commit SHA and
  canonical version.
- Require an absent `frontend/dist` and release-output directories, build into fresh staging,
  freeze the exact dist manifest, and package it once without post-freeze mutation.
- Upload one immutable artifact containing payload and internal receipt; export its numeric ID
  and service digest. Make downstream jobs download that ID and forbid rebuilds.
- Add the read-only `release-policy` aggregation job with explicit `needs` to release-build and
  the risk-selected section 6.3 jobs. Publish depends directly on release-policy and
  release-build, requires `allow_publish=true`, repeats artifact checks, and contains no build.
- Validate the exact public target repository allowlist and give only publish a protected,
  target-scoped cross-repository GitHub App or fine-grained credential. Pin every third-party
  action to a full commit SHA.
- Never auto-update the baseline and never publish after failure, cancellation, skip, neutral,
  missing evidence, unknown status, or mismatch.

QG-4 may be installed incrementally. Publication activates after the baseline digest, release
build, change classifier, required smoke jobs, release-policy aggregation, and protected target
credential are green. Optional diagnostics do not block unrelated releases. Required selected
jobs may not be replaced by placeholders, `NOT_IMPLEMENTED`, or synthetic success.

Acceptance: a branch-name or unreachable SHA, pre-existing/post-freeze `frontend/dist` file,
artifact name-pattern download, missing required `needs`, skipped selected job,
cross-repository target/credential mismatch, and digest mismatch each block publication. The
successful case has one installer SHA-256 identical in build receipt, smoke result, update
manifest, and verified published asset.

### RF-1: `patch_task` behavior-preserving split

1. Add table-driven characterization cases for every field, invalid type/bound, no-op,
   revision conflict, parent/dependency cycle, status transition, activity, and persistence
   failure.
2. Extract pure field validators, including one shared local-date validator.
3. Extract relationship graph validation.
4. Extract diff construction and transition/activity facts.
5. Keep one transaction/persistence boundary in the orchestration method.

Goals: orchestrator and extracted-function CCN near 10, strong branch coverage for new logic,
and byte-for-byte API error/status characterization unchanged. These are refactor acceptance
goals, not unrelated release blockers.

### RF-2: `do_POST` dispatch split

1. Freeze route, method, Host/Origin/CSRF/content-type/body-limit, status, error-code, and
   response-shape behavior in table-driven HTTP tests.
2. Introduce an ordered route registry with exact path/regex matcher and handler.
3. Move request parsing and endpoint-specific adaptation into bounded handlers.
4. Keep domain behavior in `WorkStack`, not in transport handlers.

Goals: dispatcher and handler CCN near 10 while all existing browser-boundary and idempotency
tests remain green. A justified local exception does not block unrelated product work.

### QG-5: Targeted mutation and Windows desktop hardening

- Expand mutation sentinels and native Windows evidence according to observed risk. Critical
  changes use the required targeted lanes; unrelated releases rely on scheduled evidence.
- Ratchet operator coverage, fixed-sentinel scope, Windows scenarios, and reporting depth
  when repeated defects justify the added cost.
- Continue to record pre-existing survivors and require line/operator-specific dispositions
  for equivalent mutants. Fail on a new non-equivalent surviving, uncovered, or timed-out
  mutant in changed scope; report rather than block on pre-existing survivors outside changed
  scope.
- Track desktop Python coverage separately and expand native WebView/process/PowerShell/installer
  scenarios through executed Windows integration or isolated-install smoke evidence.
- Maintain the anti-gaming corpus as a scheduled diagnostic with delete-and-re-add variations,
  ambiguous many-to-many refactors, mixed copy/split/edit histories, generator relocation, and
  newly observed legitimate ambiguity. New cases first run report-only against historical
  commits, then become blocking after deterministic expected mappings are reviewed.

Acceptance: scheduled mutation detects a deliberately weakened critical assertion and the
Windows lane detects a representative desktop regression. Findings create focused follow-up
work rather than retroactively blocking unrelated releases.

### RF-3: Remaining backend hotspots

Order by complexity times uncovered branches:

1. `_read_verified_archive`
2. `_validate_ready_state_locked`
3. `do_GET`
4. `cli.main`
5. `_work_session_records` and planning projection validators

The CLI dispatcher can be split after archive/store integrity because its low coverage is
less dangerous than mutation or restore logic.

### RF-4: Frontend composition hotspots

- Split `App.tsx` into shell/navigation, task mutation coordination, sync/update coordination,
  and surface composition hooks.
- Split `TaskDrawer.tsx` into overview, context, relationships, and activity sections with
  one parent draft/conflict coordinator.
- Split `api/client.ts` by resource while retaining one shared transport/error layer.
- Move global CSS into feature-owned files without changing tokens or rendered behavior.

Goals: avoid new oversized page/components, move `App.tsx` and `TaskDrawer.tsx` toward 500 lines,
keep the dependency graph acyclic, and preserve visual/e2e behavior. The line counts are planning
targets rather than global release gates.

## 9. Recommended execution waves

The order is recommended rather than ceremonial. A safe, independently useful packet may land
early when its own inputs and tests are complete.

Wave A — lightweight structural floor:

- QG-1 repository command, source/config digests, compact baseline, and essential red fixtures;
- QG-2 layer rules, the two known inversion fixes, and core Dialog lifecycle tests;
- QG-3 global/critical/changed-code coverage and non-waivable integrity results.

Wave B — practical release protection:

- QG-4 single immutable artifact, numeric-ID consumption, change classifier, release-policy,
  protected target credential, and concise receipt;
- Chromium plus Windows first-launch smoke, with extended browser/mutation/installer lanes
  selected only by relevant changes or major-release policy.

Wave C — behavior-preserving hotspot reduction:

- add characterization for the packet being changed;
- RF-1 `patch_task`;
- RF-2 `do_POST`.

Wave D — deepen proof and reduce remaining backend risk:

- QG-5 scheduled mutation, Windows, lineage, duplication, and generated-file diagnostics;
- RF-3 remaining backend hotspots;
- promote a diagnostic to a hard gate only after repeated defects demonstrate ROI.

Wave E — evidence-driven frontend work:

- RF-4 frontend composition hotspots;
- CSS movement only if churn causes active conflicts and representative screenshot-diff
  evidence is added first;
- scheduled duplication/churn and full mutation reports.

Prefer independently revertible commits and separate behavior changes from structural movement.
A combined packet is allowed when characterization makes the behavioral delta explicit and
reviewable. Work Stack/Conduit docking contracts, canonical serialization, planning-state
authority, and SSOT conflict semantics remain invariant.

## 10. Required release receipt

Every human release receipt contains:

- repository, version, commit, tree, and clean-state result;
- `candidate_source_digest`, `config_digest`, and a link to the detailed quality artifact;
- risk classifier output, selected required jobs, pass/fail summary, and applicable exceptions;
- immutable artifact ID/service digest plus installer, checksum, and update-manifest SHA-256;
- allowlisted target repository/release and published-asset digest verification;
- Work Stack-Conduit bundle root and Microsoft provider flag/evidence summary when relevant;
- explicit nonclaims, especially unsigned publisher identity and external Microsoft
  exactly-once behavior.

Per-file coverage, complexity details, mutation operators, clone identities, raw SARIF, runner
versions, and credential metadata remain access-controlled CI artifacts. They are not duplicated
into the user-facing receipt unless needed to explain a failure or exception.

## 11. Non-goals

- No big-bang rewrite.
- No release freeze until all 37 Python functions above CCN 15 are eliminated.
- No coverage target of 100% as a substitute for meaningful assertions.
- No automatic baseline regeneration.
- No weakening of Work Stack's planning-state authority, fail-closed storage, docking
  contract, or Microsoft capture privacy boundary during refactoring.
- No expansion into Notion-style free-form blocks, multiplayer editing, plugin marketplace,
  or general cloud synchronization as part of a structural-quality refactor. Work Stack's
  fixed task-centered schema, local-first SSOT, revision/conflict safety, and sanitized capture
  remain product constraints.
- No claim that artifact digests, GitHub attestations, or a successful release gate are an
  Authenticode signature. Unsigned publisher identity remains an explicit distribution
  nonclaim until a real signing identity and verification lane exist.

## 12. Audit history and proportionality decision

Two adversarial audit rounds usefully exposed baseline self-reference, real dependency
inversions, incomplete publish dependencies, cross-repository authority, source-discovery gaps,
and artifact reproducibility risks. Those underlying risks remain addressed.

The audits also accumulated controls whose maintenance cost exceeded their likely defect
reduction for the current product: special H/B commit topology, AST/body lineage as a PR gate,
per-edge registries, two-artifact self-lineage, universal mutation/browser/installer matrices,
exact per-file no-regression, and highly bureaucratic exceptions/receipts. This revision
deliberately supersedes those remedies with proportional controls:

- content digests instead of special Git history;
- layer rules and exception-only registries instead of registering every edge;
- global/critical/changed-code floors instead of freezing every file metric;
- one build-once immutable artifact instead of recursive artifact lineage;
- change-selected release jobs plus scheduled deep diagnostics;
- concise exceptions and receipts with detailed evidence retained in CI artifacts.

The governing principle is: block releases for product-integrity, security, reproducibility, or
authority failures; warn, schedule, or request focused review for maintainability trends. A new
hard gate requires evidence that the defect class is material and that the gate catches it
reliably without repeatedly blocking unrelated work.
