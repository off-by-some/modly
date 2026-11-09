import { useEffect, useRef, useState } from 'react'
import { useAppStore } from '@shared/stores/appStore'
import { checkBackendServer } from '@shared/utils/backendServer'
import { useExtensionsStore } from '@shared/stores/extensionsStore'
import { useWorkflowsStore } from '@shared/stores/workflowsStore'
import { useLlmModelsStore } from '@shared/stores/llmModelsStore'

const HEALTH_CHECK_INTERVAL_MS = 10_000
const FAILURES_BEFORE_PROMPT = 3
const FAILURES_BEFORE_FALLBACK = 6

export function BackendConnectionMonitor(): JSX.Element | null {
  const backendMode = useAppStore((state) => state.backendMode)
  const apiUrl = useAppStore((state) => state.apiUrl)
  const setBackendServer = useAppStore((state) => state.setBackendServer)
  const setRemoteBackendStatus = useAppStore((state) => state.setRemoteBackendStatus)
  const showToast = useAppStore((state) => state.showToast)
  const apiToken = useAppStore((state) => state.apiToken)
  const allowInsecureRemote = useAppStore((state) => state.allowInsecureRemote)

  const [visible, setVisible] = useState(false)
  const [checking, setChecking] = useState(false)
  const [switching, setSwitching] = useState(false)
  const [error, setError] = useState('')
  const dismissedForOutage = useRef(false)
  const consecutiveFailures = useRef(0)
  const fallbackInProgress = useRef(false)
  const automaticFallbackEnabled = useRef(true)
  const checkNow = useRef<() => Promise<boolean>>(async () => false)

  useEffect(() => {
    if (backendMode !== 'remote' || !apiUrl) {
      setVisible(false)
      setChecking(false)
      setError('')
      dismissedForOutage.current = false
      setRemoteBackendStatus('unknown')
      consecutiveFailures.current = 0
      fallbackInProgress.current = false
      automaticFallbackEnabled.current = true
      return
    }

    let cancelled = false
    let timer: number | undefined

    const check = async (): Promise<boolean> => {
      setChecking(true)
      try {
        await checkBackendServer(apiUrl, apiToken, allowInsecureRemote)
        if (cancelled) return false
        setRemoteBackendStatus('online')
        consecutiveFailures.current = 0
        setVisible(false)
        setError('')
        dismissedForOutage.current = false
        automaticFallbackEnabled.current = true
        return true
      } catch (cause) {
        if (cancelled) return false
        consecutiveFailures.current += 1
        setRemoteBackendStatus(consecutiveFailures.current >= FAILURES_BEFORE_PROMPT ? 'offline' : 'unknown')
        setError(cause instanceof Error ? cause.message : 'Could not connect to this Modly server.')
        if (consecutiveFailures.current >= FAILURES_BEFORE_PROMPT && !dismissedForOutage.current) setVisible(true)
        if (consecutiveFailures.current >= FAILURES_BEFORE_FALLBACK && automaticFallbackEnabled.current && !fallbackInProgress.current) {
          fallbackInProgress.current = true
          setSwitching(true)
          try {
            await setBackendServer('local')
            if (!cancelled) {
              const selectedApiUrl = useAppStore.getState().apiUrl
              await Promise.all([
                useExtensionsStore.getState().loadExtensions(),
                useWorkflowsStore.getState().load(),
                useLlmModelsStore.getState().fetchModels(selectedApiUrl, { force: true }),
              ])
              const message = 'The custom server went offline. Modly swapped to the local server.'
              showToast(message, 7000)
              void window.electron.notifications.show('Modly server changed', message)
              setVisible(false)
            }
          } catch (fallbackError) {
            fallbackInProgress.current = false
            if (!cancelled) {
              setError(fallbackError instanceof Error ? fallbackError.message : 'Could not start the local server.')
            }
          } finally {
            if (!cancelled) setSwitching(false)
          }
        }
        return false
      } finally {
        if (!cancelled) setChecking(false)
      }
    }

    const poll = async (): Promise<void> => {
      await check()
      if (!cancelled) timer = window.setTimeout(() => void poll(), HEALTH_CHECK_INTERVAL_MS)
    }

    checkNow.current = check
    void poll()

    return () => {
      cancelled = true
      if (timer !== undefined) window.clearTimeout(timer)
      checkNow.current = async () => false
    }
  }, [allowInsecureRemote, apiToken, apiUrl, backendMode, setBackendServer, setRemoteBackendStatus, showToast])

  async function handleSwitchToLocal(): Promise<void> {
    setSwitching(true)
    setError('')
    try {
      await setBackendServer('local')
      setVisible(false)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not start the local server.')
    } finally {
      setSwitching(false)
    }
  }

  function handleDismiss(): void {
    automaticFallbackEnabled.current = false
    dismissedForOutage.current = true
    setVisible(false)
  }

  if (!visible || backendMode !== 'remote') return null

  return (
    <div className="fixed inset-0 z-[9999] flex items-center justify-center bg-black/60 backdrop-blur-sm" role="dialog" aria-modal="true" aria-labelledby="backend-offline-title">
      <div className="w-full max-w-md mx-4 overflow-hidden rounded-xl border border-amber-500/30 bg-surface-300 shadow-2xl">
        <div className="flex items-center gap-3 border-b border-zinc-700/50 px-5 py-4">
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full border border-amber-500/30 bg-amber-500/15">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-amber-400">
              <path d="M5 12.55a11 11 0 0 1 14.08 0" />
              <path d="M8.5 16.05a6 6 0 0 1 7 0" />
              <line x1="12" y1="20" x2="12.01" y2="20" />
              <line x1="3" y1="3" x2="21" y2="21" />
            </svg>
          </div>
          <div className="min-w-0 flex-1">
            <h2 id="backend-offline-title" className="text-sm font-semibold text-zinc-100">Custom server is offline</h2>
            <p className="mt-0.5 truncate font-mono text-[11px] text-zinc-500">{apiUrl}</p>
          </div>
          <button onClick={handleDismiss} className="rounded-md p-1 text-zinc-500 hover:bg-zinc-800 hover:text-zinc-200" aria-label="Dismiss server offline notification">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
              <line x1="18" y1="6" x2="6" y2="18" />
              <line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </div>

        <div className="px-5 py-4">
          <p className="text-xs leading-5 text-zinc-300">
            Modly will keep retrying and automatically swap to the local server if the outage continues. You can retry or swap now.
          </p>
          {error && <p className="mt-3 rounded-lg border border-red-900/30 bg-red-950/30 px-3 py-2 text-[11px] text-red-400">{error}</p>}
        </div>

        <div className="flex items-center justify-end gap-2 px-5 pb-4">
          <button
            onClick={handleDismiss}
            disabled={switching}
            className="px-3 py-2 text-xs font-medium text-zinc-400 transition-colors hover:text-zinc-200 disabled:opacity-50"
          >
            Keep custom server
          </button>
          <button
            onClick={() => void checkNow.current()}
            disabled={checking || switching}
            className="rounded-lg bg-zinc-700/60 px-3 py-2 text-xs font-semibold text-zinc-200 transition-colors hover:bg-zinc-700 disabled:opacity-50"
          >
            {checking ? 'Checking…' : 'Retry'}
          </button>
          <button
            onClick={() => void handleSwitchToLocal()}
            disabled={checking || switching}
            className="rounded-lg bg-accent px-3 py-2 text-xs font-semibold text-white transition-colors hover:bg-accent-dark disabled:opacity-50"
          >
            {switching ? 'Starting…' : 'Swap to Local Server'}
          </button>
        </div>
      </div>
    </div>
  )
}
