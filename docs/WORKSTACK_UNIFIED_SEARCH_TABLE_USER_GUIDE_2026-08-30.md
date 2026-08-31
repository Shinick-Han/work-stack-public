# Work Stack unified search and Table View user guide

Date: 2026-08-30

## Unified search

1. Open Work Stack and press `Ctrl+K` or click **Search or jump**.
2. Enter at least two characters from a Task, Objective, graph note, sanitized Capture,
   or recorded activity.
3. Use the arrow keys and Enter, or click a result.
4. Task results open the shared Task Drawer. Objective results open Objective Hub.
   Other records open the nearest owning planning surface.

Search results deliberately expose only a small projection: record kind, stable ID,
title, short subtitle, and owning target. Reply bodies, fixed reply targets, raw external
locators, credentials, and recipient data are neither searched nor returned.

## Table View

1. Open Workspace and choose **Table**, or press `8` outside an editable field.
2. Click an ID, Task, Status, Priority, or Due header to sort. Click the same header again
   to reverse the order.
3. Click a Task row to open its shared Task Drawer. Click the same Task row again to
   return to the unselected full-workspace view.
4. Change a status from the row selector when a quick planning update is appropriate.
   The same revision-guarded status mutation used by Board is reused here.
5. Workspace search and Status/Priority/Objective filters apply to Table in the same way
   as Graph, Board, and Treemap.

Table is a projection of the Work Stack planning SSOT. It does not infer or update
Conduit execution state.
