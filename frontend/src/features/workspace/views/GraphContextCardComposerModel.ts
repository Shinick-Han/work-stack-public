export const GRAPH_CONTEXT_COMPOSER_COPY = {
  heading: 'Add context card',
  field: 'Context card',
  placeholder: 'Decision, assumption, or context…',
  submit: 'Add context card',
  saving: 'Saving context card…',
  verify: 'Verify context card',
  linkedHint: 'Saved as plain text and linked only to this Task.',
  closeDirtyTitle: 'Keep unsaved context card?',
  closeDirtyBody: 'Closing now discards this unsaved Context card. Keep writing, or discard it and close.',
  keep: 'Keep writing',
  discard: 'Discard and close',
  closePendingTitle: 'Context card is still saving',
  closePendingBody: 'Wait for this save to finish before closing. The same request will not be sent twice.',
  stay: 'Stay',
  closeUnknownTitle: 'Context card save is unverified',
  closeUnknownBody: 'This Context card may already exist. Verify the same request before closing.',
} as const

export const GRAPH_CONTEXT_DIALOG_FOCUSABLE =
  'button:not(:disabled), a[href], textarea:not(:disabled), input:not(:disabled):not([type="hidden"]), select:not(:disabled)'

export type GraphContextComposerOwner = {
  taskId: string
  workspaceId: string
}

export type FrozenGraphContextRequest = {
  text: string
  taskId: string
  workspaceId: string
  key: string
}

export type ComposerCloseKind = 'open' | 'pending' | 'unknown' | 'dirty'

export function isBlankContextDraft(text: string): boolean {
  return text.trim().length === 0
}

export function composerCloseKind(args: {
  text: string
  pending: boolean
  unknown: boolean
}): ComposerCloseKind {
  if (args.pending) return 'pending'
  if (args.unknown) return 'unknown'
  if (!isBlankContextDraft(args.text)) return 'dirty'
  return 'open'
}

export function sameComposerOwner(
  left: GraphContextComposerOwner,
  right: GraphContextComposerOwner,
): boolean {
  return left.taskId === right.taskId && left.workspaceId === right.workspaceId
}

export function canApplyComposerResult(args: {
  ownerAlive: boolean
  submittedToken: object
  liveToken: object
  submitted: GraphContextComposerOwner
  live: GraphContextComposerOwner
}): boolean {
  return args.ownerAlive
    && args.submittedToken === args.liveToken
    && sameComposerOwner(args.submitted, args.live)
}

export function graphContextDialogFocusables(root: ParentNode): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(GRAPH_CONTEXT_DIALOG_FOCUSABLE)).filter((element) => (
    !element.closest('[inert]') && element.getAttribute('aria-hidden') !== 'true'
  ))
}

export function composerSubmitLabel(args: { pending: boolean; unknown: boolean }): string {
  if (args.pending) return GRAPH_CONTEXT_COMPOSER_COPY.saving
  if (args.unknown) return GRAPH_CONTEXT_COMPOSER_COPY.verify
  return GRAPH_CONTEXT_COMPOSER_COPY.submit
}

export function composerSubmitDisabled(args: { pending: boolean; unknown: boolean; text: string }): boolean {
  if (args.pending) return true
  if (args.unknown) return false
  return isBlankContextDraft(args.text)
}

/**
 * The exact request one submit may send, or null when the submit must be
 * refused. While an unverified commit is frozen, only that identical request
 * may be re-sent: a drifted key, text, or Task never mints a second write.
 */
export function composerSubmitRequest(args: {
  unknown: boolean
  frozen: FrozenGraphContextRequest | null
  draft: FrozenGraphContextRequest
}): FrozenGraphContextRequest | null {
  const frozen = args.frozen
  const payload = args.unknown && frozen ? frozen : { ...args.draft, text: args.draft.text.trim() }
  if (isBlankContextDraft(payload.text)) return null
  if (args.unknown && frozen && (payload.key !== frozen.key || payload.text !== frozen.text || payload.taskId !== frozen.taskId)) {
    return null
  }
  return payload
}
