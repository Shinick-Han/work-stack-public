import { useGraphInteractiveNodes } from "./useGraphInteractiveNodes";
import { useGraphLayout } from "./useGraphLayout";
import { useGraphLocalSession } from "./useGraphLocalSession";
import { useGraphSelection } from "./useGraphSelection";
import { useGraphViewport } from "./useGraphViewport";
import type { GraphSurfaceProps } from "./GraphSurface";
import type { GraphViewProps } from "./graphViewTypes";

export function useGraphCanvas(props: GraphViewProps): GraphSurfaceProps {
  const persistWorkspaceId = props.workspaceId ?? "";
  const canonicalTasks = props.referenceTasks ?? props.tasks;
  const local = useGraphLocalSession(persistWorkspaceId);
  const selection = useGraphSelection({
    notes: props.notes,
    canonicalTasks,
    contextTargetTaskId: props.contextTargetTaskId ?? null,
    onContextTargetChange: props.onContextTargetChange,
  });
  const layout = useGraphLayout({
    tasks: props.tasks,
    objectives: props.objectives,
    notes: props.notes,
    edges: props.edges,
    selectedTaskId: props.selectedTaskId,
    selectedObjectiveId: props.selectedObjectiveId,
    keyResultProjection: props.keyResultProjection ?? null,
    outcome: props.outcome ?? null,
    selectedNoteId: selection.selectedNoteId,
    graphKeyResult: selection.graphKeyResult,
    canonicalTasks,
    graphLocal: local.graphLocal,
    gesture: local.gesture,
    persistWorkspaceId,
    catalogRef: local.catalogRef,
  });
  const viewport = useGraphViewport({
    layoutTopologyKey: layout.layout.topologyKey,
    requestedTopologyKey: layout.topologyKey,
    graphLocalViewport: local.graphLocal.viewport,
    modelNodeCount: layout.model.nodes.length,
    store: local.store,
    graphLocalRef: local.graphLocalRef,
    modelRef: layout.modelRef,
    positionedNodes: layout.positionedNodes,
    routedEdges: layout.routedEdges,
    selectedObjectiveId: props.selectedObjectiveId,
    graphLocalPositions: local.graphLocal.positions,
    writeLocal: local.writeLocal,
    skipExternalRef: local.skipExternalRef,
    setGraphLocal: local.setGraphLocal,
    setAnnouncement: local.setAnnouncement,
    cancelGesture: local.cancelGesture,
  });
  const nodes = useGraphInteractiveNodes(layout.positionedNodes, {
    persistWorkspaceId,
    gesture: local.gesture,
    onMoveKeyDown: local.onMoveKeyDown,
    onSelectTask: props.onSelectTask,
    onSelectObjective: props.onSelectObjective,
    onFocusPinChange: props.onFocusPinChange,
    clearGraphLocalSelection: selection.clearGraphLocalSelection,
    setGraphKeyResult: selection.setGraphKeyResult,
    setSelectedNoteId: selection.setSelectedNoteId,
    setContextTrigger: selection.setContextTrigger,
    setContextTargetId: selection.setContextTargetId,
    pinGenerationRef: selection.pinGenerationRef,
  });
  return {
    empty: !layout.model.nodes.length,
    emptyKind: props.emptyKind ?? "none",
    announcement: local.announcement,
    modelNodeCount: layout.model.nodes.length,
    nodes,
    edges: layout.routedEdges,
    contextTargetId: selection.contextTargetId,
    contextTask: selection.contextTask,
    contextTrigger: selection.contextTrigger,
    selectedNote: selection.selectedNote,
    pinGeneration: selection.pinGenerationRef.current,
    pinGenerationRef: selection.pinGenerationRef,
    aliveRef: selection.aliveRef,
    renderPopupPrerequisites: props.renderPopupPrerequisites,
    onSelectTask: props.onSelectTask,
    onFocusPinChange: props.onFocusPinChange,
    setContextTargetId: selection.setContextTargetId,
    setContextTrigger: selection.setContextTrigger,
    setSelectedNoteId: selection.setSelectedNoteId,
    onInit: viewport.onInit,
    onNodeDragStart: local.onNodeDragStart,
    onNodeDrag: local.onNodeDrag,
    onNodeDragStop: local.onNodeDragStop,
    onMoveEnd: viewport.onMoveEnd,
    onResetLayout: viewport.resetGraphLayout,
  };
}
