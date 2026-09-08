import { Button, LoadingBlock } from '../../components/Primitives'
import { MAX_KNOWLEDGE_REASON_CHARS, MAX_KNOWLEDGE_SEARCH_QUERY_CHARS } from './knowledgeTypes'
import {
  previewMatchesInput,
  type KnowledgePanelState,
  type KnowledgeSearchState,
} from './knowledgeSession'
import { LOCAL_MARKDOWN_SOURCE, localExcerptView } from './knowledgeSourceView'
import type { KnowledgeReadReference, KnowledgeSavedReference, KnowledgeTaskRef } from './knowledgeTypes'
import { ReferenceExcerptCard } from './ReferenceRows'
import { ReferenceHandoffPanel } from './ReferenceHandoffPanel'
import { UNBOUND_RESUME_PROGRESS, type ResumeProgressFacts } from './resumeProgressContract'
import { useTaskKnowledge } from './useTaskKnowledge'
import './TaskKnowledgePanel.css'

export interface TaskKnowledgePanelProps {
  task: KnowledgeTaskRef
  workspaceUid: string
  /** Renders `Back to task` when the drawer opens this panel as a subview. */
  onBack?: () => void
  progress?: ResumeProgressFacts
}

type KnowledgeSession = ReturnType<typeof useTaskKnowledge>

/**
 * Restarts the selection session whenever the saved records themselves change. The records
 * are encoded rather than joined on separators: `reason` is free user text, so a value
 * carrying the separators itself could imitate another record's fields and make a real
 * change sign identically — leaving the panel on a stale list. JSON escapes every
 * character that could shift a boundary.
 */
export function referenceListSignature(references: readonly KnowledgeSavedReference[]) {
  return JSON.stringify(references.map((item) => [
    item.reference_id,
    item.vault_id,
    item.document_path,
    item.start_line,
    item.end_line,
    item.source_sha256,
    item.reason,
  ]))
}

function KnowledgeUnavailable() {
  return (
    <section className="reply-launch reply-launch--unavailable">
      <div>
        <strong>Desktop knowledge host unavailable</strong>
        <p>
          Open this Task in the Work Stack desktop app to connect a knowledge source. The browser
          build cannot read or link documents on this device.
        </p>
      </div>
    </section>
  )
}

function corpusCopy(corpus: KnowledgeSearchState['corpus']) {
  const count = corpus.document_count === 1 ? '1 document' : `${corpus.document_count} documents`
  return `${corpus.label} · ${count} · indexed ${corpus.indexed_at}`
}

function KnowledgeSearchMatch({
  disabled,
  match,
  onSelect,
  vaults,
}: {
  disabled: boolean
  match: KnowledgeReadReference
  onSelect: (match: KnowledgeReadReference) => void
  vaults: KnowledgePanelState['vaults']
}) {
  return (
    <li>
      <ReferenceExcerptCard view={localExcerptView(match, vaults)}>
        <div className="knowledge-panel__actions">
          <Button disabled={disabled} onClick={() => onSelect(match)}>Use this result</Button>
        </div>
      </ReferenceExcerptCard>
    </li>
  )
}

