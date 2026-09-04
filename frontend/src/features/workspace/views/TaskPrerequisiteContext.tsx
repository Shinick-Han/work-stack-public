import type {
  CompletedVisibilityProjection,
  PrerequisiteClassification,
  PrerequisiteReason,
} from "./completedTaskVisibility";
import { REVEAL_DISPLAY_LIMIT } from "./completedTaskVisibility";
import { useEffect, useRef } from "react";

import type { WorkspaceTask } from "./types";

/**
 * One shared, accessible prerequisite context panel.
 *
 * It renders the admitted projection's classifications for a single anchor Task
 * and owns no state: the Workspace page holds the projection, counts and reveal
 * state, so the panel and the Graph popup can share this component without
 * introducing a second owner. It never expands transitively or through parents,
 * never reveals other-filter or missing entries, and never changes status,
 * search or visibility.
 */

const REASON_LABELS: Record<PrerequisiteReason, string> = {
  visible: "Shown",
  completed: "Completed · hidden",
  "other-filter": "Hidden by another filter",
  missing: "Missing from this workspace",
};

const REASON_ORDER: readonly PrerequisiteReason[] = [
  "visible",
  "completed",
  "other-filter",
  "missing",
];

export interface TaskPrerequisiteContextProps {
  /** The canonical anchor Task, or null when neither a popup nor a selection exists. */
  anchorTaskId: string | null;
  projection: CompletedVisibilityProjection;
  /** Reveal the given completed prerequisite IDs for this anchor. */
  onReveal: (taskIds: readonly string[]) => void;
  /** Clear the temporary reveal only; interaction pins are untouched. */
  onClearReveal: () => void;
  /** Explicit view action; never triggered merely by hiding. */
  onOpenTask?: (taskId: string) => void;
  className?: string;
}

function titleFor(
  id: string,
  byId: ReadonlyMap<string, WorkspaceTask>,
): string | null {
  return byId.get(id)?.title ?? null;
}

