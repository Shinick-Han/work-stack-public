import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import type { Capture, TaskStatus, WorkSessionEntryInput, WorkspaceProjection } from '../domain/types'
import { FocusPage } from '../features/focus/FocusPage'
import { useLocalToday } from '../features/focus/useLocalToday'
import { DailyReviewPage } from '../features/review/DailyReviewPage'
import { getErrorMessage } from '../utils/format'

export function FocusSurface({
  isRefreshing,
  onChangeTaskStatus,
  onCreateTask,
  onRefresh,
  onSelectTask,
  onNotice,
  workspace,
}: {
  isRefreshing: boolean
  onChangeTaskStatus: (taskId: string, status: TaskStatus) => Promise<void>
  onCreateTask: () => void
  onRefresh: () => void
  onSelectTask: (taskId: string) => void
  onNotice: (message: string, tone?: 'success' | 'error') => void
  workspace: WorkspaceProjection
}) {
  const today = useLocalToday()
  const queryClient = useQueryClient()
  const workSessionsQuery = useQuery({
    queryKey: ['work-sessions'],
    queryFn: () => api.getWorkSessions(),
  })
  const workSessionMutation = useMutation({
    mutationFn: async (operation:
      | { kind: 'start'; taskId: string }
      | { kind: 'transition'; sessionId: string; action: 'pause' | 'resume' | 'stop' }
      | { kind: 'record'; sessionId: string; input: WorkSessionEntryInput }
    ) => {
      if (operation.kind === 'start') return { kind: operation.kind, result: await api.startWorkSession(operation.taskId) }
      if (operation.kind === 'transition') return { kind: operation.kind, action: operation.action, result: await api.transitionWorkSession(operation.sessionId, operation.action) }
      return { kind: operation.kind, result: await api.recordWorkSession(operation.sessionId, operation.input) }
    },
    onSuccess: async (completed) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['work-sessions'] }),
        queryClient.invalidateQueries({ queryKey: ['review'] }),
      ])
      const message = completed.kind === 'start'
        ? 'Work session started'
        : completed.kind === 'record'
          ? 'Worklog entry recorded'
          : completed.action === 'pause'
            ? 'Work session paused'
            : completed.action === 'resume'
              ? 'Work session resumed'
              : 'Work session stopped — review the Worklog draft'
      onNotice(message)
    },
    onError: (error) => onNotice(getErrorMessage(error), 'error'),
  })

  return (
    <FocusPage
      isRefreshing={isRefreshing}
      onChangeTaskStatus={onChangeTaskStatus}
      onCreateTask={onCreateTask}
      onRefresh={onRefresh}
      onNotice={onNotice}
      onSelectTask={onSelectTask}
      isWorkSessionPending={workSessionMutation.isPending}
      onRecordWorkSession={(sessionId, input) => workSessionMutation.mutateAsync({ kind: 'record', sessionId, input }).then(() => undefined)}
      onStartWorkSession={(taskId) => workSessionMutation.mutateAsync({ kind: 'start', taskId }).then(() => undefined)}
      onTransitionWorkSession={(sessionId, action) => workSessionMutation.mutateAsync({ kind: 'transition', sessionId, action }).then(() => undefined)}
      today={today}
      workSessionError={workSessionsQuery.isError ? getErrorMessage(workSessionsQuery.error) : undefined}
      workSessions={workSessionsQuery.data}
      workspace={workspace}
    />
  )
}

export function ReviewSurface({
  captures,
  onNotice,
  onOpenCapture,
  onOpenTask,
  workspace,
}: {
  captures: Capture[]
  onNotice: (message: string, tone?: 'success' | 'error') => void
  onOpenCapture: (captureId: string) => void
  onOpenTask: (taskId: string) => void
  workspace: WorkspaceProjection
}) {
  const today = useLocalToday()
  return <DailyReviewPage captures={captures} onNotice={onNotice} onOpenCapture={onOpenCapture} onOpenTask={onOpenTask} today={today} workspace={workspace} />
}
