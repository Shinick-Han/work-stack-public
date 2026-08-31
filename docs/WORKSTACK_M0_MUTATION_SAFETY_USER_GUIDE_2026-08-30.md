# Retry-safe planning actions — user guide

This guide applies to the production build at product commit
`a37cbf2806080f65878de51caab937a20cccb0be`.

## Add an Objective or Graph note

1. Open Work Stack and choose the three-dot **More workspace actions** button in the
   top bar.
2. For an Objective, enter the outcome and quarter, then choose **Add objective**.
3. For a Graph note, enter the context, optionally check Tasks or Objectives to connect,
   then choose **Add graph note**.
4. Wait for the success message. The dialog closes and the Workspace refreshes in the
   background.
5. If Work Stack says the result may have committed, do not change the fields. Choose
   the same Add button again. The unchanged retry verifies the first intent without
   creating a duplicate.
6. Editing any field or link deliberately starts a new intent.

## Add a Task note or subtask

1. Open a Task.
2. Choose the three-dot **More task actions** button beside the Task ID.
3. Enter a concrete subtask and priority, then choose **Add subtask**; or enter context
   under Task notes and choose **Add note**.
4. If the response is lost, keep the text and priority unchanged and use the same Add
   button again. Work Stack reuses the intent identity and returns the already committed
   result when one exists.
5. Changing the draft or priority creates a new intent intentionally.

## Change a subtask status

1. Open **More task actions**.
2. Choose Open, In progress, Done, or Dropped beside the subtask.
3. If the network response disappears after the save, Work Stack reads the Task again.
   A message ending in **verified after reconnect** means the requested status was found
   in the authoritative planning record.
4. When that verification does not succeed, the dialog stays open and shows the error;
   inspect the current Task before retrying.

These actions change Work Stack planning state only. They do not start Conduit agents,
create a Taskroom, send Outlook mail, or post a Teams message.
