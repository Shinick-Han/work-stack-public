import type { ContextItem } from '../domain/types'

/**
 * Q4: neutral context helpers shared by the Task feature and the Workspace
 * Graph popover. The Task feature keeps re-exporting them, so its consumers and
 * tests are unchanged; the bodies below are the originals.
 */

export function externalContext(item: ContextItem) {
  return Boolean(item.source || item.normalized || item.kind === 'capture' || item.type === 'capture')
}

export function contextTitle(item: ContextItem) {
  return item.source?.display_title ?? item.normalized?.summary ?? item.text ?? 'Context item'
}
