export const TABLE_PREFERENCES_KEY = 'workstack:table-preferences:v1'

export type TableDensity = 'comfortable' | 'compact'
export type TableSortField = 'id' | 'title' | 'status' | 'priority' | 'due'

export interface TablePreferences {
  density: TableDensity
  descending: boolean
  sortField: TableSortField
}

const DEFAULT_TABLE_PREFERENCES: TablePreferences = {
  density: 'comfortable',
  descending: false,
  sortField: 'id',
}

const SORT_FIELDS: readonly TableSortField[] = ['id', 'title', 'status', 'priority', 'due']
const EXPECTED_KEYS = ['density', 'descending', 'sortField']

function parseTablePreferences(value: unknown): TablePreferences | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const record = value as Record<string, unknown>
  if (Object.keys(record).sort().join('|') !== EXPECTED_KEYS.join('|')) return null
  if (record.density !== 'comfortable' && record.density !== 'compact') return null
  if (typeof record.descending !== 'boolean') return null
  if (!SORT_FIELDS.includes(record.sortField as TableSortField)) return null
  return {
    density: record.density,
    descending: record.descending,
    sortField: record.sortField as TableSortField,
  }
}

export function readTablePreferences(): TablePreferences {
  if (typeof window === 'undefined') return { ...DEFAULT_TABLE_PREFERENCES }
  try {
    const value = window.localStorage.getItem(TABLE_PREFERENCES_KEY)
    if (!value) return { ...DEFAULT_TABLE_PREFERENCES }
    const preferences = parseTablePreferences(JSON.parse(value))
    if (preferences) return preferences
  } catch {
    // Fall through to fail-closed cleanup and safe defaults.
  }
  window.localStorage.removeItem(TABLE_PREFERENCES_KEY)
  return { ...DEFAULT_TABLE_PREFERENCES }
}

export function writeTablePreferences(preferences: TablePreferences): TablePreferences {
  const safe = parseTablePreferences(preferences)
  if (!safe) throw new Error('Invalid Table preferences')
  if (typeof window !== 'undefined') {
    window.localStorage.setItem(TABLE_PREFERENCES_KEY, JSON.stringify(safe))
  }
  return safe
}
