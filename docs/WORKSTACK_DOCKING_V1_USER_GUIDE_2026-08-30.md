# Work Stack docking v1 — reviewed product guide

## What this milestone provides

Work Stack can now export one committed planning Task revision as an immutable,
contract-valid file. The flow is intentionally user-mediated. It does not create a
Conduit Task, reserve one, link the products, send a network request to Conduit, or
change Work Stack planning state.

## Click-by-click product demo

1. Open Work Stack and select **Workspace**.
2. In Graph, Board, or Treemap, open any Task.
3. In the Task's **Overview** tab, find **Execution handoff** and select
   **Export to Conduit**.
4. Wait for the disclosure dialog to validate the committed Task.
5. Read **Exact title** and **Exact detail**. These are the literal values that will be
   written to the file, not the unsaved contents of an editor.
6. Confirm the boundary notice: this is a snapshot, Conduit receives a copy, import
   does not update Work Stack, and execution still requires confirmation in Conduit.
7. Confirm the omission notice: objectives, dependencies, subtasks, notes, and tags do
   not appear in snapshot v1.
8. Inspect the displayed revision and digest. The filename contains only the stable
   planning Task UUID.
9. Select **I reviewed the exact title and detail and understand the omissions**.
10. Select **Save snapshot file** and choose the browser's download location.
11. Observe the success notice: the download started and Work Stack remains unchanged.
12. Reopen the Task and confirm that its revision and planning status did not advance.

To demonstrate cancellation, repeat steps 1–8 and select **Cancel** without selecting
the checkbox. No file delivery POST occurs and no Work Stack store is mutated.

## Expected file

- Name: `<planning-task-uid>.workstack-task.json`
- Encoding: UTF-8 without BOM
- Line endings: canonical JSON followed by exactly one LF
- Authority: Work Stack remains the sole planning-state authority
- Transport: explicit user-carried file only

## CLI verification path

Use the same data directory as the product, with the server stopped:

```powershell
python run_work_stack.py --data-dir <data-directory> snapshot preview T-0001
python run_work_stack.py --data-dir <data-directory> snapshot export T-0001 `
  --out <planning-task-uid>.workstack-task.json `
  --expected-revision <reviewed-revision> `
  --expected-digest <reviewed-sha256-label> `
  --confirm-disclosure
```

The CLI refuses an existing destination and removes its temporary file if publication
fails. A pending recovery journal, incomplete migration, corrupt store, stale revision,
changed digest, invalid Unicode/content, or safety-policy match fails closed.

## Explicit nonclaims

- No Conduit client, loopback transport to Conduit, watcher, relay, back-sync, or bulk
  import exists in Work Stack.
- Export does not prove that Conduit imported or executed anything.
- The narrow frozen safety policy is a high-confidence tripwire, not comprehensive
  secret detection.
- Work Stack does not retain an export ledger or reconstruct historical revisions after
  the source Task changes; the emitted file and its digest are the immutable artifact.
