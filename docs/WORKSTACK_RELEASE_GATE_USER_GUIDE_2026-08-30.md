# Work Stack release gate — operator guide

Use this gate before treating a local commit as a reviewable product checkpoint.

## Run the complete local gate

1. Open a terminal in the Work Stack repository.
2. Install the locked frontend dependencies with `npm --prefix frontend ci`.
3. Install the Chromium test runtime once with
   `npm --prefix frontend exec -- playwright install chromium`.
4. Run backend tests with `python -m unittest discover -s tests -v`.
5. Run frontend tests with `npm --prefix frontend test`.
6. Build the production UI with `npm --prefix frontend run build`.
7. Run privacy checks with `python scripts/audit_export.py .`.
8. Run real-browser and accessibility checks with
   `npm --prefix frontend run test:e2e:run`.

The browser gate creates a temporary synthetic workspace and removes it when the server
stops. It does not use the normal Work Stack data directory.

## Read a failure

- A backend or frontend test failure blocks the checkpoint.
- A build failure means the browser artifact is not releasable.
- A privacy-audit failure identifies a file that must not enter the product export.
- A Playwright failure keeps a screenshot, video, and trace under ignored local test
  output. In GitHub Actions those files are uploaded only for a failed run.
- An axe failure lists the exact accessibility rule and affected elements. Fix the UI;
  do not exclude a rule merely to make the gate green.

## GitHub behavior

After an authorized push, GitHub runs the same gate on `windows-latest` for pushes,
pull requests, and manual dispatch. A green remote run is separate evidence from a green
local run. This guide does not authorize a push by itself.
