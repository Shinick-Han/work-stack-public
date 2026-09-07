export type TreemapMoveKind = "objective" | "task";

export type TreemapMoveItem = {
  kind: TreemapMoveKind;
  id: string;
  scope: string;
};

export type TreemapGesture = TreemapMoveItem & {
  source: "pointer" | "keyboard";
  visibleIds: string[];
  originIndex: number;
  currentIndex: number;
  startX: number;
  startY: number;
  activated: boolean;
};

export function kindTitle(kind: TreemapMoveKind): string {
  return kind === "objective" ? "Objective" : "Task";
}
