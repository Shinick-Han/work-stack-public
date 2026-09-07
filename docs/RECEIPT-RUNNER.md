# Receipt runner (Wave 1 G1)

Repository-owned fail-closed receipt runner. Plan: §2.1 D3a, Wave 1 G1, §8.5.

Release-gate *invocation wiring* is not in this document. This module records and verifies receipts; it does not classify product releases.

## Inputs

- Spec JSON supplies `base_sha`, `worktree`, `cwd`, `output_dir`, `commands`, and the remaining required fields. None of those values are hard-coded to a developer machine.
- `commands` is a non-empty list of `{"argv": ["prog", ...]}` objects. Opaque `shell` strings are rejected.
- Commands run sequentially with `subprocess.run(..., shell=False)`.

## Fail-closed rules

- Missing required fields, empty `commands` / `command_exit_codes`, or length mismatch → non-zero.
- Aggregate `exit_code` is the first non-zero child status, else 0.
- `expected_red=true` accepts a verified non-zero receipt and never rewrites the exit code to 0.
- Header `exit_code=0` with body `classify_exit=<nonzero>` is the D3a swallow class and is rejected.
- Receipt paths must stay inside the explicit `output_dir`. `step_id` is a single path segment.
- Raw stdout/stderr hashes and byte counts must match the files on disk.

## CLI

```text
python -B scripts/receipt_runner.py run --spec <spec.json>
python -B scripts/receipt_runner.py verify <receipt.md> [<receipt.md> ...]
```
