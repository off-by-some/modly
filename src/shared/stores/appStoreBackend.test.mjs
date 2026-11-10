import test from 'node:test'
import assert from 'node:assert/strict'
import { buildSync } from 'esbuild'
import { createRequire } from 'node:module'
import { mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'

function loadStore(electron) {
  const directory = mkdtempSync(join(tmpdir(), 'modly-app-store-backend-test-'))
  const outfile = join(directory, 'appStore.cjs')
  const storage = new Map()
  const localStorage = {
    getItem: (key) => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, value),
    removeItem: (key) => storage.delete(key),
  }
  globalThis.window = {
    electron,
    localStorage,
    setTimeout,
    clearTimeout,
  }
  globalThis.localStorage = localStorage
  const result = buildSync({
    entryPoints: [resolve('src/shared/stores/appStore.ts')],
    bundle: true,
    platform: 'node',
    format: 'cjs',
    write: false,
  })
  writeFileSync(outfile, result.outputFiles[0].text, 'utf8')
  return createRequire(import.meta.url)(outfile).useAppStore
}

function electronBridge(overrides = {}) {
  return {
    python: {
      start: async () => ({ success: true, port: 8765 }),
      offCrashed: () => {},
      onCrashed: () => {},
    },
    app: {
      info: async () => ({ apiUrl: 'http://127.0.0.1:8765' }),
    },
    backend: {
      check: async (apiUrl) => ({ apiUrl }),
      select: async (apiUrl) => ({ apiUrl }),
    },
    secureStore: {
      decrypt: async () => '',
    },
    notifications: {
      show: async () => ({ success: true }),
    },
    ...overrides,
  }
}

test('startup falls back before data stores mount when the persisted remote is offline', async () => {
  const selected = []
  let localStarts = 0
  const electron = electronBridge({
    python: {
      start: async () => { localStarts += 1; return { success: true, port: 8765 } },
      offCrashed: () => {},
      onCrashed: () => {},
    },
    backend: {
      check: async () => { throw new Error('offline') },
      select: async (apiUrl) => { selected.push(apiUrl); return { apiUrl } },
    },
  })
  const useAppStore = loadStore(electron)
  useAppStore.setState({
    backendMode: 'remote',
    remoteApiUrl: 'http://10.0.0.220:8765',
    remoteApiTokenEncrypted: '',
    allowInsecureRemote: false,
  })

  await useAppStore.getState().initApp()

  const state = useAppStore.getState()
  assert.equal(localStarts, 1)
  assert.deepEqual(selected, ['http://127.0.0.1:8765'])
  assert.equal(state.backendMode, 'local')
  assert.equal(state.backendStatus, 'ready')
  assert.equal(state.apiUrl, 'http://127.0.0.1:8765')
  assert.equal(state.remoteApiUrl, 'http://10.0.0.220:8765')
  assert.match(state.toast?.message ?? '', /switched to the local server/i)
})

test('startup keeps an available remote authoritative and does not start local Python', async () => {
  const selected = []
  let localStarts = 0
  const electron = electronBridge({
    python: {
      start: async () => { localStarts += 1; return { success: true, port: 8765 } },
      offCrashed: () => {},
      onCrashed: () => {},
    },
    backend: {
      check: async (apiUrl) => ({ apiUrl }),
      select: async (apiUrl) => { selected.push(apiUrl); return { apiUrl } },
    },
  })
  const useAppStore = loadStore(electron)
  useAppStore.setState({
    backendMode: 'remote',
    remoteApiUrl: 'http://10.0.0.220:8765',
    remoteApiTokenEncrypted: '',
    allowInsecureRemote: false,
  })

  await useAppStore.getState().initApp()

  const state = useAppStore.getState()
  assert.equal(localStarts, 0)
  assert.deepEqual(selected, ['http://10.0.0.220:8765'])
  assert.equal(state.backendMode, 'remote')
  assert.equal(state.backendStatus, 'ready')
  assert.equal(state.remoteBackendStatus, 'online')
})
