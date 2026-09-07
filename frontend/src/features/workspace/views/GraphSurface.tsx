import { GraphContextPopover } from "./GraphContextPopover";
import { GraphNotePopover } from "./GraphNotePopover";
import {
  GraphEmptyOverlay,
  GraphFlowCanvas,
  GraphLegend,
} from "./graphPresentation";
import type { GraphViewProps, WorkspaceFlowEdge, WorkspaceFlowNode } from "./graphViewTypes";
import type { WorkspaceNote, WorkspaceTask } from "./types";
import type { ReactFlowInstance } from "@xyflow/react";

export type GraphSurfaceProps = {
  empty: boolean;
  emptyKind: string;
  announcement: string;
  modelNodeCount: number;
  nodes: WorkspaceFlowNode[];
  edges: WorkspaceFlowEdge[];
  contextTargetId: string | null;
  contextTask: WorkspaceTask | undefined;
  contextTrigger: HTMLButtonElement | null;
  selectedNote: WorkspaceNote | null;
  pinGeneration: number;
  pinGenerationRef: { current: number };
  aliveRef: { current: boolean };
  renderPopupPrerequisites?: GraphViewProps["renderPopupPrerequisites"];
  onSelectTask: (taskId: string) => void;
  onFocusPinChange?: (taskId: string | null) => void;
  setContextTargetId: (taskId: string | null) => void;
  setContextTrigger: (trigger: HTMLButtonElement | null) => void;
  setSelectedNoteId: (taskId: string | null | ((current: string | null) => string | null)) => void;
  onInit: (instance: ReactFlowInstance<WorkspaceFlowNode, WorkspaceFlowEdge>) => void;
  onNodeDragStart: (_event: unknown, node: WorkspaceFlowNode) => void;
  onNodeDrag: (_event: unknown, node: WorkspaceFlowNode) => void;
  onNodeDragStop: (_event: unknown, node: WorkspaceFlowNode) => void;
  onMoveEnd: (_event: unknown, viewport: { x: number; y: number; zoom: number }) => void;
  onResetLayout: () => void;
};

function releaseFocusPinFor(
  generation: number,
  pinGenerationRef: { current: number },
  onFocusPinChange?: (taskId: string | null) => void,
) {
  return () => {
    if (pinGenerationRef.current !== generation) return;
    onFocusPinChange?.(null);
  };
}

function GraphPopovers(props: GraphSurfaceProps) {
  const closeContext = () => {
    props.setContextTargetId(null);
    props.setContextTrigger(null);
  };
  return (
    <>
      {props.contextTargetId && props.contextTask ? (
        <GraphContextPopover
          key={props.contextTargetId}
          taskId={props.contextTargetId}
          taskTitle={props.contextTask.title}
          trigger={props.contextTrigger}
          focusFallbackSelector="[data-workspace-focus-fallback]"
          ownerAliveRef={props.aliveRef}
          prerequisites={props.renderPopupPrerequisites?.(props.contextTargetId)}
          onClose={closeContext}
          onFocusReturned={releaseFocusPinFor(
            props.pinGeneration,
            props.pinGenerationRef,
            props.onFocusPinChange,
          )}
          onOpenTask={() => {
            const openedId = props.contextTargetId;
            closeContext();
            props.onFocusPinChange?.(null);
            if (openedId) props.onSelectTask(openedId);
          }}
        />
      ) : null}
      {props.selectedNote ? (
        <GraphNotePopover
          note={props.selectedNote}
          onClose={() => props.setSelectedNoteId(null)}
        />
      ) : null}
    </>
  );
}

export function GraphSurface(props: GraphSurfaceProps) {
  return (
    <div className="wsv-graph" aria-label="Task relationship graph" data-graph-empty={props.empty ? "true" : "false"}>
      <GraphPopovers {...props} />
      {props.empty ? <GraphEmptyOverlay emptyKind={props.emptyKind} /> : null}
      <div className="wsv-graph-live" aria-live="polite" aria-atomic="true">{props.announcement}</div>
      <GraphFlowCanvas
        modelNodeCount={props.modelNodeCount}
        nodes={props.nodes}
        edges={props.edges}
        onInit={props.onInit}
        onNodeDragStart={props.onNodeDragStart}
        onNodeDrag={props.onNodeDrag}
        onNodeDragStop={props.onNodeDragStop}
        onMoveEnd={props.onMoveEnd}
      />
      <button type="button" className="wsv-graph-reset" onClick={props.onResetLayout}>
        Reset graph layout
      </button>
      <GraphLegend />
    </div>
  );
}
