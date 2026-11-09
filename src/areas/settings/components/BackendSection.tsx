import { useEffect, useState } from 'react'
import { useAppStore } from '@shared/stores/appStore'
import { Card, Section, SegmentedControl } from '@shared/ui'
import type { BackendMode } from '@shared/utils/backendServer'
import { useExtensionsStore } from '@shared/stores/extensionsStore'
import { useWorkflowsStore } from '@shared/stores/workflowsStore'
import { useLlmModelsStore } from '@shared/stores/llmModelsStore'

type SaveState = 'idle' | 'saving' | 'saved' | 'error'

export function BackendSection(): JSX.Element {
  const backendMode = useAppStore((state) => state.backendMode)
  const remoteApiUrl = useAppStore((state) => state.remoteApiUrl)
  const apiToken = useAppStore((state) => state.apiToken)
  const allowInsecureRemote = useAppStore((state) => state.allowInsecureRemote)
  const apiUrl = useAppStore((state) => state.apiUrl)
  const remoteBackendStatus = useAppStore((state) => state.remoteBackendStatus)
  const setBackendServer = useAppStore((state) => state.setBackendServer)

  const [mode, setMode] = useState<BackendMode>(backendMode)
  const [url, setUrl] = useState(remoteApiUrl)
  const [token, setToken] = useState(apiToken)
  const [allowInsecure, setAllowInsecure] = useState(allowInsecureRemote)
  const [saveState, setSaveState] = useState<SaveState>('idle')
  const [message, setMessage] = useState('')

  useEffect(() => {
    setMode(backendMode)
    setUrl(remoteApiUrl)
    setToken(apiToken)
    setAllowInsecure(allowInsecureRemote)
  }, [allowInsecureRemote, apiToken, backendMode, remoteApiUrl])

  const dirty = mode !== backendMode || (mode === 'remote' && (
    url.trim() !== remoteApiUrl || token !== apiToken || allowInsecure !== allowInsecureRemote
  ))

  async function handleConnect(): Promise<void> {
    setSaveState('saving')
    setMessage('')
    try {
      const endpointChanged = mode !== backendMode
        || (mode === 'remote' && url.trim().replace(/\/+$/, '') !== remoteApiUrl)
      await setBackendServer(mode, url, token, allowInsecure)
      if (!endpointChanged) {
        const selectedApiUrl = useAppStore.getState().apiUrl
        await Promise.all([
          useExtensionsStore.getState().loadExtensions(),
          useWorkflowsStore.getState().load(),
          useLlmModelsStore.getState().fetchModels(selectedApiUrl, { force: true }),
        ])
      }
      setSaveState('saved')
      setMessage(mode === 'remote' ? 'Connected. Modly is now using this server.' : 'Switched to the bundled server.')
    } catch (error) {
      setSaveState('error')
      setMessage(error instanceof Error ? error.message : 'Could not connect to this server.')
    }
  }

  function changeMode(next: BackendMode): void {
    setMode(next)
    setSaveState('idle')
    setMessage('')
  }

  return (
    <Section title="Backend" subtitle="Choose the Modly API server used for generation, workflows, and models.">
      <Card
        title="API server"
        description="The bundled server remains available so you can switch back at any time."
        aside={(
          <span className={`inline-flex items-center gap-1.5 text-[11px] ${backendMode === 'remote' && remoteBackendStatus === 'offline' ? 'text-red-400' : remoteBackendStatus === 'unknown' && backendMode === 'remote' ? 'text-amber-400' : 'text-emerald-400'}`}>
            <span className={`w-1.5 h-1.5 rounded-full ${backendMode === 'remote' && remoteBackendStatus === 'offline' ? 'bg-red-400' : remoteBackendStatus === 'unknown' && backendMode === 'remote' ? 'bg-amber-400' : 'bg-emerald-400'}`} />
            {backendMode === 'remote' && remoteBackendStatus === 'offline' ? 'Offline' : remoteBackendStatus === 'unknown' && backendMode === 'remote' ? 'Checking' : 'Active'}
          </span>
        )}
      >
        <div className="px-4 py-4">
          <p className="text-xs font-medium text-zinc-300 mb-2">Server type</p>
          <SegmentedControl
            value={mode}
            onChange={changeMode}
            ariaLabel="Backend server type"
            options={[
              { value: 'local', label: 'Bundled' },
              { value: 'remote', label: 'Custom server' },
            ]}
          />
          <p className="text-[11px] text-zinc-500 mt-2">
            {mode === 'local'
              ? 'Runs the API included with this Modly installation.'
              : 'Connects to a Modly API running on another host or port.'}
          </p>
        </div>

        {mode === 'remote' && (
          <div className="px-4 py-4">
            <label htmlFor="backend-url" className="block text-xs font-medium text-zinc-300 mb-2">
              Server address
            </label>
            <input
              id="backend-url"
              type="url"
              value={url}
              onChange={(event) => {
                setUrl(event.target.value)
                setSaveState('idle')
                setMessage('')
              }}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && saveState !== 'saving') void handleConnect()
              }}
              placeholder="http://192.168.1.20:8765"
              spellCheck={false}
              className="w-full rounded-lg border border-zinc-700 bg-surface-500 px-3 py-2 text-xs font-mono text-zinc-200 placeholder:text-zinc-600 focus:outline-none focus:border-accent/70"
            />
            <p className="text-[11px] text-zinc-500 mt-2">
              Modly will verify the server's <span className="font-mono">/health</span> endpoint before switching.
            </p>
            <label htmlFor="backend-token" className="block text-xs font-medium text-zinc-300 mb-2 mt-4">
              Access token
            </label>
            <input
              id="backend-token"
              type="password"
              value={token}
              onChange={(event) => {
                setToken(event.target.value)
                setSaveState('idle')
                setMessage('')
              }}
              placeholder="MODLY_SERVER_TOKEN"
              autoComplete="off"
              className="w-full rounded-lg border border-zinc-700 bg-surface-500 px-3 py-2 text-xs font-mono text-zinc-200 placeholder:text-zinc-600 focus:outline-none focus:border-accent/70"
            />
            <p className="text-[11px] text-zinc-500 mt-2">
              Stored using your operating system's secure encryption. HTTPS is recommended outside a trusted private network.
            </p>
            {url.trim().toLowerCase().startsWith('http://') && !url.includes('127.0.0.1') && !url.includes('localhost') && (
              <label className="mt-3 flex items-start gap-2 text-[11px] text-amber-300">
                <input
                  type="checkbox"
                  checked={allowInsecure}
                  onChange={(event) => setAllowInsecure(event.target.checked)}
                  className="mt-0.5"
                />
                Allow unencrypted HTTP on this trusted network. The access token will be visible to the network.
              </label>
            )}
          </div>
        )}

        <div className="px-4 py-4 flex items-center justify-between gap-4">
          <div className="min-w-0">
            <p className="text-xs font-medium text-zinc-300">Current endpoint</p>
            <p className="text-[11px] text-zinc-500 font-mono truncate mt-0.5">{apiUrl}</p>
            {message && (
              <p className={`text-[11px] mt-1.5 ${saveState === 'error' ? 'text-red-400' : 'text-emerald-400'}`}>
                {message}
              </p>
            )}
          </div>
          <button
            type="button"
            onClick={() => void handleConnect()}
            disabled={saveState === 'saving' || (!dirty && saveState === 'saved')}
            className="shrink-0 px-3 py-2 rounded-lg bg-accent hover:bg-accent-dark text-xs font-medium text-white transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {saveState === 'saving' ? 'Connecting…' : mode === 'remote' ? 'Test & connect' : 'Use bundled server'}
          </button>
        </div>
      </Card>
    </Section>
  )
}
