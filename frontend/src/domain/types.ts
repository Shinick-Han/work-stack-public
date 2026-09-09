import type { DoneVisibility } from './workspaceFilterTypes'

export type { DoneVisibility }

export const TASK_STATUSES = ['open', 'started', 'done', 'dropped'] as const
export const TASK_PRIORITIES = ['P0', 'P1', 'P2', 'P3'] as const
export const WORKSPACE_VIEWS = ['graph', 'board', 'treemap', 'table'] as const
export const CAPTURE_STATUSES = ['inbox', 'linked', 'converted', 'dismissed'] as const
export const MICROSOFT_PROVIDERS = ['microsoft-outlook', 'microsoft-teams'] as const
export const REPLY_STATES = ['approved', 'sent', 'failed', 'unknown'] as const
export const REPLY_OUTCOMES = ['sent', 'failed', 'unknown'] as const

export type TaskStatus = (typeof TASK_STATUSES)[number]
export type TaskPriority = (typeof TASK_PRIORITIES)[number]
export type WorkspaceView = (typeof WORKSPACE_VIEWS)[number]
export type CaptureStatus = (typeof CAPTURE_STATUSES)[number]
export type MicrosoftProvider = (typeof MICROSOFT_PROVIDERS)[number]
export type ReplyState = (typeof REPLY_STATES)[number]
export type ReplyOutcome = (typeof REPLY_OUTCOMES)[number]

export interface TaskNote {
  date?: string
  text: string
}

export interface Subtask {
  id: string
  title: string
  priority?: TaskPriority
  status?: TaskStatus
}

export interface TaskKeyResultRef {
  objective_id: string
  key_result_id: string
}

export interface Task {
  id: string
  uid: string
  title: string
  detail: string
  status: TaskStatus
  priority: TaskPriority
  due: string | null
  scheduled?: string | null
  estimate_minutes?: number | null
  tags: string[]
  objective_ids: string[]
  key_result_refs?: TaskKeyResultRef[]
  parent_id: string | null
  dependencies: string[]
  subtasks: Subtask[]
  notes: TaskNote[]
  created?: string
  updated_at?: string
  revision: number
  context_count: number
}

export interface KeyResult {
  id: string
  text: string
  target?: string
  progress?: number
  status?: string
}

export interface Objective {
  id: string
  objective: string
  title?: string
  quarter?: string
  status?: string
  key_results?: KeyResult[]
  created?: string
  updated_at?: string
  revision: number
}

export interface ObjectiveActivity {
  id: string
  type: string
  created_at: string
  details: Record<string, unknown>
}

export interface ObjectiveDetail {
  objective: Objective
  tasks: Task[]
  activity: ObjectiveActivity[]
}

export interface Note {
  id: string
  text: string
  links: string[]
  created?: string
}

export interface WorkspaceEdge {
  source: string
  target: string
  kind: string
  [key: string]: unknown
}

export interface WorkspaceProjection {
  schema_version: '1.0'
  workspace: { id: string; name: string }
  tasks: Task[]
  objectives: Objective[]
  notes: Note[]
  edges: WorkspaceEdge[]
  inbox_count: number
}

export interface StorageStatus {
  workspace_id: string
  store_schema_version: number
  product_version: string
  remote_protocol_version: number
  file_count: number
  total_bytes: number
  backup_format: 'workstack-backup-v1'
  restore_requires_shutdown: true
}

export const SYNC_STATES = [
  'in-sync',
  'refreshing',
  'agent-update',
  'agent-update-committed',
  'external-change-detected',
  'invalid',
  'external-change-invalid',
  'disconnected',
  'stale',
] as const

export type SyncState = (typeof SYNC_STATES)[number]

/**
 * A content-free summary of the authoritative SSOT connection. It intentionally
 * carries file names and hashes, never Task fields or source content.
 */
export interface SyncStatus {
  state: SyncState
  workspace_id: string
  candidate_workspace_id: string
  generation: number
  manifest_digest: string | null
  changed_files: string[]
  reason: string | null
  rebind_available: boolean
}

export interface WorkspaceRebindPreview {
  state: 'workspace-identity-mismatch'
  manifest_workspace_id: string
  candidate_workspace_id: string
  manifest_digest: string
  candidate_digest: string
  changed_files: string[]
}

