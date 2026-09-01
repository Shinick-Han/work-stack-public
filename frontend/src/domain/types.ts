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

export interface Capture extends CapturePacket {
  id: string
  status: CaptureStatus
  linked_task_ids: string[]
  converted_task_ids: string[]
  revision: number
  created_at: string
  updated_at: string
}

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

export interface ContextItem {
  id?: string
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

export interface AppUrlState {
  surface: 'workspace' | 'focus' | 'inbox' | 'review' | 'objectives'
  view: WorkspaceView
  search: string
  status: 'all' | TaskStatus
  priority: 'all' | TaskPriority
  readiness: 'all' | 'ready' | 'blocked'
  timing: 'all' | 'overdue' | 'today' | 'soon' | 'unscheduled'
  objectiveId: string
  taskId: string | null
  captureId: string | null
}
