/**
 * Who owns an Escape keypress while the Task drawer shows the context subview.
 *
 * The subview is a whole surface, not a dialog, so its Escape listener sits on
 * the window and would otherwise answer for every control inside it. Unmounting
 * the subview discards an in-progress reference selection and whatever the
 * reader had typed, so this predicate is deliberately conservative: Escape
 * returns to the Task only when nothing nearer has a better claim on it.
 *
 * Four things have a better claim. A text control is still being edited, and
 * Escape there belongs to the field. An IME composition is mid-word, and the
 * Escape that ends it was never aimed at the surface. A modified Escape is a
 * different chord. An open modal dialog is above the subview, and closing the
 * surface underneath it would leave the dialog stranded. A handler that already
 * called `preventDefault()` has answered the key, so it is not answered twice.
 */

const EDITABLE_TAGS = new Set(['INPUT', 'TEXTAREA', 'SELECT'])

/** Modal surfaces layered above the subview, native `<dialog>` included. */
const OPEN_DIALOG_SELECTOR = 'dialog[open], [role="dialog"][aria-modal="true"]'

/** The IME `keydown` code every browser still reports while composing. */
const IME_COMPOSITION_KEY_CODE = 229

export function isEditableTarget(node: EventTarget | null): boolean {
  if (!(node instanceof HTMLElement)) return false
  if (EDITABLE_TAGS.has(node.tagName)) return true
  return node.isContentEditable
}

export function hasOpenDialog(scope: Document): boolean {
  return scope.querySelector(OPEN_DIALOG_SELECTOR) !== null
}

/** The document the key actually happened in, not whichever one is global. */
function documentOf(origin: EventTarget | null): Document {
  return origin instanceof Node ? origin.ownerDocument ?? document : document
}

export function escapeReturnsToTask(event: KeyboardEvent): boolean {
  if (event.key !== 'Escape' || event.defaultPrevented) return false
  if (event.isComposing || event.keyCode === IME_COMPOSITION_KEY_CODE) return false
  if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return false
  const origin = event.target ?? document.activeElement
  if (isEditableTarget(origin)) return false
  return !hasOpenDialog(documentOf(origin))
}
