export const LOCAL_API_URL = 'http://127.0.0.1:8765'

let selectedApiUrl = LOCAL_API_URL
let selectedApiToken = ''

export function normalizeSelectedApiUrl(value: string): string {
  const url = new URL(value)
  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    throw new Error('Backend URL must use HTTP or HTTPS')
  }
  if (url.username || url.password || url.search || url.hash) {
    throw new Error('Backend URL cannot contain credentials, a query, or a fragment')
  }
  return `${url.origin}${url.pathname.replace(/\/+$/, '')}`
}

export function setSelectedApiUrl(value: string, token = ''): string {
  selectedApiUrl = normalizeSelectedApiUrl(value)
  selectedApiToken = selectedApiUrl === LOCAL_API_URL ? '' : token.trim()
  return selectedApiUrl
}

export function getSelectedApiUrl(): string {
  return selectedApiUrl
}

export function isRemoteBackend(): boolean {
  return selectedApiUrl !== LOCAL_API_URL
}

export function getSelectedApiHeaders(): Record<string, string> {
  return selectedApiToken ? { Authorization: `Bearer ${selectedApiToken}` } : {}
}
