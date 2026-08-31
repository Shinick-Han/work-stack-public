import { expect, test } from 'vitest'
import { task } from '../../test/fixtures'
import { buildAgendaEvents } from './agendaModel'

test('renders planned work and a distinct immutable deadline without duplicating a shared date', () => {
  expect(buildAgendaEvents([task])).toEqual([
    expect.objectContaining({ id: 'T-0001:scheduled', start: '2026-08-31', editable: true }),
    expect.objectContaining({ id: 'T-0001:due', start: '2026-09-01', editable: false }),
  ])
  expect(buildAgendaEvents([{ ...task, due: task.scheduled ?? null }])).toHaveLength(1)
  expect(buildAgendaEvents([{ ...task, status: 'done' }])).toEqual([])
})
