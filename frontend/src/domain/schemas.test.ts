import { describe, expect, test } from 'vitest'
import {
  oobRequestSchema,
  replyCommandSchema,
  replyReceiptSchema,
  taskSchema,
  taskDetailSchema,
} from './schemas'
import { task } from '../test/fixtures'

const digest = `sha256:${'a'.repeat(64)}`
const addEncodingLayers = (value: string, additionalLayers: number) => Array.from(
  { length: additionalLayers },
  () => undefined,
).reduce<string>((encoded) => encoded.replace(/%/g, '%25'), value)

const receipt = {
  schema_version: '1.0',
  reply_id: 'R-0001',
  provider: 'microsoft-outlook',
  outcome: 'sent',
  occurred_at: '2026-08-29T10:00:00Z',
  body_digest: digest,
  target_digest: digest,
}

const command = {
  id: 'R-0001',
  task_id: 'T-0001',
  capture_id: 'C-0001',
  capture_revision: 2,
  provider: 'microsoft-outlook',
  capability: 'outlook.reply',
  target: {
    resource_type: 'mail.message',
    connection_ref: 'connection:opaque',
    container_ref: 'mailbox:opaque',
    object_ref: 'message:opaque',
    version_ref: 'change-key:v2',
  },
  body: 'Approved plain-text reply.',
  body_digest: digest,
  target_digest: digest,
  state: 'approved',
  approved_at: '2026-08-29T09:55:00Z',
  receipt: null,
  created_at: '2026-08-29T09:55:00Z',
  updated_at: '2026-08-29T09:55:00Z',
}

