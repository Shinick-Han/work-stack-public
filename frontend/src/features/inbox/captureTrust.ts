import {
  microsoftProviderGates,
  providerReadVerified,
  type MicrosoftProviderGates,
} from '../../config/providerGates'
import { MICROSOFT_PROVIDERS, type Capture, type MicrosoftProvider } from '../../domain/types'

export function captureTrust(
  capture: Capture,
  gates: MicrosoftProviderGates = microsoftProviderGates,
) {
  if (capture.provenance.capture_mode === 'manual') {
    return { label: 'Manual import', tone: 'neutral' }
  }
  const provider = capture.source.provider
  const verified = MICROSOFT_PROVIDERS.includes(provider as MicrosoftProvider)
    && providerReadVerified(provider as MicrosoftProvider, gates)
  return verified
    ? { label: 'OOB verified', tone: 'verified' }
    : { label: 'Supplied provenance · Gate 0 unverified', tone: 'neutral' }
}
