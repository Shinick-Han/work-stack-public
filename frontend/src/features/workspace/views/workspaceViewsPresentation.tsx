import { lazy, useMemo } from "react";

import { BoardView } from "./BoardView";
import { projectCompletedTaskVisibility } from "./completedTaskVisibility";
import type {
  CompletedVisibilityProjection,
  EmptyKind,
  WorkspaceViewsProps,
} from "./types";

/**
 * Presentation seam for the WorkspaceViews coordinator.
 *
 * Nothing here decides anything: the projection hook is the coordinator's own
 * memo moved verbatim, and each surface is the exact element the coordinator
 * used to inline. Keeping the lazy handles beside the surfaces that render them
 * means the code-split boundary is still one module deep, and the coordinator
 * is left with only the view switch and the shared chrome.
 */

const GraphView = lazy(() => import("./GraphView").then((module) => ({ default: module.GraphView })));
const TreemapView = lazy(() => import("./TreemapView").then((module) => ({ default: module.TreemapView })));
const TableView = lazy(() => import("./TableView").then((module) => ({ default: module.TableView })));

/** Every surface sees the coordinator's own props plus the one resolved projection. */
export type WorkspaceSurfaceProps = WorkspaceViewsProps & {
  resolved: CompletedVisibilityProjection;
};

/** The empty-state copy pair, or `null` when there is nothing to say. */
export interface WorkspaceEmptyMessage {
  title: string;
  detail: string;
}

/**
 * The single completed-visibility projection for this render.
 *
 * The Workspace owner normally supplies it; the fallback exists only for
 * isolated callers that mount the coordinator directly, and it uses the same
 * public projection rather than a second filter.
 */
export function useCompletedTaskProjection({
  projection,
  tasks,
  search = "",
  status = "all",
  priority = "all",
  objectiveId = "all",
  readiness = "all",
  timing = "all",
  outcome,
  today,
  view,
  selectedTaskId,
  contextTargetTaskId = null,
  focusPinnedTaskId = null,
}: WorkspaceViewsProps): CompletedVisibilityProjection {
  return useMemo(
    () =>
      projection
        ?? projectCompletedTaskVisibility({
          tasks,
          filters: { search, status, priority, objectiveId, readiness, timing, outcome, today },
          view,
          selectedTaskId,
          contextTargetTaskId,
          focusPinnedTaskId,
        }),
    [
      projection,
      outcome,
      tasks,
      search,
      status,
      priority,
      objectiveId,
      readiness,
      timing,
      today,
      view,
      selectedTaskId,
      contextTargetTaskId,
      focusPinnedTaskId,
    ],
  );
}

/**
 * "All matching tasks are completed" deliberately never claims the whole
 * workspace is complete: other filters may still be hiding open work.
 */
export function emptyCopy(emptyKind: EmptyKind): WorkspaceEmptyMessage | null {
  if (emptyKind === "none") return null;
  if (emptyKind === "no-tasks") {
    return {
      title: "No tasks yet",
      detail: "Create a Task to start planning this workspace.",
    };
  }
  if (emptyKind === "all-complete") {
    return {
      title: "All matching tasks are completed",
      detail: "Show completed tasks to bring them back into view.",
    };
  }
  return {
    title: "No work matches these filters",
    detail: "Clear a filter to bring tasks and their relationships back.",
  };
}

export function WorkspaceEmptyNotice({ message }: { message: WorkspaceEmptyMessage }) {
  return (
    <div className="wsv-empty" role="status">
      <strong>{message.title}</strong>
      <span>{message.detail}</span>
    </div>
  );
}

export function GraphSurface({ resolved, ...props }: WorkspaceSurfaceProps) {
  return (
    <GraphView
      workspaceId={props.workspaceId}
      outcome={props.outcome ?? null}
      tasks={resolved.visibleTasks}
      referenceTasks={resolved.referenceTasks}
      objectives={props.objectives}
      notes={props.notes}
      edges={props.edges}
      emptyKind={resolved.emptyKind}
      selectedTaskId={props.selectedTaskId}
      selectedObjectiveId={props.selectedObjectiveId}
      contextTargetTaskId={props.contextTargetTaskId ?? null}
      focusPinnedTaskId={props.focusPinnedTaskId ?? null}
      onContextTargetChange={props.onContextTargetChange}
      onFocusPinChange={props.onFocusPinChange}
      renderPopupPrerequisites={props.renderPopupPrerequisites}
      keyResultProjection={props.keyResultProjection}
      onSelectOutcome={props.onSelectOutcome}
      onSelectTask={props.onSelectTask}
      onSelectObjective={props.onSelectObjective}
    />
  );
}

export function TreemapSurface({ resolved, ...props }: WorkspaceSurfaceProps) {
  return (
    <TreemapView
      workspaceId={props.workspaceId}
      keyResultProjection={props.keyResultProjection}
      tasks={resolved.visibleTasks}
      referenceTasks={resolved.referenceTasks}
      objectives={props.objectives}
      selectedTaskId={props.selectedTaskId}
      onSelectTask={props.onSelectTask}
      onSelectObjective={props.onSelectObjective}
    />
  );
}

export function TableSurface({ resolved, ...props }: WorkspaceSurfaceProps) {
  return (
    <TableView
      workspaceId={props.workspaceId}
      keyResultProjection={props.keyResultProjection}
      onSelectOutcome={props.onSelectOutcome}
      tasks={resolved.visibleTasks}
      referenceTasks={resolved.referenceTasks}
      selectedTaskId={props.selectedTaskId}
      onSelectTask={props.onSelectTask}
      onSelectObjective={props.onSelectObjective}
      onChangeTaskStatus={props.onChangeTaskStatus}
      today={props.today}
    />
  );
}

export function BoardSurface({ resolved, ...props }: WorkspaceSurfaceProps) {
  return (
    <BoardView
      keyResultProjection={props.keyResultProjection}
      onSelectOutcome={props.onSelectOutcome}
      tasks={resolved.visibleTasks}
      referenceTasks={resolved.referenceTasks}
      selectedTaskId={props.selectedTaskId}
      onSelectTask={props.onSelectTask}
      onSelectObjective={props.onSelectObjective}
      onChangeTaskStatus={props.onChangeTaskStatus}
      today={props.today}
    />
  );
}
