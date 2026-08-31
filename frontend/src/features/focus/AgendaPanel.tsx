import FullCalendar from '@fullcalendar/react'
import dayGridPlugin from '@fullcalendar/daygrid'
import interactionPlugin from '@fullcalendar/interaction'
import listPlugin from '@fullcalendar/list'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client'
import type { Task, WorkspaceProjection } from '../../domain/types'
import { getErrorMessage } from '../../utils/format'
import { buildAgendaEvents } from './agendaModel'

interface AgendaPanelProps {
  tasks: Task[]
  onNotice: (message: string, tone?: 'success' | 'error') => void
  onSelectTask: (taskId: string) => void
}

export function AgendaPanel({ onNotice, onSelectTask, tasks }: AgendaPanelProps) {
  const queryClient = useQueryClient()
  const scheduleMutation = useMutation({
    mutationFn: ({ taskId, scheduled, revision }: { taskId: string; scheduled: string; revision: number }) => (
      api.patchTask(taskId, { scheduled, revision })
    ),
    onSuccess: (updated) => {
      queryClient.setQueryData<WorkspaceProjection>(['workspace'], (current) => current ? {
        ...current,
        tasks: current.tasks.map((task) => task.id === updated.id ? updated : task),
      } : current)
      void queryClient.invalidateQueries({ queryKey: ['task', updated.id] })
      onNotice(`${updated.id} planned for ${updated.scheduled}`)
    },
    onError: (error) => {
      void queryClient.invalidateQueries({ queryKey: ['workspace'] })
      onNotice(getErrorMessage(error), 'error')
    },
  })

  return (
    <details className="agenda-panel" open>
      <summary>
        <span><strong>Week plan</strong><small>Plan dates are movable; due dates remain deadlines.</small></span>
        <span>{tasks.filter((task) => task.scheduled && !['done', 'dropped'].includes(task.status)).length} planned</span>
      </summary>
      <div className="agenda-panel__calendar" aria-label="Task agenda">
        <FullCalendar
          buttonText={{ list: 'Agenda', month: 'Month', today: 'Today' }}
          dayMaxEvents={3}
          editable={!scheduleMutation.isPending}
          eventClick={(info) => onSelectTask(String(info.event.extendedProps.taskId))}
          eventDrop={(info) => {
            const taskId = String(info.event.extendedProps.taskId)
            const task = tasks.find((item) => item.id === taskId)
            const scheduled = info.event.startStr.slice(0, 10)
            if (!task || info.event.extendedProps.kind !== 'scheduled' || !scheduled) {
              info.revert()
              return
            }
            scheduleMutation.mutate(
              { taskId, scheduled, revision: task.revision },
              { onError: () => info.revert() },
            )
          }}
          events={buildAgendaEvents(tasks)}
          headerToolbar={{ left: 'prev,next today', center: 'title', right: 'listWeek,dayGridMonth' }}
          height="auto"
          initialView="listWeek"
          plugins={[listPlugin, dayGridPlugin, interactionPlugin]}
        />
      </div>
    </details>
  )
}
