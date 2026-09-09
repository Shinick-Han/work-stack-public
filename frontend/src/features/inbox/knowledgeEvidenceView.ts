/**
 * Presentation view model for imported knowledge evidence.
 *
 * This is a display boundary, not a wire contract and not an ingress validator. The panel
 * renders what a trusted presenter hands it: it never calls a provider, resolves a path,
 * opens a URL or decides whether a source is current. The input carries no field for a
 * source path, query string, credential, connector response or provider product name, so
 * this component never *reaches* those values — but the human-facing strings below are
 * ordinary text, and nothing here can tell a written label from a raw locator pasted into
 * one. Keeping raw values off the screen is the caller's obligation, stated below.
 *
 * The presenter that builds a `KnowledgeEvidenceInput` must therefore guarantee:
 *
 * - `sourceLabel`, `requestLabel`, `confidenceLabel`, `retrievedAt` and every row's
 *   `title`, `typeLabel`, `versionLabel` and `note` are display copy it authored for a
 *   reader. They are never a direct mapping of a URL, filesystem path, query string,
 *   credential, raw provider payload or opaque identifier.
 * - `verification: 'current'` comes from the presenter's own check against the live
 *   source. Reported version strings are provenance the import claims about itself; they
 *   are never evidence of currentness, and this module never treats them as such.
 *
 * `safeText` below strips characters that let text lie about its own rendering. It is not
 * a sanitiser for untrusted input and must not be relied on as one.
 *
 * Every claim on screen is supplied. Nothing here infers currency, freshness or truth.
 */

export const EVIDENCE_VERIFICATION_STATES = [
  'current',
  'stale',
  'offline',
  'deleted',
  'access-denied',
  'revoked',
  'unverifiable',
] as const

export type EvidenceVerification = (typeof EVIDENCE_VERIFICATION_STATES)[number]

/** A drawer is narrow and a review is a reading task, so the list stays bounded. */
export const MAX_EVIDENCE_ROWS = 10
const MAX_TITLE_CHARS = 160
const MAX_LABEL_CHARS = 120
const MAX_NOTE_CHARS = 240

export interface KnowledgeEvidenceRowInput {
  /** Stable key for rendering only. Never shown, so an opaque id cannot become copy. */
  id?: string | null
  /** Presenter-authored item name. Never a URL, path or opaque identifier. */
  title?: string | null
  /** Safe kind label such as "Page" or "Answer". Not a provider product name. */
  typeLabel?: string | null
  /**
   * Version the item reports it was read at, when the source supplied one. This is a
   * reported claim shown as provenance; presence never establishes that it is current.
   */
  versionLabel?: string | null
  note?: string | null
}

export interface KnowledgeEvidenceInput {
  /** Display copy the presenter chose, for example "Team knowledge base". */
  sourceLabel?: string | null
  /** Scope of the request this evidence answered, in the reviewer's own words. */
  requestLabel?: string | null
  retrievedAt?: string | null
  /** How many items the retrieval reported, which can exceed the rows supplied. */
  evidenceCount?: number | null
  /** Retrieval ranking confidence. Never a probability that the content is correct. */
  confidenceLabel?: string | null
  confidenceScore?: number | null
  /**
   * The presenter's verdict from its own source check. `'current'` is an authority claim,
   * so it must not be derived from the `versionLabel` strings this same import supplied.
   */
  verification?: EvidenceVerification | null
  rows?: readonly KnowledgeEvidenceRowInput[]
  /** Warnings render only when the retrieval actually flagged them. */
  resultsTruncated?: boolean
  resultsConflict?: boolean
}

export interface KnowledgeEvidenceRowView {
  key: string
  title: string
  typeLabel: string | null
  versionLabel: string | null
  note: string | null
}

export interface KnowledgeEvidenceConfidenceView {
  label: string
  score: number | null
}

export interface KnowledgeEvidenceView {
  sourceLabel: string
  requestLabel: string | null
  retrievedAt: string | null
  reportedCount: number
  confidence: KnowledgeEvidenceConfidenceView | null
  verification: EvidenceVerification
  /**
   * True when a supplied `current` was withheld because a supplied row reported no read
   * version. This is a presentation floor, not verification: it only stops the panel
   * claiming more than the import states.
   */
  currencyWithheld: boolean
  rows: readonly KnowledgeEvidenceRowView[]
  hiddenRowCount: number
  resultsTruncated: boolean
  resultsConflict: boolean
}

/**
 * Strips characters that let supplied text lie about itself: control characters, and the
 * bidirectional overrides and zero-width marks that can reorder or hide a rendered label.
 * React already escapes markup, so this guards presentation, not injection.
 */
function safeText(value: unknown, maxChars: number): string {
  if (typeof value !== 'string') return ''
  const cleaned = value
    .replace(/[\u0000-\u001F\u007F-\u009F]/g, ' ')
    .replace(/[\u200B-\u200F\u202A-\u202E\u2066-\u2069\uFEFF]/g, '')
    .replace(/\s+/g, ' ')
    .trim()
  if (cleaned.length <= maxChars) return cleaned
  return `${cleaned.slice(0, maxChars).trimEnd()}…`
}

