import { describe, expect, it } from 'vitest'
import {
  clearTableManualOrder,
  enableTableManualSort,
  setTableDensity,
  toggleTableNamedSort,
} from './tablePreferenceUpdates'
import { EMPTY_TABLE_LOCAL_VIEW } from './tablePreferences'

describe('table preference updates', () => {
  it('toggles the same named field and resets direction when the field changes', () => {
    const titled = toggleTableNamedSort(EMPTY_TABLE_LOCAL_VIEW, 'title')
    expect(titled).toEqual({ ...EMPTY_TABLE_LOCAL_VIEW, sortField: 'title', descending: false })
    expect(toggleTableNamedSort(titled, 'title')).toEqual({ ...titled, descending: true })
  })

  it('switches to Manual, density, and order reset without dropping other coordinates', () => {
    const current = {
      density: 'compact' as const,
      descending: true,
      sortField: 'due' as const,
      order: ['T-A', 'T-B'],
    }
    expect(enableTableManualSort(current)).toEqual({ ...current, sortField: 'manual' })
    expect(setTableDensity(current, 'comfortable')).toEqual({ ...current, density: 'comfortable' })
    expect(clearTableManualOrder(current)).toEqual({ ...current, order: [] })
  })
})
