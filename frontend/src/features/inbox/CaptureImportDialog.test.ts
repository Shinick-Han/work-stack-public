import { expect, test } from 'vitest'
import { parseCapturePacketText } from './CaptureImportDialog'

const manualPacket = {
  schema_version: '1.0',
  source_key: `sha256:${'a'.repeat(64)}`,
  source: {
    provider: 'manual',
    resource_type: 'manual.note',
    connection_ref: 'local-user',
    container_ref: 'manual-inbox',
    object_ref: 'manual:test',
    version_ref: 'manual:v1',
    display_title: 'Sanitized note',
    web_url: null,
    retrieved_at: '2026-08-29T08:30:00Z',
    fingerprint: `sha256:${'b'.repeat(64)}`,
  },
  normalized: { summary: 'Safe summary.', context: 'Safe context.', action_items: [], tags: ['manual'] },
  task_hints: [],
  provenance: {
    capture_mode: 'manual',
    adapter: 'manual-import',
    adapter_version: '1.0.0',
    redaction_policy_version: 'workstack-redaction-v1',
    raw_retained: false,
    created_at: '2026-08-29T08:30:03Z',
  },
}

test('accepts honest manual provenance without model or tool claims', () => {
  expect(parseCapturePacketText(JSON.stringify(manualPacket)).provenance.capture_mode).toBe('manual')
})

test('rejects prohibited raw-content keys before ingest', () => {
  expect(() => parseCapturePacketText(JSON.stringify({ ...manualPacket, body: 'raw mail' }))).toThrow(/raw-content field/i)
})

test('does not allow manual imports to synthesize model provenance', () => {
  const packet = {
    ...manualPacket,
    provenance: { ...manualPacket.provenance, model: 'made-up-model' },
  }
  expect(() => parseCapturePacketText(JSON.stringify(packet))).toThrow()
})

test('refuses self-asserted OOB provenance in the generic manual importer', () => {
  const packet = {
    ...manualPacket,
    source: { ...manualPacket.source, provider: 'microsoft-outlook', resource_type: 'mail.message', web_url: 'https://outlook.office.com/mail/deeplink/read/test' },
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
      created_at: '2026-08-29T08:30:03Z',
    },
  }
  expect(() => parseCapturePacketText(JSON.stringify(packet))).toThrow(/Microsoft 365 agent result importer/i)
})
