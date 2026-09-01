import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { Button, EmptyState, Pill } from '../../components/Primitives'
import { Icon } from '../../components/Icon'
import {
  anyProviderReadVerified,
  microsoftProviderGates,
  type MicrosoftProviderGates,
} from '../../config/providerGates'
import type { Capture, CaptureStatus, Task, WorkspaceProjection } from '../../domain/types'
import { formatDateTime, getErrorMessage, safeExternalUrl } from '../../utils/format'
import { captureTrust } from './captureTrust'
import { SourceCaptureDialog } from './SourceCaptureDialog'
import { sourceProviders, type SourceProviderKey } from './sourceProviders'
import { embeddedSourceHostAvailable, hideEmbeddedSource, requestEmbeddedSourceDraft, requestEmbeddedSourceZoom, setEmbeddedSourceZoom, showEmbeddedSource, subscribeEmbeddedSourceZoom, type EmbeddedSourceZoom } from './sourceHostBridge'
import {
  EXTERNAL_CAPTURE_ACK,
  EXTERNAL_CAPTURE_MESSAGE,
  parseExternalSourceCapture,
  sanitizeMicrosoftSourceUrl,
  type ExternalSourceCapture,
  type SourceCaptureDraft,
} from './sourceCapture'

interface InboxPageProps {
  captures: Capture[]
  workspace: WorkspaceProjection
  selectedCaptureId: string | null
  search: string
  onSearchChange: (value: string) => void
  onSelectCapture: (captureId: string) => void
  onImport: () => void
  onCopyMicrosoftRequest: () => void
  onImportAgentResult: () => void
  onLink: (captureId: string, taskId: string) => Promise<unknown>
  onConvert: (captureId: string, actionId: string) => Promise<unknown>
  onDismiss: (captureId: string) => Promise<unknown>
  onCreateSourceTask: (draft: SourceCaptureDraft) => Promise<unknown>
  providerGates?: MicrosoftProviderGates
}

const statusOptions: Array<'all' | CaptureStatus> = ['inbox', 'linked', 'converted', 'dismissed', 'all']

function sourceLabel(provider: string, resourceType = '') {
  if (provider === 'manual' && resourceType.startsWith('microsoft-web.')) {
    const key = resourceType.slice('microsoft-web.'.length)
    return key ? key[0].toUpperCase() + key.slice(1) : 'Microsoft web'
  }
  if (provider.includes('outlook')) return 'Outlook'
  if (provider.includes('teams')) return 'Teams'
  if (provider.includes('sharepoint')) return 'SharePoint'
  if (provider === 'manual') return 'Manual'
  return provider
}

function CaptureActionControl({ action, onCreate, pending }: { action: Capture['normalized']['action_items'][number]; onCreate: (actionId: string) => void; pending: string | null }) {
  if (action.task_id) return <span>Linked to {action.task_id}</span>
  return <Button
    disabled={!action.id || pending !== null}
    onClick={(event) => {
      event.stopPropagation()
      if (action.id) onCreate(action.id)
    }}
    variant="ghost"
  >{pending === `action-${action.id}` ? 'Creating…' : 'Create task'}</Button>
}

function CaptureCardHeader({ capture, onSelect }: { capture: Capture; onSelect: () => void }) {
  return <header className="capture-card__header">
    <span className="source-avatar"><Icon name={capture.source.provider === 'manual' ? 'context' : 'inbox'} size={17} /></span>
    <div>
      <div className="capture-card__source">
        <strong>{sourceLabel(capture.source.provider, capture.source.resource_type)}</strong>
        <span>·</span>
        <span>{capture.source.resource_type}</span>
      </div>
      <time>{formatDateTime(capture.source.retrieved_at)}</time>
    </div>
    <div className="capture-card__header-actions">
      <Pill tone={capture.status}>{capture.status}</Pill>
      <button aria-label={`Inspect ${capture.id}`} className="capture-inspect" onClick={onSelect} type="button"><Icon name="more" size={16} /></button>
    </div>
  </header>
}

function CaptureCardBody({ capture, providerGates, onSelect }: {
  capture: Capture
  providerGates: MicrosoftProviderGates
  onSelect: () => void
}) {
  const trust = captureTrust(capture, providerGates)
  return <div className="capture-card__body">
    <h2><button onClick={onSelect} type="button">{capture.source.display_title}</button></h2>
    <p>{capture.normalized.summary}</p>
    <div className="capture-card__meta">
      <Pill tone={trust.tone}>{trust.label}</Pill>
      <span>{capture.normalized.action_items.length} action item{capture.normalized.action_items.length === 1 ? '' : 's'}</span>
      {capture.task_hints.length ? <span>Suggested: {capture.task_hints.join(', ')}</span> : null}
    </div>
  </div>
}

