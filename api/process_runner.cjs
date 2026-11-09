const fs = require('fs')
const path = require('path')
const Module = require('module')

async function main() {
  const extensionDir = process.argv[2]
  const entry = process.argv[3]
  const payload = JSON.parse(fs.readFileSync(0, 'utf8'))
  const requireFromExtension = Module.createRequire(path.join(extensionDir, '_'))
  const processor = requireFromExtension(path.join(extensionDir, entry))
  if (typeof processor !== 'function') throw new Error('Process extension must export a function')
  const context = {
    workspaceDir: payload.workspaceDir,
    tempDir: payload.tempDir,
    nodeId: payload.input?.nodeId ?? '',
    log: () => {},
    progress: () => {},
  }
  const result = await processor(payload.input ?? {}, payload.params ?? {}, context)
  process.stdout.write(JSON.stringify({ type: 'done', result: result ?? {} }) + '\n')
}

main().catch((error) => {
  process.stdout.write(JSON.stringify({ type: 'error', message: String(error) }) + '\n')
  process.exitCode = 1
})
