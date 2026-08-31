import type { Task } from '../../domain/types'

export interface AgendaEvent {
  id: string
  title: string
  start: string
  allDay: true
  editable: boolean
  classNames: string[]
  extendedProps: { taskId: string; kind: 'scheduled' | 'due' }
}

export function buildAgendaEvents(tasks: Task[]): AgendaEvent[] {
  return tasks.flatMap((task) => {
    if (task.status === 'done' || task.status === 'dropped') return []
    const events: AgendaEvent[] = []
    if (task.scheduled) {
      events.push({
        id: `${task.id}:scheduled`,
        title: `${task.id} · ${task.title}`,
        start: task.scheduled,
        allDay: true,
        editable: true,
        classNames: [`agenda-event--${task.priority.toLowerCase()}`],
        extendedProps: { taskId: task.id, kind: 'scheduled' },
      })
    }
    if (task.due && task.due !== task.scheduled) {
      events.push({
        id: `${task.id}:due`,
        title: `Due · ${task.id} · ${task.title}`,
        start: task.due,
        allDay: true,
        editable: false,
        classNames: ['agenda-event--due'],
        extendedProps: { taskId: task.id, kind: 'due' },
      })
    }
    return events
  })
}
