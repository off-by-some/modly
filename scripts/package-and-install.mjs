import { execFileSync } from 'node:child_process'
import { existsSync, renameSync, rmSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const PROJECT_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const BUILT_APP = join(PROJECT_ROOT, 'dist', 'mac-arm64', 'Modly.app')
const INSTALL_DIR = '/Applications'
const INSTALLED_APP = '/Applications/Modly.app'
const STAGED_APP = `/Applications/.Modly.installing-${process.pid}.app`
const PREVIOUS_APP = `/Applications/.Modly.previous-${process.pid}.app`

function run(command, args, options = {}) {
  execFileSync(command, args, { stdio: 'inherit', ...options })
}

function isModlyRunning() {
  try {
    execFileSync('pgrep', ['-x', 'Modly'], { stdio: 'ignore' })
    return true
  } catch {
    return false
  }
}

function quitRunningApp() {
  if (!isModlyRunning()) return

  console.log('[package-and-install] Asking the running Modly app to quit…')
  run('osascript', ['-e', 'tell application id "com.modly.app" to quit'])

  const sleeper = new Int32Array(new SharedArrayBuffer(4))
  for (let attempt = 0; attempt < 40 && isModlyRunning(); attempt += 1) {
    Atomics.wait(sleeper, 0, 0, 250)
  }
  if (isModlyRunning()) {
    throw new Error('Modly is still running. Quit it manually, then run the command again.')
  }
}

function assertSafePaths() {
  if (INSTALL_DIR !== '/Applications' || INSTALLED_APP !== '/Applications/Modly.app') {
    throw new Error('Refusing to install: the application destination is not the expected fixed path.')
  }
  if (!BUILT_APP.endsWith('/dist/mac-arm64/Modly.app')) {
    throw new Error('Refusing to install: the packaged app path is unexpected.')
  }
}

function packageAndInstall() {
  if (process.platform !== 'darwin') {
    throw new Error('package-and-install is only supported on macOS.')
  }
  assertSafePaths()

  console.log('[package-and-install] Packaging Modly for Apple Silicon…')
  run('npm', ['run', 'package:mac'], { cwd: PROJECT_ROOT })

  if (!existsSync(join(BUILT_APP, 'Contents', 'Info.plist'))) {
    throw new Error(`Packaged application not found at ${BUILT_APP}`)
  }

  // Stage and verify the new bundle before removing the installed version. The
  // only paths this installer ever removes are the fixed app bundle and its own
  // process-specific staging bundle; user data is deliberately out of scope.
  rmSync(STAGED_APP, { recursive: true, force: true })
  rmSync(PREVIOUS_APP, { recursive: true, force: true })
  try {
    console.log('[package-and-install] Staging the new app in /Applications…')
    run('ditto', [BUILT_APP, STAGED_APP])
    run('codesign', ['--verify', '--verbose=2', STAGED_APP])

    quitRunningApp()

    if (existsSync(INSTALLED_APP)) {
      console.log('[package-and-install] Replacing the old /Applications/Modly.app…')
      renameSync(INSTALLED_APP, PREVIOUS_APP)
    }
    try {
      renameSync(STAGED_APP, INSTALLED_APP)
    } catch (error) {
      // If activating the new bundle fails, restore the previous installation.
      if (existsSync(PREVIOUS_APP) && !existsSync(INSTALLED_APP)) {
        renameSync(PREVIOUS_APP, INSTALLED_APP)
      }
      throw error
    }
    rmSync(PREVIOUS_APP, { recursive: true, force: true })
  } finally {
    rmSync(STAGED_APP, { recursive: true, force: true })
    // Once the new or restored app is present, any process-specific backup is
    // no longer needed. If neither exists, leave the backup for manual recovery.
    if (existsSync(INSTALLED_APP)) rmSync(PREVIOUS_APP, { recursive: true, force: true })
  }

  console.log('[package-and-install] Installed /Applications/Modly.app')
  console.log('[package-and-install] User data was preserved; settings, models, workspaces, and extensions were not modified.')
}

if (process.argv.includes('--help')) {
  console.log('Build the Apple Silicon app and replace /Applications/Modly.app without deleting user data.')
  console.log('Usage: npm run package-and-install')
} else {
  try {
    packageAndInstall()
  } catch (error) {
    console.error(`[package-and-install] ${error instanceof Error ? error.message : String(error)}`)
    process.exitCode = 1
  }
}
