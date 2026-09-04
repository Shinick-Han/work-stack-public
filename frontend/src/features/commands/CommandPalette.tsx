import { useEffect, useMemo, useState, type KeyboardEvent } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../../api/client'
import { Dialog } from '../../components/Dialog'
import { Icon, type IconName } from '../../components/Icon'
import type { AppUrlState, Task } from '../../domain/types'

type NavigationTarget = Partial<Pick<AppUrlState, 'surface' | 'view'>>

interface CommandPaletteProps {
  onClose: () => void
  onImportCapture: () => void
  onNavigate: (target: NavigationTarget) => void
  onNewTask: () => void
  onOpenCapture: (captureId: string) => void
  onOpenObjective: (objectiveId: string) => void
  onOpenTask: (taskId: string) => void
  open: boolean
  tasks: Task[]
}

interface CommandItem {
  description: string
  icon: IconName
  id: string
  label: string
  run: () => void
}

export function CommandPalette({
  onClose,
  onImportCapture,
  onNavigate,
  onNewTask,
  onOpenCapture,
  onOpenObjective,
  onOpenTask,
  open,
  tasks,
}: CommandPaletteProps) {
  const [query, setQuery] = useState('')
  const [remoteQuery, setRemoteQuery] = useState('')
  const [activeIndex, setActiveIndex] = useState(0)

  useEffect(() => {
    if (!open) return
    setQuery('')
    setRemoteQuery('')
    setActiveIndex(0)
  }, [open])

  useEffect(() => {
    const normalized = query.trim()
    if (!open || normalized.length < 2) { setRemoteQuery(''); return }
    const timer = window.setTimeout(() => setRemoteQuery(normalized), 180)
    return () => window.clearTimeout(timer)
  }, [open, query])

  const searchQuery = useQuery({
    enabled: open && remoteQuery.length >= 2,
    queryKey: ['search', remoteQuery],
    queryFn: () => api.search(remoteQuery),
  })

  const closeAfter = (operation: () => void) => () => {
    operation()
    onClose()
  }

  const commandItems = useMemo<CommandItem[]>(() => [
    {
      id: 'view-graph', label: 'Open Graph view', description: 'Workspace · shortcut 1', icon: 'graph',
      run: closeAfter(() => onNavigate({ surface: 'workspace', view: 'graph' })),
    },
    {
      id: 'view-board', label: 'Open Board view', description: 'Workspace · shortcut 2', icon: 'board',
      run: closeAfter(() => onNavigate({ surface: 'workspace', view: 'board' })),
    },
    {
      id: 'view-treemap', label: 'Open Treemap view', description: 'Workspace · shortcut 3', icon: 'treemap',
      run: closeAfter(() => onNavigate({ surface: 'workspace', view: 'treemap' })),
    },
    {
      id: 'view-table', label: 'Open Table view', description: 'Workspace · shortcut 4', icon: 'table',
      run: closeAfter(() => onNavigate({ surface: 'workspace', view: 'table' })),
    },
    {
      id: 'surface-focus', label: 'Open Focus', description: 'Attention queue · shortcut 5', icon: 'target',
      run: closeAfter(() => onNavigate({ surface: 'focus' })),
    },
    {
      id: 'surface-inbox', label: 'Open Context Inbox', description: 'Sanitized captures · shortcut 6', icon: 'inbox',
      run: closeAfter(() => onNavigate({ surface: 'inbox' })),
    },
    {
      id: 'surface-review', label: 'Open Daily Review', description: 'Execution evidence · shortcut 7', icon: 'activity',
      run: closeAfter(() => onNavigate({ surface: 'review' })),
    },
    {
      id: 'surface-objectives', label: 'Open Objective Hub', description: 'Goals and KRs · shortcut 8', icon: 'target',
      run: closeAfter(() => onNavigate({ surface: 'objectives' })),
    },
    {
      id: 'new-task', label: 'Create a new task', description: 'Open Quick Add', icon: 'plus',
      run: closeAfter(onNewTask),
    },
    {
      id: 'import-capture', label: 'Import a sanitized capture', description: 'Open packet import', icon: 'upload',
      run: closeAfter(onImportCapture),
    },
  ], [onClose, onImportCapture, onNavigate, onNewTask])

  const localTaskItems = useMemo<CommandItem[]>(() => {
    const normalized = query.trim().toLocaleLowerCase()
    const visibleTasks = normalized
      ? tasks.filter((task) => (
        `${task.id} ${task.title} ${task.status} ${task.priority} ${task.due ?? ''}`
          .toLocaleLowerCase()
          .includes(normalized)
      )).slice(0, 30)
      : tasks.slice(0, 20)
    return visibleTasks.map((task): CommandItem => ({
      id: `task-${task.id}`,
      label: `${task.id} · ${task.title}`,
      description: `${task.status} · ${task.priority}${task.due ? ` · due ${task.due}` : ''}`,
      icon: 'task',
      run: closeAfter(() => onOpenTask(task.id)),
    }))
  }, [onClose, onOpenTask, query, tasks])

  const remoteItems = useMemo<CommandItem[]>(() => (searchQuery.data?.items ?? []).map((item) => ({
    id: `search-${item.kind}-${item.id}`,
    label: `${item.id} · ${item.title}`,
    description: `${item.kind} · ${item.subtitle.replace(/^Graph note(?=\s*·|$)/, 'Context card')}`,
    icon: item.kind === 'task' ? 'task' : item.kind === 'objective' ? 'target' : item.kind === 'capture' ? 'inbox' : item.kind === 'activity' ? 'activity' : 'context',
    run: closeAfter(() => {
      if (item.target_kind === 'task' && item.target_id) onOpenTask(item.target_id)
      else if (item.target_kind === 'objective' && item.target_id) onOpenObjective(item.target_id)
      else if (item.target_kind === 'capture' && item.target_id) onOpenCapture(item.target_id)
      else onNavigate({ surface: 'workspace', view: 'graph' })
    }),
  })), [onClose, onNavigate, onOpenCapture, onOpenObjective, onOpenTask, searchQuery.data])

  const visibleItems = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase()
    if (!normalized) return [...commandItems, ...localTaskItems]
    const commands = commandItems.filter((item) => `${item.label} ${item.description}`.toLocaleLowerCase().includes(normalized))
    if (normalized.length < 2 || !searchQuery.data) return [...commands, ...localTaskItems]
    const remoteTaskIds = new Set(remoteItems.filter((item) => item.id.startsWith('search-task-')).map((item) => item.id.slice('search-task-'.length)))
    return [
      ...commands,
      ...remoteItems,
      ...localTaskItems.filter((item) => !remoteTaskIds.has(item.id.slice('task-'.length))),
    ].slice(0, 50)
  }, [commandItems, localTaskItems, query, remoteItems, searchQuery.data])

  useEffect(() => {
    setActiveIndex((current) => Math.min(current, Math.max(visibleItems.length - 1, 0)))
  }, [visibleItems.length])

  if (!open) return null

  const handleKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'ArrowDown') {
      event.preventDefault()
      setActiveIndex((current) => visibleItems.length ? (current + 1) % visibleItems.length : 0)
    } else if (event.key === 'ArrowUp') {
      event.preventDefault()
      setActiveIndex((current) => visibleItems.length ? (current - 1 + visibleItems.length) % visibleItems.length : 0)
    } else if (event.key === 'Enter' && visibleItems[activeIndex]) {
      event.preventDefault()
      visibleItems[activeIndex].run()
    }
  }

  return (
    <Dialog
      description="Navigate, open a task, or start a common action without leaving the keyboard."
      onClose={onClose}
      open={open}
      size="medium"
      title="Search or jump"
    >
      <div className="command-palette">
        <label className="command-palette__search">
          <Icon name="search" size={17} />
          <input
            aria-activedescendant={visibleItems[activeIndex]?.id}
            aria-controls="command-palette-results"
            aria-label="Search commands and workspace"
            autoComplete="off"
            autoFocus
            onChange={(event) => { setQuery(event.target.value); setActiveIndex(0) }}
            onKeyDown={handleKeyDown}
            placeholder="Search Tasks, Objectives, Context cards…"
            role="searchbox"
            value={query}
          />
          <kbd>Esc</kbd>
        </label>

        <div aria-label="Commands and workspace results" className="command-palette__results" id="command-palette-results" role="listbox">
          {visibleItems.length ? visibleItems.map((item, index) => (
            <button
              aria-selected={index === activeIndex}
              className={index === activeIndex ? 'is-active' : ''}
              id={item.id}
              key={item.id}
              onClick={item.run}
              onMouseEnter={() => setActiveIndex(index)}
              role="option"
              type="button"
            >
              <span className="command-palette__icon"><Icon name={item.icon} size={16} /></span>
              <span><strong>{item.label}</strong><small>{item.description}</small></span>
            </button>
          )) : <p className="command-palette__empty">{searchQuery.isFetching ? 'Searching the local workspace…' : 'No matching command or workspace record.'}</p>}
        </div>
        <footer className="command-palette__help"><span><kbd>↑</kbd><kbd>↓</kbd> move</span><span><kbd>↵</kbd> open</span></footer>
      </div>
    </Dialog>
  )
}
