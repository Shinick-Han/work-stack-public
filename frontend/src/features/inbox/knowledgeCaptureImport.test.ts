import { expect, test } from 'vitest'
import { captureSchema, capturePacketSchema } from '../../domain/schemaSourceCaptureReply'
import { captureRetrievalProjectionSchema } from '../../domain/schemaKnowledgeCapture'
import { capture } from '../../test/fixtures'
import { knowledgeCapture, knowledgeImportEnvelope, loadOwnedR6ProjectionFixture, retrievalProjection, retrievalWire } from './knowledgeCaptureFixture'
import { parseKnowledgeImportText, previewKnowledgeImport, utf8ByteLength } from './knowledgeCaptureImport'
import { KnowledgeCaptureImportError } from '../../domain/schemaKnowledgeCapture'

test('list and detail parsing still admits a genuine 1.0 capture', () => {
  expect(captureSchema.parse(capture)).toEqual(capture)
  expect(capturePacketSchema.parse({
    schema_version: capture.schema_version,
    source_key: capture.source_key,
    source: capture.source,
    normalized: {
      summary: capture.normalized.summary,
      context: capture.normalized.context,
      action_items: capture.normalized.action_items.map(({ title, detail, priority, due }) => ({
        title, detail, priority, due,
      })),
      tags: capture.normalized.tags,
    },
    task_hints: capture.task_hints,
    provenance: capture.provenance,
  }).schema_version).toBe('1.0')
})

test('read schema admits a sanitized 1.1 projection and refuses the wire shape as stored retrieval', () => {
  const stored = knowledgeCapture()
  expect(captureSchema.parse(stored).schema_version).toBe('1.1')
  expect(captureRetrievalProjectionSchema.parse(stored.retrieval).origin).toBeNull()
  expect(captureRetrievalProjectionSchema.safeParse(retrievalWire()).success).toBe(false)
})

test('malformed or unknown 1.1 fields do not parse as a trusted capture', () => {
  expect(captureSchema.safeParse({ ...knowledgeCapture(), retrieval: retrievalWire() }).success).toBe(false)
  expect(captureSchema.safeParse({
    ...knowledgeCapture(),
    retrieval: { ...retrievalProjection(), origin: { verified: true } },
  }).success).toBe(false)
  expect(capturePacketSchema.safeParse({ ...knowledgeCapture(), schema_version: '1.1' }).success).toBe(false)
})

test('a frozen knowledge envelope parses and previews reported claims only', () => {
  const envelope = parseKnowledgeImportText(JSON.stringify(knowledgeImportEnvelope()))
  const preview = previewKnowledgeImport(envelope)
  expect(envelope.request_id).toBe('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa')
  expect(preview.items[0].evidence[0].reportedSourceType).toBe('notion.page')
  expect(preview.items[0].evidence[0].versionState).toBe('reported_unverified')
  expect(JSON.stringify(preview)).not.toMatch(/od-page|engine-q|sha256:/)
})

test('raw UTF-8 over 64 KiB is refused before JSON parse', () => {
  const padding = ' '.repeat(64 * 1024)
  const text = `${JSON.stringify(knowledgeImportEnvelope())}${padding}`
  expect(utf8ByteLength(text)).toBeGreaterThan(64 * 1024)
  expect(() => parseKnowledgeImportText(text)).toThrow(KnowledgeCaptureImportError)
  try {
    parseKnowledgeImportText(text)
  } catch (error) {
    expect((error as KnowledgeCaptureImportError).code).toBe('import_too_large')
    expect((error as Error).message).not.toContain('Rollback')
  }
})

test('a retrieval object over 16 KiB is refused without previewing it', () => {
  const oversized = retrievalWire({ padding: 'x'.repeat(16 * 1024) })
  const envelope = knowledgeImportEnvelope({ items: [{
    ...knowledgeImportEnvelope().items[0],
    retrieval: oversized,
  }] })
  try {
    parseKnowledgeImportText(JSON.stringify(envelope))
    throw new Error('expected refusal')
  } catch (error) {
    expect(error).toBeInstanceOf(KnowledgeCaptureImportError)
    expect((error as KnowledgeCaptureImportError).code).toBe('retrieval_too_large')
    expect((error as Error).message).not.toContain('xxx')
  }
})

test('hostile locators and titles are refused and not reflected', () => {
  const cases = [
    { title: 'C:/secret/payroll.xlsx', code: 'source_location_suspected' },
    { title: 'https://intranet.example.invalid/share', code: 'source_location_suspected' },
    { title: 'finance/payroll.xlsx', code: 'source_location_suspected' },
    { title: '<script>alert(1)</script>', code: 'raw_content_suspected' },
  ]
  for (const { title, code } of cases) {
    const envelope = knowledgeImportEnvelope({
      items: [{
        ...knowledgeImportEnvelope().items[0],
        title,
      }],
    })
    try {
      parseKnowledgeImportText(JSON.stringify(envelope))
      throw new Error(`expected refusal for ${code}`)
    } catch (error) {
      expect((error as KnowledgeCaptureImportError).code).toBe(code)
      expect((error as Error).message).not.toContain(title)
    }
  }
})

