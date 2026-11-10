import { ipcMain, BrowserWindow, Notification, dialog, app, shell } from 'electron'
import { autoUpdater } from 'electron-updater'
import { basename, join } from 'path'
import { rm as rmAsync, readFile, writeFile, mkdir, readdir, cp } from 'fs/promises'
import { createReadStream, existsSync } from 'fs'
import axios from 'axios'
import { PythonBridge, API_BASE_URL } from './python-bridge'
import { getSelectedApiHeaders, getSelectedApiUrl, isRemoteBackend, normalizeSelectedApiUrl, setSelectedApiUrl } from './backend-target'
import {
  type DownloadProgress,
} from './model-downloader'
import { getSettings, setSettings } from './settings-store'
import { checkSetupNeeded, markSetupDone, runFullSetup } from './python-setup'
import { logger } from './logger'
import { registerWorkspaceAssetLibraryIpcHandlers } from './artifact-registry-service'
import { updatesSupported } from './updater'
import { readLocalFileBase64 } from './bounded-file-reader'
import { encryptSecret, decryptSecret } from './secure-store'
import { getHfToken, initHfToken, setHfToken } from './hf-token'

type WindowGetter = () => BrowserWindow | null

export function setupIpcHandlers(pythonBridge: PythonBridge, getWindow: WindowGetter): void {
  async function selectedServerUsesDocker(failClosed = false): Promise<boolean> {
    try {
      const { data } = await axios.get<{ useDocker?: boolean; configuredUseDocker?: boolean }>(
        `${selectedApiUrl()}/extensions/runtime`,
        { timeout: 10_000 },
      )
      return data.configuredUseDocker ?? data.useDocker ?? false
    } catch (error) {
      if (failClosed) throw error
      return false
    }
  }
  const selectedApiUrl = () => getSelectedApiUrl() || API_BASE_URL
  // Attach the selected server credential only to that exact API prefix. The
  // global client is also used for GitHub/Hugging Face, so host + path matching
  // is mandatory to prevent credential leakage.
  axios.interceptors?.request?.use((config) => {
    const authorization = getSelectedApiHeaders?.()?.Authorization
    if (!authorization || !config.url) return config
    try {
      const requestUrl = new URL(config.url, config.baseURL)
      const base = new URL(selectedApiUrl())
      const basePath = base.pathname.replace(/\/+$/, '')
      if (
        requestUrl.origin === base.origin
        && (requestUrl.pathname === basePath || requestUrl.pathname.startsWith(`${basePath}/`))
      ) {
        config.headers.set('Authorization', authorization)
      }
    } catch { /* Axios handles malformed request URLs. */ }
    return config
  })
  const stageFileOnSelectedServer = async (filePath: string): Promise<string> => {
    if ((!isRemoteBackend() && !await selectedServerUsesDocker(true)) || !existsSync(filePath)) return filePath
    const { data } = await axios.post<{ file_path: string }>(
      `${selectedApiUrl()}/workspace-admin/upload`,
      createReadStream(filePath),
      {
        headers: {
          'Content-Type': 'application/octet-stream',
          'X-Modly-Filename': encodeURIComponent(basename(filePath)),
        },
        maxBodyLength: Infinity,
        maxContentLength: Infinity,
        timeout: 0,
      },
    )
    return data.file_path
  }

  ipcMain.handle('backend:check', async (_event, apiUrl: string, token = '') => {
    const normalized = normalizeSelectedApiUrl(apiUrl)
    const { data } = await axios.get<{ status?: string }>(`${normalized}/health`, {
      timeout: 5000,
      headers: token.trim() ? { Authorization: `Bearer ${token.trim()}` } : undefined,
    })
    if (data.status !== 'ok') throw new Error('The server returned an unexpected health response.')
    return { apiUrl: normalized }
  })
  ipcMain.handle('backend:select', (_event, apiUrl: string, token = '') => {
    const previous = getSelectedApiUrl()
    const selected = setSelectedApiUrl(apiUrl, token)
    setSettings(app.getPath('userData'), { backendApiUrl: selected })
    if (selected !== previous) setTimeout(() => getWindow()?.webContents.reload(), 250)
    return { apiUrl: selected }
  })
  const notifyWeightChange = () => {
    getWindow()?.webContents.send('model:weightsChanged')
  }
  // The log viewer follows the selected server in local and remote modes.
  ipcMain.on('log:error', (_event, message: string) => logger.error(`[Renderer] ${message}`))
  ipcMain.handle('log:getPath', () => join(app.getPath('userData'), 'logs', 'modly.log'))
  ipcMain.handle('log:readAll', async () => {
    const { data } = await axios.get(`${selectedApiUrl()}/logs`, { timeout: 5000 })
    return data
  })
  ipcMain.handle('log:listSessions', () => (
    axios.get(`${selectedApiUrl()}/logs/sessions`, { timeout: 5000 }).then(({ data }) => data)
  ))

  // Secure storage — OS-level encryption (Keychain/DPAPI/libsecret) for secrets
  // the renderer would otherwise have to keep in plain-text localStorage (API keys).
  ipcMain.handle('secure:encrypt', (_, plainText: string) => encryptSecret(plainText))
  ipcMain.handle('secure:decrypt', (_, stored: string) => decryptSecret(stored))

  // Window controls (frameless window)
  ipcMain.on('window:minimize', () => getWindow()?.minimize())
  ipcMain.on('window:maximize', () => {
    const win = getWindow()
    if (!win) return
    win.isMaximized() ? win.restore() : win.maximize()
  })
  ipcMain.on('window:close', () => getWindow()?.close())
  ipcMain.handle('window:isMaximized', () => getWindow()?.isMaximized() ?? false)

  // Native OS notification (Windows toast / macOS Notification Center / Linux),
  // for events the user wants to know about even when the window is minimized or
  // behind other apps — e.g. a generation or workflow run finishing. Routed through
  // the main process rather than the renderer's own Notification API so it works
  // the same way regardless of focus and carries the app's own icon.
  ipcMain.handle('notifications:show', (_event, title: string, body: string) => {
    if (!Notification.isSupported()) return { success: false, error: 'Notifications not supported' }
    const notification = new Notification({
      title,
      body,
      icon: join(__dirname, '../../resources/icons/icon.png'),
    })
    notification.on('click', () => {
      const win = getWindow()
      if (!win) return
      if (win.isMinimized()) win.restore()
      win.show()
      win.focus()
    })
    notification.show()
    return { success: true }
  })

  // Setup handlers — skipped in dev (uses .venv instead of python-embed)
  ipcMain.handle('setup:check', async () => {
    const userData = app.getPath('userData')
    const defaultDataDir = join(app.getPath('documents'), 'Modly')
    return {
      needed: checkSetupNeeded(userData),
      defaultDataDir,
      platform: process.platform,
      arch: process.arch,
    }
  })

  ipcMain.handle('setup:saveDataDir', async (_event, { baseDir }: { baseDir: string }) => {
    const userData = app.getPath('userData')
    setSettings(userData, {
      modelsDir:        join(baseDir, 'models'),
      workspaceDir:     join(baseDir, 'workspace'),
      workflowsDir:     join(baseDir, 'workflows'),
      extensionsDir:    join(baseDir, 'extensions'),
      dependenciesDir:  join(baseDir, 'dependencies'),
      agentDir:         join(baseDir, 'agent'),
    })
  })

  ipcMain.handle('setup:run', async () => {
    const userData = app.getPath('userData')
    const win = getWindow()
    if (!win) return { success: false, error: 'No window available' }
    try {
      await runFullSetup(win, userData)
      markSetupDone(userData)
      return { success: true }
    } catch (err) {
      return { success: false, error: String(err) }
    }
  })

  // Python bridge
  ipcMain.handle('python:start', async () => {
    try {
      await pythonBridge.start()
      return { success: true, port: pythonBridge.getPort() }
    } catch (err) {
      return { success: false, error: String(err) }
    }
  })

  ipcMain.handle('python:status', () => ({
    ready: pythonBridge.isReady(),
    apiUrl: API_BASE_URL
  }))

  // File system
  ipcMain.handle('fs:selectImage', async () => {
    const win = getWindow()
    if (!win) return null

    const result = await dialog.showOpenDialog(win, {
      title: 'Select an image',
      filters: [{ name: 'Images', extensions: ['jpg', 'jpeg', 'png', 'webp'] }],
      properties: ['openFile']
    })

    return result.canceled ? null : result.filePaths[0]
  })

  ipcMain.handle('fs:selectMeshFile', async () => {
    const win = getWindow()
    if (!win) return null

    const result = await dialog.showOpenDialog(win, {
      title: 'Select a 3D mesh file',
      filters: [{ name: '3D Mesh', extensions: ['glb', 'obj', 'stl', 'ply', 'splat'] }],
      properties: ['openFile']
    })

    return result.canceled ? null : result.filePaths[0]
  })

  ipcMain.handle('fs:saveModel', async (_, defaultName: string) => {
    const win = getWindow()
    if (!win) return null

    const result = await dialog.showSaveDialog(win, {
      title: 'Save 3D Model',
      defaultPath: defaultName,
      filters: [
        { name: 'OBJ', extensions: ['obj'] },
        { name: 'GLB', extensions: ['glb'] },
        { name: 'STL', extensions: ['stl'] }
      ]
    })

    return result.canceled ? null : result.filePath
  })

  ipcMain.handle('fs:savePath', async (_, args: { filters: { name: string; extensions: string[] }[]; defaultPath?: string }) => {
    const win = getWindow()
    if (!win) return null
    const result = await dialog.showSaveDialog(win, {
      title:       'Choose output path',
      filters:     args.filters,
      defaultPath: args.defaultPath,
    })
    return result.canceled ? null : result.filePath
  })

  ipcMain.handle('model:unloadAll', async (): Promise<{ success: boolean; error?: string }> => {
    try {
      await axios.post(`${selectedApiUrl()}/model/unload-all`, {}, { timeout: 10_000 })
      return { success: true }
    } catch (err) {
      return { success: false, error: String(err) }
    }
  })

  ipcMain.handle('model:delete', async (_, modelId: string): Promise<{ success: boolean; error?: string }> => {
    try {
      await axios.delete(`${selectedApiUrl()}/model/storage/model/${encodeURIComponent(modelId)}`, { timeout: 60_000 })
      notifyWeightChange()
      return { success: true }
    } catch (err) {
      return { success: false, error: axios.isAxiosError(err) ? (err.response?.data?.detail ?? err.message) : String(err) }
    }
  })

  ipcMain.handle('model:deleteWeightVariant', async (_, modelId: string, variantId: string): Promise<{ success: boolean; error?: string }> => {
    try {
      await axios.delete(`${selectedApiUrl()}/model/storage/${encodeURIComponent(modelId)}/variants/${encodeURIComponent(variantId)}`, { timeout: 60_000 })
      notifyWeightChange()
      return { success: true }
    } catch (err) {
      return { success: false, error: axios.isAxiosError(err) ? (err.response?.data?.detail ?? err.message) : String(err) }
    }
  })

  ipcMain.handle('model:showInFolder', (_, modelId: string) => {
    if (isRemoteBackend()) return
    const modelDir = join(getSettings(app.getPath('userData')).modelsDir, modelId)
    if (existsSync(modelDir)) {
      shell.openPath(modelDir)
    }
  })

  // Read local file → base64 (bypasses file:// restrictions in the renderer)
  ipcMain.handle('fs:readFileBase64', async (_, filePath: string) => {
    if (!isRemoteBackend() || existsSync(filePath)) return readLocalFileBase64(filePath)
    const { data: paths } = await axios.get<{ workspace_dir: string }>(
      `${selectedApiUrl()}/settings/paths`, { timeout: 5000 },
    )
    const normalizedFile = filePath.replace(/\\/g, '/')
    const workspaceRoot = paths.workspace_dir.replace(/\\/g, '/').replace(/\/+$/, '')
    if (!normalizedFile.startsWith(`${workspaceRoot}/`)) {
      throw new Error('Remote file is outside the selected server workspace')
    }
    const relativePath = normalizedFile.slice(workspaceRoot.length + 1)
    const response = await axios.get<ArrayBuffer>(
      `${selectedApiUrl()}/workspace/${relativePath.split('/').map(encodeURIComponent).join('/')}`,
      { responseType: 'arraybuffer', timeout: 60_000, maxContentLength: 100 * 1024 * 1024 },
    )
    return Buffer.from(response.data).toString('base64')
  })

  ipcMain.handle('fs:readScreenshotDataUrl', async (_, filename: string) => {
    const filePath = app.isPackaged
      ? join(process.resourcesPath, 'screenshots', filename)
      : join(app.getAppPath(), 'src/assets', filename)
    const buffer = await readFile(filePath)
    return `data:image/png;base64,${buffer.toString('base64')}`
  })

  // Model storage and download state are owned by the selected server.
  ipcMain.handle('model:listDownloaded', () => (
    axios.get<{ id: string; name: string; downloaded: boolean; size_gb?: number }[]>(
      `${selectedApiUrl()}/model/all`, { timeout: 10_000 },
    ).then(({ data }) => data.filter((model) => model.downloaded).map((model) => ({
      id: model.id, name: model.name, size_gb: model.size_gb ?? 0,
    })))
  ))

  ipcMain.handle('model:isDownloaded', async (_, modelId: string): Promise<boolean> => {
    try {
      const { data } = await axios.get(`${selectedApiUrl()}/model/storage/${encodeURIComponent(modelId)}/state`, { timeout: 10_000 })
      return Boolean(data.downloaded)
    } catch { return false }
  })

  ipcMain.handle('model:installedWeightVariants', async (_, modelId: string): Promise<string[] | null> => {
    try {
      const { data } = await axios.get(`${selectedApiUrl()}/model/storage/${encodeURIComponent(modelId)}/state`, { timeout: 10_000 })
      return data.installedWeightVariants ?? null
    } catch { return null }
  })

  ipcMain.handle('model:hasLocalData', async (_, modelId: string): Promise<boolean> => {
    try {
      const { data } = await axios.get(`${selectedApiUrl()}/model/storage/${encodeURIComponent(modelId)}/state`, { timeout: 10_000 })
      return Boolean(data.hasLocalData)
    } catch { return false }
  })

  ipcMain.handle('model:sharedGroups', async (_, extensionId: string) => {
    try {
      const { data } = await axios.get(
        `${selectedApiUrl()}/model/storage/extensions/${encodeURIComponent(extensionId)}/shared-groups`,
        { timeout: 10_000 },
      )
      return data
    } catch { return [] }
  })

  ipcMain.handle('model:deleteSharedGroup', async (_, extensionId: string, groupId: string): Promise<{ success: boolean; error?: string }> => {
    try {
      await axios.delete(
        `${selectedApiUrl()}/model/storage/extensions/${encodeURIComponent(extensionId)}/shared-groups/${encodeURIComponent(groupId)}`,
        { timeout: 60_000 },
      )
      notifyWeightChange()
      return { success: true }
    } catch (err) {
      return { success: false, error: axios.isAxiosError(err) ? (err.response?.data?.detail ?? err.message) : String(err) }
    }
  })

  ipcMain.handle('model:deleteExtensionWeights', async (_, extensionId: string): Promise<{ success: boolean; error?: string }> => {
    try {
      await axios.delete(`${selectedApiUrl()}/model/storage/extensions/${encodeURIComponent(extensionId)}`, { timeout: 60_000 })
      notifyWeightChange()
      return { success: true }
    } catch (err) {
      return { success: false, error: axios.isAxiosError(err) ? (err.response?.data?.detail ?? err.message) : String(err) }
    }
  })

  ipcMain.handle('model:activeDownloads', async () => {
    const { data } = await axios.get(`${selectedApiUrl()}/model/managed-downloads`, { timeout: 5000 })
    return data
  })

  ipcMain.handle('model:download', async (event, modelId: string, requestedVariantId?: string | null) => {
    try {
      const variantId = requestedVariantId ?? undefined
      const apiUrl = selectedApiUrl()
      event.sender.send('model:downloadProgress', {
        modelId, variantId, percent: 0, status: 'Preparing server download…', logs: ['Preparing server download'],
      })
      type ManagedDownloadResponse = {
        paused?: boolean
        cancelled?: boolean
        progress?: DownloadProgress & { logs?: string[] }
      }
      const requestState: {
        settled: boolean
        error: unknown
        data: ManagedDownloadResponse | null
      } = { settled: false, error: null, data: null }
      const requestController = new AbortController()
      let lastProgress: (DownloadProgress & { logs?: string[] }) | undefined
      let missingPolls = 0
      let reconciledComplete = false
      const request = axios.post(`${apiUrl}/model/managed-download`, null, {
        params: { model_id: modelId, variant_id: variantId },
        timeout: 0,
        signal: requestController.signal,
      }).then((response) => { requestState.data = response.data })
        .catch((error) => { requestState.error = error })
        .finally(() => { requestState.settled = true })

      while (!requestState.settled) {
        await new Promise((resolve) => setTimeout(resolve, 500))
        try {
          const response = await axios.get<Array<DownloadProgress & {
            modelId: string
            variantId?: string
            logs?: string[]
          }>>(`${apiUrl}/model/managed-downloads`, { timeout: 5_000 })
          const progress = response.data.find((candidate) => candidate.modelId === modelId)
          if (progress) {
            lastProgress = progress
            missingPolls = 0
            event.sender.send('model:downloadProgress', progress)
          } else {
            missingPolls += 1
            // The download POST can lose its response while the server-owned
            // job still finishes successfully. Once an observed job vanishes
            // (or two initial polls find none), reconcile against authoritative
            // server storage instead of leaving the UI at its last percentage.
            if (lastProgress || missingPolls >= 2) {
              const { data: state } = await axios.get<{
                downloaded?: boolean
                installedWeightVariants?: string[] | null
              }>(`${apiUrl}/model/storage/${encodeURIComponent(modelId)}/state`, { timeout: 10_000 })
              const variantReady = variantId
                ? state.installedWeightVariants?.includes(variantId) === true
                : Boolean(state.downloaded)
              if (variantReady) {
                reconciledComplete = true
                requestController.abort()
                break
              }
            }
          }
        } catch {
          // The blocking server request remains authoritative through transient polls.
        }
      }
      if (reconciledComplete) {
        event.sender.send('model:downloadProgress', {
          modelId,
          variantId,
          percent: 100,
          status: 'done',
          logs: lastProgress?.logs,
        })
        notifyWeightChange()
        return { success: true }
      }
      await request
      if (requestState.error) throw requestState.error
      const responseData = requestState.data
      if (responseData?.progress) event.sender.send('model:downloadProgress', { modelId, variantId, ...responseData.progress })
      if (responseData?.paused) {
        event.sender.send('model:downloadProgress', { modelId, variantId, percent: responseData.progress?.percent ?? 0, status: 'paused', paused: true })
        return { success: false, paused: true }
      }
      if (responseData?.cancelled) {
        event.sender.send('model:downloadProgress', { modelId, variantId, percent: 0, status: 'cancelled', cancelled: true })
        return { success: false, cancelled: true }
      }
      event.sender.send('model:downloadProgress', { modelId, variantId, percent: 100, status: 'done', logs: responseData?.progress?.logs })
      notifyWeightChange()
      return { success: true }
    } catch (err) {
      return { success: false, error: axios.isAxiosError(err) ? (err.response?.data?.detail ?? err.message) : String(err) }
    }
  })

  ipcMain.handle('model:pauseDownload', async (_, modelId: string): Promise<{ success: boolean; error?: string }> => {
    try {
      await axios.post(`${selectedApiUrl()}/model/managed-download/pause`, null, { params: { model_id: modelId }, timeout: 5000 })
      return { success: true }
    } catch (err) {
      return { success: false, error: axios.isAxiosError(err) ? (err.response?.data?.detail ?? err.message) : String(err) }
    }
  })

  ipcMain.handle('model:cancelDownload', async (_, modelId: string): Promise<{ success: boolean; error?: string }> => {
    try {
      await axios.post(`${selectedApiUrl()}/model/managed-download/cancel`, null, { params: { model_id: modelId }, timeout: 5000 })
      return { success: true }
    } catch (err) {
      return { success: false, error: axios.isAxiosError(err) ? (err.response?.data?.detail ?? err.message) : String(err) }
    }
  })

  // Export mesh to GLB / STL / OBJ
  ipcMain.handle('model:export', async (_, { outputUrl, format }: { outputUrl: string; format: string }) => {
    const win = getWindow()
    if (!win) return { success: false, error: 'No window' }

    const meshPath = outputUrl.replace(/^\/workspace\//, '')
    const baseName = meshPath.split('/').pop()?.replace(/\.\w+$/, '') ?? 'model'

    const result = await dialog.showSaveDialog(win, {
      title: 'Export 3D Model',
      defaultPath: `${baseName}.${format}`,
      filters: [{ name: format.toUpperCase(), extensions: [format] }],
    })
    if (result.canceled || !result.filePath) return { success: false }

    try {
      const response = await axios.get(
        `${selectedApiUrl()}/export/${format}?path=${encodeURIComponent(meshPath)}`,
        { responseType: 'arraybuffer' }
      )
      await writeFile(result.filePath, Buffer.from(response.data as ArrayBuffer))
      return { success: true }
    } catch (err) {
      return { success: false, error: String(err) }
    }
  })

  // Shell
  ipcMain.handle('shell:openExternal', (_, url: string) => shell.openExternal(url))

  // Open a model in OrcaSlicer via its orcaslicer://open?file=<url> deeplink.
  //
  // The returned error only covers the shell refusing the call outright. It is
  // NOT an install check: on Windows an unregistered scheme still makes
  // ShellExecuteEx succeed — the OS shows its own "You'll need a new app to open
  // this orcaslicer link" dialog and this resolves with success. Detecting a
  // missing OrcaSlicer would take a per-platform handler probe (registry on
  // Windows), so the renderer must not promise the user that it knows.
  ipcMain.handle('slicer:open', async (_, url: string): Promise<{ success: boolean; error?: string }> => {
    if (typeof url !== 'string' || !url.startsWith('orcaslicer://')) {
      return { success: false, error: 'slicer:open requires an orcaslicer:// URL' }
    }
    try {
      await shell.openExternal(url)
      return { success: true }
    } catch (err) {
      return { success: false, error: err instanceof Error ? err.message : String(err) }
    }
  })

  // Server resource counters always describe the selected server.
  ipcMain.handle('system:memory', async () => {
    const { data } = await axios.get<{ total: number; used: number; available: number }>(
      `${selectedApiUrl()}/system/memory`, { timeout: 5000 },
    )
    return data
  })

  ipcMain.handle('app:info', () => ({
    version:   app.getVersion(),
    userData:  app.getPath('userData'),
    modelsDir: getSettings(app.getPath('userData')).modelsDir,
    apiUrl:    API_BASE_URL,
    platform:  process.platform,
    arch:      process.arch,
  }))

  // Settings — decrypt the HF token (migrating a legacy plaintext one) and seed
  // it into the main-process env at startup.
  {
    const token = initHfToken(app.getPath('userData'))
    if (token) {
      process.env['HUGGING_FACE_HUB_TOKEN'] = token
      process.env['HF_TOKEN']               = token
    }
  }

  ipcMain.handle('settings:get', async () => {
    // hfToken is stored encrypted — hand the renderer the usable value.
    const local = getSettings(app.getPath('userData'))
    const { data } = await axios.get<{
      models_dir: string
      workspace_dir: string
      workflows_dir: string
      extensions_dir: string
    }>(`${selectedApiUrl()}/settings/paths`, { timeout: 5000 })
    return {
      ...local,
      modelsDir: data.models_dir,
      workspaceDir: data.workspace_dir,
      workflowsDir: data.workflows_dir,
      extensionsDir: data.extensions_dir,
      hfToken: getHfToken(),
    }
  })

  ipcMain.handle('settings:set', async (_event, patch: { modelsDir?: string; workspaceDir?: string; workflowsDir?: string; extensionsDir?: string; hfToken?: string }) => {
    const { hfToken, ...dirs } = patch
    await axios.post(`${selectedApiUrl()}/settings/paths`, {
      models_dir: dirs.modelsDir,
      workspace_dir: dirs.workspaceDir,
      workflows_dir: dirs.workflowsDir,
      extensions_dir: dirs.extensionsDir,
    }, { timeout: 60_000 })
    if (!isRemoteBackend()) {
      // Persist bundled-server launch paths after the live server accepts them.
      setSettings(app.getPath('userData'), dirs)
    }
    // Keep main-process env in sync so child processes spawned after token change inherit it
    if (hfToken !== undefined) {
      setHfToken(app.getPath('userData'), hfToken)
      process.env['HUGGING_FACE_HUB_TOKEN'] = hfToken
      process.env['HF_TOKEN']               = hfToken
      // Also push the token into the live FastAPI process env so extension
      // subprocesses spawned by ExtensionProcess._build_env() pick it up
      // without requiring a full app restart.
      try {
        await axios.post(`${selectedApiUrl()}/settings/hf-token`, { token: hfToken }, { timeout: 3000 })
      } catch { /* FastAPI may not be running yet — ignore */ }
    }
    const { data } = await axios.get(`${selectedApiUrl()}/settings/paths`, { timeout: 5000 })
    return {
      ...getSettings(app.getPath('userData')),
      modelsDir: data.models_dir,
      workspaceDir: data.workspace_dir,
      workflowsDir: data.workflows_dir,
      extensionsDir: data.extensions_dir,
      hfToken: getHfToken(),
    }
  })

  // Directory picker
  ipcMain.handle('fs:selectDirectory', async (_event, defaultPath?: string) => {
    const win = getWindow()
    if (!win) return null
    const result = await dialog.showOpenDialog(win, {
      properties: ['openDirectory', 'createDirectory'],
      ...(defaultPath && { defaultPath }),
    })
    return result.canceled ? null : result.filePaths[0]
  })

  // Cache clear — deletes and recreates the gen-cache folder
  // NOTE: userData/cache (lowercase) = Chromium disk cache on Windows (case-insensitive)
  //       → use a dedicated subfolder to avoid collision
  ipcMain.handle('cache:clear', async () => {
    const cacheDir = join(app.getPath('userData'), 'gen-cache')
    try {
      if (existsSync(cacheDir)) {
        await rmAsync(cacheDir, { recursive: true, force: true })
      }
      await mkdir(cacheDir, { recursive: true })
      return { success: true }
    } catch (err) {
      console.error('[cache:clear] error:', err)
      return { success: false, error: String(err) }
    }
  })

  // Workspace persistence is owned by the selected server.
  registerWorkspaceAssetLibraryIpcHandlers({
    ipcMain,
    getWorkspaceDir: () => getSettings(app.getPath('userData')).workspaceDir,
    getRemoteApiUrl: () => selectedApiUrl(),
    getRemoteApiHeaders: getSelectedApiHeaders,
  })

  ipcMain.handle('workspace:listCollections', async () => {
    const { data } = await axios.get(`${selectedApiUrl()}/workspace-admin/collections`, { timeout: 10_000 })
    return data
  })

  ipcMain.handle('workspace:createCollection', async (_, name: string) => {
    await axios.post(`${selectedApiUrl()}/workspace-admin/collections/${encodeURIComponent(name)}`, {}, { timeout: 10_000 })
  })

  ipcMain.handle('workspace:renameCollection', async (_, { oldName, newName }: { oldName: string; newName: string }) => {
    await axios.post(`${selectedApiUrl()}/workspace-admin/rename-collection`, { old_name: oldName, new_name: newName }, { timeout: 10_000 })
  })

  ipcMain.handle('workspace:deleteCollection', async (_, name: string) => {
    await axios.delete(`${selectedApiUrl()}/workspace-admin/collections/${encodeURIComponent(name)}`, { timeout: 10_000 })
  })

  ipcMain.handle('workspace:listJobs', async (_, collection: string) => {
    try {
      const { data } = await axios.get(`${selectedApiUrl()}/workspace-admin/collections/${encodeURIComponent(collection)}/jobs`, { timeout: 10_000 })
      return data
    } catch { return [] }
  })

  ipcMain.handle('workspace:saveJobMeta', async (_, { collection, filename, meta }: { collection: string; filename: string; meta: unknown }) => {
    await axios.put(`${selectedApiUrl()}/workspace-admin/collections/${encodeURIComponent(collection)}/jobs`, { filename, meta }, { timeout: 10_000 })
  })

  ipcMain.handle('workspace:deleteJob', async (_, { collection, filename }: { collection: string; filename: string }) => {
    await axios.delete(`${selectedApiUrl()}/workspace-admin/collections/${encodeURIComponent(collection)}/jobs/${encodeURIComponent(filename)}`, { timeout: 10_000 })
  })

  ipcMain.handle('workspace:stageFile', async (_, filePath: string) => stageFileOnSelectedServer(filePath))

  // Directory utilities for settings
  ipcMain.handle('fs:listDir', async (_, dirPath: string) => {
    try {
      const entries = await readdir(dirPath, { withFileTypes: true })
      return entries.filter(e => e.isDirectory()).map(e => e.name)
    } catch {
      return []
    }
  })

  // List files (not directories) in a folder, optionally filtered by extension.
  // `extensions` are lowercase without the dot (e.g. ['txt', 'png']).
  ipcMain.handle('fs:listFiles', async (_, dirPath: string, extensions?: string[]) => {
    if (isRemoteBackend() && !existsSync(dirPath)) {
      try {
        const { data } = await axios.post<string[]>(`${selectedApiUrl()}/workspace-admin/list-files`, {
          path: dirPath,
          extensions,
        }, { timeout: 10_000 })
        return data
      } catch {
        return []
      }
    }
    try {
      const wanted = extensions?.map(e => e.toLowerCase().replace(/^\./, ''))
      const entries = await readdir(dirPath, { withFileTypes: true })
      return entries
        .filter(e => e.isFile())
        .map(e => e.name)
        .filter(name => {
          if (!wanted || wanted.length === 0) return true
          const ext = name.split('.').pop()?.toLowerCase() ?? ''
          return wanted.includes(ext)
        })
        .sort((a, b) => a.localeCompare(b, undefined, { numeric: true }))
    } catch {
      return []
    }
  })

  // Text-file picker for the standalone Load Prompt node.
  ipcMain.handle('fs:selectTextFile', async () => {
    const win = getWindow()
    if (!win) return null
    const result = await dialog.showOpenDialog(win, {
      title: 'Select a prompt text file',
      filters: [{ name: 'Text', extensions: ['txt', 'md', 'prompt'] }],
      properties: ['openFile'],
    })
    return result.canceled ? null : result.filePaths[0]
  })

  // Adding a model is an upload because the selected server cannot fetch a
  // client-only local file independently.
  ipcMain.handle('agent:addModel', async (): Promise<{ success: boolean; cancelled?: boolean; fileName?: string; error?: string }> => {
    const win = getWindow()
    if (!win) return { success: false, error: 'No window available' }
    const result = await dialog.showOpenDialog(win, {
      title: 'Add a model',
      filters: [{ name: 'GGUF model', extensions: ['gguf'] }],
      properties: ['openFile'],
    })
    if (result.canceled || !result.filePaths[0]) return { success: false, cancelled: true }
    const src = result.filePaths[0]
    const fileName = basename(src)
    if (!fileName.toLowerCase().endsWith('.gguf')) return { success: false, error: 'Only .gguf files can be added.' }
    try {
      await axios.post(`${selectedApiUrl()}/llm/models/upload`, createReadStream(src), {
        headers: {
          'Content-Type': 'application/octet-stream',
          'X-Modly-Filename': encodeURIComponent(fileName),
        },
        maxBodyLength: Infinity,
        maxContentLength: Infinity,
        timeout: 0,
      })
      return { success: true, fileName }
    } catch (err) {
      return {
        success: false,
        error: axios.isAxiosError(err) ? (err.response?.data?.detail ?? err.message) : String(err),
      }
    }
  })

  ipcMain.handle('fs:moveDirectory', async (_, { src, dest }: { src: string; dest: string }) => {
    try {
      await mkdir(dest, { recursive: true })
      await cp(src, dest, { recursive: true })
      await rmAsync(src, { recursive: true, force: true })
      return { success: true }
    } catch (err) {
      return { success: false, error: String(err) }
    }
  })

  ipcMain.handle('fs:deleteDirectory', async (_, dirPath: string) => {
    try {
      const { data: paths } = await axios.get<{ workspace_dir: string }>(
        `${selectedApiUrl()}/settings/paths`, { timeout: 5000 },
      )
      const normalized = dirPath.replace(/\\/g, '/').replace(/\/+$/, '')
      const serverTmp = `${paths.workspace_dir.replace(/\\/g, '/').replace(/\/+$/, '')}/tmp`
      if (normalized !== serverTmp) return { success: false, error: 'Only the selected server workspace temp directory can be cleared' }
      await axios.delete(`${selectedApiUrl()}/workspace-admin/tmp`, { timeout: 10_000 })
      return { success: true }
    } catch (err) {
      return { success: false, error: String(err) }
    }
  })

  // Extensions are discovered and filtered by the currently selected server.
  ipcMain.handle('extensions:runtimeStatus', async () => {
    const { data } = await axios.get(`${selectedApiUrl()}/extensions/runtime`, { timeout: 10_000 })
    return data
  })

  ipcMain.handle('extensions:setDockerEnabled', async (_, useDocker: boolean) => {
    try {
      const { data } = await axios.put(
        `${selectedApiUrl()}/extensions/runtime`,
        { useDocker: Boolean(useDocker) },
        { timeout: 60_000 },
      )
      return data
    } catch (error) {
      const message = axios.isAxiosError(error)
        ? (error.response?.data?.detail ?? error.message)
        : String(error)
      throw new Error(String(message))
    }
  })

  ipcMain.handle('extensions:list', async () => {
    const { data } = await axios.get(`${selectedApiUrl()}/extensions/list`, { timeout: 10_000 })
    return data
  })

  // Install an extension from a GitHub repo URL
  ipcMain.handle('extensions:installFromGitHub', async (event, githubUrl: string) => {
    const win    = getWindow()
    const emit   = (data: object) => win?.webContents.send('extensions:installProgress', data)
    const installState: {
        result: Record<string, unknown> | null
        serverError: { message: string; details?: string } | null
      } = { result: null, serverError: null }
      try {
        const response = await axios.post<NodeJS.ReadableStream>(
          `${selectedApiUrl()}/extensions/install-github`,
          { url: githubUrl },
          {
            responseType: 'stream',
            timeout: 0,
            headers: { Accept: 'text/event-stream' },
          },
        )
        const installId = String(response.headers['x-modly-install-id'] ?? '')
        let buffer = ''
        let lastEvent = ''
        const acceptEvent = (installEvent: Record<string, unknown>) => {
          const fingerprint = JSON.stringify(installEvent)
          if (fingerprint === lastEvent) return
          lastEvent = fingerprint
          if (installEvent.step === 'result') installState.result = installEvent
          else {
            if (installEvent.step === 'error') {
              installState.serverError = {
                message: String(installEvent.message ?? 'Extension installation failed'),
                details: typeof installEvent.details === 'string' ? installEvent.details : undefined,
              }
            }
            emit(installEvent)
          }
        }
        const consumeEvent = (block: string) => {
          const payload = block
            .split('\n')
            .filter((line) => line.startsWith('data:'))
            .map((line) => line.slice(5).trimStart())
            .join('\n')
          if (!payload) return
          acceptEvent(JSON.parse(payload) as Record<string, unknown>)
        }
        let streamFailure: unknown = null
        try {
          for await (const chunk of response.data) {
            buffer += Buffer.isBuffer(chunk) ? chunk.toString('utf-8') : String(chunk)
            const blocks = buffer.split(/\r?\n\r?\n/)
            buffer = blocks.pop() ?? ''
            for (const block of blocks) consumeEvent(block)
          }
          if (buffer.trim()) consumeEvent(buffer)
        } catch (error) {
          streamFailure = error
        }

        // The server owns the transaction after it starts. If a long wheel
        // download outlives the SSE socket, recover its latest/terminal state
        // instead of reporting a false failure while setup continues remotely.
        if (!installState.result && !installState.serverError && installId) {
          let consecutiveFailures = 0
          for (let attempt = 0; attempt < 1800 && !installState.result && !installState.serverError; attempt += 1) {
            await new Promise((resolve) => setTimeout(resolve, 2_000))
            try {
              const status = await axios.get<{
                done: boolean
                latest?: Record<string, unknown> | null
                terminal?: Record<string, unknown> | null
              }>(`${selectedApiUrl()}/extensions/install-github/${encodeURIComponent(installId)}/status`, {
                timeout: 10_000,
              })
              consecutiveFailures = 0
              const recovered = status.data.terminal ?? status.data.latest
              if (recovered) acceptEvent(recovered)
              if (status.data.done && !status.data.terminal) {
                throw new Error('The server completed the install without terminal status')
              }
            } catch (error) {
              consecutiveFailures += 1
              if (consecutiveFailures >= 30) {
                throw new Error(`Lost the install progress stream and could not recover server status: ${String(error)}`)
              }
            }
          }
        }
        if (installState.serverError) throw new Error(installState.serverError.message)
        if (!installState.result || installState.result.success !== true) {
          throw streamFailure ?? new Error('The server ended the install without a result')
        }
        emit({ step: 'done', extensionId: installState.result.extensionId })
        return installState.result
      } catch (err) {
        const error = installState.serverError?.message ?? (
          axios.isAxiosError(err) ? (err.response?.data?.detail ?? err.message) : String(err)
        )
        if (!installState.serverError) emit({ step: 'error', message: error })
        return { success: false, error }
      }
  })

  // Extension lifecycle mutations are always owned by the selected server.
  ipcMain.handle('extensions:uninstall', async (_, extensionId: string) => {
    try {
      await axios.delete(
        `${selectedApiUrl()}/extensions/${encodeURIComponent(extensionId)}`,
        { timeout: 60_000 },
      )
      return { success: true }
    } catch (err) {
      return {
        success: false,
        error: axios.isAxiosError(err) ? (err.response?.data?.detail ?? err.message) : String(err),
      }
    }
  })

  // Repair runs on the selected server, including the bundled local server.
  ipcMain.handle('extensions:repair', async (_, extensionId: string) => {
    try {
      await axios.post(
        `${selectedApiUrl()}/extensions/setup/${encodeURIComponent(extensionId)}`,
        {},
        { timeout: 30 * 60_000 },
      )
      return { success: true }
    } catch (err) {
      return {
        success: false,
        error: axios.isAxiosError(err) ? (err.response?.data?.detail ?? err.message) : String(err),
      }
    }
  })

  // A local checkout is selected by the desktop, but linking and validation
  // remain server-owned. Standalone/remote servers reject host filesystem paths.
  ipcMain.handle('extensions:installFromLocal', async () => {
    if (isRemoteBackend()) {
      return { success: false, error: 'Local-folder extensions cannot be linked to a remote server. Install from GitHub instead.' }
    }
    const win = getWindow()
    const emit = (data: object) => win?.webContents.send('extensions:installProgress', data)
    const pickResult = await dialog.showOpenDialog(win!, {
      title: 'Select local extension folder',
      properties: ['openDirectory'],
    })
    if (pickResult.canceled || pickResult.filePaths.length === 0) {
      return { success: false, cancelled: true }
    }
    try {
      emit({ step: 'validating' })
      const { data } = await axios.post(`${selectedApiUrl()}/extensions/install-local`, {
        path: pickResult.filePaths[0],
      }, { timeout: 60_000 })
      emit(data.success
        ? { step: 'done', extensionId: data.extensionId }
        : { step: 'error', extensionId: data.extensionId, message: data.error })
      return data
    } catch (err) {
      const error = axios.isAxiosError(err) ? (err.response?.data?.detail ?? err.message) : String(err)
      emit({ step: 'error', message: error })
      return { success: false, error }
    }
  })

  // Trigger Python extension reload (without touching the filesystem)
  ipcMain.handle('extensions:reload', async () => {
    try {
      const res = await axios.post(`${selectedApiUrl()}/extensions/reload`, {}, { timeout: 60_000 })
      return { success: true, errors: (res.data as { errors?: Record<string, string> }).errors ?? {} }
    } catch (err) {
      return { success: false, error: String(err) }
    }
  })

  // Process extensions execute on the selected server in every runtime mode.
  ipcMain.handle('extensions:runProcess', async (_, extensionId: string, input: { filePath?: string; text?: string; texts?: (string | undefined)[]; nodeId?: string }, params: Record<string, unknown>) => {
    try {
      const serverInput = { ...input }
      if (serverInput.filePath) serverInput.filePath = await stageFileOnSelectedServer(serverInput.filePath)
      const serverParams = { ...params }
      if (Array.isArray(serverParams.extra_image_paths)) {
        serverParams.extra_image_paths = await Promise.all(
          serverParams.extra_image_paths.map((path) => (
            typeof path === 'string' ? stageFileOnSelectedServer(path) : path
          )),
        )
      }
      const { data } = await axios.post(`${selectedApiUrl()}/extensions/run-process`, {
        extension_id: extensionId,
        input: serverInput,
        params: serverParams,
      }, { timeout: 0 })
      return data
    } catch (err) {
      return {
        success: false,
        error: axios.isAxiosError(err) ? (err.response?.data?.detail ?? err.message) : String(err),
      }
    }
  })

  // Auto-updater
  ipcMain.handle('updater:check', async () => {
    if (!app.isPackaged || !updatesSupported) return { success: false }
    try {
      await autoUpdater.checkForUpdates()
      return { success: true }
    } catch (err) {
      logger.error(`[updater:check] ${err}`)
      return { success: false }
    }
  })

  ipcMain.handle('updater:quitAndInstall', () => {
    if (!updatesSupported) return
    app.removeAllListeners('window-all-closed')
    BrowserWindow.getAllWindows().forEach(w => w.destroy())
    autoUpdater.quitAndInstall(true, true)
  })

  // Update FastAPI paths at runtime (without restarting)
  ipcMain.handle('api:updatePaths', async (_event, patch: { modelsDir?: string; workspaceDir?: string; extensionsDir?: string }) => {
    try {
      await axios.post(`${selectedApiUrl()}/settings/paths`, {
        models_dir:     patch.modelsDir,
        workspace_dir:  patch.workspaceDir,
        extensions_dir: patch.extensionsDir,
      })
      if (!isRemoteBackend()) setSettings(app.getPath('userData'), patch)
      return { success: true }
    } catch (err) {
      return { success: false, error: String(err) }
    }
  })

  // ── Workflows ────────────────────────────────────────────────────────────

  ipcMain.handle('workflows:list', async () => {
    const { data } = await axios.get(`${selectedApiUrl()}/workflows`, { timeout: 10_000 })
    return data
  })

  ipcMain.handle('workflows:save', async (_, workflow: { id: string; [key: string]: unknown }) => {
    try {
      await axios.put(`${selectedApiUrl()}/workflows/${encodeURIComponent(workflow.id)}`, workflow, { timeout: 10_000 })
      return { success: true }
    } catch (err) {
      return { success: false, error: String(err) }
    }
  })

  ipcMain.handle('workflows:delete', async (_, id: string) => {
    try {
      await axios.delete(`${selectedApiUrl()}/workflows/${encodeURIComponent(id)}`, { timeout: 10_000 })
      return { success: true }
    } catch (err) {
      return { success: false, error: String(err) }
    }
  })

  ipcMain.handle('workflows:import', async () => {
    const win = getWindow()
    const result = await dialog.showOpenDialog(win!, {
      title: 'Import Workflow',
      filters: [{ name: 'Workflow', extensions: ['json'] }],
      properties: ['openFile'],
    })
    if (result.canceled || result.filePaths.length === 0) return { success: false }
    try {
      const raw = await readFile(result.filePaths[0], 'utf-8')
      const workflow = JSON.parse(raw)
      if (!workflow.id || !workflow.nodes) return { success: false, error: 'Invalid workflow file' }
      await axios.put(`${selectedApiUrl()}/workflows/${encodeURIComponent(workflow.id)}`, workflow, { timeout: 10_000 })
      return { success: true, workflow }
    } catch (err) {
      return { success: false, error: String(err) }
    }
  })

  ipcMain.handle('workflows:export', async (_, workflow: { id: string; name?: string; [key: string]: unknown }) => {
    const win = getWindow()
    const result = await dialog.showSaveDialog(win!, {
      title: 'Export Workflow',
      defaultPath: `${workflow.name ?? workflow.id}.json`,
      filters: [{ name: 'Workflow', extensions: ['json'] }],
    })
    if (result.canceled || !result.filePath) return { success: false }
    try {
      await writeFile(result.filePath, JSON.stringify(workflow, null, 2), 'utf-8')
      return { success: true }
    } catch (err) {
      return { success: false, error: String(err) }
    }
  })
}
