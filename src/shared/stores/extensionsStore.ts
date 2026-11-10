import { create } from 'zustand'
import type { ModelExtension, ProcessExtension, AnyExtension } from '@shared/types/electron.d'

// ─── Re-exports for consumers ─────────────────────────────────────────────────

export type { ModelExtension, ProcessExtension, AnyExtension }


export type InstallStep = 'downloading' | 'extracting' | 'validating' | 'setting_up' | 'done' | 'error'

export interface InstallProgress {
  step:         InstallStep
  percent?:     number
  extensionId?: string
  message?:     string
  details?:     string
}

// ─── Store ────────────────────────────────────────────────────────────────────

interface ExtensionsStore {
  modelExtensions:   ModelExtension[]
  processExtensions: ProcessExtension[]
  loading:           boolean
  installProgress:   InstallProgress | null
  installError:      string | null
  installLogs:       string[]
  loadErrors:        Record<string, string>
  /** Installed weight variant ids, keyed by "ext_id/node_id", for nodes that declare variants */
  installedWeightVariants: Record<string, string[]>

  loadExtensions:    () => Promise<void>
  refreshInstalledWeightVariants: () => Promise<void>
  installFromGitHub: (url: string) => Promise<{ success: boolean; error?: string }>
  installFromLocal:  () => Promise<{ success: boolean; error?: string; cancelled?: boolean; needsRepair?: boolean }>
  uninstall:         (extensionId: string) => Promise<{ success: boolean; error?: string }>
  reload:            () => Promise<void>
  clearInstallState: () => void
}

export function partitionExtensionsByType(list: AnyExtension[]): {
  modelExtensions: ModelExtension[]
  processExtensions: ProcessExtension[]
} {
  return {
    modelExtensions: list.filter((extension): extension is ModelExtension =>
      extension.type === 'model',
    ),
    processExtensions: list.filter((extension): extension is ProcessExtension =>
      extension.type === 'process',
    ),
  }
}

export const useExtensionsStore = create<ExtensionsStore>((set, get) => ({
  modelExtensions:   [],
  processExtensions: [],
  loading:           false,
  installProgress:   null,
  installError:      null,
  installLogs:       [],
  loadErrors:        {},
  installedWeightVariants: {},

  // ── Load list ──────────────────────────────────────────────────────────────

  async loadExtensions() {
    set({ loading: true })
    try {
      const list = (await window.electron.extensions.list()) as AnyExtension[]
      const extensions = partitionExtensionsByType(list)
      set({
        ...extensions,
        loading:           false,
      })
      await get().refreshInstalledWeightVariants()
    } catch {
      set({ loading: false })
    }
  },

  async refreshInstalledWeightVariants() {
    const entries = await Promise.all(
      get().modelExtensions.flatMap((ext) => ext.nodes
        .filter((node) => node.weightVariants)
        .map(async (node) => {
          const fullId = `${ext.id}/${node.id}`
          return [fullId, await window.electron.model.installedWeightVariants(fullId)] as const
        })),
    )
    // A node whose state could not be read stays absent: undefined reads as "unknown",
    // which the UI keeps neutral, while [] would claim no variant is installed.
    set({
      installedWeightVariants: Object.fromEntries(
        entries.filter((entry): entry is readonly [string, string[]] => entry[1] !== null),
      ),
    })
  },

  // ── Install from GitHub ────────────────────────────────────────────────────

  async installFromGitHub(url: string) {
    return installExtension(() => window.electron.extensions.installFromGitHub(url), set)
  },

  // ── Install from local folder ──────────────────────────────────────────────

  async installFromLocal() {
    const result = await installExtension(() => window.electron.extensions.installFromLocal(), set)
    // If user cancelled the folder picker, treat as a no-op (not an error)
    if ((result as any).cancelled) {
      set({ installProgress: null, installError: null, installLogs: [] })
      return { success: false, cancelled: true }
    }
    return result
  },

  // ── Uninstall ──────────────────────────────────────────────────────────────

  async uninstall(extensionId: string) {
    const result = await window.electron.extensions.uninstall(extensionId)
    if (result.success) {
      set((state) => ({
        modelExtensions:   state.modelExtensions.filter((e)   => e.id !== extensionId),
        processExtensions: state.processExtensions.filter((e) => e.id !== extensionId),
      }))
    }
    return result
  },

  // ── Reload (rescan extensions dir + Python registry) ──────────────────────

  async reload() {
    const result = await window.electron.extensions.reload()
    if (result.success) {
      set({ loadErrors: result.errors ?? {} })
    }
    await get().loadExtensions()
  },

  // ── Helpers ────────────────────────────────────────────────────────────────

  clearInstallState() {
    set({ installProgress: null, installError: null, installLogs: [] })
  },
}))

