# Quality gate runtime operations

The reusable `quality` job in `.github/workflows/quality-reusable.yml` uses
`timeout-minutes: 60`. The increased ceiling responds to the observed 30-minute
timeouts below. A complete CI run must still confirm that the new budget is sufficient.
Test selection, coverage floors, `continue-on-error`, and failure policy are unchanged.

## Why 60 minutes

GitHub records a job timeout as `conclusion: cancelled`. Cancellation alone is
not evidence: a newer push, a user abort, a hung step, and the job ceiling all
look like `cancelled` until the check-run annotation and the last live log lines
are read.

There is no `concurrency:` block under `.github/workflows`. Two independent
quality jobs on 2026-09-06 ran in parallel for the full 30-minute budget, so a
newer-push concurrency cancel is not the mechanism.

### Run 34082355823 / job 101619927738

- Head `e8d23e32` on `wave3/integration`.
- Job `quality / quality` started `2026-09-07T04:14:34Z`, completed
  `2026-09-07T04:44:48Z` (30m14s).
- Check-run annotation: `The job has exceeded the maximum execution time of 30m0s`.
- Log tail: unittest still printing `ok` at `04:44:33Z`
  (`test_invalid_session_transition_is_fail_closed`); launcher then raised
  `KeyboardInterrupt` and Actions emitted `##[error]The operation was canceled.`
  at `04:44:36Z`.
- Next push on the same branch was run `34085198661` at `05:00:51Z`, 16 minutes
  later. Not a concurrency cancel.

### Run 34038594488 / job 101501109467

- Head `80c86281` on `codex/o1-1.0.7-candidate-20260904`.
- Job started `2026-09-06T14:15:32Z`, completed `2026-09-06T14:45:44Z` (30m12s).
- Same annotation: `The job has exceeded the maximum execution time of 30m0s`.
- Log tail: unittest still printing `ok` at `14:45:33Z`
  (`test_wave_three_runtime_contracts_are_activation_prerequisites`); then
  `KeyboardInterrupt` and `##[error]The operation was canceled.`
- Sibling run `34038433004` on the same branch also ran ~30 minutes in parallel.

Supporting, not used as the primary pair: run `34085198661` / job `101627780072`
finished `Run backend tests with branch coverage` successfully in 24m54s
(`2026-09-07T05:04:54Z`–`05:29:48Z`) and was then cancelled during
`Run frontend tests` by the same 30m0s annotation. Last successful quality run
`33589093578` / job `100119232803` completed in 26m11s on 2026-09-02, when
backend coverage itself took only 10m10s.

Healthy tests now spend ~25 minutes on backend coverage alone and still have
frontend coverage, floors, the production build, and the three browser gates
ahead. The last fully green duration plus that growth is already above 30
minutes with no hung step.

## Digest follow-up

`.github/workflows/quality-reusable.yml` is listed in
`quality/quality-config.json` `config_inputs`. Changing the timeout invalidates
`quality/structural-baseline.json` `config_digest`. Reconcile that digest only;
do not edit other baseline fields.
