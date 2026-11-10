import test from 'node:test'
import assert from 'node:assert/strict'
import { transformSync } from 'esbuild'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'

const ipcCode = transformSync(readFileSync('electron/main/ipc-handlers.ts', 'utf8'), {
  loader: 'ts',
  format: 'cjs',
}).code

function fixture({ lostDownloadResponse = false } = {}) {
  const handlers = new Map()
  const calls = []
  const events = []
  const savedSettings = []
  let managedDownloadPolls = 0
  const axios = {
    interceptors: { request: { use: () => {} } },
    isAxiosError: () => false,
    get: async (url) => {
      calls.push(['get', url])
      if (url.endsWith('/model/managed-downloads')) {
        managedDownloadPolls += 1
        if (lostDownloadResponse && managedDownloadPolls === 1) {
          return { data: [{ modelId: 'demo/a', percent: 98, status: 'Downloading final file' }] }
        }
        return { data: [] }
      }
      if (url.endsWith('/model/all')) return { data: [{ id: 'demo/a', name: 'Demo', downloaded: true }] }
      if (url.includes('/model/storage/')) {
        return { data: { downloaded: true, hasLocalData: true, installedWeightVariants: ['fast'] } }
      }
      if (url.endsWith('/system/memory')) return { data: { total: 10, used: 4, available: 6 } }
      if (url.endsWith('/logs')) return { data: { 'server.log': 'ready' } }
      if (url.endsWith('/logs/sessions')) return { data: [] }
      if (url.endsWith('/workspace-admin/collections')) return { data: ['Exports'] }
      if (url.endsWith('/settings/paths')) {
        return { data: { models_dir: '/server/models', workspace_dir: '/server/workspace', workflows_dir: '/server/workflows', extensions_dir: '/server/extensions' } }
      }
      return { data: {} }
    },
    post: async (url, body, config) => {
      calls.push(['post', url, body, config])
      if (url.endsWith('/model/managed-download')) {
        if (lostDownloadResponse) {
          return new Promise((_resolve, reject) => {
            config.signal.addEventListener('abort', () => reject(new Error('cancelled')))
          })
        }
        return { data: { success: true, progress: { percent: 100, logs: ['done'] } } }
      }
      return { data: {} }
    },
    put: async (url, body) => { calls.push(['put', url, body]); return { data: {} } },
    delete: async (url) => { calls.push(['delete', url]); return { data: {} } },
  }
  const stub = new Proxy({}, { get: () => () => {} })
  const dependencies = (name) => {
    if (name === 'electron') {
      return {
        ipcMain: { handle: (id, fn) => handlers.set(id, fn), on: () => {} },
        app: {
          getPath: () => '/client/data',
          getVersion: () => 'test',
          getAppPath: () => '/client/app',
          on: () => {},
          isPackaged: false,
        },
        Notification: { isSupported: () => false },
      }
    }
    if (name === 'axios') return axios
    if (name === './backend-target') {
      return {
        getSelectedApiHeaders: () => ({}),
        getSelectedApiUrl: () => 'http://test',
        isRemoteBackend: () => false,
        normalizeSelectedApiUrl: (value) => value,
        setSelectedApiUrl: (value) => value,
      }
    }
    if (name === './settings-store') {
      return {
        getSettings: () => ({ modelsDir: '/client/models', workspaceDir: '/client/workspace' }),
        setSettings: (_root, patch) => savedSettings.push(patch),
      }
    }
    if (name === './python-bridge') return { API_BASE_URL: 'http://test' }
    if (name === './artifact-registry-service') return { registerWorkspaceAssetLibraryIpcHandlers: () => {} }
    if (name.startsWith('./') || name === 'electron-updater') return stub
    return {}
  }
  const module = { exports: {} }
  vm.runInNewContext(ipcCode, {
    module,
    exports: module.exports,
    require: dependencies,
    process,
    console,
    Buffer,
    URL,
    AbortController,
    setTimeout,
    clearTimeout,
  })
  module.exports.setupIpcHandlers({}, () => ({
    webContents: { send: (...event) => events.push(event), reload: () => {} },
  }))
  const event = { sender: { send: (...args) => events.push(args) } }
  return {
    calls,
    events,
    savedSettings,
    invoke: (name, ...args) => handlers.get(name)(event, ...args),
  }
}

test('bundled-mode model downloads are owned by the selected server', async () => {
  const f = fixture()
  assert.equal((await f.invoke('model:download', 'demo/a')).success, true)
  assert.ok(f.calls.some(([method, url]) => method === 'post' && url === 'http://test/model/managed-download'))
  assert.ok(f.events.some(([name, progress]) => name === 'model:downloadProgress' && progress.percent === 100))
})

test('completed server download clears stale progress when the POST response is lost', async () => {
  const f = fixture({ lostDownloadResponse: true })
  assert.equal((await f.invoke('model:download', 'demo/a')).success, true)
  assert.ok(f.events.some(([name, progress]) => (
    name === 'model:downloadProgress' && progress.percent === 98
  )))
  assert.ok(f.events.some(([name, progress]) => (
    name === 'model:downloadProgress' && progress.percent === 100 && progress.status === 'done'
  )))
  assert.ok(f.calls.some(([method, url]) => (
    method === 'get' && url === 'http://test/model/storage/demo%2Fa/state'
  )))
})

test('bundled-mode model deletion and state checks use server storage routes', async () => {
  const f = fixture()
  assert.equal(await f.invoke('model:isDownloaded', 'demo/a'), true)
  assert.equal((await f.invoke('model:delete', 'demo/a')).success, true)
  assert.ok(f.calls.some(([method, url]) => method === 'delete' && url === 'http://test/model/storage/model/demo%2Fa'))
})

test('RAM logs and workspace metadata describe the selected server in bundled mode', async () => {
  const f = fixture()
  assert.deepEqual(await f.invoke('system:memory'), { total: 10, used: 4, available: 6 })
  assert.deepEqual(await f.invoke('log:readAll'), { 'server.log': 'ready' })
  assert.deepEqual(await f.invoke('workspace:listCollections'), ['Exports'])
})

test('bundled path changes are accepted by the server before being persisted for restart', async () => {
  const f = fixture()
  await f.invoke('settings:set', { modelsDir: '/new/models' })
  assert.ok(f.calls.some(([method, url]) => method === 'post' && url === 'http://test/settings/paths'))
  assert.equal(f.savedSettings.at(-1).modelsDir, '/new/models')
})