export function TaskPrerequisiteContext({
  anchorTaskId,
  projection,
  onReveal,
  onClearReveal,
  onOpenTask,
  className = "",
}: TaskPrerequisiteContextProps) {
  const revealRef = useRef<HTMLButtonElement>(null);
  const clearRef = useRef<HTMLButtonElement>(null);
  // Which of the two toggles the user actually activated last. Activating one
  // removes it from the tree, so focus would otherwise fall to the document
  // body even though this panel and its modal are still live.
  const activatedRef = useRef<
    null | { kind: "reveal" | "clear"; element: HTMLButtonElement | null }
  >(null);

  useEffect(() => {
    const activated = activatedRef.current;
    if (!activated) return;
    const active = document.activeElement;

    // Removal is proven by the source element leaving the document, never by
    // focus being on BODY. A control that is still connected was not removed.
    if (activated.element?.isConnected !== false) {
      // It survived, so it keeps focus if it still has it; if the user has
      // moved on, the intent is stale and must be discarded rather than kept.
      if (active !== activated.element) activatedRef.current = null;
      return;
    }

    // Focus is on some other live element, so the user moved it deliberately
    // and this panel must not take it back.
    if (active && active !== document.body) {
      activatedRef.current = null;
      return;
    }

    // The activated control is gone and focus fell out of the panel. Hand it to
    // the surviving counterpart in this same live panel.
    const successor = activated.kind === "reveal" ? clearRef.current : revealRef.current;
    // The successor may only appear on a later commit of the same settling
    // update; keep the intent until it is actually honoured.
    if (!successor?.isConnected) return;
    activatedRef.current = null;
    successor.focus();
  });

  const canonicalById = new Map(
    projection.referenceTasks.map((task) => [task.id, task]),
  );

  if (!anchorTaskId || !canonicalById.has(anchorTaskId)) {
    return (
      <section
        aria-label="Prerequisite context"
        className={`wsv-prereq ${className}`.trim()}
      >
        <p className="wsv-prereq__empty">
          Select a Task to see its prerequisites.
        </p>
      </section>
    );
  }

  const anchorTitle = titleFor(anchorTaskId, canonicalById) ?? anchorTaskId;
  const entries: readonly PrerequisiteClassification[] =
    projection.prerequisitesByTaskId[anchorTaskId] ?? [];

  const counts = REASON_ORDER.map((reason) => ({
    reason,
    total: entries.filter((entry) => entry.reason === reason).length,
  })).filter((entry) => entry.total > 0);

  // Only completed-hidden entries are revealable, and only for this anchor.
  const revealable = entries.filter((entry) => entry.revealable);
  const activeReveal =
    projection.reveal?.anchorTaskId === anchorTaskId
      ? projection.reveal.taskIds
      : [];
  const revealCandidates = revealable
    .map((entry) => entry.id)
    .filter((id) => !activeReveal.includes(id))
    .slice(0, REVEAL_DISPLAY_LIMIT - activeReveal.length);

  return (
    <section
      aria-label="Prerequisite context"
      className={`wsv-prereq ${className}`.trim()}
      data-anchor-task={anchorTaskId}
    >
      <header className="wsv-prereq__header">
        <h3>Prerequisites for {anchorTaskId}</h3>
        <p className="wsv-prereq__anchor-title">{anchorTitle}</p>
      </header>

      {entries.length === 0 ? (
        <p className="wsv-prereq__empty">This Task has no prerequisites.</p>
      ) : (
        <>
          <ul className="wsv-prereq__counts" aria-label="Prerequisite summary">
            {counts.map(({ reason, total }) => (
              <li key={reason}>
                <span>{REASON_LABELS[reason]}</span>
                <strong>{total}</strong>
              </li>
            ))}
          </ul>

          <ul className="wsv-prereq__list" aria-label="Direct prerequisites">
            {entries.map((entry) => {
              const title = titleFor(entry.id, canonicalById);
              const revealed = activeReveal.includes(entry.id);
              return (
                <li
                  key={entry.id}
                  data-prereq-reason={entry.reason}
                  data-prereq-revealed={revealed ? "true" : undefined}
                >
                  <span className="wsv-prereq__id">{entry.id}</span>
                  {/* A missing prerequisite has no canonical Task, so no title. */}
                  {title ? (
                    <span className="wsv-prereq__title">{title}</span>
                  ) : null}
                  <span className={`wsv-prereq__reason wsv-prereq__reason--${entry.reason}`}>
                    {revealed ? "Completed · revealed" : REASON_LABELS[entry.reason]}
                  </span>
                  {onOpenTask && entry.reason !== "missing" ? (
                    <button
                      onClick={() => onOpenTask(entry.id)}
                      type="button"
                    >
                      View {entry.id}
                    </button>
                  ) : null}
                </li>
              );
            })}
          </ul>
        </>
      )}

      {revealCandidates.length ? (
        <button
          className="wsv-prereq__reveal"
          onClick={() => {
            activatedRef.current = { kind: "reveal", element: revealRef.current };
            onReveal([...activeReveal, ...revealCandidates]);
          }}
          ref={revealRef}
          type="button"
        >
          Reveal completed prerequisites
        </button>
      ) : null}

      {activeReveal.length ? (
        <button
          className="wsv-prereq__clear"
          onClick={() => {
            activatedRef.current = { kind: "clear", element: clearRef.current };
            onClearReveal();
          }}
          ref={clearRef}
          type="button"
        >
          Clear temporary reveal
        </button>
      ) : null}

      {activeReveal.length ? (
        <p className="wsv-prereq__cap" role="status">
          {`Showing ${activeReveal.length} of at most ${REVEAL_DISPLAY_LIMIT} completed prerequisites`}
          {projection.revealRemainingCount
            ? ` · ${projection.revealRemainingCount} more not revealed`
            : ""}
        </p>
      ) : null}
    </section>
  );
}
