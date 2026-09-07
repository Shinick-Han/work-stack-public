/**
 * Bounded origin-local edited daily-report buffer.
 * This is not an authoritative Work Stack revision, worklog row, or server document.
 */

export const REPORT_DRAFT_STORAGE_KEY = 'workstack.reportDrafts.v1'
export const REPORT_DRAFT_LOCK_NAME = 'workstack.reportDrafts.v1'
export const REPORT_DRAFT_VERSION = 1
export const REPORT_DRAFT_MAX_RECORDS = 14
export const REPORT_DRAFT_MAX_CHARS = 2_000_000
export const REPORT_DRAFT_MAX_MARKDOWN = 100_000
export const REPORT_DRAFT_TEMPLATE = 'daily-v1' as const

export type ReportDraftTemplate = typeof REPORT_DRAFT_TEMPLATE

export type ReportDraftErrorCode =
  | 'invalid_input'
  | 'invalid_storage'
  | 'storage_unavailable'
  | 'storage_write_failed'
  | 'conflict'
  | 'capacity'
  | 'not_found'
  | 'revision_exhausted'

export type DraftResult<T> =
  | { ok: true; value: T }
  | { ok: false; code: ReportDraftErrorCode }

export type ReportDraftCoordinate = {
  workspaceUid: string
  date: string
  template: ReportDraftTemplate
}

export type ReportDraftInput = ReportDraftCoordinate & {
  sourceDigest: string
  baseGeneratedAt: string
  baseMarkdown: string
  markdown: string
}

export type ReportDraft = ReportDraftInput & {
  localRevision: number
  updatedAt: string
}

export type DraftStorage = {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
}

export type ExclusiveLockRunner = <T>(
  name: string,
  run: () => Promise<T>,
) => Promise<T>

export type ReportDraftStore = {
  load(coordinate: ReportDraftCoordinate): Promise<DraftResult<ReportDraft | null>>
  save(
    input: ReportDraftInput,
    expectedRevision: number | null,
  ): Promise<DraftResult<ReportDraft>>
  remove(
    coordinate: ReportDraftCoordinate,
    expectedRevision: number,
  ): Promise<DraftResult<true>>
}

const ENVELOPE_KEYS = ['records', 'version'] as const
const DRAFT_KEYS = [
  'baseGeneratedAt',
  'baseMarkdown',
  'date',
  'localRevision',
  'markdown',
  'sourceDigest',
  'template',
  'updatedAt',
  'workspaceUid',
] as const
const INPUT_KEYS = [
  'baseGeneratedAt',
  'baseMarkdown',
  'date',
  'markdown',
  'sourceDigest',
  'template',
  'workspaceUid',
] as const
const COORDINATE_KEYS = ['date', 'template', 'workspaceUid'] as const
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
const NIL_UUID = '00000000-0000-0000-0000-000000000000'
const DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/
const ISO_UTC_PATTERN = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/
const GENERATED_UTC_PATTERN = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z$/
const DIGEST_PATTERN = /^sha256:[0-9a-f]{64}$/
const LOCK_UNAVAILABLE = { unavailable: true as const }

function ok<T>(value: T): DraftResult<T> {
  return { ok: true, value }
}

function fail<T>(code: ReportDraftErrorCode): DraftResult<T> {
  return { ok: false, code }
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return false
  const proto = Object.getPrototypeOf(value)
  return proto === Object.prototype || proto === null
}

function exactPlainObject(
  value: unknown,
  keys: readonly string[],
): Record<string, unknown> | null {
  if (!isPlainObject(value)) return null
  const actual = Object.keys(value)
  if (actual.length !== keys.length) return null
  const expected = new Set(keys)
  for (const key of actual) {
    if (!expected.has(key)) return null
  }
  return value
}

function isWorkspaceUid(value: unknown): value is string {
  return typeof value === 'string' && UUID_PATTERN.test(value) && value !== NIL_UUID
}

function isIsoCalendarDate(value: unknown): value is string {
  if (typeof value !== 'string' || !DATE_PATTERN.test(value)) return false
  const year = Number(value.slice(0, 4))
  const month = Number(value.slice(5, 7))
  const day = Number(value.slice(8, 10))
  const utc = new Date(`${value}T00:00:00.000Z`)
  return year >= 1 && utc.getUTCFullYear() === year && utc.getUTCMonth() === month - 1 && utc.getUTCDate() === day
}

