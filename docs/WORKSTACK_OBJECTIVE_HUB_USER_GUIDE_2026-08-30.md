# Objective/KR Hub — user guide

## Open and inspect an Objective

1. Open **Objective Hub** from the sidebar or press `7` while focus is not inside an input.
2. Choose an Objective from the left index. The URL keeps the selected Objective ID.
3. Read its average Key Result progress, linked Tasks, and recorded change count.
4. Choose a linked Task to open the same authoritative Task Drawer used by Workspace.

## Add a Key Result

1. Under **Key Results**, enter a measurable outcome in **New Key Result**.
2. Optionally enter a short target label such as `5 days` or `95%`.
3. Choose **Add Key Result**.
4. If the response may have been lost, leave both fields unchanged and choose
   **Retry unchanged KR**. Work Stack reuses the first intent identity and does not add
   another Key Result when the first request already committed.

## Update progress or status

1. Move the Key Result progress slider to the explicit measured value.
2. Choose `active`, `done`, or `dropped` independently from the status field.
3. Choose **Save KR**.
4. Change the Objective-level status from its own status selector when the whole goal is
   done or intentionally dropped.

Each confirmed change advances the Objective revision. If another edit wins first, Work
Stack rejects the stale write, refreshes the authoritative Objective, and asks you to make
the decision again. Objective and KR changes remain Work Stack planning facts; they do not
start Conduit, create a Taskroom, or infer agent execution.
