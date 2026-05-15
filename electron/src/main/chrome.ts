import { spawn, exec } from 'node:child_process'
import { existsSync, mkdirSync, unlinkSync } from 'node:fs'
import { join } from 'node:path'
import { homedir } from 'node:os'
import { promisify } from 'node:util'

const execAsync = promisify(exec)

const CDP_ENDPOINT = 'http://127.0.0.1:9222'
const CDP_PORT = 9222
const LOGIN_PATH_HINTS = ['/login', '/checkpoint', '/uas/login', '/authwall', '/signup']
const LOGGED_IN_PATH_HINTS = ['/feed', '/in/', '/jobs', '/mynetwork', '/messaging', '/company/']

export interface CdpProbe {
  alive: boolean
  endpoint: string
}

export interface LoginStatus {
  state: 'logged_in' | 'logged_out' | 'no_tab' | 'unknown'
  url: string | null
}

export interface LaunchResult {
  launched: boolean
  executable: string | null
  error: string | null
}

export async function probeCdp(timeoutMs = 1500): Promise<CdpProbe> {
  try {
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), timeoutMs)
    const response = await fetch(`${CDP_ENDPOINT}/json/version`, { signal: controller.signal })
    clearTimeout(timer)
    return { alive: response.ok, endpoint: CDP_ENDPOINT }
  } catch {
    return { alive: false, endpoint: CDP_ENDPOINT }
  }
}

export async function isChromeRunning(): Promise<boolean> {
  if (process.platform === 'win32') {
    try {
      const { stdout } = await execAsync('tasklist /FI "IMAGENAME eq chrome.exe" /NH')
      return /chrome\.exe/i.test(stdout)
    } catch {
      return false
    }
  }
  try {
    const cmd = process.platform === 'darwin' ? 'pgrep -x "Google Chrome"' : 'pgrep -x chrome'
    const { stdout } = await execAsync(cmd)
    return stdout.trim().length > 0
  } catch {
    return false
  }
}

async function waitUntilNoChrome(timeoutMs: number): Promise<boolean> {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (!(await isChromeRunning())) return true
    await delay(400)
  }
  return false
}

export async function killChrome(): Promise<{ killed: boolean }> {
  if (process.platform === 'win32') {
    // Try graceful first so Chrome saves session…
    try {
      await execAsync('taskkill /IM chrome.exe /T')
    } catch {
      // ignore — Chrome may not be running
    }
    if (await waitUntilNoChrome(2500)) {
      cleanupSingletonLocks()
      return { killed: true }
    }
    // …then force everything: main + utility + GPU + background tray.
    try {
      await execAsync('taskkill /F /IM chrome.exe /T')
    } catch {
      // ignore
    }
    // Some users have Chrome variants like chrome.exe AND chromedriver/elevation
    // helpers — sweep common siblings too. Failures are fine.
    for (const image of ['chrome.exe', 'GoogleCrashHandler.exe', 'GoogleCrashHandler64.exe']) {
      try {
        await execAsync(`taskkill /F /IM ${image} /T`)
      } catch {
        // ignore
      }
    }
    const gone = await waitUntilNoChrome(5000)
    cleanupSingletonLocks()
    return { killed: gone }
  }
  const cmd =
    process.platform === 'darwin' ? 'pkill -x "Google Chrome"' : 'pkill -x chrome'
  try {
    await execAsync(cmd)
  } catch {
    // ignore
  }
  if (await waitUntilNoChrome(2500)) {
    cleanupSingletonLocks()
    return { killed: true }
  }
  try {
    await execAsync(cmd.replace('pkill', 'pkill -9'))
  } catch {
    // ignore
  }
  const gone = await waitUntilNoChrome(5000)
  cleanupSingletonLocks()
  return { killed: gone }
}

function cleanupSingletonLocks(): void {
  // Stale Singleton* files can keep Chrome from launching cleanly into the
  // dedicated profile after a hard kill. Clean them in our profile only —
  // never touch the user's default Chrome profile.
  const dir = dedicatedProfileDir()
  for (const name of ['SingletonLock', 'SingletonCookie', 'SingletonSocket']) {
    const path = join(dir, name)
    try {
      if (existsSync(path)) unlinkSync(path)
    } catch {
      // ignore — file may be transient or permission-locked
    }
  }
}

