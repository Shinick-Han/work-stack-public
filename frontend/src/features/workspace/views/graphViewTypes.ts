import type { KeyboardEvent, ReactNode } from "react";
import type {
  Edge as FlowEdge,
  Node as FlowNode,
} from "@xyflow/react";
import type { GraphEdgeRoute } from "./graphLayout";
import type { KeyResultProjection, OutcomeFilter } from "./keyResultModel";
import type { DerivedEdgeKind } from "./keyResultViewModel";
import type {
  WorkspaceEdge,
  WorkspaceNote,
  WorkspaceObjective,
  WorkspaceTask,
} from "./types";

export type GraphKeyResultSelection = { objectiveId: string; keyResultId: string };

export type GraphNodeKind = "objective" | "task" | "note" | "key-result";

export type GraphEntityKind = "task" | "objective" | "note";

/**
 * Deferred K3A click-through seam. Graph KR activation is local selection
 * only; it must not mutate the global outcome filter.
 */
export type DeferredKeyResultDetailCoordinator = {
  openKeyResultDetail(selection: { objectiveId: string; keyResultId: string }): void
};

export type GraphNodeData = Record<string, unknown> & {
  id: string;
  kind: GraphNodeKind;
  title: string;
  eyebrow: string;
  contextCount: number;
  priority?: string;
  status?: string;
  selected: boolean;
  related: boolean;
  /** Key-result presentation only; never canonical Task or Objective data. */
  outcome?: {
    objectiveId: string;
    keyResultId: string;
    /** Recorded value, or null when the workspace never recorded one. */
    recordedProgress: number | null;
    target: string | null;
    status: string | null;
    linkedTotal: number;
    visibleTotal: number;
  };
  onActivate?: () => void;
  onOpenContext?: (trigger: HTMLButtonElement) => void;
  moveHandleLabel?: string;
  movePressed?: boolean;
  onMoveKeyDown?: (event: KeyboardEvent<HTMLButtonElement>) => void;
};

export type WorkspaceFlowNode = FlowNode<GraphNodeData, "workspace">;

export interface GraphViewProps {
  /** Stable workspace identity for the per-workspace local-view envelope. */
  workspaceId?: string;
  /** Already-projected, visible Tasks. Never filtered again here. */
  tasks: readonly WorkspaceTask[];
  /**
   * Canonical Tasks. Popup identity resolves against these, so completion or a
   * filter cannot dismiss an open popup. Defaults to tasks for isolated callers;
   * the real Workspace path always supplies the canonical array.
   */
  referenceTasks?: readonly WorkspaceTask[];
  objectives: readonly WorkspaceObjective[];
  notes: readonly WorkspaceNote[];
  edges: readonly WorkspaceEdge[];
  emptyKind?: string;
  selectedTaskId?: string | null;
  selectedObjectiveId?: string | null;
  contextTargetTaskId?: string | null;
  focusPinnedTaskId?: string | null;
  onContextTargetChange?: (taskId: string | null) => void;
  onFocusPinChange?: (taskId: string | null) => void;
  /** Page-owned prerequisite controls rendered inside the context modal. */
  renderPopupPrerequisites?: (taskId: string) => ReactNode;
  /** Supplied outcome projection; this view never builds its own. */
  keyResultProjection?: KeyResultProjection | null;
  /** The already-normalized scoped coordinate; selection display only. */
  outcome?: OutcomeFilter | null;
  /**
   * Page outcome-filter callback. Graph KR clicks do not invoke it; the
   * global navigator remains the only writer of that coordinate.
   */
  onSelectOutcome?: (selection: { objectiveId: string; keyResultId: string }) => void;
  onSelectTask: (taskId: string) => void;
  onSelectObjective: (objectiveId: string) => void;
}

export const EDGE_COLORS = {
  alignment: "var(--wsv-relation-alignment)",
  dependency: "var(--wsv-relation-dependency)",
  parent: "var(--wsv-relation-parent)",
  reference: "var(--wsv-relation-reference)",
} as const;

export type GraphEdgeData = Record<string, unknown> & {
  /** Canonical relationship kinds plus the separate derived presentation kinds. */
  kind: keyof typeof EDGE_COLORS | DerivedEdgeKind;
  route?: GraphEdgeRoute;
};

export type WorkspaceFlowEdge = FlowEdge<GraphEdgeData>;

export type GraphMoveGesture = {
  id: string;
  kind: GraphNodeKind;
  labelId: string;
  workspaceId: string;
  origin: { x: number; y: number };
  current: { x: number; y: number };
  source: "pointer" | "keyboard";
};
