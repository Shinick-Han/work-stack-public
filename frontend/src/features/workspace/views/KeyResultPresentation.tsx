import type { KeyResultNode } from "./keyResultModel";
import type { OutcomeChip } from "./keyResultViewModel";

/**
 * Shared, view-agnostic outcome presentation.
 *
 * Both components consume only supplied model data and callbacks: they run no
 * projection, filter or count of their own, and they never derive a progress
 * percentage from Task completion.
 */

export interface OutcomeSelection {
  objectiveId: string;
  keyResultId: string;
}

interface KeyResultCatalogProps {
  keyResults: readonly KeyResultNode[];
  onSelectOutcome?: (selection: OutcomeSelection) => void;
  selected?: OutcomeSelection | null;
  title?: string;
}

function progressLabel(node: KeyResultNode) {
  // A recorded 0 is a measurement; a missing value is not.
  return node.recordedProgress === null
    ? "Unrecorded"
    : `Recorded progress ${node.recordedProgress}`;
}

function countsLabel(node: KeyResultNode) {
  const { total, open, started, done, dropped } = node.counts;
  return `${total} linked · ${open} open · ${started} started · ${done} done · ${dropped} dropped`;
}

export function KeyResultCatalog({
  keyResults,
  onSelectOutcome,
  selected = null,
  title = "Outcomes",
}: KeyResultCatalogProps) {
  return (
    <section aria-label={title} className="outcome-catalog">
      <h3>{title}</h3>
      {keyResults.length === 0 ? (
        <p className="outcome-catalog__empty">No outcomes are defined yet.</p>
      ) : (
        <ul className="outcome-catalog__list">
          {keyResults.map((node) => {
            const isSelected = Boolean(
              selected
                && selected.objectiveId === node.objectiveId
                && selected.keyResultId === node.keyResultId,
            );
            return (
              <li className="outcome-catalog__item" key={node.key}>
                <button
                  aria-pressed={isSelected}
                  className="outcome-catalog__select"
                  disabled={!onSelectOutcome}
                  onClick={() => onSelectOutcome?.({
                    objectiveId: node.objectiveId,
                    keyResultId: node.keyResultId,
                  })}
                  type="button"
                >
                  <strong>{node.objectiveId} · {node.keyResultId}</strong>
                  <span>{node.text}</span>
                </button>
                {node.target ? <span className="outcome-catalog__target">Target {node.target}</span> : null}
                {node.status ? <span className="outcome-catalog__status">Status {node.status}</span> : null}
                <span className="outcome-catalog__progress">{progressLabel(node)}</span>
                <span className="outcome-catalog__counts">{countsLabel(node)}</span>
                <span className="outcome-catalog__visible">
                  {`${node.visibleCounts.total} visible of ${node.counts.total} linked`}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

/** Human-readable text for the model's closed reason values; nothing invented. */
const UNRESOLVED_EXPLANATIONS: Record<string, string> = {
  "unaligned-parent": "Its Objective is not aligned on this Task.",
  "missing-objective": "No matching Objective exists in this workspace.",
  "ambiguous-objective": "Several Objectives share that identifier.",
  "missing-key-result": "No matching Key Result exists under that Objective.",
  "ambiguous-key-result": "Several Key Results share that identifier.",
};

function unresolvedExplanation(reason: string | null) {
  return (reason && UNRESOLVED_EXPLANATIONS[reason]) || "This reference cannot be resolved.";
}

interface TaskOutcomeChipsProps {
  chips: readonly OutcomeChip[];
  onSelectOutcome?: (selection: OutcomeSelection) => void;
  unassignedLabel?: string;
}

/**
 * Per-Task chips. Activation chooses the exact scoped pair and stops the parent
 * card or row from also activating; it never writes a Task or a Key Result.
 */
export function TaskOutcomeChipList({
  chips,
  onSelectOutcome,
  unassignedLabel = "Unassigned outcome",
}: TaskOutcomeChipsProps) {
  if (chips.length === 0) {
    return <span className="outcome-chip outcome-chip--unassigned">{unassignedLabel}</span>;
  }
  return (
    <span className="outcome-chips">
      {chips.map((chip) => (
        <button
          aria-label={`Filter by outcome ${chip.objectiveId} ${chip.keyResultId}`}
          className={chip.resolved ? "outcome-chip" : "outcome-chip outcome-chip--unresolved"}
          key={chip.key}
          onClick={(event) => {
            event.stopPropagation();
            onSelectOutcome?.({
              objectiveId: chip.objectiveId,
              keyResultId: chip.keyResultId,
            });
          }}
          onKeyDown={(event) => {
            if (event.key === "Enter" || event.key === " ") event.stopPropagation();
          }}
          title={chip.resolved ? undefined : unresolvedExplanation(chip.reason)}
          type="button"
        >
          <strong>{chip.label}</strong>
          <span>{chip.resolved ? chip.text : "Unresolved outcome"}</span>
          {chip.resolved ? null : (
            <span className="sr-only">{unresolvedExplanation(chip.reason)}</span>
          )}
        </button>
      ))}
    </span>
  );
}
