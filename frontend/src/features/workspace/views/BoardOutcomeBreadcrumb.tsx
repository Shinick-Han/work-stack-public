import type { KeyResultProjection } from "./keyResultModel";
import type { WorkspaceTask } from "./types";
import type { OutcomeSelection } from "./KeyResultPresentation";
import { outcomeChipsForTask } from "./keyResultViewModel";

import "./OutcomeNavigator.css";

export interface BoardOutcomeBreadcrumbProps {
  task: WorkspaceTask;
  keyResultProjection?: KeyResultProjection;
  onSelectObjective?: (objectiveId: string) => void;
  onSelectOutcome?: (selection: OutcomeSelection) => void;
}

function stopCardActivation(event: { stopPropagation: () => void }) {
  event.stopPropagation();
}

function ObjectiveAction({
  objectiveId,
  onSelectObjective,
}: {
  objectiveId: string;
  onSelectObjective?: (objectiveId: string) => void;
}) {
  if (!onSelectObjective) {
    return <span>{objectiveId}</span>;
  }
  return (
    <button
      type="button"
      aria-label={`Focus objective ${objectiveId}`}
      onClick={(event) => {
        stopCardActivation(event);
        onSelectObjective(objectiveId);
      }}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") stopCardActivation(event);
      }}
    >
      {objectiveId}
    </button>
  );
}

function OutcomeAction({
  objectiveId,
  keyResultId,
  onSelectOutcome,
  children,
}: {
  objectiveId: string;
  keyResultId: string;
  onSelectOutcome?: (selection: OutcomeSelection) => void;
  children: string;
}) {
  if (!onSelectOutcome) {
    return <span>{children}</span>;
  }
  return (
    <button
      type="button"
      aria-label={`Filter by outcome ${objectiveId} ${keyResultId}`}
      onClick={(event) => {
        stopCardActivation(event);
        onSelectOutcome({ objectiveId, keyResultId });
      }}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") stopCardActivation(event);
      }}
    >
      {children}
    </button>
  );
}

export function BoardOutcomeBreadcrumb({
  task,
  keyResultProjection,
  onSelectObjective,
  onSelectOutcome,
}: BoardOutcomeBreadcrumbProps) {
  const chips = keyResultProjection
    ? outcomeChipsForTask(keyResultProjection, task.id).chips
    : [];
  const objectiveIds = task.objective_ids ?? [];

  if (chips.length > 0) {
    return (
      <div className="board-outcome-breadcrumb" aria-label={`Outcomes for ${task.id}`}>
        {chips.map((chip) => (
          <span className="board-outcome-breadcrumb__trail" key={chip.key}>
            <ObjectiveAction
              objectiveId={chip.objectiveId}
              onSelectObjective={onSelectObjective}
            />
            <span className="board-outcome-breadcrumb__sep" aria-hidden="true">/</span>
            {chip.resolved ? (
              <OutcomeAction
                objectiveId={chip.objectiveId}
                keyResultId={chip.keyResultId}
                onSelectOutcome={onSelectOutcome}
              >
                {chip.keyResultId}
              </OutcomeAction>
            ) : (
              <span className="board-outcome-breadcrumb__unresolved">
                <OutcomeAction
                  objectiveId={chip.objectiveId}
                  keyResultId={chip.keyResultId}
                  onSelectOutcome={onSelectOutcome}
                >
                  {chip.keyResultId}
                </OutcomeAction>
                <span> Unresolved outcome</span>
              </span>
            )}
          </span>
        ))}
      </div>
    );
  }

  if (objectiveIds.length > 0) {
    return (
      <div className="board-outcome-breadcrumb" aria-label={`Outcomes for ${task.id}`}>
        {objectiveIds.map((objectiveId) => (
          <span className="board-outcome-breadcrumb__trail" key={objectiveId}>
            <ObjectiveAction
              objectiveId={objectiveId}
              onSelectObjective={onSelectObjective}
            />
          </span>
        ))}
      </div>
    );
  }

  return (
    <div className="board-outcome-breadcrumb" aria-label={`Outcomes for ${task.id}`}>
      <span className="board-outcome-breadcrumb__unaligned">Unaligned</span>
    </div>
  );
}
