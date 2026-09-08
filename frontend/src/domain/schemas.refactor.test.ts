import { describe, expect, test } from 'vitest'
import * as facade from './schemas'
import {
  noteSchema,
  planningSnapshotSchema,
  snapshotPreviewSchema,
  taskSchema,
  worklogEntrySchema,
} from './schemaPlanning'
import {
  capturePacketSchema,
  captureSchema,
  findForbiddenCaptureKey,
  oobRequestSchema,
  replyCommandSchema,
  replyReceiptSchema,
} from './schemaSourceCaptureReply'
import {
  checkpointAuditEntrySchema,
  checkpointAuditSchema,
  checkpointTransitionEventSchema,
} from './schemaCheckpoint'
import { capture, task } from '../test/fixtures'

const PUBLIC_EXPORTS = [
  'capturePacketSchema',
  'captureSchema',
  'checkpointAuditEntrySchema',
  'checkpointAuditSchema',
  'checkpointTransitionEventSchema',
  'findForbiddenCaptureKey',
  'noteSchema',
  'objectiveDetailSchema',
  'objectiveSchema',
  'oobRequestSchema',
  'planningSnapshotSchema',
  'replyCommandSchema',
  'replyReceiptSchema',
  'replyTargetSchema',
  'reviewProjectionSchema',
  'searchProjectionSchema',
  'snapshotPreviewSchema',
  'storageStatusSchema',
  'syncStatusSchema',
  'taskDetailSchema',
  'taskSchema',
  'workSessionProjectionSchema',
  'workSessionSchema',
  'worklogEntrySchema',
  'workspaceRebindPreviewSchema',
  'workspaceRebindResultSchema',
  'workspaceSchema',
] as const

describe('schemas facade extraction', () => {
  test('keeps the public export set and the same runtime schema objects', () => {
    expect(Object.keys(facade).sort()).toEqual([...PUBLIC_EXPORTS].sort())
    expect(facade.taskSchema).toBe(taskSchema)
    expect(facade.noteSchema).toBe(noteSchema)
    expect(facade.planningSnapshotSchema).toBe(planningSnapshotSchema)
    expect(facade.snapshotPreviewSchema).toBe(snapshotPreviewSchema)
    expect(facade.worklogEntrySchema).toBe(worklogEntrySchema)
    expect(facade.capturePacketSchema).toBe(capturePacketSchema)
    expect(facade.captureSchema).toBe(captureSchema)
    expect(facade.oobRequestSchema).toBe(oobRequestSchema)
    expect(facade.replyCommandSchema).toBe(replyCommandSchema)
    expect(facade.replyReceiptSchema).toBe(replyReceiptSchema)
    expect(facade.findForbiddenCaptureKey).toBe(findForbiddenCaptureKey)
    expect(facade.checkpointTransitionEventSchema).toBe(checkpointTransitionEventSchema)
    expect(facade.checkpointAuditEntrySchema).toBe(checkpointAuditEntrySchema)
    expect(facade.checkpointAuditSchema).toBe(checkpointAuditSchema)
  })

  test('still walks nested forbidden capture keys from the facade export', () => {
    expect(facade.findForbiddenCaptureKey({ nested: [{ Body: 'raw' }] })).toBe('$.nested[0].Body')
    expect(facade.findForbiddenCaptureKey({ summary: 'ok' })).toBeNull()
  })

  test('composed task-detail still accepts a capture context row and rejects extras', () => {
    const parsed = facade.taskDetailSchema.parse({
      task,
      context: [capture],
      activity: [],
    })
    expect(parsed.replies).toEqual([])
    expect(parsed.context[0]).toEqual(capture)
    expect(() => facade.taskDetailSchema.parse({
      task,
      context: [],
      activity: [],
      unexpected: true,
    })).toThrow()
  })
})