export function findChromeExecutable(): string | null {
  const candidates: string[] = []
  if (process.platform === 'win32') {
    const local = process.env.LOCALAPPDATA || join(homedir(), 'AppData', 'Local')
    const programFiles = process.env['PROGRAMFILES'] || 'C:\\Program Files'
    const programFilesX86 = process.env['PROGRAMFILES(X86)'] || 'C:\\Program Files (x86)'
    candidates.push(
      join(programFiles, 'Google', 'Chrome', 'Application', 'chrome.exe'),
      join(programFilesX86, 'Google', 'Chrome', 'Application', 'chrome.exe'),
      join(local, 'Google', 'Chrome', 'Application', 'chrome.exe')
    )
  } else if (process.platform === 'darwin') {
    candidates.push(
      '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
      join(homedir(), 'Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    )
  } else {
    candidates.push('/usr/bin/google-chrome', '/usr/bin/google-chrome-stable', '/usr/bin/chromium')
  }
  return candidates.find((p) => existsSync(p)) ?? null
}

function dedicatedProfileDir(): string {
  // Chrome 136+ silently drops --remote-debugging-port when --user-data-dir
  // points at the OS-default Chrome profile (cookie-theft mitigation). The
  // only way to get CDP is to use a non-default profile directory. We keep
  // one dedicated to this app so the LinkedIn login persists across runs.
  if (process.platform === 'win32') {
    const local = process.env.LOCALAPPDATA || join(homedir(), 'AppData', 'Local')
    return join(local, 'BeautifulLinkedIn', 'ChromeProfile')
  }
  if (process.platform === 'darwin') {
    return join(homedir(), 'Library', 'Application Support', 'BeautifulLinkedIn', 'ChromeProfile')
  }
  return join(homedir(), '.config', 'beautiful-linkedin', 'chrome-profile')
}

function ensureProfileDir(): string {
  const dir = dedicatedProfileDir()
  try {
    mkdirSync(dir, { recursive: true })
  } catch {
    // best-effort: Chrome will also try to create it
  }
  return dir
}

export async function launchChromeWithCdp(initialUrl?: string): Promise<LaunchResult> {
  const executable = findChromeExecutable()
  if (!executable) {
    return {
      launched: false,
      executable: null,
      error: 'Chrome não encontrado nos caminhos padrão. Instale o Google Chrome.'
    }
  }
  const userDataDir = ensureProfileDir()
  const startUrl = initialUrl && /^https?:\/\//i.test(initialUrl)
    ? initialUrl
    : 'https://www.linkedin.com/feed/'
  try {
    const child = spawn(
      executable,
      [
        `--remote-debugging-port=${CDP_PORT}`,
        `--user-data-dir=${userDataDir}`,
        '--no-first-run',
        '--no-default-browser-check',
        startUrl
      ],
      { detached: true, stdio: 'ignore' }
    )
    child.unref()
    return { launched: true, executable, error: null }
  } catch (error) {
    return {
      launched: false,
      executable,
      error: error instanceof Error ? error.message : String(error)
    }
  }
}

export async function waitForCdp(timeoutMs = 15000): Promise<boolean> {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const { alive } = await probeCdp(800)
    if (alive) return true
    await delay(500)
  }
  return false
}

interface CdpTarget {
  id: string
  type: string
  url: string
  title?: string
  webSocketDebuggerUrl?: string
}

async function listTargets(): Promise<CdpTarget[]> {
  try {
    const response = await fetch(`${CDP_ENDPOINT}/json/list`)
    if (!response.ok) return []
    return (await response.json()) as CdpTarget[]
  } catch {
    return []
  }
}

function classifyLinkedInUrl(url: string): LoginStatus['state'] {
  if (!/linkedin\.com/i.test(url)) return 'no_tab'
  const path = url.toLowerCase()
  if (LOGIN_PATH_HINTS.some((hint) => path.includes(hint))) return 'logged_out'
  if (LOGGED_IN_PATH_HINTS.some((hint) => path.includes(hint))) return 'logged_in'
  return 'unknown'
}

export async function checkLinkedInLogin(): Promise<LoginStatus> {
  const targets = await listTargets()
  const linkedinTabs = targets.filter(
    (t) => t.type === 'page' && /linkedin\.com/i.test(t.url)
  )
  if (linkedinTabs.length === 0) {
    return { state: 'no_tab', url: null }
  }
  let best: { state: LoginStatus['state']; url: string } | null = null
  for (const tab of linkedinTabs) {
    const state = classifyLinkedInUrl(tab.url)
    if (state === 'logged_in') return { state, url: tab.url }
    if (!best || state === 'logged_out') best = { state, url: tab.url }
  }
  return best ?? { state: 'unknown', url: linkedinTabs[0]?.url ?? null }
}

export async function openUrl(url: string): Promise<{ opened: boolean }> {
  const target = encodeURIComponent(url)
  try {
    const response = await fetch(`${CDP_ENDPOINT}/json/new?${target}`, { method: 'PUT' })
    if (response.ok) return { opened: true }
  } catch {
    // PUT may not be supported on older Chrome — try GET fallback
  }
  try {
    const response = await fetch(`${CDP_ENDPOINT}/json/new?${target}`)
    return { opened: response.ok }
  } catch {
    return { opened: false }
  }
}

export async function listTabs(): Promise<{ url: string; title?: string }[]> {
  const targets = await listTargets()
  return targets
    .filter((t) => t.type === 'page')
    .map((t) => ({ url: t.url, title: t.title }))
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}
