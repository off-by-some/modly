import { useEffect, useState } from 'react'
import { useAppStore } from '@shared/stores/appStore'
import { Card, Row, Section, SegmentedControl, Toggle } from '@shared/ui'
import type { BackendMode } from '@shared/utils/backendServer'
import type { ExtensionRuntimeStatus } from '@shared/types/electron.d'
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
  const [runtime, setRuntime] = useState<ExtensionRuntimeStatus | null>(null)
  const [runtimeLoading, setRuntimeLoading] = useState(true)
  const [runtimeError, setRuntimeError] = useState('')

  useEffect(() => {
    let cancelled = false
    setRuntimeLoading(true)
    setRuntimeError('')
    window.electron.extensions.runtimeStatus()
      .then((status) => { if (!cancelled) setRuntime(status) })
      .catch((error) => {
        if (!cancelled) {
          setRuntime(null)
          setRuntimeError(error instanceof Error ? error.message : 'Could not inspect Docker on this server.')
        }
      })
      .finally(() => { if (!cancelled) setRuntimeLoading(false) })
    return () => { cancelled = true }
  }, [apiUrl, backendMode])

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

  async function changeDockerMode(enabled: boolean): Promise<void> {
    setRuntimeLoading(true)
    setRuntimeError('')
    try {
      const status = await window.electron.extensions.setDockerEnabled(enabled)
      setRuntime(status)
      await useExtensionsStore.getState().loadExtensions()
    } catch (error) {
      setRuntimeError(error instanceof Error ? error.message : 'Could not update the extension runtime.')
    } finally {
      setRuntimeLoading(false)
    }
  }

  return (
    <Section title="Backend" subtitle="Choose the Modly API server used for generation, workflows, and models.">
      <div className="space-y-4">
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
              Access token <span className="font-normal text-zinc-500">(optional)</span>
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
              Leave blank for a server without MODLY_SERVER_TOKEN. Entered tokens are stored using your operating system's secure encryption.
            </p>
            {token.trim() && url.trim().toLowerCase().startsWith('http://') && !url.includes('127.0.0.1') && !url.includes('localhost') && (
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
        <Card
        title="Extension runtime"
        description="Choose how extensions installed from a GitHub URL run on the selected server."
      >
        <Row
          label="Use Docker"
          description={runtime?.dockerAvailable
            ? `Build and run URL-installed extensions in isolated containers${runtime.dockerVersion ? ` (Docker ${runtime.dockerVersion})` : ''}${runtime.nvidiaToolkitAvailable ? ` with NVIDIA GPU access via ${runtime.nvidiaContainerMode === 'cdi' ? 'CDI' : 'the NVIDIA runtime'}` : ''}. Switching modes hides extensions installed for the other runtime.`
            : runtimeLoading
              ? 'Checking Docker on the selected server…'
              : runtime?.reason || runtimeError || 'Docker is not available on the selected server.'}
        >
          <Toggle
            value={runtime?.configuredUseDocker ?? false}
            disabled={runtimeLoading || (!runtime?.dockerAvailable && !runtime?.configuredUseDocker)}
            onChange={(enabled) => void changeDockerMode(enabled)}
          />
        </Row>
        {runtime?.dockerAvailable && !runtime.nvidiaToolkitAvailable && (
          <div className="mx-4 my-3 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2.5 text-[11px] text-amber-200">
            {runtime.nvidiaToolkitInstalled
              ? 'NVIDIA Container Toolkit is installed, but this Docker daemon has no configured NVIDIA runtime or usable CDI device. Docker extensions will not use an NVIDIA GPU until Docker is configured.'
              : 'No NVIDIA Container Toolkit was detected. Docker extensions will not use an NVIDIA GPU even if one is available.'}{' '}
            <button
              type="button"
              className="underline underline-offset-2 hover:text-amber-100"
              onClick={() => void window.electron.shell.openExternal('https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html')}
            >
              {runtime.nvidiaToolkitInstalled ? 'Configure Docker GPU support' : 'Install NVIDIA Container Toolkit'}
            </button>
          </div>
        )}
        {runtimeError && runtime?.dockerAvailable && (
          <p className="px-4 py-3 text-[11px] text-red-400">{runtimeError}</p>
        )}
        </Card>
      </div>
    </Section>
  )
}
