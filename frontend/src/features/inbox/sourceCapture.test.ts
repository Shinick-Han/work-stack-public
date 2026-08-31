import { describe, expect, test } from 'vitest'
import { buildManualWebCapturePacket, classifyMicrosoftSourceUrl, parseExternalSourceCapture, sanitizeMicrosoftSourceUrl } from './sourceCapture'

describe('Microsoft web source capture', () => {
  test('accepts a bounded external handoff and rejects unknown providers', () => {
    expect(parseExternalSourceCapture({
      provider: 'teams',
      title: '  Review deployment signal  ',
      text: 'Create a follow-up task.',
      sourceUrl: 'https://teams.microsoft.com/v2/',
      capturedAt: '2026-08-31T01:00:00Z',
    })).toMatchObject({ provider: 'teams', title: 'Review deployment signal' })
    expect(parseExternalSourceCapture({ provider: 'word', title: 'x', text: 'y', sourceUrl: '', capturedAt: '' })).toBeNull()
  })

  test('drops unsupported or credential-shaped source URLs', () => {
    expect(sanitizeMicrosoftSourceUrl('https://teams.microsoft.com/v2/')).toBe('https://teams.microsoft.com/v2/')
    expect(sanitizeMicrosoftSourceUrl('https://teams.microsoft.com/v2/?access_token=secret')).toBeNull()
    expect(sanitizeMicrosoftSourceUrl('https://teams.live.com/v2/')).toBe('https://teams.live.com/v2/')
    expect(sanitizeMicrosoftSourceUrl('https://outlook.live.com/mail/inbox/id/abc')).toBe('https://outlook.live.com/mail/inbox/id/abc')
  })

  test('distinguishes item deep links from app and authentication links', () => {
    expect(classifyMicrosoftSourceUrl('outlook', 'https://outlook.live.com/mail/inbox/id/abc')).toBe('item')
    expect(classifyMicrosoftSourceUrl('outlook', 'https://outlook.office.com/mail/')).toBe('app')
    expect(classifyMicrosoftSourceUrl('outlook', 'https://login.live.com/')).toBe('auth')
    expect(classifyMicrosoftSourceUrl('teams', 'https://teams.microsoft.com/l/message/19:thread/123')).toBe('item')
  })

  test('builds a canonical manual capture without claiming OOB provenance', async () => {
    const packet = await buildManualWebCapturePacket({
      provider: 'onenote',
      captureTitle: 'Planning note',
      text: 'Prepare the owner-by-owner action plan.',
      taskTitle: 'Turn the note into a task',
      taskDetail: 'Prepare the owner-by-owner action plan.',
      sourceUrl: 'https://tenant.sharepoint.com/sites/work/Shared%20Documents/notebook',
      capturedAt: '2026-08-31T01:00:00Z',
      priority: 'P2',
      due: null,
      objectiveIds: ['O-1'],
      taskId: null,
    })
    expect(packet.source.provider).toBe('manual')
    expect(packet.source.resource_type).toBe('microsoft-web.onenote')
    expect(packet.source.display_title).toBe('Planning note')
    expect(packet.normalized.action_items[0]?.title).toBe('Turn the note into a task')
    expect(packet.source_key).toMatch(/^sha256:[0-9a-f]{64}$/)
    expect(packet.source.fingerprint).toMatch(/^sha256:[0-9a-f]{64}$/)
    expect(packet.provenance.capture_mode).toBe('manual')
    expect(packet.normalized.tags).toContain('source:onenote')
  })
})