describe('OOB and reply wire schemas', () => {
  test('accepts only the strict OobRequest v1 envelope', () => {
    const request = {
      request_id: '11111111-1111-4111-8111-111111111111',
      schema_version: '1.0',
      provider: 'microsoft-teams',
      operation: 'search_and_capture',
      query: `release review for ${['teammate', 'example.invalid'].join('@')}`,
      result_limit: 5,
      requested_at: '2026-08-29T09:00:00Z',
    }
    expect(oobRequestSchema.parse(request)).toEqual(request)
    const unexpectedCredentialKey = ['access', 'token'].join('_')
    expect(() => oobRequestSchema.parse({ ...request, [unexpectedCredentialKey]: 'redacted-canary' })).toThrow()

    const bearerQuery = ['Bear' + 'er', 'a'.repeat(24)].join(' ')
    const encodedAssignment = [['access', '%5Ftoken'].join(''), 'b'.repeat(24)].join('%3D')
    const encodedTokenUrl = addEncodingLayers(
      `https%3A%2F%2Fexample.invalid%2Fcallback%3F${encodedAssignment}`,
      1,
    )
    const rawCanary = ['RAW', 'CANARY', 'DO', 'NOT', 'STORE'].join('_')
    const unsafeQueries = [
      bearerQuery,
      encodedAssignment,
      encodedTokenUrl,
      rawCanary,
      '%3Cb%3Eraw%3C%2Fb%3E',
      ['T' + 'o', 'opaque-recipient'].join('%3A'),
      ['recipients', 'opaque-recipient'].join('%3A'),
      ['release', 'review'].join('%00'),
      addEncodingLayers('release%20review', 5),
    ]
    for (const query of unsafeQueries) {
      expect(() => oobRequestSchema.parse({ ...request, query })).toThrow(/must not contain/i)
    }
  })

  test('binds provider capability and rejects recipient-shaped receipt extras', () => {
    expect(replyCommandSchema.parse(command)).toMatchObject({ id: 'R-0001', capability: 'outlook.reply' })
    expect(() => replyCommandSchema.parse({ ...command, capability: 'teams.reply' })).toThrow(/outlook.reply/i)
    expect(() => replyReceiptSchema.parse({ ...receipt, recipients: ['opaque-recipient'] })).toThrow()
    expect(() => replyReceiptSchema.parse({ ...receipt, web_url: 'https://example.com/message' })).toThrow(/microsoft/i)
  })

  test('accepts opaque Microsoft message references but rejects URLs and recipient-shaped values', () => {
    expect(replyReceiptSchema.parse({ ...receipt, remote_message_ref: '19:meeting_N2Qx@thread.v2' }).remote_message_ref).toBe('19:meeting_N2Qx@thread.v2')
    expect(() => replyReceiptSchema.parse({ ...receipt, remote_message_ref: 'https://graph.microsoft.com/message' })).toThrow(/URL/i)
    const addressShaped = ['opaque', 'example.invalid'].join('@')
    expect(() => replyReceiptSchema.parse({ ...receipt, remote_message_ref: addressShaped })).toThrow(/email/i)
    const credentialShaped = [['access', 'token'].join('_'), 'abcdefgh'].join(':')
    expect(() => replyReceiptSchema.parse({ ...receipt, remote_message_ref: credentialShaped })).toThrow(/credential/i)

    const encodedSafeReference = '19%3Ameeting_N2Qx%40thread.v2'
    const deeplyEncodedSafeReference = addEncodingLayers(encodedSafeReference, 4)
    for (const remoteMessageRef of [encodedSafeReference, deeplyEncodedSafeReference]) {
      expect(replyReceiptSchema.parse({ ...receipt, remote_message_ref: remoteMessageRef }).remote_message_ref).toBe(remoteMessageRef)
    }
    const encodedBearer = ['Bear' + 'er', 'a'.repeat(24)].join('%20')
    const encodedAssignment = [['access', '%5Ftoken'].join(''), 'b'.repeat(24)].join('%3D')
    const encodedAddress = ['recipient', ['example', 'invalid'].join('.')].join('%40')
    const encodedRecipientAssignment = ['recipients', 'opaque+other'].join('%3D')
    const recipientAssignment = ['recipient', 'opaque'].join(':')
    const deeplyEncodedBearer = addEncodingLayers(encodedBearer, 4)
    const sixLayerBearer = addEncodingLayers(encodedBearer, 5)
    const elevenLayerBearer = addEncodingLayers(encodedBearer, 10)
    const sixLayerSafeReference = addEncodingLayers(encodedSafeReference, 5)
    const encodedJson = 'id:%7B%22connector%22%3A%22dump%22%7D'
    const encodedHtml = 'id:%3Cb%3Eraw%3C%2Fb%3E'
    const encodedHeader = ['T' + 'o', 'opaque'].join('%3A')
    const encodedControl = ['id:', 'opaque'].join('%00')
    const rawCanary = ['RAW', 'CANARY', 'DO', 'NOT', 'STORE'].join('_')
    const encodedRawCanary = `id:${rawCanary.replace('RAW', '%52%41%57')}`
    for (const remoteMessageRef of [
      encodedBearer,
      encodedAssignment,
      encodedAddress,
      encodedRecipientAssignment,
      recipientAssignment,
      deeplyEncodedBearer,
      sixLayerBearer,
      elevenLayerBearer,
      sixLayerSafeReference,
      encodedJson,
      encodedHtml,
      encodedHeader,
      encodedControl,
      encodedRawCanary,
    ]) {
      expect(() => replyReceiptSchema.parse({ ...receipt, remote_message_ref: remoteMessageRef })).toThrow(/encoded content/i)
    }
  })

  test('rejects credential material anywhere in receipt URLs while preserving Microsoft deep links', () => {
    const accessName = ['access', 'token'].join('_')
    const clientSecretName = ['client', 'secret'].join('_')
    const secretValue = ['synthetic', 'credential', 'value'].join('-')
    const inner = `https://example.invalid/callback?${accessName}=${secretValue}`
    const bearerValue = ['Bearer', 'a'.repeat(26)].join(' ')
    const jwtValue = ['eyJ' + 'd'.repeat(12), 'e'.repeat(16), 'f'.repeat(16)].join('.')
    const unsafeUrls = [
      `https://outlook.office.com/mail/read#${accessName}=${secretValue}`,
      `https://outlook.office.com/mail/read?continue=${encodeURIComponent(encodeURIComponent(inner))}`,
      `https://outlook.office.com/mail/read?${clientSecretName}=${secretValue}`,
      `https://outlook.office.com/mail/read?${'co' + 'de'}=${secretValue}`,
      `https://outlook.office.com/mail/read?payload=${encodeURIComponent(bearerValue)}`,
      `https://teams.microsoft.com/l/message/opaque?payload=${jwtValue}`,
      `https://outlook.office.com/mail/read?to=${['recipient', 'example.invalid'].join('@')}`,
      `https://outlook.office.com/mail/read?to=${['recipient', 'example.invalid'].join('%40')}`,
      `https://teams.microsoft.com/l/message/${['recipient', 'example.invalid'].join('%40')}/opaque`,
      'https://outlook.office.com/mail/read?recipients=opaque+other',
    ]
    for (const webUrl of unsafeUrls) {
      expect(() => replyReceiptSchema.parse({ ...receipt, web_url: webUrl })).toThrow(/token-free/i)
    }

    const legitimateUrls = [
      'https://outlook.office.com/mail/deeplink/read/opaque?ItemID=opaque-item&exvsurl=1&path=%2Fmail%2Finbox',
      'https://teams.microsoft.com/l/message/19%3Ademo%40thread.v2/1740000000000?tenantId=opaque-tenant&groupId=opaque-group&context=%7B%22contextType%22%3A%22channel%22%7D',
      'https://outlook.office.com/mail/deeplink/read/opaque#path=/mail/inbox',
    ]
    for (const webUrl of legitimateUrls) {
      expect(replyReceiptSchema.parse({ ...receipt, web_url: webUrl }).web_url).toBe(webUrl)
    }
  })

  test('strictly parses task-detail replies while defaulting old empty details', () => {
    expect(taskDetailSchema.parse({ task, context: [], activity: [] }).replies).toEqual([])
    expect(taskDetailSchema.parse({ task, context: [], activity: [], replies: [command] }).replies[0].id).toBe('R-0001')
  })
})

describe('task identity wire schema', () => {
  test('requires a stable UUID and an explicit safe revision', () => {
    expect(taskSchema.parse(task)).toMatchObject({ uid: task.uid, revision: task.revision })
    const { uid: _uid, ...withoutUid } = task
    const { revision: _revision, ...withoutRevision } = task
    expect(() => taskSchema.parse(withoutUid)).toThrow()
    expect(() => taskSchema.parse(withoutRevision)).toThrow()
    expect(() => taskSchema.parse({ ...task, uid: 'ffffffff-ffff-ffff-ffff-ffffffffffff' })).toThrow()
    expect(() => taskSchema.parse({ ...task, uid: '00000000-0000-0000-0000-000000000000' })).toThrow()
    expect(() => taskSchema.parse({ ...task, revision: Number.MAX_SAFE_INTEGER + 1 })).toThrow()
  })
})
