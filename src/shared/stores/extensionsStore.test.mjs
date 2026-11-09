import test from 'node:test'
import assert from 'node:assert/strict'
import { buildSync } from 'esbuild'
import { createRequire } from 'node:module'
import { mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'

function loadModule() {
  const outfile = join(mkdtempSync(join(tmpdir(), 'modly-ext-store-test-')), 'extensionsStore.cjs')
  const require = createRequire(import.meta.url)
  const result = buildSync({
    entryPoints: [resolve('src/shared/stores/extensionsStore.ts')],
    bundle: true,
    platform: 'node',
    format: 'cjs',
    write: false,
  })
  writeFileSync(outfile, result.outputFiles[0].text, 'utf8')
  return require(outfile)
}

test('extension list keeps an interrupted model in the model store with recovery metadata', () => {
  const { partitionExtensionsByType } = loadModule()
  const pendingModel = {
    type: 'model',
    id: 'pixal3d',
    name: 'Pixal3D',
    trusted: false,
    builtin: false,
    nodes: [{ id: 'generate' }],
    corrupted: true,
    manifestError: 'incomplete',
  }
  const processExtension = {
    type: 'process',
    id: 'mesh-tool',
    name: 'Mesh Tool',
    trusted: false,
    builtin: false,
    entry: 'processor.js',
    nodes: [{ id: 'simplify' }],
  }

  const result = partitionExtensionsByType([pendingModel, processExtension])

  assert.deepEqual(result.modelExtensions, [pendingModel])
  assert.deepEqual(result.processExtensions, [processExtension])
})

test('failed extension setup retains live output and terminal traceback', async () => {
  let progress
  globalThis.window = {
    electron: {
      extensions: {
        onInstallProgress(callback) {
          progress = callback
        },
        offInstallProgress() {},
        async installFromGitHub() {
          progress({ step: 'setting_up', message: 'Setting up Python environment' })
          progress({ step: 'setting_up', message: 'installing dependency' })
          progress({
            step: 'error',
            message: 'Setting up Python environment failed (exit 1)',
            details: 'installing dependency\nTraceback (most recent call last):\n  File "setup.py", line 1\nRuntimeError: setup exploded',
          })
          return { success: false, error: 'Setting up Python environment failed (exit 1)' }
        },
      },
    },
  }

  try {
    const { useExtensionsStore } = loadModule()
    const result = await useExtensionsStore.getState().installFromGitHub('https://github.com/acme/example')
    const state = useExtensionsStore.getState()

    assert.equal(result.success, false)
    assert.equal(state.installProgress.step, 'error')
    assert.equal(state.installError, 'Setting up Python environment failed (exit 1)')
    assert.deepEqual(state.installLogs, [
      'Setting up Python environment',
      'installing dependency',
      'Traceback (most recent call last):',
      '  File "setup.py", line 1',
      'RuntimeError: setup exploded',
    ])
  } finally {
    delete globalThis.window
  }
})