function CaptureActionList({ capture, onCreate, pending }: {
  capture: Capture
  onCreate: (actionId: string) => void
  pending: string | null
}) {
  if (!capture.normalized.action_items.length) return null
  return <div className="capture-actions">
    {capture.normalized.action_items.map((action, index) => (
      <div className="capture-action" key={action.id ?? `${capture.id}-${index}`}>
        <span className="capture-action__check"><Icon name="check" size={13} /></span>
        <div><strong>{action.title}</strong><small>{action.priority}{action.due ? ` · due ${action.due}` : ''}</small></div>
        <CaptureActionControl action={action} onCreate={onCreate} pending={pending} />
      </div>
    ))}
  </div>
}

function CaptureCardFooter({ capture, onDismiss, onLink, pending, run, setTaskId, sourceUrl, taskId, tasks }: {
  capture: Capture
  onDismiss: () => Promise<unknown>
  onLink: (taskId: string) => Promise<unknown>
  pending: string | null
  run: (key: string, operation: () => Promise<unknown>) => Promise<void>
  setTaskId: (taskId: string) => void
  sourceUrl: string | null
  taskId: string
  tasks: Task[]
}) {
  return <footer className="capture-card__footer" onClick={(event) => event.stopPropagation()}>
    {sourceUrl ? (
      <a className="button button--ghost" href={sourceUrl} rel="noopener noreferrer" target="_blank">
        <Icon name="arrowUpRight" size={15} /> Open source
      </a>
    ) : <span className="source-unavailable">No source link</span>}
    <div className="capture-card__link">
      <label>
        <span className="sr-only">Task to link</span>
        <select onChange={(event) => setTaskId(event.target.value)} value={taskId}>
          {tasks.map((task) => <option key={task.id} value={task.id}>{task.id} · {task.title}</option>)}
        </select>
      </label>
      <Button
        disabled={!taskId || pending !== null}
        onClick={() => void run('link', () => onLink(taskId))}
      >{pending === 'link' ? 'Linking…' : 'Link to task'}</Button>
      {capture.status !== 'dismissed' ? (
        <Button
          disabled={pending !== null}
          onClick={() => void run('dismiss', onDismiss)}
          variant="ghost"
        >{pending === 'dismiss' ? 'Dismissing…' : 'Dismiss'}</Button>
      ) : null}
    </div>
  </footer>
}

function CaptureCard({
  capture,
  isSelected,
  onConvert,
  onDismiss,
  onLink,
  onSelect,
  tasks,
  providerGates,
}: {
  capture: Capture
  isSelected: boolean
  tasks: Task[]
  providerGates: MicrosoftProviderGates
  onSelect: () => void
  onLink: (taskId: string) => Promise<unknown>
  onConvert: (actionId: string) => Promise<unknown>
  onDismiss: () => Promise<unknown>
}) {
  const hintedTask = capture.task_hints.find((id) => tasks.some((task) => task.id === id))
  const [taskId, setTaskId] = useState(hintedTask ?? tasks[0]?.id ?? '')
  const [pending, setPending] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const sourceUrl = safeExternalUrl(capture.source.web_url)

  const run = async (key: string, operation: () => Promise<unknown>) => {
    setPending(key)
    setError(null)
    try {
      await operation()
    } catch (operationError) {
      setError(getErrorMessage(operationError))
    } finally {
      setPending(null)
    }
  }

  const createAction = (actionId: string) => {
    void run(`action-${actionId}`, () => onConvert(actionId))
  }

  return (
    <article aria-current={isSelected ? 'true' : undefined} className={`capture-card ${isSelected ? 'is-selected' : ''}`}>
      <CaptureCardHeader capture={capture} onSelect={onSelect} />
      <CaptureCardBody capture={capture} onSelect={onSelect} providerGates={providerGates} />
      <CaptureActionList capture={capture} onCreate={createAction} pending={pending} />

      {error ? <p className="inline-error" role="alert">{error}</p> : null}
      <CaptureCardFooter
        capture={capture}
        onDismiss={onDismiss}
        onLink={onLink}
        pending={pending}
        run={run}
        setTaskId={setTaskId}
        sourceUrl={sourceUrl}
        taskId={taskId}
        tasks={tasks}
      />
    </article>
  )
}

