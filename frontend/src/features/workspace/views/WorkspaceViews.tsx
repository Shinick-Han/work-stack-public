import { lazy, Suspense, useMemo } from "react";

import { BoardView } from "./BoardView";
import type { WorkspaceViewsProps } from "./types";
import { filterWorkspaceTasks } from "./viewModels";
import "./workspace-views.css";

const GraphView = lazy(() => import("./GraphView").then((module) => ({ default: module.GraphView })));
const TreemapView = lazy(() => import("./TreemapView").then((module) => ({ default: module.TreemapView })));
const TableView = lazy(() => import("./TableView").then((module) => ({ default: module.TableView })));

/**
 * Read-only Graph/Treemap and status-mutating Board/Table coordinator.
 * Filtering is centralized here so all four views show the same task set.
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
  className = "",
}: WorkspaceViewsProps) {
  const filteredTasks = useMemo(
    () =>
      filterWorkspaceTasks(tasks, {
        search,
        status,
        priority,
        objectiveId,
        readiness,
        timing,
        today,
      }),
    [tasks, search, status, priority, objectiveId, readiness, timing, today],
  );

  return (
    <section
      className={`wsv-root ${className}`.trim()}
      data-workspace-view={view}
      role="tabpanel"
      aria-label={`${view} workspace view`}
    >
      <Suspense fallback={<div className="wsv-loading" role="status">Loading visualization…</div>}>
        {view === "graph" ? (
          <GraphView
            tasks={filteredTasks}
            objectives={objectives}
            notes={notes}
            edges={edges}
            selectedTaskId={selectedTaskId}
            selectedObjectiveId={selectedObjectiveId}
            onSelectTask={onSelectTask}
            onSelectObjective={onSelectObjective}
          />
        ) : null}
        {view === "treemap" ? (
          <TreemapView
            tasks={filteredTasks}
            objectives={objectives}
            selectedTaskId={selectedTaskId}
            onSelectTask={onSelectTask}
            onSelectObjective={onSelectObjective}
          />
        ) : null}
        {view === "table" ? (
          <TableView
            tasks={filteredTasks}
            referenceTasks={tasks}
            selectedTaskId={selectedTaskId}
            onSelectTask={onSelectTask}
            onSelectObjective={onSelectObjective}
            onChangeTaskStatus={onChangeTaskStatus}
            today={today}
          />
        ) : null}
      </Suspense>
      {view === "board" ? (
        <BoardView
          tasks={filteredTasks}
          referenceTasks={tasks}
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