function KnowledgeSearch({
  disabled,
  session,
}: {
  disabled: boolean
  session: KnowledgeSession
}) {
  const { search, searchQuery } = session.state
  return (
    <div className="knowledge-search">
      <h5>Search this source</h5>
      <p>
        Searches the configured corpus snapshot for the selected source. Opening a Task never
        searches. This is not a full index of the source.
      </p>
      <label className="field">
        <span>Search query</span>
        <textarea
          aria-label="Document search query"
          disabled={disabled}
          maxLength={MAX_KNOWLEDGE_SEARCH_QUERY_CHARS}
          onChange={(event) => session.setSearchQuery(event.target.value)}
          placeholder="Words from the saved Task title"
          rows={2}
          value={searchQuery}
        />
      </label>
      <div className="knowledge-panel__actions">
        <Button disabled={disabled || !searchQuery.trim()} onClick={session.searchDocuments}>Search</Button>
      </div>
      {search ? (
        <>
          <p className="knowledge-search__corpus">{corpusCopy(search.corpus)}</p>
          {search.omittedCount > 0 ? (
            <p className="knowledge-search__corpus">
              {search.omittedCount === 1
                ? '1 hit was omitted because it changed, is missing, or is outside this source.'
                : `${search.omittedCount} hits were omitted because they changed, are missing, or are outside this source.`}
            </p>
          ) : null}
          {search.matches.length === 0 ? (
            <p className="knowledge-search__corpus">No matching documents in this corpus snapshot.</p>
          ) : (
            <ul aria-label="Search matches" className="knowledge-panel__matches">
              {search.matches.map((match) => (
                <KnowledgeSearchMatch
                  disabled={disabled}
                  key={`${match.document_path}:${match.start_line}:${match.end_line}:${match.source_sha256}`}
                  match={match}
                  onSelect={session.selectSearchMatch}
                  vaults={session.state.vaults}
                />
              ))}
            </ul>
          )}
        </>
      ) : null}
    </div>
  )
}

function KnowledgeSourceCard({
  disabled,
  session,
}: {
  disabled: boolean
  session: KnowledgeSession
}) {
  const { selectedVaultId, vaults } = session.state
  return (
    <div className="knowledge-source">
      <h5>Knowledge sources</h5>
      <p>
        <strong>{LOCAL_MARKDOWN_SOURCE.connectorName}</strong> is the connector available in this
        build. {LOCAL_MARKDOWN_SOURCE.connectorNote}
      </p>
      <div className="knowledge-panel__row">
        <label className="field">
          <span>Source</span>
          <select
            aria-label="Selected source"
            disabled={disabled || vaults.length === 0}
            onChange={(event) => session.selectVault(event.target.value)}
            value={selectedVaultId}
          >
            {vaults.length === 0 ? <option value="">No source connected</option> : null}
            {vaults.map((vault) => <option key={vault.vault_id} value={vault.vault_id}>{vault.label}</option>)}
          </select>
        </label>
        <Button disabled={disabled} icon="plus" onClick={session.connectVault}>Choose folder</Button>
      </div>
    </div>
  )
}

function KnowledgeComposer({
  canLink,
  disabled,
  session,
}: {
  canLink: boolean
  disabled: boolean
  session: KnowledgeSession
}) {
  const { documentPath, endLine, reason, startLine } = session.state
  return (
    <div className="form-stack">
      <h5>Link a document by location</h5>
      <label className="field">
        <span>Document path <small>source-relative .md</small></span>
        <input
          aria-label="Document relative path"
          disabled={disabled}
          onChange={(event) => session.setDocumentPath(event.target.value)}
          placeholder="projects/review.md"
          value={documentPath}
        />
      </label>
      <div className="knowledge-panel__span">
        <label className="field">
          <span>Start line <small>optional</small></span>
          <input aria-label="Start line" disabled={disabled} inputMode="numeric" min={1} onChange={(event) => session.setStartLine(event.target.value)} placeholder="1" type="number" value={startLine} />
        </label>
        <label className="field">
          <span>End line <small>optional</small></span>
          <input aria-label="End line" disabled={disabled} inputMode="numeric" min={1} onChange={(event) => session.setEndLine(event.target.value)} placeholder="40" type="number" value={endLine} />
        </label>
      </div>
      <label className="field">
        <span>Linked reason <small>required to link</small></span>
        <textarea
          aria-label="Link reason"
          disabled={disabled}
          maxLength={MAX_KNOWLEDGE_REASON_CHARS}
          onChange={(event) => session.setReason(event.target.value)}
          placeholder="Why this document belongs with this Task"
          rows={3}
          value={reason}
        />
      </label>
      <div className="knowledge-panel__actions">
        <Button
          disabled={disabled || !documentPath.trim()}
          onClick={() => session.preview(documentPath, startLine, endLine)}
        >
          Preview
        </Button>
        <Button disabled={disabled || !canLink} onClick={() => session.link(reason)} variant="primary">Link</Button>
      </div>
    </div>
  )
}

