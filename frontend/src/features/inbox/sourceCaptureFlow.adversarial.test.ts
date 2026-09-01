import { expect, test } from 'vitest'
import {
  buildManualWebCapturePacket,
  classifyMicrosoftSourceUrl,
  sanitizeMicrosoftSourceUrl,
  type SourceCaptureDraft,
} from './sourceCapture'

const baseDraft: SourceCaptureDraft = {
  intentId: '22222222-2222-4222-8222-222222222222',
  provider: 'outlook',
  captureTitle: 'Reviewed Outlook message',
  text: 'Keep only the reviewed action context.',
  taskTitle: 'Follow up on the reviewed message',
  taskDetail: 'Complete the reviewed next action.',
  sourceUrl: 'https://outlook.office.com/mail/inbox/id/opaque-message',
  capturedAt: '2026-08-31T10:00:00.000Z',
  priority: 'P2',
  due: null,
  objectiveIds: [],
  taskId: null,
}

test('manual web capture never claims OOB-verified provenance', async () => {
  const packet = await buildManualWebCapturePacket(baseDraft)

  expect(packet.source.provider).toBe('manual')
  expect(packet.source.resource_type).toBe('microsoft-web.outlook')
  expect(packet.normalized.tags).toEqual(['source:outlook', 'manual-web-capture'])
  expect(packet.provenance).toEqual({
    capture_mode: 'manual',
    adapter: 'microsoft-web-capture',
    adapter_version: '1.0.0',
    redaction_policy_version: 'workstack-redaction-v1',
    raw_retained: false,
    created_at: baseDraft.capturedAt,
  })
})

test('p2 provider-mismatched Microsoft link is not retained under the wrong source label', async () => {
  const teamsLink = 'https://teams.microsoft.com/l/message/19:opaque@thread.v2/1234567890'
  const packet = await buildManualWebCapturePacket({ ...baseDraft, sourceUrl: teamsLink })

  expect(classifyMicrosoftSourceUrl('outlook', teamsLink)).toBe('missing')
  expect(packet.source.web_url).toBeNull()
})

test('p2 credential-bearing fragment is rejected before the UI calls the server', async () => {
  const credentialKey = ['access', 'token'].join('_')
  const credentialValue = ['abcdefgh', 'ijklmnop'].join('')
  const unsafe = `${baseDraft.sourceUrl}#${credentialKey}=${credentialValue}`

  expect(sanitizeMicrosoftSourceUrl(unsafe)).toBeNull()
  const packet = await buildManualWebCapturePacket({ ...baseDraft, sourceUrl: unsafe })
  expect(packet.source.web_url).toBeNull()
})

test('p2 OneNote host accepted by the desktop provider is not silently dropped from the capture', async () => {
  const oneNoteItem = 'https://www.onenote.com/notebooks/opaque-note-id'
  const packet = await buildManualWebCapturePacket({
    ...baseDraft,
    provider: 'onenote',
    sourceUrl: oneNoteItem,
  })

  expect(sanitizeMicrosoftSourceUrl(oneNoteItem)).toBe(oneNoteItem)
  expect(packet.source.web_url).toBe(oneNoteItem)
})