export function InboxPage({
  captures,
  onConvert,
  onDismiss,
  onCreateSourceTask,
  onImport,
  onCopyMicrosoftRequest,
  onImportAgentResult,
  onLink,
  onSearchChange,
  onSelectCapture,
  providerGates = microsoftProviderGates,
  search,
  selectedCaptureId,
  workspace,
}: InboxPageProps) {
  const [status, setStatus] = useState<'all' | CaptureStatus>('inbox')
  const [sourceDialogOpen, setSourceDialogOpen] = useState(false)
  const [sourceDialogProvider, setSourceDialogProvider] = useState<SourceProviderKey>('outlook')
  const [sourceDialogSeed, setSourceDialogSeed] = useState<ExternalSourceCapture | null>(null)
  const [activeSourceProvider, setActiveSourceProvider] = useState<SourceProviderKey>('outlook')
  const [embeddedSourceHost] = useState(embeddedSourceHostAvailable)
  const [sourceZoom, setSourceZoom] = useState<EmbeddedSourceZoom>({ outlook: 100, teams: 100, onenote: 100 })
  const sourceHostRef = useRef<HTMLDivElement>(null)
  const microsoftReadAvailable = anyProviderReadVerified(providerGates)
  const visibleCaptures = useMemo(() => {
    const query = search.trim().toLowerCase()
    return captures.filter((capture) => {
      const haystack = [
        capture.id,
        capture.source.provider,
        capture.source.display_title,
        capture.normalized.summary,
        capture.normalized.context,
        ...capture.normalized.tags,
      ].join(' ').toLowerCase()
      return (status === 'all' || capture.status === status) && (!query || haystack.includes(query))
    })
  }, [captures, search, status])

  const openSourceDraft = async (provider: SourceProviderKey, seed: ExternalSourceCapture | null = null) => {
    let resolvedSeed = seed
    if (!resolvedSeed && embeddedSourceHost) {
      const sourceDraft = await requestEmbeddedSourceDraft(provider)
      const currentUrl = sanitizeMicrosoftSourceUrl(sourceDraft?.url ?? '') ?? ''
      const copiedText = sourceDraft?.text.trim().slice(0, 4000) ?? ''
      const suggestedTitle = sourceDraft?.title.trim().slice(0, 500)
        || copiedText.split(/\r?\n/).map((line) => line.trim()).find(Boolean)?.slice(0, 500)
        || ''
      resolvedSeed = {
        provider,
        title: suggestedTitle,
        text: copiedText,
        sourceUrl: currentUrl,
        capturedAt: new Date().toISOString(),
      }
    }
    setSourceDialogProvider(provider)
    setSourceDialogSeed(resolvedSeed)
    setSourceDialogOpen(true)
  }

  useEffect(() => {
    const receiveCapture = (event: MessageEvent) => {
      if (event.source !== window || event.origin !== window.location.origin) return
      const message = event.data as { type?: unknown; token?: unknown; payload?: unknown } | null
      if (!message || message.type !== EXTERNAL_CAPTURE_MESSAGE) return
      const parsed = parseExternalSourceCapture(message.payload)
      if (!parsed) return
      void openSourceDraft(parsed.provider, parsed)
      window.postMessage({ type: EXTERNAL_CAPTURE_ACK, token: typeof message.token === 'string' ? message.token : '' }, window.location.origin)
    }
    window.addEventListener('message', receiveCapture)
    return () => window.removeEventListener('message', receiveCapture)
  }, [])

  useLayoutEffect(() => {
    if (!embeddedSourceHost) return
    let frame = 0
    const updateHost = () => {
      frame = 0
      const host = sourceHostRef.current
      if (!host) return
      showEmbeddedSource(activeSourceProvider, host.getBoundingClientRect())
    }
    const scheduleUpdate = () => {
      if (frame) cancelAnimationFrame(frame)
      frame = requestAnimationFrame(updateHost)
    }
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(scheduleUpdate)
    if (sourceHostRef.current) observer?.observe(sourceHostRef.current)
    window.addEventListener('resize', scheduleUpdate)
    window.addEventListener('scroll', scheduleUpdate, true)
    updateHost()
    return () => {
      if (frame) cancelAnimationFrame(frame)
      observer?.disconnect()
      window.removeEventListener('resize', scheduleUpdate)
      window.removeEventListener('scroll', scheduleUpdate, true)
      hideEmbeddedSource()
    }
  }, [activeSourceProvider, embeddedSourceHost])

  useEffect(() => {
    if (!embeddedSourceHost) return
    const unsubscribe = subscribeEmbeddedSourceZoom(setSourceZoom)
    requestEmbeddedSourceZoom()
    return unsubscribe
  }, [embeddedSourceHost])

  const activeProviderDefinition = sourceProviders.find((provider) => provider.key === activeSourceProvider)!

  return (
    <section className="inbox-page" aria-labelledby="inbox-heading">
      <header className="page-heading page-heading--compact">
        <div>
          <div className="eyebrow"><Icon name="context" size={14} /> Context pipeline</div>
          <h1 id="inbox-heading">Turn signal into useful work.</h1>
          <p>Review sanitized context before it becomes part of your execution system.</p>
        </div>
        <div className="page-heading__actions inbox-heading-actions">
          {microsoftReadAvailable ? <Button icon="command" onClick={onCopyMicrosoftRequest} variant="primary">Copy Microsoft 365 request</Button> : null}
          {microsoftReadAvailable ? <Button icon="upload" onClick={onImportAgentResult}>Import agent result</Button> : null}
          <Button icon="upload" onClick={onImport} variant="ghost">Import packet</Button>
        </div>
      </header>

      <section className={`source-provider-dock ${embeddedSourceHost ? 'source-provider-dock--embedded' : ''}`} aria-labelledby="source-provider-heading">
        <header><div><span className="eyebrow"><Icon name="spark" size={13} /> Source adapters</span><h2 id="source-provider-heading">Use Microsoft where the conversation already lives.</h2></div><p>{embeddedSourceHost ? 'The original Microsoft web app stays inside Source Inbox. Copy only the useful context, then review it before creating planning state.' : 'Open the original web app, select only the useful execution context, then review it here before creating planning state.'}</p></header>
        {embeddedSourceHost ? (
          <div className="embedded-source-browser">
            <div aria-label="Microsoft source" className="embedded-source-tabs" role="tablist">
              {sourceProviders.map((provider) => (
                <button
                  aria-controls="embedded-source-surface"
                  aria-selected={provider.key === activeSourceProvider}
                  className={provider.key === activeSourceProvider ? 'is-active' : ''}
                  key={provider.key}
                  onClick={() => setActiveSourceProvider(provider.key)}
                  role="tab"
                  type="button"
                >
                  <span className={`source-provider-mark source-provider-mark--${provider.key}`}><Icon name={provider.key === 'onenote' ? 'context' : 'inbox'} size={16} /></span>
                  <span><strong>{provider.label}</strong><small>{provider.captureMode === 'selection' ? 'Select or copy' : 'Copy explicitly'}</small></span>
                </button>
              ))}
            </div>
            <div aria-label={`${activeProviderDefinition.label} web app`} className="embedded-source-surface" id="embedded-source-surface" ref={sourceHostRef} role="tabpanel">
              <div className="embedded-source-placeholder"><Icon name="inbox" size={22} /><span>Loading {activeProviderDefinition.label} inside Work Stack…</span></div>
            </div>
            <footer className="embedded-source-actions">
              <div><strong>{activeProviderDefinition.label} → Task</strong><span>{activeSourceProvider === 'outlook' ? 'Open a message, then capture its visible subject and body for review.' : 'Copy the selected message or note, then review the Task draft before saving.'}</span></div>
              <div aria-label={`${activeProviderDefinition.label} zoom`} className="source-zoom-control" role="group">
                <button aria-label={`Zoom out ${activeProviderDefinition.label}`} disabled={sourceZoom[activeSourceProvider] <= 50} onClick={() => setEmbeddedSourceZoom(activeSourceProvider, Math.max(50, sourceZoom[activeSourceProvider] - 10))} type="button">−</button>
                <output aria-live="polite">{sourceZoom[activeSourceProvider]}%</output>
                <button aria-label={`Zoom in ${activeProviderDefinition.label}`} disabled={sourceZoom[activeSourceProvider] >= 200} onClick={() => setEmbeddedSourceZoom(activeSourceProvider, Math.min(200, sourceZoom[activeSourceProvider] + 10))} type="button">+</button>
                <button aria-label={`Reset ${activeProviderDefinition.label} zoom`} disabled={sourceZoom[activeSourceProvider] === 100} onClick={() => setEmbeddedSourceZoom(activeSourceProvider, 100)} type="button">Reset</button>
              </div>
              <a className="button button--ghost" href={activeProviderDefinition.webUrl} rel="noopener noreferrer" target="_blank"><Icon name="arrowUpRight" size={15} /> Open separately</a>
              <Button aria-label={`Capture ${activeProviderDefinition.label} source`} onClick={() => void openSourceDraft(activeSourceProvider)} variant="primary">Capture source</Button>
            </footer>
          </div>
        ) : (
          <div className="source-provider-grid">
            {sourceProviders.map((provider) => (
              <article className="source-provider-card" key={provider.key}>
                <div className={`source-provider-mark source-provider-mark--${provider.key}`}><Icon name={provider.key === 'onenote' ? 'context' : 'inbox'} size={19} /></div>
                <div><h3>{provider.label}</h3><p>{provider.description}</p><small>{provider.captureMode === 'selection' ? 'Selection first' : 'Copy explicitly · no clipboard monitoring'}</small></div>
                <div className="source-provider-actions"><a className="button button--secondary" href={provider.webUrl} rel="noopener noreferrer" target="_blank"><Icon name="arrowUpRight" size={15} /> Open web app</a><Button aria-label={`Capture copied ${provider.label} content`} onClick={() => void openSourceDraft(provider.key)} variant="ghost">Review capture</Button></div>
              </article>
            ))}
          </div>
        )}
      </section>

      <div className="inbox-summary">
        <div><span className="inbox-summary__icon"><Icon name="inbox" /></span><strong>{captures.filter((capture) => capture.status === 'inbox').length}</strong><span>Awaiting review</span></div>
        <div><span className="inbox-summary__icon"><Icon name="context" /></span><strong>{captures.filter((capture) => capture.status === 'linked').length}</strong><span>Linked context</span></div>
        <div><span className="inbox-summary__icon"><Icon name="task" /></span><strong>{captures.reduce((total, capture) => total + capture.converted_task_ids.length, 0)}</strong><span>Tasks created</span></div>
      </div>

      <div className="inbox-toolbar">
        <div className="segmented-control" aria-label="Capture status">
          {statusOptions.map((option) => (
            <button
              className={status === option ? 'is-active' : ''}
              key={option}
              onClick={() => setStatus(option)}
              type="button"
            >
              {option === 'all' ? 'All' : option[0].toUpperCase() + option.slice(1)}
              <span>{option === 'all' ? captures.length : captures.filter((capture) => capture.status === option).length}</span>
            </button>
          ))}
        </div>
        <label className="search-control inbox-search">
          <span className="sr-only">Search context inbox</span>
          <Icon name="search" size={16} />
          <input
            onChange={(event) => onSearchChange(event.target.value)}
            placeholder="Search context…"
            type="search"
            value={search}
          />
        </label>
      </div>

      <div className="capture-list">
        {visibleCaptures.length ? visibleCaptures.map((capture) => (
          <CaptureCard
            capture={capture}
            isSelected={capture.id === selectedCaptureId}
            key={capture.id}
            onConvert={(actionId) => onConvert(capture.id, actionId)}
            onDismiss={() => onDismiss(capture.id)}
            onLink={(taskId) => onLink(capture.id, taskId)}
            onSelect={() => onSelectCapture(capture.id)}
            providerGates={providerGates}
            tasks={workspace.tasks}
          />
        )) : (
          <EmptyState
            action={<Button icon="upload" onClick={onImport}>Import sanitized packet</Button>}
            icon={status === 'inbox' ? 'check' : 'search'}
            title={status === 'inbox' && !search ? 'Inbox zero' : 'No captures found'}
          >
            {status === 'inbox' && !search
              ? 'Every captured signal has been reviewed.'
              : 'Try a different status or search term.'}
          </EmptyState>
        )}
      </div>
      <SourceCaptureDialog
        onClose={() => setSourceDialogOpen(false)}
        onSubmit={onCreateSourceTask}
        open={sourceDialogOpen}
        provider={sourceDialogProvider}
        seed={sourceDialogSeed}
        sourceUrlManaged={embeddedSourceHost}
        workspace={workspace}
      />
    </section>
  )
}
