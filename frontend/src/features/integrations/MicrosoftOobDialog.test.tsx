import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test, vi } from 'vitest'
import { capture } from '../../test/fixtures'
import {
  MicrosoftOobDialog,
  parseAgentCaptureResultText,
} from './MicrosoftOobDialog'
import {
  outlookReadVerifiedProviderGates,
  verifiedMicrosoftProviderGates,
} from '../../test/providerGates'

const verifiedPacket = {
  schema_version: capture.schema_version,
  source_key: capture.source_key,
  source: capture.source,
  normalized: {
    ...capture.normalized,
    action_items: capture.normalized.action_items.map(({ id: _id, task_id: _taskId, ...action }) => action),
  },
  task_hints: capture.task_hints,
  provenance: {
    capture_mode: 'oob_verified',
    adapter: 'outlook-oob',
    adapter_version: '1.0.0',
    model: 'agent-model',
    prompt_version: 'workstack-oob-v1',
    redaction_policy_version: 'workstack-redaction-v1',
    tool_trace_digest: `sha256:${'c'.repeat(64)}`,
    allowed_tools: ['m365.outlook.read', 'workstack.capture.write'],
    raw_retained: false,
    created_at: '2026-08-29T08:00:03Z',
  },
}

test('copies a read-only Microsoft request and imports the returned packet without editing', async () => {
  const writeText = vi.fn().mockResolvedValue(undefined)
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
  const onSubmit = vi.fn()
  render(
    <MicrosoftOobDialog
      initialMode="request"
      onClose={vi.fn()}
      onSubmit={onSubmit}
      open
      pending={false}
      providerGates={outlookReadVerifiedProviderGates}
      serverError={null}
    />,
  )

  await userEvent.selectOptions(screen.getByLabelText('Microsoft source'), 'microsoft-outlook')
  expect(screen.getByRole('option', { name: /Teams messages/i })).toBeDisabled()
  await userEvent.type(screen.getByLabelText('What should the agent find?'), 'release review messages')
  await userEvent.click(screen.getByRole('button', { name: 'Copy request' }))

  expect(writeText).toHaveBeenCalledOnce()
  expect(writeText.mock.calls[0][0]).toContain('do not perform any write action')
  expect(writeText.mock.calls[0][0]).toContain('"operation": "search_and_capture"')
  expect(screen.getByText(/request ready for your connected agent/i)).toBeInTheDocument()

  await userEvent.click(screen.getByRole('button', { name: 'Import agent result' }))
  fireEvent.change(screen.getByLabelText('Agent result'), { target: { value: `\`\`\`json\n${JSON.stringify([verifiedPacket])}\n\`\`\`` } })
  expect(screen.getByText('1 sanitized capture ready')).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: 'Import 1 result' }))
  expect(onSubmit).toHaveBeenCalledWith([expect.objectContaining({ source_key: capture.source_key })])
})

test('agent-result parser rejects raw fields and non-verified handoffs', () => {
  const parseResult = (value: unknown) => parseAgentCaptureResultText(JSON.stringify(value), verifiedMicrosoftProviderGates)
  expect(() => parseResult({ ...verifiedPacket, body: 'raw message' })).toThrow(/raw-content/i)
  expect(() => parseResult({
    ...verifiedPacket,
    provenance: capture.provenance,
  })).toThrow(/oob_verified/i)

  expect(() => parseResult({
    ...verifiedPacket,
    provenance: { ...verifiedPacket.provenance, allowed_tools: ['m365.teams.read', 'workstack.capture.write'] },
  })).toThrow(/provider read tool/i)

  const overDepthPath = Array.from({ length: 5 }, () => undefined)
    .reduce<string>((value) => value.replace(/%/g, '%25'), '%2Fmail%2Fread')
  expect(() => parseResult({
    ...verifiedPacket,
    source: { ...verifiedPacket.source, web_url: `https://outlook.office.com/mail/read?path=${overDepthPath}` },
  })).toThrow(/Microsoft source links/i)

  const unsafeLocators = {
    resource_type: ['cc', 'opaque'].join('%3A'),
    connection_ref: ['recipients', 'opaque+other'].join('%3D'),
    container_ref: ['recipient', 'opaque'].join('%3A'),
    object_ref: ['to', 'opaque'].join('%3A'),
    version_ref: ['bcc', 'opaque'].join('%3D'),
  }
  for (const [field, value] of Object.entries(unsafeLocators)) {
    expect(() => parseResult({
      ...verifiedPacket,
      source: { ...verifiedPacket.source, [field]: value },
    })).toThrow(/source locator/i)
  }

  const teamsPacket = {
    ...verifiedPacket,
    source: {
      ...verifiedPacket.source,
      provider: 'microsoft-teams',
      resource_type: 'channel.message',
      connection_ref: 'tenant:opaque',
      container_ref: 'channel:19:meeting_demo@thread.v2',
      object_ref: 'message:1740000000000',
      version_ref: 'etag:opaque',
      web_url: 'https://teams.microsoft.com/l/message/19%3Ameeting_demo%40thread.v2/1740000000000',
    },
    provenance: {
      ...verifiedPacket.provenance,
      allowed_tools: ['m365.teams.read', 'workstack.capture.write'],
    },
  }
  expect(parseResult(teamsPacket)[0].source.container_ref).toBe('channel:19:meeting_demo@thread.v2')
})

test('does not copy a Microsoft request containing credential material', async () => {
  const writeText = vi.fn().mockResolvedValue(undefined)
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
  render(
    <MicrosoftOobDialog
      initialMode="request"
      onClose={vi.fn()}
      onSubmit={vi.fn()}
      open
      pending={false}
      providerGates={outlookReadVerifiedProviderGates}
      serverError={null}
    />,
  )

  const unsafeQuery = ['Bear' + 'er', 'a'.repeat(24)].join(' ')
  await userEvent.type(screen.getByLabelText('What should the agent find?'), unsafeQuery)
  await userEvent.click(screen.getByRole('button', { name: 'Copy request' }))

  expect(writeText).not.toHaveBeenCalled()
  expect(screen.getByRole('alert')).toHaveTextContent(/must not contain credentials/i)
})
