import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { expect, test } from 'vitest'
import { describeKnowledgeExecutionFailure } from '../../api/knowledgeExecution'
import { KnowledgeCaptureImportError } from '../../domain/schemaKnowledgeCapture'
import { acceptKnowledgeExecutionProposal } from './knowledgeExecutionAccept'

/**
 * Cross-language regression for the R31 browser defect.
 *
 * `knowledgeExecutionProposalWire.owned.json` is *generated* by
 * `tests/test_knowledge_execution_proposal.py` from the real backend
 * projection; that test fails if this file goes stale. Nothing here invents a
 * response, so a repair that only satisfies one language cannot pass both.
 *
 * `accepted_data` is what the execute route returns today. `internal_digest_data`
 * is what it returned before the repair: staging's internal digest material,
 * whose normalized actions carry the Capture model's generated `id`. The parser
 * stays strictly closed in both directions — this asserts the backend stopped
 * sending a field, not that the browser started tolerating one.
 */
interface ProposalWireFixture {
  request_id: string
  result_limit: number
  accepted_data: unknown
  internal_digest_data: unknown
  generated_action_ids: string[]
}

function fixture(): ProposalWireFixture {
  return JSON.parse(readFileSync(
    join(dirname(fileURLToPath(import.meta.url)), 'knowledgeExecutionProposalWire.owned.json'),
    'utf8',
  )) as ProposalWireFixture
}

function issued(wire: ProposalWireFixture) {
  return { request_id: wire.request_id, result_limit: wire.result_limit }
}

test('the real execute projection is admitted by the closed import parser', () => {
  const wire = fixture()
  const envelope = acceptKnowledgeExecutionProposal(wire.accepted_data, issued(wire))
  expect(envelope.request_id).toBe(wire.request_id)
  expect(envelope.items).toHaveLength(1)
  const actions = envelope.items[0].normalized.action_items
  // An empty action list hides this defect entirely: the two shapes coincide.
  expect(actions.length).toBeGreaterThanOrEqual(1)
  expect(actions.map((action) => action.title)).toEqual([
    'Confirm the rollback owner',
    'Re-run the gate after the fix',
  ])
  expect(actions.map((action) => action.priority)).toEqual(['P2', 'P1'])
  expect(actions.map((action) => action.due)).toEqual([null, '2026-09-30'])
  for (const action of actions) {
    expect(Object.keys(action).sort()).toEqual(['detail', 'due', 'priority', 'title'])
  }
})

test('the pre-repair digest shape is still refused as an unknown field', () => {
  const wire = fixture()
  expect(wire.generated_action_ids.length).toBeGreaterThanOrEqual(1)
  for (const id of wire.generated_action_ids) {
    expect(id).toMatch(/^A-[0-9a-f]{16}$/)
  }
  expect(JSON.stringify(wire.internal_digest_data)).toContain(wire.generated_action_ids[0])
  let caught: unknown
  try {
    acceptKnowledgeExecutionProposal(wire.internal_digest_data, issued(wire))
  } catch (error) {
    caught = error
  }
  expect(caught).toBeInstanceOf(KnowledgeCaptureImportError)
  expect((caught as KnowledgeCaptureImportError).code).toBe('unknown_field')
  // Why the browser showed a generic unknown outcome and no Review result.
  expect(describeKnowledgeExecutionFailure(caught)).toMatch(/outcome is not known/)
})

test('an action id added to the admitted wire is refused, not tolerated', () => {
  const wire = fixture()
  const widened = JSON.parse(JSON.stringify(wire.accepted_data)) as {
    items: { normalized: { action_items: Record<string, unknown>[] } }[]
  }
  widened.items[0].normalized.action_items[0].id = wire.generated_action_ids[0]
  expect(() => acceptKnowledgeExecutionProposal(widened, issued(wire)))
    .toThrow(KnowledgeCaptureImportError)
})