function isIsoUtc(value: unknown): value is string {
  if (typeof value !== 'string' || !ISO_UTC_PATTERN.test(value)) return false
  const ms = Date.parse(value)
  return Number.isFinite(ms) && new Date(ms).toISOString() === value
}

function isGeneratedUtc(value: unknown): value is string {
  if (typeof value !== 'string' || !GENERATED_UTC_PATTERN.test(value)) return false
  const canonical = value.includes('.') ? value : value.replace('Z', '.000Z')
  return isIsoCalendarDate(value.slice(0, 10)) && isIsoUtc(canonical)
}

function isDigest(value: unknown): value is string {
  return typeof value === 'string' && DIGEST_PATTERN.test(value)
}

function isMarkdown(value: unknown): value is string {
  return typeof value === 'string' && value.length <= REPORT_DRAFT_MAX_MARKDOWN
}

function isPositiveSafeInteger(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value) && value >= 1 && value <= Number.MAX_SAFE_INTEGER
}

function isTemplate(value: unknown): value is ReportDraftTemplate {
  return value === REPORT_DRAFT_TEMPLATE
}

function coordinateKey(coordinate: ReportDraftCoordinate): string {
  return `${coordinate.workspaceUid}|${coordinate.date}|${coordinate.template}`
}

function parseCoordinate(value: unknown): ReportDraftCoordinate | null {
  const row = exactPlainObject(value, COORDINATE_KEYS)
  if (!row || !isWorkspaceUid(row.workspaceUid) || !isIsoCalendarDate(row.date) || !isTemplate(row.template)) {
    return null
  }
  return { workspaceUid: row.workspaceUid, date: row.date, template: row.template }
}

function parseDraftBody(row: Record<string, unknown>): Omit<ReportDraftInput, keyof ReportDraftCoordinate> | null {
  if (!isDigest(row.sourceDigest) || !isGeneratedUtc(row.baseGeneratedAt)) return null
  if (!isMarkdown(row.baseMarkdown) || !isMarkdown(row.markdown)) return null
  return {
    sourceDigest: row.sourceDigest,
    baseGeneratedAt: row.baseGeneratedAt,
    baseMarkdown: row.baseMarkdown,
    markdown: row.markdown,
  }
}

function parseDraft(value: unknown): ReportDraft | null {
  const row = exactPlainObject(value, DRAFT_KEYS)
  if (!row || !isPositiveSafeInteger(row.localRevision) || !isIsoUtc(row.updatedAt)) return null
  const coordinate = parseCoordinate({
    workspaceUid: row.workspaceUid,
    date: row.date,
    template: row.template,
  })
  const body = parseDraftBody(row)
  if (!coordinate || !body) return null
  return { ...coordinate, ...body, localRevision: row.localRevision, updatedAt: row.updatedAt }
}

function parseInput(value: unknown): ReportDraftInput | null {
  const row = exactPlainObject(value, INPUT_KEYS)
  if (!row) return null
  const coordinate = parseCoordinate({
    workspaceUid: row.workspaceUid,
    date: row.date,
    template: row.template,
  })
  const body = parseDraftBody(row)
  if (!coordinate || !body) return null
  return { ...coordinate, ...body }
}

function parseEnvelope(raw: string): DraftResult<ReportDraft[]> {
  if (raw.length > REPORT_DRAFT_MAX_CHARS) return fail('invalid_storage')
  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    return fail('invalid_storage')
  }
  const envelope = exactPlainObject(parsed, ENVELOPE_KEYS)
  if (!envelope || envelope.version !== REPORT_DRAFT_VERSION || !Array.isArray(envelope.records)) {
    return fail('invalid_storage')
  }
  if (envelope.records.length > REPORT_DRAFT_MAX_RECORDS) return fail('invalid_storage')
  return collectRecords(envelope.records)
}

function collectRecords(items: unknown[]): DraftResult<ReportDraft[]> {
  const records: ReportDraft[] = []
  const seen = new Set<string>()
  for (const item of items) {
    const draft = parseDraft(item)
    if (!draft) return fail('invalid_storage')
    const key = coordinateKey(draft)
    if (seen.has(key)) return fail('invalid_storage')
    seen.add(key)
    records.push(draft)
  }
  return ok(records)
}

