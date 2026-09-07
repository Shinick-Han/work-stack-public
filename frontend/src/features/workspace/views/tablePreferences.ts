import { exactPlainObject, uniqueStringArray } from './localViewState'
import { TABLE_ORDER_CAP, type TableNamedSortField } from './tableOrdering'

export const TABLE_PREFERENCES_KEY = 'workstack:table-preferences:v1'

export type TableDensity = 'comfortable' | 'compact'
export type { TableNamedSortField }
export type TableSortField = 'manual' | TableNamedSortField

export interface TablePreferences {
  density: TableDensity
  descending: boolean
  sortField: TableNamedSortField
}

export interface TableLocalViewData {
  density: TableDensity
  sortField: TableSortField
  descending: boolean
  order: string[]
}

const DEFAULT_TABLE_PREFERENCES: TablePreferences = {
  density: 'comfortable',
  descending: false,
  sortField: 'id',
}

export const EMPTY_TABLE_LOCAL_VIEW: TableLocalViewData = {
  density: 'comfortable',
  descending: false,
  sortField: 'id',
  order: [],
}

const LEGACY_SORT_FIELDS: readonly TableNamedSortField[] = ['id', 'title', 'status', 'priority', 'due']
const LOCAL_SORT_FIELDS: readonly TableSortField[] = ['manual', ...LEGACY_SORT_FIELDS]
const EXPECTED_KEYS = ['density', 'descending', 'sortField']
const LOCAL_DATA_KEYS = ['density', 'descending', 'order', 'sortField']

function isDensity(value: unknown): value is TableDensity {
  return value === 'comfortable' || value === 'compact'
}

function parseTablePreferences(value: unknown): TablePreferences | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const record = value as Record<string, unknown>
  if (Object.keys(record).sort().join('|') !== EXPECTED_KEYS.join('|')) return null
  if (!isDensity(record.density)) return null
  if (typeof record.descending !== 'boolean') return null
  if (!LEGACY_SORT_FIELDS.includes(record.sortField as TableNamedSortField)) return null
  return {
    density: record.density,
    descending: record.descending,
    sortField: record.sortField as TableNamedSortField,
  }
}

export function parseTableLocalViewData(value: unknown): TableLocalViewData | null {
  const record = exactPlainObject(value, LOCAL_DATA_KEYS)
  if (!record) return null
  if (!isDensity(record.density)) return null
  if (typeof record.descending !== 'boolean') return null
  if (!LOCAL_SORT_FIELDS.includes(record.sortField as TableSortField)) return null
  const order = uniqueStringArray(record.order)
  if (!order || order.length > TABLE_ORDER_CAP) return null
  return {
    density: record.density,
    descending: record.descending,
    sortField: record.sortField as TableSortField,
    order,
  }
}

export function seedTableLocalViewData(
  legacy: TablePreferences = readTablePreferences(),
): TableLocalViewData {
  return {
    density: legacy.density,
    descending: legacy.descending,
    sortField: legacy.sortField,
    order: [],
  }
}

function legacyStorage(): Storage | null {
  try {
    if (typeof window === 'undefined' || !window.localStorage) return null
    return window.localStorage
  } catch {
    return null
  }
}

export function readTablePreferences(): TablePreferences {
  const local = legacyStorage()
  if (!local) return { ...DEFAULT_TABLE_PREFERENCES }
  let raw: string | null
  try {
    raw = local.getItem(TABLE_PREFERENCES_KEY)
  } catch {
    return { ...DEFAULT_TABLE_PREFERENCES }
  }
  if (!raw) return { ...DEFAULT_TABLE_PREFERENCES }
  try {
    const preferences = parseTablePreferences(JSON.parse(raw))
    if (preferences) return preferences
  } catch {
    // Malformed JSON or an invalid record: try one guarded cleanup.
  }
  try {
    local.removeItem(TABLE_PREFERENCES_KEY)
  } catch {
    // Fail closed; never retry an unguarded remove.
  }
  return { ...DEFAULT_TABLE_PREFERENCES }
}

export function writeTablePreferences(preferences: TablePreferences): TablePreferences {
  const safe = parseTablePreferences(preferences)
  if (!safe) throw new Error('Invalid Table preferences')
  const local = legacyStorage()
  if (!local) return safe
  try {
    local.setItem(TABLE_PREFERENCES_KEY, JSON.stringify(safe))
  } catch {
    // Disabled storage or quota: keep the validated in-memory record.
  }
  return safe
}
