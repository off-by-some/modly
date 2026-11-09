export type BackendMode = 'local' | 'remote'

/**
 * Turn a user-entered server address into the base URL used by every API call.
 * A scheme-less host is assumed to be HTTP, which is convenient for LAN hosts.
 */
export function normalizeBackendUrl(value: string): string {
  const trimmed = value.trim()
  if (!trimmed) throw new Error('Enter a server address.')

  const candidate = trimmed.includes('://') ? trimmed : `http://${trimmed}`
  let url: URL
  try {
    url = new URL(candidate)
  } catch {
    throw new Error('Enter a valid server address, such as http://192.168.1.20:8765.')
  }

  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    throw new Error('The server address must use HTTP or HTTPS.')
  }
  if (url.username || url.password) {
    throw new Error('Credentials are not supported in the server address.')
  }
  if (url.search || url.hash) {
    throw new Error('Remove the query string or fragment from the server address.')
  }

  const path = url.pathname.replace(/\/+$/, '')
  return `${url.origin}${path === '/' ? '' : path}`
}

export async function checkBackendServer(value: string, timeoutMs = 5_000): Promise<string> {
  const baseUrl = normalizeBackendUrl(value)
  const controller = new AbortController()
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs)

  try {
    const response = await fetch(`${baseUrl}/health`, { signal: controller.signal })
    if (!response.ok) throw new Error(`Server returned HTTP ${response.status}.`)

    const data = await response.json() as { status?: string }
    if (data.status !== 'ok') throw new Error('The server returned an unexpected health response.')
    return baseUrl
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') {
      throw new Error('The server did not respond within 5 seconds.')
    }
    if (error instanceof Error && error.message.startsWith('Server ')) throw error
    if (error instanceof Error && error.message.startsWith('The server ')) throw error
    throw new Error('Could not connect to this Modly server.')
  } finally {
    window.clearTimeout(timeout)
  }
}
