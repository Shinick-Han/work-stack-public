import { describe, expect, test } from 'vitest'
import type { ContextItem } from '../domain/types'
import {
  captureLinkRemovalTarget,
  contextPlainBody,
  contextTitle,
  contextUnknownFields,
  externalContext,
} from './taskContext'

const item = (patch: Record<string, unknown>) => patch as ContextItem

describe('contextReadable parts', () => {
  test('keeps the established title fallback chain', () => {
    expect(contextTitle(item({ source: { display_title: 'Mail subject' } }))).toBe('Mail subject')
    expect(contextTitle(item({ normalized: { summary: 'Summary line' } }))).toBe('Summary line')
    expect(contextTitle(item({ text: 'Plain note' }))).toBe('Plain note')
    expect(contextTitle(item({}))).toBe('Context item')
  })

  test('omits a body that would reprint the heading and keeps a distinct body', () => {
    expect(contextPlainBody(item({ text: 'Plain note' }))).toBeNull()
    expect(contextPlainBody(item({
      source: { display_title: 'Mail subject' },
      text: 'Reviewed the thread.',
    }))).toBe('Reviewed the thread.')
    expect(contextPlainBody(item({
      source: { display_title: 'Mail subject' },
      normalized: { context: 'Reviewed the thread.' },
    }))).toBe('Reviewed the thread.')
    expect(contextPlainBody(item({
      source: { display_title: 'Mail subject' },
      text: 'Mail subject',
    }))).toBeNull()
    expect(contextPlainBody(item({
      source: { display_title: 'Mail subject' },
      normalized: { context: 'Mail subject' },
      text: 'Reviewed the thread after the heading was reused.',
    }))).toBe('Reviewed the thread after the heading was reused.')
    expect(contextPlainBody(item({
      source: { display_title: 'Mail subject' },
      normalized: { context: 'Mail subject' },
    }))).toBeNull()
  })

  test('exposes unknown own-keys as inspectable text and ignores known fields', () => {
    expect(contextUnknownFields(item({
      id: 'C-1',
      text: 'Known note',
      connections: [],
      ticket: { id: 'WS-41' },
    }))).toEqual([{ label: 'ticket', value: '{"id":"WS-41"}' }])
    expect(externalContext(item({ kind: 'capture' }))).toBe(true)
  })

  test('keeps unrendered recorded metadata discoverable without repeating title or body', () => {
    const recorded = item({
      id: 'C-meta',
      ref: { kind: 'capture', id: 'C-meta' },
      text: 'Reviewed the thread after the heading was reused.',
      source: {
        provider: 'microsoft-outlook',
        display_title: 'Mail subject',
        web_url: 'https://example.test/thread',
        resource_type: 'message',
        connection_ref: 'conn-1',
        fingerprint: 'sha256:abc',
        extra_attr: 'nested-unknown',
      },
      normalized: {
        summary: 'Mail subject',
        context: 'Mail subject',
        action_items: [{ title: 'Reply today', detail: 'Keep the recorded title.', priority: 'P2', due: null }],
        tags: ['resume', 'brief'],
        mystery: 'keep-me',
      },
      provenance: {
        capture_mode: 'manual',
        adapter: 'outlook',
        raw_retained: false,
      },
      connections: [{ target: { kind: 'task', id: 'T-1' }, reasons: ['capture-link'] }],
      ticket: { id: 'WS-41' },
    })

    expect(contextTitle(recorded)).toBe('Mail subject')
    expect(contextPlainBody(recorded)).toBe('Reviewed the thread after the heading was reused.')
    expect(contextUnknownFields(recorded)).toEqual([
      { label: 'ticket', value: '{"id":"WS-41"}' },
      { label: 'provenance.capture_mode', value: 'manual' },
      { label: 'provenance.adapter', value: 'outlook' },
      { label: 'provenance.raw_retained', value: 'false' },
      { label: 'connections', value: '[{"target":{"kind":"task","id":"T-1"},"reasons":["capture-link"]}]' },
      { label: 'source.resource_type', value: 'message' },
      { label: 'source.connection_ref', value: 'conn-1' },
      { label: 'source.fingerprint', value: 'sha256:abc' },
      { label: 'source.extra_attr', value: 'nested-unknown' },
      { label: 'normalized.tags', value: '["resume","brief"]' },
      { label: 'normalized.mystery', value: 'keep-me' },
    ])
    expect(contextUnknownFields(recorded).map((field) => field.value)).not.toContain('Mail subject')
    expect(contextUnknownFields(recorded).map((field) => field.value)).not.toContain(
      'Reviewed the thread after the heading was reused.',
    )
    expect(contextUnknownFields(item({
      source: { display_title: 'Mail subject', provider: 'microsoft-outlook' },
    })).some((field) => field.label.startsWith('source.'))).toBe(false)
  })
})

describe('R22 explicit Capture link identity', () => {
  const linked = (patch: Record<string, unknown> = {}) => item({
    id: 'C-0001',
    ref: { kind: 'capture', id: 'C-0001' },
    revision: 3,
    connections: [{ target: { kind: 'task', id: 'T-0001' }, reasons: ['capture-link'] }],
    ...patch,
  })

  test('names the capture and the displayed revision for this Task own link', () => {
    expect(captureLinkRemovalTarget(linked(), 'T-0001')).toEqual({ captureId: 'C-0001', revision: 3 })
    expect(captureLinkRemovalTarget(linked({ revision: 0 }), 'T-0001')).toEqual({ captureId: 'C-0001', revision: 0 })
  })

  test('keeps the link removable when a conversion also connects the same Task', () => {
    const both = linked({
      connections: [{ target: { kind: 'task', id: 'T-0001' }, reasons: ['capture-link', 'capture-conversion'] }],
    })
    expect(captureLinkRemovalTarget(both, 'T-0001')).toEqual({ captureId: 'C-0001', revision: 3 })
  })

  test('refuses every card that is not this Task explicit Capture link', () => {
    // Conversion only: an origin, not a reversible reference.
    expect(captureLinkRemovalTarget(linked({
      connections: [{ target: { kind: 'task', id: 'T-0001' }, reasons: ['capture-conversion'] }],
    }), 'T-0001')).toBeNull()
    // Another Task link, and an Objective connection.
    expect(captureLinkRemovalTarget(linked(), 'T-0002')).toBeNull()
    expect(captureLinkRemovalTarget(linked({
      connections: [{ target: { kind: 'objective', id: 'O-1' }, reasons: ['capture-link'] }],
    }), 'T-0001')).toBeNull()
    // A note card, whatever its reasons.
    expect(captureLinkRemovalTarget(item({
      id: 'N-1',
      ref: { kind: 'note', id: 'N-1' },
      revision: 3,
      connections: [{ target: { kind: 'task', id: 'T-0001' }, reasons: ['note-link'] }],
    }), 'T-0001')).toBeNull()
    // Legacy unknown identity: no ref, no connections, so no action.
    expect(captureLinkRemovalTarget(item({ id: 'C-0009', revision: 3 }), 'T-0001')).toBeNull()
    expect(captureLinkRemovalTarget(linked({ connections: undefined }), 'T-0001')).toBeNull()
  })

  test('refuses a card with no usable displayed revision', () => {
    for (const revision of [undefined, null, '3', 1.5, -1, true, Number.NaN, Number.MAX_SAFE_INTEGER + 2]) {
      expect(captureLinkRemovalTarget(linked({ revision }), 'T-0001')).toBeNull()
    }
    expect(captureLinkRemovalTarget(linked(), '')).toBeNull()
  })
})
