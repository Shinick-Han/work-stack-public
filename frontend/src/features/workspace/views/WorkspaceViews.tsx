import { Suspense } from "react";

import type { WorkspaceViewsProps } from "./types";
import {
  BoardSurface,
  GraphSurface,
  TableSurface,
  TreemapSurface,
  WorkspaceEmptyNotice,
  emptyCopy,
  useCompletedTaskProjection,
} from "./workspaceViewsPresentation";
import "./workspace-views.css";

/**
 * Read-only Graph/Treemap and status-mutating Board/Table coordinator.
 *
 * There is exactly one authoritative task set. The Workspace owner computes the
 * completed-visibility projection and passes it down, so the summary and every
 * renderer agree within the same render. The fallback inside the projection hook
 * exists only for isolated callers that mount this component directly; it uses
 * the same public projection rather than a second filter.
 */
export function WorkspaceViews(props: WorkspaceViewsProps) {
  const { view, className = "" } = props;
  const resolved = useCompletedTaskProjection(props);

  // Graph owns its own overlay so an emptied projection does not unmount its
  // canvas; the other renderers share this one presentation.
  const emptyMessage = view === "graph" ? null : emptyCopy(resolved.emptyKind);
  const surface = { ...props, resolved };

  return (
    <section
      className={`wsv-root ${className}`.trim()}
      data-workspace-view={view}
      data-empty-kind={resolved.emptyKind}
      role="tabpanel"
      aria-label={`${view} workspace view`}
    >
      {emptyMessage ? <WorkspaceEmptyNotice message={emptyMessage} /> : null}
      <Suspense fallback={<div className="wsv-loading" role="status">Loading visualization…</div>}>
        {view === "graph" ? <GraphSurface {...surface} /> : null}
        {view === "treemap" && !emptyMessage ? <TreemapSurface {...surface} /> : null}
        {view === "table" && !emptyMessage ? <TableSurface {...surface} /> : null}
      </Suspense>
      {view === "board" && !emptyMessage ? <BoardSurface {...surface} /> : null}
    </section>
  );
}
