import { useEffect, useRef } from "react";

import type { KeyResultProjection } from "./keyResultModel";
import { useTreemapKeyboardMove } from "./treemapKeyboard";
import {
  useTreemapPointerController,
  type TreemapPointerApi,
} from "./treemapPointerController";
import { TreemapEmpty, TreemapSurface } from "./treemapPresentation";
import {
  pressedMoveId,
  useTreemapGestureActions,
  useTreemapOrderedGroups,
  useTreemapProjection,
  useTreemapReorder,
  useTreemapReset,
  useTreemapStore,
} from "./treemapViewBindings";
import type { WorkspaceObjective, WorkspaceTask } from "./types";
import "./TreemapView.drag.css";

export { TreemapObjectiveGroup, TreemapObjectiveNavigator } from "./treemapPresentation";

interface TreemapViewProps {
  workspaceId?: string;
  tasks: readonly WorkspaceTask[];
  referenceTasks?: readonly WorkspaceTask[];
  objectives: readonly WorkspaceObjective[];
  keyResultProjection?: KeyResultProjection;
  selectedTaskId?: string | null;
  onSelectTask: (taskId: string) => void;
  onSelectObjective: (objectiveId: string) => void;
}

export function TreemapView({
  workspaceId,
  tasks,
  referenceTasks,
  objectives,
  keyResultProjection,
  selectedTaskId,
  onSelectTask,
  onSelectObjective,
}: TreemapViewProps) {
  const projection = useTreemapProjection({
    tasks, referenceTasks, objectives, keyResultProjection,
  });
  const persistence = useTreemapStore(workspaceId ?? "");
  const writeReorder = useTreemapReorder(
    persistence.store,
    persistence.setLocal,
    persistence.skipExternalRef,
    projection.catalogRef,
    projection.modeRef,
  );
  const pointerApiRef = useRef<TreemapPointerApi>({ closeCapture() {}, detachPointer() {} });
  const surfaceRef = useRef<HTMLDivElement | null>(null);
  const actions = useTreemapGestureActions({
    writeReorder,
    gateRef: persistence.gateRef,
    setAnnouncement: persistence.setAnnouncement,
    visibleRef: projection.visibleRef,
    modeRef: projection.modeRef,
    pointerApiRef,
  });
  const pointer = useTreemapPointerController({
    surfaceRef,
    gestureRef: actions.gestureRef,
    gateRef: persistence.gateRef,
    pointerApiRef,
    beginTracked: actions.beginTracked,
    cancelGesture: actions.cancelGesture,
    commitGesture: actions.commitGesture,
    setGesture: actions.setGesture,
    setAnnouncement: persistence.setAnnouncement,
  });
  const onMoveKeyDown = useTreemapKeyboardMove(actions.gesture, {
    gestureRef: actions.gestureRef,
    beginTracked: actions.beginTracked,
    cancelGesture: actions.cancelGesture,
    commitGesture: actions.commitGesture,
    setGesture: actions.setGesture,
    setAnnouncement: persistence.setAnnouncement,
  });
  const orderedGroups = useTreemapOrderedGroups(
    projection.visibleGroups, persistence.local, projection.mode, actions.gesture,
  );
  const resetOrder = useTreemapReset(
    actions.cancelGesture,
    persistence.store,
    persistence.setLocal,
    persistence.skipExternalRef,
    persistence.setAnnouncement,
  );
  useEffect(() => () => {
    pointer.detachPointer();
    if (actions.gestureRef.current) {
      persistence.gateRef.current.endGesture();
      actions.gestureRef.current = null;
    }
  }, [pointer.detachPointer, persistence.store]);

  if (!projection.visibleGroups.length) return <TreemapEmpty />;
  return (
    <TreemapSurface
      announcement={persistence.announcement}
      mode={projection.mode}
      movePressedId={pressedMoveId(actions.gesture)}
      onMoveKeyDown={onMoveKeyDown}
      onMovePointerDown={pointer.onMovePointerDown}
      onResetOrder={resetOrder}
      onSelectObjective={onSelectObjective}
      onSelectTask={onSelectTask}
      orderedGroups={orderedGroups}
      selectedTaskId={selectedTaskId}
      surfaceRef={surfaceRef}
    />
  );
}
