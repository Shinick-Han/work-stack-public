import { providerReplyVerified, type MicrosoftProviderGates } from '../../config/providerGates'
import { cyclicRelationshipCandidates } from '../../domain/taskRelationships'
import type { ContextItem, Objective, Task, WorkspaceProjection } from '../../domain/types'
import type { ReplySource } from './ReplyComposer'
import { microsoftReplySource } from './taskDrawerModel'

interface TaskDrawerSelectorInput {
  context: ContextItem[]
  draft: Task | null
  providerGates: MicrosoftProviderGates
  workspace: WorkspaceProjection
}

export interface TaskDrawerSelection {
  availableDependencyTasks: Task[]
  availableParentTasks: Task[]
  childTasks: Task[]
  dependencyTasks: Task[]
  dependentTasks: Task[]
  parentTask: Task | null
  replySources: ReplySource[]
  replyUnavailableSources: ReplySource[]
  taskObjectives: Objective[]
}

const emptySelection = (): TaskDrawerSelection => ({
  availableDependencyTasks: [],
  availableParentTasks: [],
  childTasks: [],
  dependencyTasks: [],
  dependentTasks: [],
  parentTask: null,
  replySources: [],
  replyUnavailableSources: [],
  taskObjectives: [],
})

export function selectTaskDrawerData({ context, draft, providerGates, workspace }: TaskDrawerSelectorInput): TaskDrawerSelection {
  if (!draft) return emptySelection()

  const cyclicParentCandidates = cyclicRelationshipCandidates(workspace.tasks, draft.id, 'parent_id')
  const cyclicDependencyCandidates = cyclicRelationshipCandidates(workspace.tasks, draft.id, 'dependencies')
  const linkedMicrosoftSources = context
    .map(microsoftReplySource)
    .filter((source): source is ReplySource => source !== null)

  return {
    availableDependencyTasks: workspace.tasks.filter((task) => task.id !== draft.id
      && !draft.dependencies.includes(task.id)
      && !cyclicDependencyCandidates.has(task.id)),
    availableParentTasks: workspace.tasks.filter((task) => task.id !== draft.id
      && (task.id === draft.parent_id || !cyclicParentCandidates.has(task.id))),
    childTasks: workspace.tasks.filter((task) => task.parent_id === draft.id),
    dependencyTasks: workspace.tasks.filter((task) => draft.dependencies.includes(task.id)),
    dependentTasks: workspace.tasks.filter((task) => task.dependencies.includes(draft.id)),
    parentTask: workspace.tasks.find((task) => task.id === draft.parent_id) ?? null,
    replySources: linkedMicrosoftSources.filter((source) => providerReplyVerified(source.provider, providerGates)),
    replyUnavailableSources: linkedMicrosoftSources.filter((source) => !providerReplyVerified(source.provider, providerGates)),
    taskObjectives: workspace.objectives.filter((objective) => draft.objective_ids.includes(objective.id)),
  }
}
