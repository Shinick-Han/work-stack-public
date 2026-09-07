import { useCallback, useEffect, useRef, useState } from "react";
import type { WorkspaceNote, WorkspaceTask } from "./types";
import type { GraphKeyResultSelection } from "./graphViewTypes";

export function useGraphSelection(args: {
  notes: readonly WorkspaceNote[];
  canonicalTasks: readonly WorkspaceTask[];
  contextTargetTaskId: string | null;
  onContextTargetChange?: (taskId: string | null) => void;
}) {
  const { notes, canonicalTasks, contextTargetTaskId, onContextTargetChange } = args;
  const pinGenerationRef = useRef(0);
  const aliveRef = useRef(true);
  useEffect(() => {
    aliveRef.current = true;
    return () => { aliveRef.current = false; };
  }, []);
  const [contextTrigger, setContextTrigger] = useState<HTMLButtonElement | null>(null);
  const [localContextTaskId, setLocalContextTaskId] = useState<string | null>(null);
  const [graphKeyResult, setGraphKeyResult] = useState<GraphKeyResultSelection | null>(null);
  const [selectedNoteId, setSelectedNoteId] = useState<string | null>(null);
  const contextTargetId = onContextTargetChange ? contextTargetTaskId : localContextTaskId;
  const setContextTargetId = useCallback((taskId: string | null) => {
    setLocalContextTaskId(taskId);
    onContextTargetChange?.(taskId);
  }, [onContextTargetChange]);
  const clearGraphLocalSelection = useCallback(() => {
    setGraphKeyResult(null);
    setSelectedNoteId(null);
  }, []);
  const contextTask = canonicalTasks.find((task) => task.id === contextTargetId);
  useEffect(() => {
    if (contextTargetId && !contextTask) {
      setLocalContextTaskId(null);
      onContextTargetChange?.(null);
      setContextTrigger(null);
    }
  }, [contextTargetId, contextTask, onContextTargetChange]);
  const selectedNote = notes.find((note) => note.id === selectedNoteId) ?? null;
  useEffect(() => {
    if (selectedNoteId && !selectedNote) setSelectedNoteId(null);
  }, [selectedNoteId, selectedNote]);
  return {
    pinGenerationRef,
    aliveRef,
    contextTrigger,
    setContextTrigger,
    graphKeyResult,
    setGraphKeyResult,
    selectedNoteId,
    setSelectedNoteId,
    contextTargetId,
    setContextTargetId,
    clearGraphLocalSelection,
    contextTask,
    selectedNote,
  };
}
