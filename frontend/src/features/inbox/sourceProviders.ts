export const SOURCE_PROVIDER_KEYS = ['outlook', 'teams', 'onenote'] as const

export type SourceProviderKey = (typeof SOURCE_PROVIDER_KEYS)[number]

export interface SourceProviderDefinition {
  key: SourceProviderKey
  label: string
  description: string
  webUrl: string
  captureMode: 'selection' | 'clipboard'
}
export const sourceProviders: SourceProviderDefinition[] = [
  {
    key: 'outlook',
    label: 'Outlook',
    description: 'Open mail, select the decision or action sentence, and hand it to a Task draft.',
    webUrl: 'https://outlook.office.com/mail/',
    captureMode: 'selection',
  },
  {
    key: 'teams',
    label: 'Teams',
    description: 'Copy a message, then use the Work Stack capture button to open a reviewed Task draft.',
    webUrl: 'https://teams.microsoft.com/v2/',
    captureMode: 'clipboard',
  },
  {
    key: 'onenote',
    label: 'OneNote',
    description: 'Select a note excerpt when available, with the same explicit clipboard fallback as Teams.',
    webUrl: 'https://www.office.com/launch/onenote',
    captureMode: 'clipboard',
  },
]

export function isSourceProviderKey(value: unknown): value is SourceProviderKey {
  return typeof value === 'string' && SOURCE_PROVIDER_KEYS.includes(value as SourceProviderKey)
}

export function sourceProvider(key: SourceProviderKey) {
  return sourceProviders.find((provider) => provider.key === key)!
}