function encodeDraft(draft: ReportDraft): ReportDraft {
  return {
    workspaceUid: draft.workspaceUid,
    date: draft.date,
    template: draft.template,
    sourceDigest: draft.sourceDigest,
    baseGeneratedAt: draft.baseGeneratedAt,
    baseMarkdown: draft.baseMarkdown,
    markdown: draft.markdown,
    localRevision: draft.localRevision,
    updatedAt: draft.updatedAt,
  }
}

function serializeEnvelope(records: readonly ReportDraft[]): string | null {
  let serialized: string
  try {
    serialized = JSON.stringify({
      version: REPORT_DRAFT_VERSION,
      records: records.map(encodeDraft),
    })
  } catch {
    return null
  }
  if (serialized.length > REPORT_DRAFT_MAX_CHARS) return null
  return serialized
}

function isQuotaError(error: unknown): boolean {
  if (error === null || typeof error !== 'object') return false
  const name = 'name' in error ? error.name : undefined
  return name === 'QuotaExceededError' || name === 'NS_ERROR_DOM_QUOTA_REACHED'
}

function isSecurityError(error: unknown): boolean {
  return error !== null && typeof error === 'object' && 'name' in error && error.name === 'SecurityError'
}

function readStorage(supplier: () => DraftStorage): DraftResult<DraftStorage> {
  try {
    const storage = supplier()
    if (!storage) return fail('storage_unavailable')
    return ok(storage)
  } catch {
    return fail('storage_unavailable')
  }
}

function readRaw(storage: DraftStorage): DraftResult<string | null> {
  try {
    const raw = storage.getItem(REPORT_DRAFT_STORAGE_KEY)
    if (raw === null) return ok(null)
    if (typeof raw !== 'string') return fail('invalid_storage')
    return ok(raw)
  } catch (error) {
    return fail(isSecurityError(error) ? 'storage_unavailable' : 'storage_unavailable')
  }
}

function writeRaw(storage: DraftStorage, serialized: string): DraftResult<true> {
  try {
    storage.setItem(REPORT_DRAFT_STORAGE_KEY, serialized)
    return ok(true)
  } catch (error) {
    if (isSecurityError(error)) return fail('storage_unavailable')
    if (isQuotaError(error)) return fail('storage_write_failed')
    return fail('storage_write_failed')
  }
}

function loadRecords(supplier: () => DraftStorage): DraftResult<ReportDraft[]> {
  const storage = readStorage(supplier)
  if (!storage.ok) return storage
  const raw = readRaw(storage.value)
  if (!raw.ok) return raw
  if (raw.value === null) return ok([])
  return parseEnvelope(raw.value)
}

function findDraft(records: readonly ReportDraft[], coordinate: ReportDraftCoordinate): number {
  const key = coordinateKey(coordinate)
  return records.findIndex((draft) => coordinateKey(draft) === key)
}

function commitSave(
  supplier: () => DraftStorage,
  input: ReportDraftInput,
  expectedRevision: number | null,
  now: () => Date,
): DraftResult<ReportDraft> {
  if (expectedRevision !== null && !isPositiveSafeInteger(expectedRevision)) {
    return fail('invalid_input')
  }
  const records = loadRecords(supplier)
  if (!records.ok) return records
  const index = findDraft(records.value, input)
  const next = nextDraft(records.value, index, input, expectedRevision, now().toISOString())
  if (!next.ok) return next
  const storage = readStorage(supplier)
  if (!storage.ok) return storage
  const serialized = serializeEnvelope(next.value.records)
  if (serialized === null) return fail('capacity')
  const written = writeRaw(storage.value, serialized)
  if (!written.ok) return written
  return ok(next.value.saved)
}

function nextDraft(
  records: ReportDraft[],
  index: number,
  input: ReportDraftInput,
  expectedRevision: number | null,
  updatedAt: string,
): DraftResult<{ records: ReportDraft[]; saved: ReportDraft }> {
  if (index < 0) return createDraft(records, input, expectedRevision, updatedAt)
  return updateDraft(records, index, input, expectedRevision, updatedAt)
}