export interface WorkspaceRebindResult {
  state: 'in-sync'
  workspace_id: string
  generation: number
  recovery_receipt_digest: string
  planning_mutated: false
}

export interface BackupDownload {
  blob: Blob
  digest: string
  filename: string
}

export interface CaptureSource {
  provider: string
  resource_type: string
  connection_ref: string
  container_ref: string
  object_ref: string
  version_ref: string
  display_title: string
  web_url: string | null
  retrieved_at: string
  fingerprint: string
}

export interface CaptureAction {
  id?: string
  task_id?: string
  title: string
  detail: string
  priority: TaskPriority
  due: string | null
}

export interface CaptureNormalized {
  summary: string
  context: string
  action_items: CaptureAction[]
  tags: string[]
}

export interface ManualProvenance {
  capture_mode: 'manual'
  adapter: string
  adapter_version: string
  redaction_policy_version: string
  raw_retained: false
  created_at: string
}

export interface VerifiedProvenance {
  capture_mode: 'oob_verified'
  adapter: string
  adapter_version: string
  model: string
  prompt_version: string
  redaction_policy_version: string
  tool_trace_digest: string
  allowed_tools: string[]
  raw_retained: false
  created_at: string
}

export type CaptureProvenance = ManualProvenance | VerifiedProvenance

export interface CapturePacket {
  schema_version: '1.0'
  source_key: string
  source: CaptureSource
  normalized: CaptureNormalized
  task_hints: string[]
  provenance: CaptureProvenance
}

export const RETRIEVAL_SOURCE_TYPES = ['notion.page', 'nas.file', 'knowledge.answer'] as const
export const RETRIEVAL_ANSWER_SCOPES = ['single_source', 'synthesized'] as const
export const RETRIEVAL_CONFIDENCE_LEVELS = ['low', 'medium', 'high'] as const
export const RETRIEVAL_VERSION_STATES = [
  'unreported',
  'reported_unverified',
  'verified_current',
  'verified_stale',
] as const
export const RETRIEVAL_ORIGIN_STATES = ['synthesized', 'reported_unverified', 'verified'] as const

export type RetrievalSourceType = (typeof RETRIEVAL_SOURCE_TYPES)[number]
export type RetrievalAnswerScope = (typeof RETRIEVAL_ANSWER_SCOPES)[number]
export type RetrievalConfidenceLevel = (typeof RETRIEVAL_CONFIDENCE_LEVELS)[number]
export type RetrievalVersionState = (typeof RETRIEVAL_VERSION_STATES)[number]
export type RetrievalOriginState = (typeof RETRIEVAL_ORIGIN_STATES)[number]

/** Trusted origin pair. Present only when the host attested it; never copied from the wire. */
export interface CaptureRetrievalOrigin {
  document_ref: string
  source_type: RetrievalSourceType
}

/**
 * Derived retrieval projection returned on Capture 1.1 list/detail reads.
 * Distinct from stored wire and from the import envelope: evidence uses
 * `reported_source_type`, and origin / version_state / capture_source_type
 * are re-derived by the host.
 */
export interface CaptureRetrievalEvidence {
  reported_source_type: RetrievalSourceType
  title: string
  document_ref: string
  chunk_ref: string | null
  reported_source_version: string | null
  version_state: RetrievalVersionState
  indexed_digest: string | null
  web_url: null
}

export interface CaptureRetrievalProjection {
  schema: 'workstack.capture-retrieval.v1.1'
  capture_schema_version: '1.1'
  request_id: string
  query_id: string
  answer_scope: RetrievalAnswerScope
  confidence: { level: RetrievalConfidenceLevel; score: number }
  evidence: CaptureRetrievalEvidence[]
  truncated: boolean
  reported_origin: CaptureRetrievalOrigin | null
  origin: CaptureRetrievalOrigin | null
  origin_state: RetrievalOriginState
  capture_source_type: RetrievalSourceType
}

interface CaptureRecordFields {
  id: string
  status: CaptureStatus
  linked_task_ids: string[]
  converted_task_ids: string[]
  revision: number
  created_at: string
  updated_at: string
}

/** Stored Capture Packet v1.0 read. Generic ingest stays 1.0-only. */
export interface CaptureV10 extends CapturePacket, CaptureRecordFields {
  schema_version: '1.0'
  retrieval?: undefined
}

