import { useEffect, useId, useRef, useState } from "react";

import type { OutcomeHierarchy, OutcomeHierarchyKeyResultNode } from "../../../domain/outcomeHierarchy";
import type { OutcomeFilter } from "./keyResultModel";

import "./OutcomeNavigator.css";

export interface OutcomeNavigatorProps {
  hierarchy: OutcomeHierarchy;
  filter: OutcomeFilter;
  onFilterChange: (filter: OutcomeFilter) => void;
}

function pairPressed(filter: OutcomeFilter, node: OutcomeHierarchyKeyResultNode) {
  return (
    filter.kind === "pair"
    && filter.objectiveId === node.objectiveId
    && filter.keyResultId === node.keyResultId
  );
}

function recordedProgressLabel(value: number | null) {
  return value === null ? "Unrecorded" : `Recorded progress ${value}`;
}

function visibleLinkedLabel(node: OutcomeHierarchyKeyResultNode) {
  return `${node.visibleCounts.total} visible of ${node.counts.total} linked`;
}

function filterSummary(filter: OutcomeFilter, empty: boolean) {
  if (filter.kind === "unassigned") return "Unassigned outcome";
  if (filter.kind === "pair") return `${filter.objectiveId} · ${filter.keyResultId}`;
  return empty ? "No outcomes" : "All outcomes";
}

function escapeOwnedByNavigator(root: HTMLElement | null, event: KeyboardEvent) {
  if (event.key !== "Escape" || event.defaultPrevented || !root) return false;
  const origin = event.target instanceof Node ? event.target : document.activeElement;
  return origin instanceof Node && root.contains(origin);
}

export function OutcomeNavigator({
  hierarchy,
  filter,
  onFilterChange,
}: OutcomeNavigatorProps) {
  const [panelOpen, setPanelOpen] = useState(false);
  const rootRef = useRef<HTMLElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelId = useId();
  const summaryId = useId();
  const empty = hierarchy.objectiveNodes.length === 0;
  const summary = filterSummary(filter, empty);

  useEffect(() => {
    if (!panelOpen) return undefined;
    const onKeyDown = (event: KeyboardEvent) => {
      if (!escapeOwnedByNavigator(rootRef.current, event)) return;
      event.preventDefault();
      setPanelOpen(false);
      triggerRef.current?.focus();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [panelOpen]);

  return (
    <nav ref={rootRef} aria-label="Outcome navigator" className="outcome-navigator">
      <button
        ref={triggerRef}
        type="button"
        className="outcome-navigator__trigger"
        aria-label="Outcome navigator"
        aria-expanded={panelOpen}
        aria-controls={panelId}
        aria-describedby={summaryId}
        onClick={() => setPanelOpen((open) => !open)}
      >
        Outcome navigator
        <span id={summaryId} className="outcome-navigator__summary">{summary}</span>
      </button>
      <div
        id={panelId}
        className="outcome-navigator__panel"
        hidden={!panelOpen}
      >
        <div className="outcome-navigator__filters">
          <button
            type="button"
            aria-pressed={filter.kind === "all"}
            onClick={() => onFilterChange({ kind: "all" })}
          >
            All outcomes
          </button>
          <button
            type="button"
            aria-pressed={filter.kind === "unassigned"}
            onClick={() => onFilterChange({ kind: "unassigned" })}
          >
            Unassigned outcome
          </button>
        </div>
        {empty ? (
          <p className="outcome-navigator__empty" role="status">
            No outcomes are defined yet
          </p>
        ) : (
          hierarchy.objectiveNodes.map((objective) => (
            <details
              key={objective.objectiveId}
              className="outcome-navigator__objective"
              open
            >
              <summary>{objective.objectiveId}</summary>
              <ul className="outcome-navigator__kr-list">
                {objective.keyResults.map((node) => {
                  const text = hierarchy.projection.byKey[node.key]?.text ?? "";
                  const countsId = `${panelId}-kr-counts-${node.key}`;
                  const progressId = `${panelId}-kr-progress-${node.key}`;
                  return (
                    <li key={node.key}>
                      <button
                        type="button"
                        className="outcome-navigator__kr"
                        aria-pressed={pairPressed(filter, node)}
                        aria-label={`Select outcome ${node.objectiveId} ${node.keyResultId}`}
                        aria-describedby={`${countsId} ${progressId}`}
                        onClick={() => onFilterChange({
                          kind: "pair",
                          objectiveId: node.objectiveId,
                          keyResultId: node.keyResultId,
                        })}
                      >
                        <strong>{node.objectiveId} · {node.keyResultId}</strong>
                        {text ? <span className="outcome-navigator__kr-text">{text}</span> : null}
                        <span id={countsId} className="outcome-navigator__kr-counts">
                          {visibleLinkedLabel(node)}
                        </span>
                        <span id={progressId} className="outcome-navigator__kr-progress">
                          {recordedProgressLabel(node.recordedProgress)}
                        </span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            </details>
          ))
        )}
      </div>
    </nav>
  );
}