function KnowledgeFeedback({ session }: { session: KnowledgeSession }) {
  const { documentPath, endLine, error, errorAction, notice, pending, preview, startLine } = session.state
  return (
    <>
      {preview ? <ReferenceExcerptCard view={localExcerptView(preview, session.state.vaults)} /> : null}
      {pending ? (
        <div className="knowledge-panel__actions">
          <Button onClick={session.cancel} variant="ghost">Cancel</Button>
        </div>
      ) : null}
      {notice ? <p className="knowledge-panel__notice" role="status">{notice}</p> : null}
      {error ? (
        <div className="inline-error" role="alert">
          <span>{error}</span>
          {errorAction === 'refresh-preview' ? (
            <Button onClick={() => session.preview(documentPath, startLine, endLine)} variant="ghost">
              Refresh preview
            </Button>
          ) : null}
          {errorAction === 'reload' ? <Button onClick={session.reload} variant="ghost">Try again</Button> : null}
        </div>
      ) : null}
    </>
  )
}

function ReferenceRowActions({
  busy,
  reference,
  session,
}: {
  busy: boolean
  reference: KnowledgeSavedReference
  session: KnowledgeSession
}) {
  const { opened, vaults } = session.state
  return (
    <>
      <div className="knowledge-panel__row-actions">
        <Button disabled={busy} onClick={() => session.readReference(reference)}>Read</Button>
        <Button disabled={busy} onClick={() => session.unlink(reference.reference_id)} variant="danger">
          Unlink
        </Button>
      </div>
      {opened?.referenceId === reference.reference_id ? (
        <ReferenceExcerptCard view={localExcerptView(opened.read, vaults)} />
      ) : null}
    </>
  )
}

export function TaskKnowledgePanel({
  onBack,
  progress = UNBOUND_RESUME_PROGRESS,
  task,
  workspaceUid,
}: TaskKnowledgePanelProps) {
  const session = useTaskKnowledge(workspaceUid, task)
  const { state } = session
  const busy = state.pending !== null
  const composerDisabled = busy || !state.selectedVaultId
  const canLink = Boolean(state.reason.trim())
    && previewMatchesInput(state.preview, state.selectedVaultId, state.documentPath, state.startLine, state.endLine)

  return (
    <section
      aria-busy={busy}
      aria-label={`Prepare a resume brief for ${task.title}`}
      className="drawer-section knowledge-panel"
    >
      <div className="knowledge-panel__bar">
        {onBack ? <Button onClick={onBack} variant="ghost">Back to task</Button> : null}
        <span className="knowledge-panel__task">{task.id}</span>
      </div>
      <h3>Prepare a resume brief</h3>
      <p className="knowledge-panel__lede">
        Bundle the saved Task, its recorded progress and the references you pick into one brief.
      </p>
      {!state.hostAvailable ? <KnowledgeUnavailable /> : null}
      {state.hostAvailable && state.loading ? <LoadingBlock label="Loading linked references…" /> : null}
      {state.hostAvailable && !state.loading ? (
        <>
          <ReferenceHandoffPanel
            progress={progress}
            renderRowActions={(reference) => (
              <ReferenceRowActions busy={busy} reference={reference} session={session} />
            )}
            seed={{
              references: state.references,
              reload: session.reload,
              signature: referenceListSignature(state.references),
            }}
            task={task}
            vaults={state.vaults}
            workspaceUid={workspaceUid}
          />
          <details className="knowledge-panel__disclosure">
            <summary>Find or link a document</summary>
            <div className="knowledge-panel__disclosure-body">
              <KnowledgeSourceCard disabled={state.pending === 'load'} session={session} />
              <KnowledgeSearch disabled={composerDisabled} session={session} />
              <KnowledgeComposer canLink={canLink} disabled={composerDisabled} session={session} />
              <KnowledgeFeedback session={session} />
            </div>
          </details>
        </>
      ) : null}
      <p className="knowledge-panel__scope">{LOCAL_MARKDOWN_SOURCE.scopeNote}</p>
    </section>
  )
}