/**
 * Stored Capture 1.1 read. Written only by knowledge import: manual provider,
 * knowledge.answer, null URL, and workstack.knowledge-import provenance.
 */
export interface CaptureV11 extends CaptureRecordFields {
  schema_version: '1.1'
  source_key: string
  source: CaptureSource
  normalized: CaptureNormalized
  task_hints: string[]
  provenance: CaptureProvenance
  retrieval: CaptureRetrievalProjection
}

export type Capture = CaptureV10 | CaptureV11

export interface OobRequest {
  request_id: string
  schema_version: '1.0'
  provider: MicrosoftProvider
  operation: 'search_and_capture'
  query: string
  result_limit: number
  requested_at: string
}

export interface ReplyTarget {
  resource_type: string
  connection_ref: string
  container_ref: string
  object_ref: string
  version_ref: string
}

export interface ReplyReceipt {
  schema_version: '1.0'
  reply_id: string
  provider: MicrosoftProvider
  outcome: ReplyOutcome
  remote_message_ref?: string
  web_url?: string
  occurred_at: string
  body_digest: string
  target_digest: string
  error_code?: string
}

export interface ReplyCommand {
  id: string
  task_id: string
  capture_id: string
  capture_revision: number
  provider: MicrosoftProvider
  capability: 'outlook.reply' | 'teams.reply'
  target: ReplyTarget
  body: string
  body_digest: string
  target_digest: string
  state: ReplyState
  approved_at: string
  receipt: ReplyReceipt | null
  created_at: string
  updated_at: string
}

export interface ContextRef {
  kind: 'note' | 'capture'
  id: string
}

export interface ContextConnection {
  target: { kind: 'task' | 'objective'; id: string }
  reasons: Array<'note-link' | 'capture-link' | 'capture-conversion'>
}

export interface ContextItem {
  id?: string
  ref?: ContextRef
  connections?: ContextConnection[]
  date_precision?: 'date' | 'instant' | 'unknown'
  kind?: string
  type?: string
  text?: string
  created?: string
  created_at?: string
  source?: Partial<CaptureSource>
  normalized?: Partial<CaptureNormalized>
  provenance?: Partial<CaptureProvenance>
  [key: string]: unknown
}

export interface ActivityItem {
  id?: string
  type?: string
  action?: string
  message?: string
  created_at?: string
  at?: string
  actor?: string
  task_id?: string
  task_uid?: string
  prior_status?: TaskStatus | null
  status?: TaskStatus
  prior_revision?: number | null
  new_revision?: number
  provenance?: string
  [key: string]: unknown
}

export interface PlanningSnapshot {
  detail: string
  due_date: string | null
  format: 'workstack.planning-task-snapshot.v1'
  legacy_task_id: string
  origin_ref: string
  planning_priority: TaskPriority
  planning_status: TaskStatus
  planning_task_uid: string
  revision: number
  title: string
  workspace_uid: string
}

export interface SnapshotPreview {
  snapshot: PlanningSnapshot
  digest: string
  filename: string
  omissions: ['objectives', 'dependencies', 'subtasks', 'notes', 'tags']
}

export interface SnapshotDownload {
  blob: Blob
  digest: string
  filename: string
}

export interface TaskDetail {
  task: Task
  context: ContextItem[]
  activity: ActivityItem[]
  replies: ReplyCommand[]
}

export interface TaskPatch {
  title?: string
  detail?: string
  status?: TaskStatus
  priority?: TaskPriority
  due?: string | null
  scheduled?: string | null
  estimate_minutes?: number | null
  tags?: string[]
  objective_ids?: string[]
  key_result_refs?: TaskKeyResultRef[]
  parent_id?: string | null
  dependencies?: string[]
  revision: number
}

export interface QuickTaskInput {
  title: string
  detail?: string
  priority?: TaskPriority
  due?: string | null
  scheduled?: string | null
  estimate_minutes?: number | null
  tags?: string[]
  objective_ids?: string[]
}

export interface CaptureTaskInput extends QuickTaskInput {
  intent_id?: string
  parent_id?: string | null
  dependencies?: string[]
}

export interface ApprovedReplyInput {
  task_id: string
  capture_id: string
  body: string
  approved: true
}

export interface WorklogEntry {
  task_id: string
  task: string
  done: string[]
  next: string[]
  blockers: string[]
  session_id?: string
  duration_seconds?: number
}

