import type { ReactNode } from 'react'
import { Button, EmptyState } from '../../components/Primitives'

function OnboardingActions({
  onCreateTask,
  onOpenObjectives,
}: {
  onCreateTask: () => void
  onOpenObjectives: () => void
}) {
  return (
    <div className="workspace-onboarding-actions">
      <Button icon="target" onClick={onOpenObjectives} variant="primary">Define an objective</Button>
      <Button icon="plus" onClick={onCreateTask}>Create first task</Button>
    </div>
  )
}

/**
 * A zero-Task workspace that already has outcomes still shows the Graph
 * catalog, so no onboarding overlay covers usable key results. The onboarding
 * actions stay reachable in this banner instead.
 */
export function WorkspaceOnboardingBanner({
  onCreateTask,
  onOpenObjectives,
}: {
  onCreateTask: () => void
  onOpenObjectives: () => void
}) {
  return (
    <div className="workspace-onboarding-banner">
      <p>No Tasks yet. Your outcomes are shown below.</p>
      <OnboardingActions onCreateTask={onCreateTask} onOpenObjectives={onOpenObjectives} />
    </div>
  )
}

/** The stage a workspace with neither Tasks nor an outcome catalog shows. */
export function WorkspaceFirstRunStage({
  onCreateTask,
  onOpenObjectives,
  outcomeNavigator,
}: {
  onCreateTask: () => void
  onOpenObjectives: () => void
  outcomeNavigator: ReactNode
}) {
  return (
    <div className="workspace-stage">
      {outcomeNavigator}
      <div className="workspace-canvas workspace-canvas--first-run">
        <EmptyState
          action={(
            <OnboardingActions onCreateTask={onCreateTask} onOpenObjectives={onOpenObjectives} />
          )}
          icon="target"
          title="Start with an outcome—or capture the first task."
        >
          Objectives describe what success looks like. Tasks carry the next concrete action and
          can be aligned to an Objective now or later. Both remain local planning facts.
        </EmptyState>
      </div>
    </div>
  )
}
