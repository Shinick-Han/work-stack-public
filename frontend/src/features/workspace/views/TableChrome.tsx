import type { TableDensity, TableNamedSortField, TableSortField } from './tablePreferences'

const SORT_COLUMNS: ReadonlyArray<readonly [TableNamedSortField, string]> = [
  ['id', 'ID'], ['title', 'Task'], ['status', 'Status'], ['priority', 'Priority'], ['due', 'Due'],
]

function headerSort(
  sortField: TableSortField,
  descending: boolean,
  field: TableNamedSortField,
): 'ascending' | 'descending' | 'none' {
  if (sortField !== field) return 'none'
  return descending ? 'descending' : 'ascending'
}

export function TableToolbar({
  density,
  manual,
  onChangeDensity,
  onManual,
  onReset,
}: {
  density: TableDensity
  manual: boolean
  onChangeDensity: (density: TableDensity) => void
  onManual: () => void
  onReset: () => void
}) {
  return (
    <div className="wsv-table-toolbar">
      <span>Row density</span>
      <div aria-label="Table row density" role="group">
        <button aria-label="Comfortable rows" aria-pressed={density === 'comfortable'} onClick={() => onChangeDensity('comfortable')} type="button">Comfortable</button>
        <button aria-label="Compact rows" aria-pressed={density === 'compact'} onClick={() => onChangeDensity('compact')} type="button">Compact</button>
      </div>
      <button aria-pressed={manual} onClick={onManual} type="button">Manual order</button>
      <button aria-label="Reset table order" onClick={onReset} type="button">Reset table order</button>
      <small>On narrow screens, low-value technical columns collapse automatically.</small>
    </div>
  )
}

export function TableHead({
  sortField,
  descending,
  onChangeSort,
}: {
  sortField: TableSortField
  descending: boolean
  onChangeSort: (field: TableNamedSortField) => void
}) {
  return (
    <thead><tr>{SORT_COLUMNS.map(([field, label]) => (
      <th
        aria-sort={headerSort(sortField, descending, field)}
        className={field === 'id' ? 'wsv-table-col--technical' : undefined}
        key={field}
        scope="col"
      >
        <button aria-label={`Sort by ${label}`} onClick={() => onChangeSort(field)} type="button">
          {label}{sortField === field ? <span>{descending ? '↓' : '↑'}</span> : null}
        </button>
      </th>
    ))}<th scope="col">Readiness</th><th className="wsv-table-col--technical" scope="col">Steps</th><th scope="col">Objective</th><th scope="col">Key Result</th><th className="wsv-table-col--technical" scope="col">Context</th><th className="wsv-table-col--technical" scope="col">Rev</th></tr></thead>
  )
}
