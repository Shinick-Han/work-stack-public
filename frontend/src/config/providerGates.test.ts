import { describe, expect, test } from 'vitest'
import { providerGatesFromEnv } from './providerGates'

describe('Microsoft Gate 0 build flags', () => {
  test('defaults every read and reply capability to unavailable', () => {
    expect(providerGatesFromEnv({})).toEqual({
      'microsoft-outlook': { read: false, reply: false },
      'microsoft-teams': { read: false, reply: false },
    })
  })

  test('accepts only explicit true and never enables reply without read', () => {
    expect(providerGatesFromEnv({
      VITE_WORKSTACK_OUTLOOK_READ_VERIFIED: ' TRUE ',
      VITE_WORKSTACK_OUTLOOK_REPLY_VERIFIED: 'true',
      VITE_WORKSTACK_TEAMS_READ_VERIFIED: '1',
      VITE_WORKSTACK_TEAMS_REPLY_VERIFIED: 'true',
    })).toEqual({
      'microsoft-outlook': { read: true, reply: true },
      'microsoft-teams': { read: false, reply: false },
    })
  })
})
