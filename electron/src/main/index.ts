import { app, BrowserWindow, ipcMain, shell } from 'electron'
import { fileURLToPath } from 'node:url'
import { dirname, join, resolve } from 'node:path'
import { startSidecar, type SidecarHandle } from './sidecar'
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

const __dirname = dirname(fileURLToPath(import.meta.url))
const isDev = !!process.env.ELECTRON_RENDERER_URL

let sidecar: SidecarHandle | null = null
let mainWindow: BrowserWindow | null = null
let sidecarError: string | null = null

async function bootSidecar(): Promise<void> {
  const projectRoot = resolve(__dirname, '..', '..', '..')
  sidecarError = null
  sidecar = await startSidecar({
    cwd: projectRoot,
    onLog: (line) => console.log(line)
  })
}

function createWindow(): void {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 980,
    minHeight: 640,
    show: false,
    backgroundColor: '#10131e',
    autoHideMenuBar: true,
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

  createWindow()

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
  })
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit()
})

app.on('before-quit', async () => {
  if (sidecar) {
    await sidecar.shutdown()
    sidecar = null
  }
})