function createDraft(
  records: ReportDraft[],
  input: ReportDraftInput,
  expectedRevision: number | null,
  updatedAt: string,
): DraftResult<{ records: ReportDraft[]; saved: ReportDraft }> {
  if (expectedRevision !== null) return fail('conflict')
  if (records.length >= REPORT_DRAFT_MAX_RECORDS) return fail('capacity')
  const saved: ReportDraft = { ...input, localRevision: 1, updatedAt }
  return ok({ records: [...records, saved], saved })
}

function updateDraft(
  records: ReportDraft[],
  index: number,
  input: ReportDraftInput,
  expectedRevision: number | null,
  updatedAt: string,
): DraftResult<{ records: ReportDraft[]; saved: ReportDraft }> {
  const current = records[index]
  if (expectedRevision === null || current.localRevision !== expectedRevision) {
    return fail('conflict')
  }
  if (current.localRevision >= Number.MAX_SAFE_INTEGER) return fail('revision_exhausted')
  const saved: ReportDraft = {
    ...input,
    localRevision: current.localRevision + 1,
    updatedAt,
  }
  const next = records.slice()
  next[index] = saved
  return ok({ records: next, saved })
}

function commitRemove(
  supplier: () => DraftStorage,
  coordinate: ReportDraftCoordinate,
  expectedRevision: number,
): DraftResult<true> {
  if (!isPositiveSafeInteger(expectedRevision)) return fail('invalid_input')
  const records = loadRecords(supplier)
  if (!records.ok) return records
  const index = findDraft(records.value, coordinate)
  if (index < 0) return fail('not_found')
  if (records.value[index].localRevision !== expectedRevision) return fail('conflict')
  const next = records.value.filter((_, position) => position !== index)
  const storage = readStorage(supplier)
  if (!storage.ok) return storage
  const serialized = serializeEnvelope(next)
  if (serialized === null) return fail('capacity')
  const written = writeRaw(storage.value, serialized)
  if (!written.ok) return written
  return ok(true)
}

export function browserReportDraftStorage(): DraftStorage {
  if (typeof window === 'undefined' || !window.localStorage) {
    throw LOCK_UNAVAILABLE
  }
  return window.localStorage
}

export async function runWithBrowserExclusiveLock<T>(
  name: string,
  run: () => Promise<T>,
): Promise<T> {
  const locks = typeof navigator === 'undefined' ? undefined : navigator.locks
  if (!locks || typeof locks.request !== 'function') {
    return Promise.reject(LOCK_UNAVAILABLE)
  }
  try {
    return await locks.request(name, { mode: 'exclusive' }, () => run())
  } catch (error) {
    if (error === LOCK_UNAVAILABLE) return Promise.reject(LOCK_UNAVAILABLE)
    return Promise.reject(LOCK_UNAVAILABLE)
  }
}

async function withWriteLock<T>(
  lock: ExclusiveLockRunner,
  work: () => DraftResult<T>,
): Promise<DraftResult<T>> {
  try {
    return await lock(REPORT_DRAFT_LOCK_NAME, () => Promise.resolve(work()))
  } catch {
    return fail('storage_unavailable')
  }
}

export function createReportDraftStore(options: {
  storage: () => DraftStorage
  withExclusiveLock: ExclusiveLockRunner
  now?: () => Date
}): ReportDraftStore {
  const now = options.now ?? (() => new Date())
  return {
    async load(coordinate) {
      const parsed = parseCoordinate(coordinate)
      if (!parsed) return fail('invalid_input')
      const records = loadRecords(options.storage)
      if (!records.ok) return records
      const index = findDraft(records.value, parsed)
      return ok(index < 0 ? null : encodeDraft(records.value[index]))
    },
    async save(input, expectedRevision) {
      const parsed = parseInput(input)
      if (!parsed) return fail('invalid_input')
      return withWriteLock(options.withExclusiveLock, () => (
        commitSave(options.storage, parsed, expectedRevision, now)
      ))
    },
    async remove(coordinate, expectedRevision) {
      const parsed = parseCoordinate(coordinate)
      if (!parsed) return fail('invalid_input')
      return withWriteLock(options.withExclusiveLock, () => (
        commitRemove(options.storage, parsed, expectedRevision)
      ))
    },
  }
}
