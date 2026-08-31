# Work Stack local continuity user guide

Date: 2026-08-30

## Verified full-workspace backup

1. Open **More workspace actions** in the top bar.
2. In **Local continuity**, confirm that the store says **Ready** and check the workspace ID.
3. Read the full-data warning and select **I understand this file contains the full local workspace**.
4. Click **Download verified backup** and keep the ZIP private.

The ZIP contains the complete local Work Stack store, a strict member allowlist, per-file
SHA-256 values, the workspace identity, and the store schema version. The running server builds
it while holding its own store lease; this download does not add a planning fact or change a
Task revision. Restore is deliberately not available in the live browser. Close Work Stack and
use the verified offline maintenance restore flow so a corrupt or mismatched archive is rejected
before any destination write.

## Saved filters

1. Choose a Workspace view and set Search, Status, Priority, and Objective filters.
2. Click **Save view**. Work Stack generates a concise local name from the active filters.
3. Choose the saved entry from **Saved filters** to restore the exact view and filters.
4. Click **Remove saved view** while it is active to delete only that local preset.

Up to 12 presets are kept in this browser profile. They contain only filter coordinates;
they do not contain Task bodies, Capture payloads, reply material, or docking bytes.

## Quick Add draft recovery

- Quick Add saves title, definition of done, priority, due date, Objective selection, and
  tags in this local browser profile.
- Closing or refreshing the page preserves the draft.
- Successful Task creation or **Clear draft** removes it.
- Capture packets, Outlook/Teams reply text and targets, credentials, and Conduit data are
  never part of this draft store.
- Malformed or unknown draft fields cause the whole stored draft to be discarded.

## Cross-tab refresh

When one Work Stack tab completes a planning mutation, other tabs receive a content-free
refresh hint and re-read active projections. No planning content is sent through the tab
channel. Server-side revisions remain authoritative, so a stale edit is still refused even
if the hint is delayed or unavailable.

## Status Undo

After a Board or Table status change, click **Undo** in the confirmation message. Undo is
available only for the latest notice and submits a new revision-guarded status transition.
It does not delete or rewrite the earlier planning-status fact. If another writer has already
advanced the Task revision, Undo fails closed and Work Stack refreshes the authoritative Task.