function optionalText(value: unknown, maxChars: number): string | null {
  return safeText(value, maxChars) || null
}

/** A row reports a read version when it supplies one that survives display cleaning. */
function hasReportedVersion(row: KnowledgeEvidenceRowInput): boolean {
  return optionalText(row.versionLabel, MAX_LABEL_CHARS) !== null
}

function evidenceRowView(row: KnowledgeEvidenceRowInput, index: number): KnowledgeEvidenceRowView {
  return {
    // A supplied id is a rendering key only, and is qualified by position because two
    // supplied rows can carry the same id. A missing title falls back to a plain phrase
    // rather than to the id, so a long opaque identifier never becomes the item's name.
    key: typeof row.id === 'string' && row.id ? `${index}:${row.id}` : `evidence-${index}`,
    title: safeText(row.title, MAX_TITLE_CHARS) || 'Untitled item',
    typeLabel: optionalText(row.typeLabel, MAX_LABEL_CHARS),
    versionLabel: optionalText(row.versionLabel, MAX_LABEL_CHARS),
    note: optionalText(row.note, MAX_NOTE_CHARS),
  }
}

function confidenceView(label: unknown, score: unknown): KnowledgeEvidenceConfidenceView | null {
  const text = safeText(label, MAX_LABEL_CHARS)
  const numeric = typeof score === 'number' && Number.isFinite(score)
    ? Math.round(score * 100) / 100
    : null
  if (!text && numeric === null) return null
  return { label: text || 'Not labelled', score: numeric }
}

function reportedCount(supplied: unknown, rowCount: number): number {
  return typeof supplied === 'number' && Number.isInteger(supplied) && supplied >= 0
    ? supplied
    : rowCount
}

export function buildKnowledgeEvidenceView(input: KnowledgeEvidenceInput): KnowledgeEvidenceView {
  const supplied = input.rows ?? []
  const rows = supplied.slice(0, MAX_EVIDENCE_ROWS).map(evidenceRowView)
  const requested = input.verification
  const state: EvidenceVerification = EVIDENCE_VERIFICATION_STATES.includes(
    requested as EvidenceVerification,
  )
    ? (requested as EvidenceVerification)
    : 'unverifiable'
  // Currency is only claimed when every *supplied* item reports the version it was read
  // at. The check runs over the whole set before the display cap, because an unversioned
  // row past the tenth is precisely the one a reader cannot see and check for themselves.
  // Without that, a green "current" would be an assertion nobody actually made.
  const versioned = supplied.length > 0 && supplied.every(hasReportedVersion)
  const currencyWithheld = state === 'current' && !versioned
  const count = reportedCount(input.evidenceCount, supplied.length)
  return {
    sourceLabel: safeText(input.sourceLabel, MAX_LABEL_CHARS) || 'Source not named',
    requestLabel: optionalText(input.requestLabel, MAX_NOTE_CHARS),
    retrievedAt: optionalText(input.retrievedAt, MAX_LABEL_CHARS),
    reportedCount: count,
    confidence: confidenceView(input.confidenceLabel, input.confidenceScore),
    verification: currencyWithheld ? 'unverifiable' : state,
    currencyWithheld,
    rows,
    hiddenRowCount: Math.max(0, Math.max(count, supplied.length) - rows.length),
    resultsTruncated: input.resultsTruncated === true,
    resultsConflict: input.resultsConflict === true,
  }
}

/** Plain-language state names. No protocol words, no adapter vocabulary. */
export function evidenceVerificationLabel(state: EvidenceVerification): string {
  switch (state) {
    case 'current': return 'Source checked · current'
    case 'stale': return 'Source changed since retrieval'
    case 'offline': return 'Source could not be reached'
    case 'deleted': return 'Source no longer exists'
    case 'access-denied': return 'No permission to open the source'
    case 'revoked': return 'Access to the source was withdrawn'
    default: return 'Not verified'
  }
}

export function evidenceVerificationTone(state: EvidenceVerification): string {
  switch (state) {
    case 'current': return 'verified'
    case 'stale':
    case 'offline': return 'unknown'
    case 'deleted':
    case 'access-denied':
    case 'revoked': return 'failed'
    default: return 'neutral'
  }
}

export function evidenceVerificationNote(view: KnowledgeEvidenceView): string {
  if (view.currencyWithheld) {
    return 'The items below do not all say which version they were read at, so we cannot confirm they are still current.'
  }
  switch (view.verification) {
    case 'current':
      return 'The source was reachable and still matches the version each item was read at.'
    case 'stale':
      return 'The source changed after this evidence was read. Check the source again before relying on it.'
    case 'offline':
      return 'The source could not be reached just now. You are reading what was saved earlier.'
    case 'deleted':
      return 'The source has since been removed. This is a saved copy of what was read.'
    case 'access-denied':
      return 'This account cannot open the source, so its current state is unknown.'
    case 'revoked':
      return 'Access to the source was withdrawn after this evidence was read.'
    default:
      return 'Nobody has confirmed whether the source still says this. Treat it as unverified.'
  }
}
