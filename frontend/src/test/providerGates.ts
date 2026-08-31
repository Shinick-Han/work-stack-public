import type { MicrosoftProviderGates } from '../config/providerGates'

export const verifiedMicrosoftProviderGates: MicrosoftProviderGates = {
  'microsoft-outlook': { read: true, reply: true },
  'microsoft-teams': { read: true, reply: true },
}

export const outlookReadVerifiedProviderGates: MicrosoftProviderGates = {
  'microsoft-outlook': { read: true, reply: false },
  'microsoft-teams': { read: false, reply: false },
}
