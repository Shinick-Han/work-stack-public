import { lazy, Suspense, useMemo } from "react";

import { BoardView } from "./BoardView";
import type { WorkspaceViewsProps } from "./types";
import { projectCompletedTaskVisibility } from "./completedTaskVisibility";
import "./workspace-views.css";

const GraphView = lazy(() => import("./GraphView").then((module) => ({ default: module.GraphView })));
const TreemapView = lazy(() => import("./TreemapView").then((module) => ({ default: module.TreemapView })));
const TableView = lazy(() => import("./TableView").then((module) => ({ default: module.TableView })));

/**
 * Read-only Graph/Treemap and status-mutating Board/Table coordinator.
 *
 * There is exactly one authoritative task set. The Workspace owner computes the
 * completed-visibility projection and passes it down, so the summary and every
 * renderer agree within the same render. The fallback below exists only for
 * isolated callers that mount this component directly; it uses the same public
 * projection rather than a second filter.
 */
export function WorkspaceViews({
  view,
  tasks,
  objectives,
  notes,
  edges,
  search = "",
  status = "all",
  priority = "all",
  objectiveId = "all",
  readiness = "all",
  timing = "all",
  today,
  selectedTaskId,
  selectedObjectiveId,
  onSelectTask,
  onSelectObjective,
  onChangeTaskStatus,
  projection,
  outcome,
  keyResultProjection,
  onSelectOutcome,
  contextTargetTaskId = null,
  focusPinnedTaskId = null,
  onContextTargetChange,
  onFocusPinChange,
  renderPopupPrerequisites,
  className = "",
}: WorkspaceViewsProps) {
  const resolved = useMemo(
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

  const visibleTasks = resolved.visibleTasks;
  const referenceTasks = resolved.referenceTasks;

  // Graph owns its own overlay so an emptied projection does not unmount its
  // canvas; the other renderers share this one presentation.
  const emptyMessage = view === "graph" ? null : emptyCopy(resolved.emptyKind);

  return (
    <section
      className={`wsv-root ${className}`.trim()}
      data-workspace-view={view}
      data-empty-kind={resolved.emptyKind}
      role="tabpanel"
      aria-label={`${view} workspace view`}
    >
      {emptyMessage ? (
        <div className="wsv-empty" role="status">
          <strong>{emptyMessage.title}</strong>
          <span>{emptyMessage.detail}</span>
        </div>
      ) : null}
      <Suspense fallback={<div className="wsv-loading" role="status">Loading visualization…</div>}>
        {view === "graph" ? (
          <GraphView
            outcome={outcome ?? null}
            tasks={visibleTasks}
            referenceTasks={referenceTasks}
            objectives={objectives}
            notes={notes}
            edges={edges}
            emptyKind={resolved.emptyKind}
            selectedTaskId={selectedTaskId}
            selectedObjectiveId={selectedObjectiveId}
            contextTargetTaskId={contextTargetTaskId}
            focusPinnedTaskId={focusPinnedTaskId}
            onContextTargetChange={onContextTargetChange}
            onFocusPinChange={onFocusPinChange}
            renderPopupPrerequisites={renderPopupPrerequisites}
            keyResultProjection={keyResultProjection}
            onSelectOutcome={onSelectOutcome}
            onSelectTask={onSelectTask}
            onSelectObjective={onSelectObjective}
          />
        ) : null}
        {view === "treemap" && !emptyMessage ? (
          <TreemapView
            keyResultProjection={keyResultProjection}
            tasks={visibleTasks}
            objectives={objectives}
            selectedTaskId={selectedTaskId}
            onSelectTask={onSelectTask}
            onSelectObjective={onSelectObjective}
          />
        ) : null}
        {view === "table" && !emptyMessage ? (
          <TableView
            keyResultProjection={keyResultProjection}
            onSelectOutcome={onSelectOutcome}
            tasks={visibleTasks}
            referenceTasks={referenceTasks}
            selectedTaskId={selectedTaskId}
            onSelectTask={onSelectTask}
            onSelectObjective={onSelectObjective}
            onChangeTaskStatus={onChangeTaskStatus}
            today={today}
          />
        ) : null}
      </Suspense>
      {view === "board" && !emptyMessage ? (
        <BoardView
          keyResultProjection={keyResultProjection}
          onSelectOutcome={onSelectOutcome}
          tasks={visibleTasks}
          referenceTasks={referenceTasks}
          selectedTaskId={selectedTaskId}
          onSelectTask={onSelectTask}
          onSelectObjective={onSelectObjective}
          onChangeTaskStatus={onChangeTaskStatus}
          today={today}
        />
      ) : null}
    </section>
  );
}

/**
 * "All matching tasks are completed" deliberately never claims the whole
 * workspace is complete: other filters may still be hiding open work.
 */
function emptyCopy(emptyKind: string) {
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
