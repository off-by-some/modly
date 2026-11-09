import test from 'node:test'
import assert from 'node:assert/strict'
import { buildSync } from 'esbuild'
import { createRequire } from 'node:module'
import { mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'

function loadModule() {
  const dir = mkdtempSync(join(tmpdir(), 'modly-backend-server-test-'))
  const outfile = join(dir, 'backendServer.cjs')
  const result = buildSync({
    entryPoints: [resolve('src/shared/utils/backendServer.ts')],
    bundle: true,
    platform: 'node',
    format: 'cjs',
    write: false,
  })
  writeFileSync(outfile, result.outputFiles[0].text, 'utf8')
  return createRequire(import.meta.url)(outfile)
}

const { checkBackendServer, normalizeBackendUrl } = loadModule()

test('normalizes backend server addresses', () => {
  assert.equal(normalizeBackendUrl(' 192.168.1.20:8765/ '), 'http://192.168.1.20:8765')
  assert.equal(normalizeBackendUrl('https://modly.example/api/'), 'https://modly.example/api')
})

test('rejects unsupported or unsafe backend server addresses', () => {
  assert.throws(() => normalizeBackendUrl(''), /Enter a server address/)
  assert.throws(() => normalizeBackendUrl('ftp://example.com'), /HTTP or HTTPS/)
  assert.throws(() => normalizeBackendUrl('http://user:pass@example.com'), /Credentials/)
  assert.throws(() => normalizeBackendUrl('https://example.com?q=1'), /query string/)
})

test('checks the normalized health endpoint before accepting a server', async () => {
  const originalWindow = globalThis.window
  const originalFetch = globalThis.fetch
  let requestedUrl = ''
  globalThis.window = { setTimeout, clearTimeout }
  globalThis.fetch = async (url) => {
    requestedUrl = String(url)
    return { ok: true, json: async () => ({ status: 'ok' }) }
  }

  try {
    assert.equal(await checkBackendServer('modly.local:8765/'), 'http://modly.local:8765')
    assert.equal(requestedUrl, 'http://modly.local:8765/health')
  } finally {
    globalThis.window = originalWindow
    globalThis.fetch = originalFetch
  }
})

test('rejects a server with an incompatible health response', async () => {
  const originalWindow = globalThis.window
  const originalFetch = globalThis.fetch
  globalThis.window = { setTimeout, clearTimeout }
  globalThis.fetch = async () => ({ ok: true, json: async () => ({ status: 'starting' }) })

  try {
    await assert.rejects(() => checkBackendServer('http://modly.local:8765'), /unexpected health response/)
  } finally {
    globalThis.window = originalWindow
    globalThis.fetch = originalFetch
  }
})