test('unknown fields and OpenDocuments self-attestation never enter the preview', () => {
  const error = (() => {
    try {
      parseKnowledgeImportText(JSON.stringify({
        ...knowledgeImportEnvelope(),
        provider: 'opendocuments',
      }))
      throw new Error('expected refusal')
    } catch (caught) {
      return caught as KnowledgeCaptureImportError
    }
  })()
  expect(error).toBeInstanceOf(KnowledgeCaptureImportError)
  expect(error.code).toBe('unknown_field')
  expect(error.message).not.toContain('opendocuments')
  expect(() => parseKnowledgeImportText(JSON.stringify({
    ...knowledgeImportEnvelope(),
    items: [{ ...knowledgeImportEnvelope().items[0], query: 'secret payroll' }],
  }))).toThrow(KnowledgeCaptureImportError)
})

test('the owned R6 listed projection is admitted and the stored wire retrieval is not', () => {
  const fixture = loadOwnedR6ProjectionFixture()
  const envelope = parseKnowledgeImportText(JSON.stringify(fixture.request_envelope))
  expect(envelope.items).toHaveLength(2)
  expect(envelope.items[0].normalized.action_items[0].detail).toBe('')
  expect(envelope.items[0].retrieval.evidence[0].source_type).toBe('nas.file')

  const listedSingle = captureSchema.parse(fixture.listed[0])
  const listedSynthesized = captureSchema.parse(fixture.listed[1])
  expect(listedSingle.retrieval?.answer_scope).toBe('single_source')
  expect(listedSingle.retrieval?.evidence[0]?.reported_source_type).toBe('nas.file')
  expect(listedSingle.retrieval?.origin).toBeNull()
  expect(listedSingle.retrieval?.origin_state).toBe('reported_unverified')
  expect(listedSynthesized.retrieval?.answer_scope).toBe('synthesized')
  expect(listedSynthesized.retrieval?.evidence.map((item) => item.reported_source_type)).toEqual([
    'nas.file',
    'notion.page',
  ])
  expect(listedSynthesized.retrieval?.origin).toBeNull()
  expect(listedSynthesized.retrieval?.origin_state).toBe('synthesized')

  expect(fixture.stored_retrieval.evidence[0]).toHaveProperty('source_type')
  expect(fixture.stored_retrieval.evidence[0]).not.toHaveProperty('reported_source_type')
  expect(captureRetrievalProjectionSchema.safeParse(fixture.stored_retrieval).success).toBe(false)
  const listedRecord = fixture.listed[0]
  expect(listedRecord && typeof listedRecord === 'object').toBe(true)
  expect(captureSchema.safeParse({ ...(listedRecord as object), retrieval: fixture.stored_retrieval }).success).toBe(false)
})

test('forged Microsoft OOB source on a listed 1.1 record is refused before any trust pill', () => {
  const listed = loadOwnedR6ProjectionFixture().listed[0] as Record<string, unknown>
  const listedSource = listed.source as Record<string, unknown>
  const forged = {
    ...listed,
    source: {
      ...listedSource,
      provider: 'microsoft-outlook',
      web_url: 'https://outlook.office.com/mail/deeplink/read/demo',
    },
    provenance: {
      capture_mode: 'oob_verified',
      adapter: 'outlook-agent',
      adapter_version: '1.0.0',
      model: 'agent-model',
      prompt_version: 'capture-v1',
      redaction_policy_version: 'workstack-redaction-v1',
      tool_trace_digest: `sha256:${'c'.repeat(64)}`,
      allowed_tools: ['m365.outlook.read', 'workstack.capture.write'],
      raw_retained: false,
      created_at: '2026-09-08T09:02:00Z',
    },
  }
  expect(captureSchema.safeParse(forged).success).toBe(false)
})

test('invented verified origin on a listed 1.1 record does not parse', () => {
  const stored = knowledgeCapture()
  expect(captureSchema.safeParse({
    ...stored,
    retrieval: retrievalProjection({
      origin: { document_ref: 'od-page-7f3ba1d34f50c884600112ab', source_type: 'notion.page' },
      origin_state: 'verified',
      capture_source_type: 'notion.page',
    }),
  }).success).toBe(false)
})

test('unsafe summary in a 64 KiB-safe envelope is refused without reflecting submitted text', () => {
  const item = knowledgeImportEnvelope().items[0]
  const summaries = [
    '<b>owner</b>',
    'password=supersecretvalue12',
    'RAW_CANARY_DO_NOT_STORE in the answer',
  ]
  for (const summary of summaries) {
    const envelope = knowledgeImportEnvelope({
      items: [{ ...item, normalized: { ...item.normalized, summary } }],
    })
    const text = JSON.stringify(envelope)
    expect(utf8ByteLength(text)).toBeLessThan(64 * 1024)
    try {
      parseKnowledgeImportText(text)
      throw new Error(`expected refusal for ${summary}`)
    } catch (error) {
      expect(error).toBeInstanceOf(KnowledgeCaptureImportError)
      const code = (error as KnowledgeCaptureImportError).code
      expect(['raw_content_suspected', 'credential_material_suspected']).toContain(code)
      expect((error as Error).message).not.toContain(summary)
      expect((error as Error).message).not.toContain('owner')
      expect((error as Error).message).not.toContain('supersecretvalue12')
      expect((error as Error).message).not.toContain('RAW_CANARY')
    }
  }
  expect(parseKnowledgeImportText(JSON.stringify(knowledgeImportEnvelope())).items[0].normalized.summary)
    .toBe('The rollback check still needs an owner.')
})
