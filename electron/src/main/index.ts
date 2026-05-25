import { app, BrowserWindow, ipcMain, shell } from 'electron'
import { fileURLToPath } from 'node:url'
import { dirname, join, resolve } from 'node:path'
import {
  resolvePackagedSidecarExecutable,
  startSidecar,
  type SidecarHandle
} from './sidecar'
import {
  probeCdp,
  isChromeRunning,
  killChrome,
  launchChromeWithCdp,
  waitForCdp,
  checkLinkedInLogin,
  openUrl,
  listTabs
} from './chrome'
import { embeddedManager, type EmbeddedBounds } from './embedded-browser'

// Expose Electron's Chromium as a CDP target on a dedicated port (9223).
// This is loopback-only and lets the Python sidecar connect to the in-app
// WebContentsView without spawning a separate Chrome window.
app.commandLine.appendSwitch('remote-debugging-port', '9223')
app.commandLine.appendSwitch('remote-debugging-address', '127.0.0.1')

const __dirname = dirname(fileURLToPath(import.meta.url))
const isDev = !!process.env.ELECTRON_RENDERER_URL

let sidecar: SidecarHandle | null = null
let mainWindow: BrowserWindow | null = null
let sidecarError: string | null = null

function packagedSidecarExecutable(): string | undefined {
  return resolvePackagedSidecarExecutable(
    app.isPackaged,
    process.resourcesPath,
    process.platform
  )
}

async function bootSidecar(): Promise<void> {
  const projectRoot = resolve(__dirname, '..', '..', '..')
  const isPackaged = app.isPackaged
  sidecarError = null
  sidecar = await startSidecar({
    cwd: isPackaged ? app.getPath('userData') : projectRoot,
    projectRoot: isPackaged ? undefined : projectRoot,
    sidecarExecutablePath: packagedSidecarExecutable(),
    onLog: (line) => console.log(line)
  })
}

function registerEmbeddedBrowserHandlers(): void {
  ipcMain.handle('embedded:prepare', async (_event, liAt: string, url: string) => {
    console.log('[IPC embedded:prepare] url=', url, 'liAt length=', liAt?.length ?? 0)
    if (!mainWindow) {
      console.error('[IPC embedded:prepare] mainWindow é null')
      return { ready: false, url: null, onAuthwall: false, error: 'Janela não encontrada.' }
    }
    const result = await embeddedManager.prepare(liAt, url)
    console.log('[IPC embedded:prepare] result=', JSON.stringify(result))
    return result
  })
  ipcMain.handle('embedded:show', (_event, bounds?: EmbeddedBounds) => {
    console.log('[IPC embedded:show] bounds=', bounds ?? 'default')
    embeddedManager.show(bounds)
  })
  ipcMain.handle('embedded:open-login', () => {
    console.log('[IPC embedded:open-login]')
    return embeddedManager.openLogin()
  })
  ipcMain.handle('embedded:hide', () => {
    console.log('[IPC embedded:hide]')
    embeddedManager.hide()
  })
  ipcMain.handle('embedded:status', () => {
    const s = embeddedManager.getStatus()
    console.log('[IPC embedded:status]', JSON.stringify(s))
    return s
  })
  ipcMain.handle('embedded:cdp-endpoint', () => {
    console.log('[IPC embedded:cdp-endpoint] → http://127.0.0.1:9223')
    return { endpoint: 'http://127.0.0.1:9223', port: 9223 }
  })
  ipcMain.handle('embedded:check-session', () => {
    console.log('[IPC embedded:check-session]')
    return embeddedManager.checkSession()
  })
  ipcMain.handle('embedded:await-login', (_event, timeoutMs?: number) => {
    const ms = typeof timeoutMs === 'number' ? timeoutMs : 120_000
    console.log(`[IPC embedded:await-login] timeoutMs=${ms}`)
    return embeddedManager.awaitLogin(ms)
  })
}

function createWindow(): void {
  const devIcon = isDev ? join(__dirname, '../../resources/icon.png') : undefined

  mainWindow = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 980,
    minHeight: 640,
    show: false,
    backgroundColor: '#10131e',
    autoHideMenuBar: true,
    icon: devIcon,
    webPreferences: {
      preload: join(__dirname, '../preload/index.js'),
      sandbox: true,
      contextIsolation: true,
      nodeIntegration: false
    }
  })

  mainWindow.on('ready-to-show', () => mainWindow?.show())

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url)
    return { action: 'deny' }
  })

  if (isDev) {
    void mainWindow.loadURL(process.env.ELECTRON_RENDERER_URL!)
  } else {
    void mainWindow.loadFile(join(__dirname, '../renderer/index.html'))
  }
}

app.whenReady().then(async () => {
  try {
    await bootSidecar()
  } catch (error) {
    sidecarError = error instanceof Error ? error.message : String(error)
    console.error('Falha ao iniciar o sidecar Python:', error)
  }

  ipcMain.handle('sidecar:get-base-url', () => sidecar?.baseUrl ?? null)
  ipcMain.handle('sidecar:status', () => ({
    running: sidecar !== null,
    baseUrl: sidecar?.baseUrl ?? null,
    port: sidecar?.port ?? null,
    error: sidecarError
  }))

  ipcMain.handle('chrome:probe', () => probeCdp())
  ipcMain.handle('chrome:is-running', () => isChromeRunning())
  ipcMain.handle('chrome:kill', () => killChrome())
  ipcMain.handle('chrome:launch', (_event, initialUrl?: string) =>
    launchChromeWithCdp(typeof initialUrl === 'string' ? initialUrl : undefined)
  )
  ipcMain.handle('chrome:wait-cdp', (_event, timeoutMs?: number) =>
    waitForCdp(typeof timeoutMs === 'number' ? timeoutMs : 15000)
  )
  ipcMain.handle('chrome:check-linkedin', () => checkLinkedInLogin())
  ipcMain.handle('chrome:open-url', (_event, url: string) => openUrl(url))
  ipcMain.handle('chrome:list-tabs', () => listTabs())

  registerEmbeddedBrowserHandlers()
  createWindow()
  embeddedManager.attach(mainWindow!)

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
  })
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit()
})

app.on('before-quit', async () => {
  embeddedManager.destroy()
  if (sidecar) {
    await sidecar.shutdown()
    sidecar = null
  }
})
