import { expect, test } from 'vitest'

import { connectionCenterGatesFrom } from './connectionCenterGates'

test('keeps registry and activation routing dark by default', () => {
  expect(connectionCenterGatesFrom({}, undefined)).toEqual({ registry: false, activation: false })
  expect(connectionCenterGatesFrom({ VITE_WORKSTACK_CONNECTION_REGISTRY: 'yes' }, { connectionRegistry: 1 })).toEqual({ registry: false, activation: false })
})

test('accepts only explicit build or runtime opt-in and never activates without registry routing', () => {
  expect(connectionCenterGatesFrom({ VITE_WORKSTACK_CONNECTION_REGISTRY: ' TRUE ' }, undefined)).toEqual({ registry: true, activation: false })
  expect(connectionCenterGatesFrom({}, { connectionRegistry: true })).toEqual({ registry: true, activation: false })
  expect(connectionCenterGatesFrom(
    { VITE_WORKSTACK_CONNECTION_REGISTRY_ACTIVATION: 'true' },
    undefined,
  )).toEqual({ registry: false, activation: false })
  expect(connectionCenterGatesFrom({}, {
    connectionRegistry: true,
    connectionRegistryActivation: true,
  })).toEqual({ registry: true, activation: true })
})

test('enables path selection and restart activation for the trusted desktop host', () => {
  expect(connectionCenterGatesFrom({}, undefined, true)).toEqual({ registry: true, activation: true })
})