export interface ReviewDay {
  date: string
  start_time: string | null
  entries: WorklogEntry[]
}

export interface WeeklyReviewProject extends WorklogEntry {
  objective_ids: string[]
  dates: string[]
  duration_seconds: number
}

export interface WeeklyReview {
  range: { start: string; end: string; days: number }
  objectives: Array<{ id: string; objective: string }>
  projects: WeeklyReviewProject[]
}

export interface ReviewProjection {
  day: ReviewDay
  weekly: WeeklyReview
}

export interface ReviewEntryInput {
  date: string
  task_id: string
  done: string[]
  next: string[]
  blockers: string[]
}

export type WorkSessionState = 'running' | 'paused' | 'stopped'
export type WorkSessionWorklogState = 'not_ready' | 'pending' | 'recorded'

export interface WorkSession {
  id: string
  task_id: string
  task: string
  date: string
  state: WorkSessionState
  started_at: string
  updated_at: string
  elapsed_seconds: number
  worklog_state: WorkSessionWorklogState
}

export interface WorkSessionProjection {
  current: WorkSession | null
  pending: WorkSession[]
}

export interface WorkSessionEntryInput {
  done: string[]
  next: string[]
  blockers: string[]
}

export interface SearchItem {
  kind: 'task' | 'objective' | 'note' | 'capture' | 'activity'
  id: string
  title: string
  subtitle: string
  target_kind: 'task' | 'objective' | 'capture' | 'workspace'
  target_id: string | null
}

export interface SearchProjection {
  query: string
  items: SearchItem[]
}

import type { OutcomeFilter } from './workspaceFilterTypes'

export interface AppUrlState {
  surface: 'workspace' | 'focus' | 'inbox' | 'review' | 'objectives'
  view: WorkspaceView
  search: string
  status: 'all' | TaskStatus
  priority: 'all' | TaskPriority
  readiness: 'all' | 'ready' | 'blocked'
  timing: 'all' | 'overdue' | 'today' | 'soon' | 'unscheduled'
  objectiveId: string
  outcomeFilter?: OutcomeFilter
  taskId: string | null
  captureId: string | null
  /** Explicit Review target, independent of the Task drawer coordinate. */
  reviewTaskId?: string
  /**
   * Durable completed-Task visibility coordinate. Optional on input so every
   * existing state construction stays valid; each normalized reader/writer
   * output carries a concrete enum — see NormalizedAppUrlState.
   */
  doneVisibility?: DoneVisibility
}

/** An AppUrlState that has been normalized, so its coordinates are concrete. */
export type NormalizedAppUrlState = AppUrlState & {
  doneVisibility: DoneVisibility
  outcomeFilter: OutcomeFilter
}

/**
 * The durable transition event. Unlike the SSE notice it DOES carry reason,
 * and its eleven fields are a different exact schema.
 */
export interface CheckpointTransitionEventRecord {
  type: 'worklog.superseded' | 'worklog.restored'
  workspace_uid: string
  task_id: string
  checkpoint_id: string
  date: string
  ordinal: number
  entry_digest: string
  state: 'superseded' | 'active'
  revision: number
  reason: { code: string; explanation: string }
  origin: 'agent-cli-v1' | null
}

export interface CheckpointAuditLocator {
  workspace_uid: string
  task_id: string | null
  date: string
  ordinal: number
  entry_digest: string | null
}

/** The recorded fact is known metadata: exactly these eight bound fields. */
export interface CheckpointRecordedFact {
  type: 'worklog.recorded'
  workspace_uid: string
  task_id: string
  checkpoint_id: string
  date: string
  ordinal: number
  entry_digest: string
  origin: 'agent-cli-v1' | null
}

export interface CheckpointAuditEntry {
  locator: CheckpointAuditLocator
  checkpoint_id: string | null
  /** Opaque original payload. Rendered with a fallback, never trusted. */
  entry: unknown
  recorded: CheckpointRecordedFact | null
  state: 'active' | 'superseded'
  revision: number
  transitions: CheckpointTransitionEventRecord[]
}

export interface CheckpointAudit {
  workspace_uid: string
  entries: CheckpointAuditEntry[]
}

export interface CheckpointTransitionInput {
  state: 'superseded' | 'active'
  revision: number
  reason: { code: string; explanation: string }
}
