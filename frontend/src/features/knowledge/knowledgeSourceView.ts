import type {
  KnowledgeReadReference,
  KnowledgeSavedReference,
  KnowledgeVault,
} from './knowledgeTypes'

/**
 * Presentation boundary between a knowledge source and the shared Task surfaces.
 *
 * Common rows render a `KnowledgeReferenceView` and nothing else. A view carries no
 * filesystem path, line span or vault field, so a future page/block source can supply
 * its own provenance without changing the Task layout. Everything Markdown-specific
 * lives in the local adapter below.
 */

export type KnowledgeReferenceAction = 'read' | 'unlink'

export interface KnowledgeSourcePresentation {
  /** Stable connector identity used to qualify document identities across sources. */
  connectorId: string
  /** Product name of the connector, shown on its own source card. */
  connectorName: string
  /** What the connector actually explains about itself on its source card. */
  connectorNote: string
  /** Truthful storage/availability scope for this source, never a blanket claim. */
  scopeNote: string
  /** Actions the common reference rows may offer for this source. */
  supportedActions: readonly KnowledgeReferenceAction[]
}

export interface KnowledgeReferenceView {
  referenceId: string
  /** Source-qualified identity. A title or filename alone cannot identify a document. */
  identityKey: string
  connectorName: string
  /** Display name of the connected source, for example the chosen folder label. */
  sourceName: string
  documentTitle: string
  /** Container trail inside the source, or null when the source has none. */
  documentLocation: string | null
  /** Where the excerpt sits inside the document, or null when the source has no coordinates. */
  excerptLocation: string | null
  linkedReason: string
  actions: readonly KnowledgeReferenceAction[]
  scopeNote: string
}

export interface KnowledgeExcerptView {
  identityKey: string
  connectorName: string
  sourceName: string
  documentTitle: string
  documentLocation: string | null
  excerptLocation: string | null
  excerpt: string
  excerptTruncated: boolean
  freshness: KnowledgeReadReference['freshness']
  /** How this source describes the evidence it hands back. */
  trustNote: string
}

export const LOCAL_MARKDOWN_SOURCE: KnowledgeSourcePresentation = {
  connectorId: 'markdown-vault',
  connectorName: 'Local Markdown',
  connectorNote:
    'Choose a folder of Markdown documents on this device. An Obsidian vault is one such folder.',
  scopeNote: 'Links are saved on this device. They are not included in workspace sync or backups.',
  supportedActions: ['read', 'unlink'],
}

export const EXTERNAL_EVIDENCE_NOTE =
  'External reference · read-only evidence, not Task permission'

/**
 * Encodes the identity tuple rather than concatenating it. Remote document keys can be
 * URLs or contain separators of their own, so a delimiter-joined string would let two
 * different documents collapse onto one identity.
 */
export function sourceQualifiedIdentity(connectorId: string, sourceId: string, documentKey: string) {
  return JSON.stringify([connectorId, sourceId, documentKey])
}

export function sourceDisplayName(vaults: readonly KnowledgeVault[], vaultId: string) {
  return vaults.find((vault) => vault.vault_id === vaultId)?.label ?? vaultId
}

/** Markdown documents are named by their file. The folder trail stays a separate field. */
export function markdownDocumentTitle(documentPath: string) {
  const segments = documentPath.split('/').filter(Boolean)
  return segments[segments.length - 1] ?? documentPath
}

export function markdownDocumentLocation(documentPath: string) {
  const segments = documentPath.split('/').filter(Boolean)
  if (segments.length <= 1) return null
  return segments.slice(0, -1).join(' / ')
}

export function markdownLineSpanLabel(startLine: number, endLine: number) {
  return startLine === endLine ? `line ${startLine}` : `lines ${startLine}–${endLine}`
}

function markdownDocumentKey(documentPath: string, startLine: number, endLine: number) {
  return `${documentPath}#${startLine}-${endLine}`
}

export function localReferenceView(
  reference: KnowledgeSavedReference,
  vaults: readonly KnowledgeVault[],
): KnowledgeReferenceView {
  return {
    referenceId: reference.reference_id,
    identityKey: sourceQualifiedIdentity(
      LOCAL_MARKDOWN_SOURCE.connectorId,
      reference.vault_id,
      markdownDocumentKey(reference.document_path, reference.start_line, reference.end_line),
    ),
    connectorName: LOCAL_MARKDOWN_SOURCE.connectorName,
    sourceName: sourceDisplayName(vaults, reference.vault_id),
    documentTitle: markdownDocumentTitle(reference.document_path),
    documentLocation: markdownDocumentLocation(reference.document_path),
    excerptLocation: markdownLineSpanLabel(reference.start_line, reference.end_line),
    linkedReason: reference.reason,
    actions: LOCAL_MARKDOWN_SOURCE.supportedActions,
    scopeNote: LOCAL_MARKDOWN_SOURCE.scopeNote,
  }
}

export function localExcerptView(
  read: KnowledgeReadReference,
  vaults: readonly KnowledgeVault[],
): KnowledgeExcerptView {
  return {
    identityKey: sourceQualifiedIdentity(
      LOCAL_MARKDOWN_SOURCE.connectorId,
      read.vault_id,
      markdownDocumentKey(read.document_path, read.start_line, read.end_line),
    ),
    connectorName: LOCAL_MARKDOWN_SOURCE.connectorName,
    sourceName: sourceDisplayName(vaults, read.vault_id),
    documentTitle: read.title || markdownDocumentTitle(read.document_path),
    // Display-only trail. The exported path lives in the envelope and in the brief's
    // `- Document:` line, which keeps the full path rather than this shortened form.
    documentLocation: markdownDocumentLocation(read.document_path),
    excerptLocation: markdownLineSpanLabel(read.start_line, read.end_line),
    excerpt: read.excerpt,
    excerptTruncated: read.excerpt_truncated,
    freshness: read.freshness,
    trustNote: EXTERNAL_EVIDENCE_NOTE,
  }
}

/** Joins the parts of a reference's origin that the active source actually supplies. */
export function referenceOriginLabel(view: {
  sourceName: string
  documentLocation: string | null
  excerptLocation: string | null
}) {
  return [view.sourceName, view.documentLocation, view.excerptLocation]
    .filter((part): part is string => Boolean(part))
    .join(' · ')
}