async function installExtension(
  invoke: () => Promise<{
    success: boolean
    error?: string
    extension?: AnyExtension
    extensionId?: string
    needsRepair?: boolean
  }>,
  set: (partial: Partial<ExtensionsStore> | ((state: ExtensionsStore) => Partial<ExtensionsStore>)) => void,
) {
    set({ installProgress: { step: 'downloading', percent: 0 }, installError: null, installLogs: [] })

    window.electron.extensions.onInstallProgress((data) => {
      if (data.step === 'error') {
        set((state) => ({
          installProgress: data as InstallProgress,
          installError: data.message ?? 'Unknown error',
          installLogs: appendInstallLog(state.installLogs, data.details ?? data.message),
        }))
      } else {
        set((state) => ({
          installProgress: data as InstallProgress,
          installLogs: appendInstallLog(state.installLogs, data.message),
        }))
      }
    })

    try {
      const result = await invoke()

      if (result.success && result.extension) {
        const ext = result.extension as AnyExtension
        set((state) => {
          if (ext.type === 'process') {
            const filtered = state.processExtensions.filter((e) => e.id !== ext.id)
            return {
              processExtensions: [...filtered, ext],
              modelExtensions: state.modelExtensions.filter((e) => e.id !== ext.id),
              installProgress:   { step: 'done', extensionId: result.extensionId },
              installError:      null,
            }
          } else {
            const filtered = state.modelExtensions.filter((e) => e.id !== ext.id)
            return {
              modelExtensions: [...filtered, ext],
              processExtensions: state.processExtensions.filter((e) => e.id !== ext.id),
              installProgress: { step: 'done', extensionId: result.extensionId },
              installError:    null,
            }
          }
        })
      } else if (result.needsRepair && result.extension) {
        const ext = result.extension as AnyExtension
        const error = result.error ?? 'Extension setup is incomplete. Click Repair and retry.'
        set((state) => ({
          modelExtensions: ext.type === 'model'
            ? [...state.modelExtensions.filter((entry) => entry.id !== ext.id), ext]
            : state.modelExtensions.filter((entry) => entry.id !== ext.id),
          processExtensions: ext.type === 'process'
            ? [...state.processExtensions.filter((entry) => entry.id !== ext.id), ext]
            : state.processExtensions.filter((entry) => entry.id !== ext.id),
          loadErrors: { ...state.loadErrors, [ext.id]: error },
          installProgress: { step: 'error', message: error },
          installError: error,
        }))
      } else {
        const error = result.error ?? 'Installation failed'
        set((state) => ({
          installProgress: state.installProgress?.step === 'error'
            ? state.installProgress
            : { step: 'error', message: error },
          installError: error,
          installLogs: state.installLogs.length > 0
            ? state.installLogs
            : appendInstallLog(state.installLogs, error),
        }))
      }

      return result
    } catch (err) {
      const error = String(err)
      set((state) => ({
        installProgress: { step: 'error', message: error },
        installError: error,
        installLogs: appendInstallLog(state.installLogs, error),
      }))
      return { success: false, error }
    } finally {
      window.electron.extensions.offInstallProgress()
    }
}

// Large Docker installs can print a structured extension failure followed by
// hundreds of BuildKit summary lines. Keep enough history for the UI to retain
// the underlying pip/runtime error, not just Docker's final exit-code wrapper.
const MAX_INSTALL_LOG_LINES = 2000

function appendInstallLog(current: string[], text?: string): string[] {
  if (!text) return current
  const incoming = text
    .split(/\r?\n/)
    .map((line) => line.trimEnd().slice(-4000))
    .filter(Boolean)
  if (incoming.length === 0) return current

  // Terminal errors include subprocess output again so a dropped live event
  // cannot hide the useful part. Find that replay within the terminal payload
  // and append only lines the live stream did not already deliver.
  for (let overlap = Math.min(current.length, incoming.length); overlap > 0; overlap -= 1) {
    const existingTail = current.slice(current.length - overlap)
    for (let start = 0; start <= incoming.length - overlap; start += 1) {
      const replay = incoming.slice(start, start + overlap)
      if (existingTail.every((line, index) => line === replay[index])) {
        return [...current, ...incoming.slice(start + overlap)].slice(-MAX_INSTALL_LOG_LINES)
      }
    }
  }
  return [...current, ...incoming].slice(-MAX_INSTALL_LOG_LINES)
}
