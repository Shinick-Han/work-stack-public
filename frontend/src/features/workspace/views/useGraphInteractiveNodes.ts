import { useMemo, type KeyboardEvent } from "react";
import { graphMoveHandleLabel } from "./graphViewState";
import { moveLabelId } from "./graphMoveSession";
import type { GraphKeyResultSelection, GraphMoveGesture, WorkspaceFlowNode } from "./graphViewTypes";

type InteractionApi = {
  persistWorkspaceId: string;
  gesture: GraphMoveGesture | null;
  onMoveKeyDown: (node: WorkspaceFlowNode, event: KeyboardEvent<HTMLButtonElement>) => void;
  onSelectTask: (taskId: string) => void;
  onSelectObjective: (objectiveId: string) => void;
  onFocusPinChange?: (taskId: string | null) => void;
  clearGraphLocalSelection: () => void;
  setGraphKeyResult: (value: GraphKeyResultSelection | null | ((current: GraphKeyResultSelection | null) => GraphKeyResultSelection | null)) => void;
  setSelectedNoteId: (value: string | null | ((current: string | null) => string | null)) => void;
  setContextTrigger: (trigger: HTMLButtonElement | null) => void;
  setContextTargetId: (taskId: string | null) => void;
  pinGenerationRef: { current: number };
};

function toggleGraphKeyResult(
  node: WorkspaceFlowNode,
  api: InteractionApi,
) {
  const next = {
    objectiveId: node.data.outcome!.objectiveId,
    keyResultId: node.data.outcome!.keyResultId,
  };
  api.setSelectedNoteId(null);
  api.setGraphKeyResult((current) => (
    current
      && current.objectiveId === next.objectiveId
      && current.keyResultId === next.keyResultId
      ? null
      : next
  ));
}

function activationFor(node: WorkspaceFlowNode, api: InteractionApi) {
  if (node.data.kind === "task") {
    return () => {
      api.clearGraphLocalSelection();
      api.onSelectTask(node.data.id);
    };
  }
  if (node.data.kind === "objective") {
    return () => {
      api.clearGraphLocalSelection();
      api.onSelectObjective(node.data.id);
    };
  }
  if (node.data.kind === "key-result" && node.data.outcome) {
    return () => toggleGraphKeyResult(node, api);
  }
  if (node.data.kind === "note") {
    return () => {
      api.setGraphKeyResult(null);
      api.setSelectedNoteId((current) => current === node.data.id ? null : node.data.id);
    };
  }
  return undefined;
}

function contextOpenerFor(node: WorkspaceFlowNode, api: InteractionApi) {
  if (node.data.kind !== "task") return undefined;
  return (trigger: HTMLButtonElement) => {
    api.setContextTrigger(trigger);
    api.setContextTargetId(node.data.id);
    api.pinGenerationRef.current += 1;
    api.onFocusPinChange?.(node.data.id);
  };
}

export function attachGraphNodeInteractions(node: WorkspaceFlowNode, api: InteractionApi): WorkspaceFlowNode {
  return {
    ...node,
    data: {
      ...node.data,
      moveHandleLabel: graphMoveHandleLabel(node.data.kind, moveLabelId(node.data)),
      movePressed: api.gesture?.id === node.id && api.gesture.workspaceId === api.persistWorkspaceId,
      onMoveKeyDown: (event: KeyboardEvent<HTMLButtonElement>) => api.onMoveKeyDown(node, event),
      onActivate: activationFor(node, api),
      onOpenContext: contextOpenerFor(node, api),
    },
  };
}

export function useGraphInteractiveNodes(
  positionedNodes: readonly WorkspaceFlowNode[],
  api: InteractionApi,
) {
  return useMemo(
    () => positionedNodes.map((node) => attachGraphNodeInteractions(node, api)),
    [
      positionedNodes,
      api.gesture,
      api.onMoveKeyDown,
      api.onSelectObjective,
      api.onSelectTask,
      api.setContextTargetId,
      api.onFocusPinChange,
      api.persistWorkspaceId,
      api.clearGraphLocalSelection,
      api.setGraphKeyResult,
      api.setSelectedNoteId,
      api.setContextTrigger,
    ],
  );
}
