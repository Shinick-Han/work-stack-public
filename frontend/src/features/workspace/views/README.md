# Workspace views integration

`WorkspaceViews` is the only component the workspace shell needs to import:

```tsx
<WorkspaceViews
  view={view} // "graph" | "treemap" | "board"
  tasks={workspace.tasks}
  objectives={workspace.objectives}
  notes={workspace.notes}
  edges={workspace.edges}
  search={search}
  status={status} // "all" or open/started/done/dropped
  priority={priority} // "all" or P0/P1/P2/P3
  objectiveId={objectiveId} // "all" or an objective ID
  selectedTaskId={selectedTaskId}
  onSelectTask={selectTask}
  onChangeTaskStatus={changeTaskStatus}
/>
```

Graph and Treemap are read-only projections; selection is their only callback. Board
optimistically moves a card when its explicit status selector or drag affordance is
used. `onChangeTaskStatus` must return a promise. If that promise rejects, Board drops
its local override so the authoritative refetched status is shown, and displays a
dismissible error.

The graph reconstructs alignment, dependency, parent, and note-reference relationships
from DTO fields, then de-duplicates any supplied `edges`. Legacy edge kinds `objective`
and `note` are normalized to `alignment` and `reference`, respectively.
