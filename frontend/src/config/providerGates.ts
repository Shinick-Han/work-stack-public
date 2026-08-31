import type { MicrosoftProvider } from '../domain/types'

export interface ProviderCapabilityGate {
  read: boolean
  reply: boolean
}

export type MicrosoftProviderGates = Record<MicrosoftProvider, ProviderCapabilityGate>

type ProviderGateEnvironment = Partial<Record<
  | 'VITE_WORKSTACK_OUTLOOK_READ_VERIFIED'
  | 'VITE_WORKSTACK_OUTLOOK_REPLY_VERIFIED'
  | 'VITE_WORKSTACK_TEAMS_READ_VERIFIED'
  | 'VITE_WORKSTACK_TEAMS_REPLY_VERIFIED',
  unknown
>>

function verified(value: unknown) {
  return typeof value === 'string' && value.trim().toLowerCase() === 'true'
}

export function providerGatesFromEnv(environment: ProviderGateEnvironment): MicrosoftProviderGates {
  const outlookRead = verified(environment.VITE_WORKSTACK_OUTLOOK_READ_VERIFIED)
  const teamsRead = verified(environment.VITE_WORKSTACK_TEAMS_READ_VERIFIED)
  return {
    'microsoft-outlook': {
      read: outlookRead,
      reply: outlookRead && verified(environment.VITE_WORKSTACK_OUTLOOK_REPLY_VERIFIED),
    },
    'microsoft-teams': {
      read: teamsRead,
      reply: teamsRead && verified(environment.VITE_WORKSTACK_TEAMS_REPLY_VERIFIED),
    },
  }
}

export const microsoftProviderGates = providerGatesFromEnv(import.meta.env)

export function providerReadVerified(provider: MicrosoftProvider, gates = microsoftProviderGates) {
  return gates[provider].read
}

export function providerReplyVerified(provider: MicrosoftProvider, gates = microsoftProviderGates) {
  return gates[provider].read && gates[provider].reply
}

export function anyProviderReadVerified(gates = microsoftProviderGates) {
  return Object.values(gates).some((gate) => gate.read)
}
