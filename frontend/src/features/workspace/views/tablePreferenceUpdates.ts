import type { TableDensity, TableLocalViewData, TableNamedSortField } from './tablePreferences'

export function toggleTableNamedSort(
  current: TableLocalViewData,
  field: TableNamedSortField,
): TableLocalViewData {
  if (field === current.sortField) {
    return { ...current, descending: !current.descending }
  }
  return { ...current, descending: false, sortField: field }
}

export function enableTableManualSort(current: TableLocalViewData): TableLocalViewData {
  return { ...current, sortField: 'manual' }
}

export function setTableDensity(
  current: TableLocalViewData,
  density: TableDensity,
): TableLocalViewData {
  return { ...current, density }
}

export function clearTableManualOrder(current: TableLocalViewData): TableLocalViewData {
  return { ...current, order: [] }
}
