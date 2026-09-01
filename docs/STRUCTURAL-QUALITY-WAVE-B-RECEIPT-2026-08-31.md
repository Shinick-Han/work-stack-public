# Structural Quality Wave B Receipt — 2026-08-31

## Scope and source coordinate

This receipt covers the practical release-protection packet built on top of Wave A in the
`codex/workstack-python-desktop-20260831` worktree. The pre-implementation repository coordinate
remained:

- HEAD: `fc3e64e84f8b4759d6143a792a865ff008544ba4`
- tree: `b1e1427b73f1cefda9713967d4521096bd177ca2`

The Wave A and Wave B changes are intentionally uncommitted at this receipt boundary. No push or
public release occurred.

## Implemented release controls

- `.github/workflows/ci.yml` now calls one reusable locked quality workflow.
- `.github/workflows/quality-reusable.yml` checks out an explicit candidate SHA and retains the
  existing structural, coverage, unit, build, export, Chromium, Firefox, and WebKit gates.
- `.github/workflows/release.yml` accepts a full candidate SHA, canonical version, and full previous
  release SHA; refuses an unrelated range; validates default-branch reachability, exact HEAD/tree,
  source version, clean status, and submodule state.
- The release job requires absent output directories, builds the frontend exactly once, freezes the
  frontend path/size/SHA-256 manifest, packages once, rechecks both frozen bytes and Git state, and
  uploads one product artifact.
- Downstream first-launch, native Chromium, extended Windows, and publish jobs download the product
  artifact by numeric artifact ID. First-launch and publish contain no checkout or build step.
- `quality/release-path-policy.json` selects proportional gates over the full range from the prior
  release. Unknown paths select every optional gate.
- `scripts/release_gate.py` supplies fail-closed candidate, change-range, frozen-tree, bundle, and
  release-policy validation.
- `scripts/windows/Test-WorkStackReleaseBundle.ps1` independently validates the exact bundle file
  set, receipt entries, sizes, SHA-256 values, sidecar, and update manifest under Windows PowerShell
  5.1 without depending on module auto-loading.
- `scripts/run_mutation_sentinels.py` creates disposable source copies and proves that focused tests
  kill three real weakened invariants: the safe-integer revision ceiling, Microsoft source URL
  bound, and canonical snapshot byte envelope. The product worktree is not mutated.
- A read-only `release-policy` job depends explicitly on every selectable result, refuses selected
  skipped/missing/unknown/non-success results, and writes a concise human summary. Publication is
  hardcoded to `Shinick-Han/work-stack-public` and additionally requires `publish=true`, policy
  allow, a protected environment, and its target-scoped secret.
- All external actions in all three workflows are pinned to full commit SHAs.

## Test and analysis evidence

- RED-first release tests initially failed because `scripts/release_gate.py` and `release.yml` did
  not exist. The completed release/mutation/workflow test set passes.
- Backend: 239 tests passed; one pre-existing Windows symlink-permission case skipped.
- Frontend: 46 files and 231 tests passed.
- Frontend coverage: 89.22% lines, 81.60% branches, 72.55% functions.
- Python coverage from the complete branch-coverage run before the final non-product fixture text
  correction remained above the configured 69% line and 60% branch global floors; the proportional
  gate passed. The final complete non-coverage rerun passed all 239 tests.
- Structural gate: PASS, 82 production files; zero unclassified production sources, unresolved code
  imports, architecture violations, or dependency cycles.
- Mutation sentinels: 3 killed, 0 survived.
- Export audit: PASS, 327 UTF-8 source-policy files.
- Frontend production build: PASS. The existing large-chunk warning remains report-only.
- `git diff --check`: PASS; only repository line-ending conversion warnings were printed.
- Official actionlint 1.7.12 was downloaded outside the repository, verified against the release
  checksum (`6e7241b51e6817ea6a047693d8e6fed13b31819c9a0dd6c5a726e1592d22f6e9`), and returned exit 0 for
  all workflow files.

## Native build-once smoke evidence

The current uncommitted source was used only for a local workflow-mechanics smoke. This is not a
publishable candidate receipt because the repository coordinate above predates the working-tree
changes.

- Built installer: `WorkStack-Setup-1.0.5.ps1`
- Size: 24,905,044 bytes
- Installer SHA-256: `ed5b194381dad82240c77a43e18d36239933787172987a0ecac1d246b74528d4`
- Frozen dist manifest SHA-256:
  `da2df8d63fb2149531c94a23b74bee5f2e7ffd2bce749d6c8a2a17339085f0fa`
- Build receipt and Python verifier: PASS
- Shipping Windows PowerShell verifier: PASS
- Isolated install: PASS
- Installed launcher ownership receipt: `started`
- Installed HTTP readiness: `ready/v1` on loopback port 19065
- Stop verification: the owned server process was absent after shutdown
- Ignored local evidence directory:
  `.artifacts/wave-b-local-352cc2d1f602468dbe8811db7209f0d9`

## Current structural baseline

- Baseline file SHA-256: `431cf3157cc3166b0c1dde9668c5d54ad306a2acd79c37625b80ece5a179df9a`
- Quality configuration SHA-256:
  `2bcb8df3e396229a01c0f1e4919e159748acb6df08ae8d77555568853856112b`
- Release path policy SHA-256:
  `b3eec57498db0384769bf8593690af161401b07e0896c3a151916c84a09a7f5c`
- Populations: 62 frontend production, 13 Python core, 2 Python desktop, 1 Python entrypoint,
  and 4 quality/release tooling files.
- Existing critical complexity debt remains 26 symbols; no new critical debt was admitted.

## External activation requirements and nonclaims

Source bytes cannot prove GitHub repository settings. Before the first real `publish=true` run, an
owner must configure and independently verify the protected environment
`work-stack-public-release`, its `WORKSTACK_PUBLIC_RELEASE_TOKEN`, default-branch protection, and
the token or GitHub App's target-only authority. No claim is made that those settings currently
exist.

This packet does not claim code signing, a successful GitHub-hosted workflow run, a numeric artifact
ID, an artifact-service digest, public publication, universal Windows endpoint compatibility,
Microsoft tenant acceptance, or external exactly-once delivery. Those claims require the exact
committed candidate to pass the remote workflow. It also does not replace the deeper scheduled
mutmut/Stryker and CodeQL work planned in later waves; the bounded sentinels are immediate changed-
critical protection, not a comprehensive mutation campaign.
