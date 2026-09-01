import { expect, test } from 'vitest'

import { connectionRecoveryGateFrom } from './connectionRecoveryGate'

test('keeps startup activation recovery dark by default', () => {
  expect(connectionRecoveryGateFrom({}, undefined)).toBe(false)
  expect(connectionRecoveryGateFrom({ VITE_WORKSTACK_CONNECTION_RECOVERY: 'yes' }, 1)).toBe(false)
})

test('requires an explicit build or trusted runtime opt-in', () => {
  expect(connectionRecoveryGateFrom({ VITE_WORKSTACK_CONNECTION_RECOVERY: ' true ' }, undefined)).toBe(true)
  expect(connectionRecoveryGateFrom({}, true)).toBe(true)
})
