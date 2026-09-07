import type { AppUrlState, Objective, Task, WorkspaceProjection } from '../domain/types'

/**
 * The URL-patch contract the App shell hands to every navigation surface. It lives beside the
 * sidebar model because the sidebar is the surface that navigates most, and every other consumer
 * inside the shell speaks exactly the same patch vocabulary.
 */
export type UpdateUrl = (patch: Partial<AppUrlState>, options?: { replace?: boolean }) => boolean

export interface SidebarTaskResults {
  total: number
  visible: WorkspaceProjection['tasks']
}

export const unassignedGroupKey = '__unassigned__'

/** The sidebar Task navigator lists at most this many matches and states the remainder. */
const sidebarTaskVisibleLimit = 50

export function isOpenTask(task: Task) {
  return task.status !== 'done' && task.status !== 'dropped'
}

function openTasksFirst(left: Task, right: Task) {
  return Number(isOpenTask(right)) - Number(isOpenTask(left)) || left.id.localeCompare(right.id)
}

export interface SidebarTaskGroups {
  byObjective: Map<string, Task[]>
  objectiveCounts: Map<string, number>
  unassigned: Task[]
}

/**
 * Sidebar grouping semantics, fixed here so the rendered order never depends on hook timing:
 * a Task is listed under every Objective this projection can resolve, so a Task carrying several
 * Objectives is deliberately repeated once per Objective and each copy states that fan-out; a Task
 * with no link — or with only links this projection cannot resolve — stays reachable in the
 * unassigned group instead of disappearing.
 */
export function groupSidebarTasks(objectives: readonly Objective[], tasks: readonly Task[]): SidebarTaskGroups {
  const byObjective = new Map<string, Task[]>(objectives.map((objective) => [objective.id, []]))
  const objectiveCounts = new Map<string, number>()
  const unassigned: Task[] = []
  for (const task of tasks) {
    const owners = task.objective_ids.filter((objectiveId) => byObjective.has(objectiveId))
    objectiveCounts.set(task.id, owners.length)
    if (!owners.length) {
      unassigned.push(task)
      continue
    }
    for (const objectiveId of owners) byObjective.get(objectiveId)?.push(task)
  }
  for (const linked of byObjective.values()) linked.sort(openTasksFirst)
  unassigned.sort(openTasksFirst)
  return { byObjective, objectiveCounts, unassigned }
}

export function sharedObjectiveSuffix(sharedCount: number) {
  if (sharedCount < 2) return ''
  return `, shared with ${sharedCount - 1} other ${sharedCount === 2 ? 'Objective' : 'Objectives'}`
}

/**
 * The flat sidebar Task navigator: a case-insensitive match over the fields the row already shows,
 * capped so a large workspace cannot flood the sidebar while the total stays reportable.
 */
export function projectSidebarTaskResults(rawQuery: string, tasks: WorkspaceProjection['tasks'] | undefined): SidebarTaskResults {
  const query = rawQuery.trim().toLocaleLowerCase()
  const matches = (tasks ?? []).filter((task) => (
    !query
    || `${task.id} ${task.title} ${task.status} ${task.priority}`.toLocaleLowerCase().includes(query)
  ))
  return { total: matches.length, visible: matches.slice(0, sidebarTaskVisibleLimit) }
}
