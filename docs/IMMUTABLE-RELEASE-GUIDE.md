# Immutable release workflow

Work Stack public releases are built by `.github/workflows/release.yml`. The workflow turns one
reviewed source commit into one immutable GitHub Actions artifact and makes every artifact-level
smoke and publication job consume that artifact by its numeric artifact ID. Downstream jobs do not
rebuild the installer.

## Required dispatch inputs

- `candidate_sha`: the full lowercase 40-character SHA to release. It must be checked out exactly,
  match `HEAD`, and be reachable from the protected default branch.
- `version`: canonical `X.Y.Z`, exactly matching `workstack/__init__.py`.
- `previous_version`: canonical `X.Y.Z` of the immediately preceding public installer. The
  Windows extended gate downloads that exact release and proves upgrade, configuration/SSOT
  preservation, and injected-failure rollback against the candidate artifact.
- `release_base_sha`: the full SHA recorded by the preceding published release. It must be an
  ancestor of `candidate_sha`; the complete range is used for risk selection.
- `publish`: leave false for a build-and-smoke rehearsal. Set true only after the protected public
  release environment and target-scoped credential below are configured.

Do not use a branch name, abbreviated SHA, tag, unrelated base, or locally rebuilt installer as a
substitute for these inputs.

## Build and verification contract

The release build refuses pre-existing `frontend/dist` and release-output directories, validates a
clean candidate checkout, builds the frontend once, freezes a sorted path/size/SHA-256 manifest,
and then runs the existing offline Windows installer builder. It rechecks the frozen tree and Git
checkout after packaging.

The one uploaded product artifact contains exactly:

- `WorkStack-Setup-X.Y.Z.ps1`;
- its exact `.sha256` sidecar;
- `workstack-update.json`;
- `frozen-dist-manifest.json`;
- `Test-WorkStackReleaseBundle.ps1`;
- `build-receipt.json` with candidate commit/tree and every payload size and SHA-256.

The receipt intentionally does not claim to contain the GitHub artifact's own ID or service digest.
Those values exist only after upload and are recorded in the release-policy job summary.

## Risk selection

`quality/release-path-policy.json` is fail closed. Every release runs the reusable full quality gate,
an installed Windows first-launch check, and a native Chromium render of the installed artifact.
Every release also runs the isolated prior-version Windows upgrade, exact configuration/SSOT
preservation, and injected-failure rollback matrix. Relevant changes additionally select
Firefox/WebKit compatibility and three real critical mutation sentinels. A path unknown to the
policy selects every optional gate.

The read-only `release-policy` job has explicit dependencies on every possible selected job.
Failure, cancellation, a selected skip, a missing result, or an unknown result denies publication.

## Public target configuration

Publication is hardcoded to `Shinick-Han/work-stack-public`. Before enabling it, configure:

1. a protected GitHub environment named `work-stack-public-release` with the desired reviewer and
   default-branch restrictions;
2. an environment secret named `WORKSTACK_PUBLIC_RELEASE_TOKEN` whose fine-grained or GitHub App
   authority is limited to releases in that public repository;
3. default-branch protection requiring the normal Work Stack quality workflow.

The ordinary private-repository `GITHUB_TOKEN` is not used as cross-repository publish authority.
The repository cannot prove those external settings from source bytes; verify them in GitHub before
the first `publish=true` dispatch.

## Evidence and nonclaims

The workflow summary records candidate commit/tree, release base, version, selected gate results,
numeric artifact ID, service digest, installer SHA-256, target, and exceptions. Detailed structural,
coverage, browser, and failure reports remain CI evidence.

Passing this workflow proves source/artifact identity and the executed local smoke matrix. It does
not prove code signing, universal endpoint compatibility, Microsoft tenant policy acceptance,
external exactly-once delivery, or that the protected environment/credential is configured until a
real guarded publication succeeds.
