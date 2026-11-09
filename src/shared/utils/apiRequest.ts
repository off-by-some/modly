import axios from 'axios'
import { useAppStore } from '../stores/appStore'

export function apiHeaders(headers?: HeadersInit): Headers {
  const result = new Headers(headers)
  const token = useAppStore.getState().apiToken
  if (token) result.set('Authorization', `Bearer ${token}`)
  return result
}

export function apiFetch(input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> {
  return fetch(input, { ...init, headers: apiHeaders(init.headers) })
}

export function apiClient(apiUrl: string) {
  const token = useAppStore.getState().apiToken
  return axios.create({
    baseURL: apiUrl,
    headers: token ? { Authorization: `Bearer ${token}` } : undefined,
  })
}
