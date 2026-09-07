import { Icon } from '../../components/Icon'
import type { NormalizedSavedFilter, SavedFilter } from './savedFilters'

export type SavedViewEditor = {
  mode: 'create' | 'rename'
  name: string
}

/**
 * The saved-view picker, its overflow actions and the inline name editor. Every
 * saved-view list and editor value stays owned by the Page; this component only
 * renders them and reports the intent back.
 */
export function SavedViewControls({
  editor,
  hasMatchingSavedFilter,
  onApply,
  onCancelEditor,
  onEditorNameChange,
  onRemove,
  onStartCreate,
  onStartRename,
  onSubmitEditor,
  onUpdate,
  savedFilters,
  selectedSavedFilter,
  selectedSavedFilterChanged,
}: {
  editor: SavedViewEditor | null
  hasMatchingSavedFilter: boolean
  onApply: (filterId: string) => void
  onCancelEditor: () => void
  onEditorNameChange: (name: string) => void
  onRemove: () => void
  onStartCreate: () => void
  onStartRename: () => void
  onSubmitEditor: () => void
  onUpdate: () => void
  savedFilters: readonly NormalizedSavedFilter[]
  selectedSavedFilter: SavedFilter | null
  selectedSavedFilterChanged: boolean
}) {
  return (
    <>
      <span className="saved-filter-controls">
        <select
          aria-label="Saved filters"
          onChange={(event) => onApply(event.target.value)}
          value={selectedSavedFilter?.id ?? ''}
        >
          <option value="">Saved filters</option>
          {savedFilters.map((filter) => <option key={filter.id} value={filter.id}>{filter.name}</option>)}
        </select>
        <button
          className="text-button"
          disabled={hasMatchingSavedFilter}
          onClick={onStartCreate}
          type="button"
        >Save view</button>
        {selectedSavedFilter ? (
          <details className="saved-view-menu">
            <summary aria-label="Saved view actions" role="button"><Icon name="more" size={14} /></summary>
            <div>
              <button
                aria-label="Update saved view"
                disabled={!selectedSavedFilterChanged}
                onClick={onUpdate}
                type="button"
              >Update current filters</button>
              <button
                aria-label="Rename saved view"
                onClick={onStartRename}
                type="button"
              >Rename</button>
              <button onClick={onRemove} type="button">Remove saved view</button>
            </div>
          </details>
        ) : null}
      </span>
      {editor ? (
        <form
          className="saved-view-editor"
          onSubmit={(event) => { event.preventDefault(); onSubmitEditor() }}
        >
          <label>
            <span className="sr-only">Saved view name</span>
            <input
              aria-label="Saved view name"
              autoFocus
              maxLength={120}
              onChange={(event) => onEditorNameChange(event.target.value)}
              value={editor.name}
            />
          </label>
          <button className="text-button" disabled={!editor.name.trim()} type="submit">
            {editor.mode === 'create' ? 'Create saved view' : 'Save name'}
          </button>
          <button className="text-button" onClick={onCancelEditor} type="button">Cancel</button>
        </form>
      ) : null}
    </>
  )
}
