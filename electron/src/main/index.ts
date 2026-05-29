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
import { cleanCompetingProcesses } from './process-cleanup'

// Expose Electron's Chromium as a CDP target on a dedicated port (9223).
// This is loopback-only and lets the Python sidecar connect to the in-app
// WebContentsView without spawning a separate Chrome window.
app.commandLine.appendSwitch('remote-debugging-port', '9223')
app.commandLine.appendSwitch('remote-debugging-address', '127.0.0.1')

// O scrape do People roda com o WebContentsView do LinkedIn ESCONDIDO
// (removeChildView), enquanto o sidecar Python conecta via CDP nessa porta.
// Sem estas flags, o Chromium congela o renderer oculto/occluído: ele para de
// responder a comandos CDP (Page.enable, Runtime.enable...). O Playwright faz
// connect_over_cdp anexando a TODOS os targets de página e fica travado para
// sempre nesse renderer congelado — era a causa do "timeout após 120s" na
// busca de leads. Mantemos os renderers de fundo ativos para o CDP funcionar.
app.commandLine.appendSwitch('disable-renderer-backgrounding')
app.commandLine.appendSwitch('disable-backgrounding-occluded-windows')
app.commandLine.appendSwitch('disable-background-timer-throttling')

// LinkedIn aciona passkey/Windows Hello na tela de login. No Windows, o diálogo
// nativo de WebAuthn pode nunca receber foco e travar a página indefinidamente
// (a Promise de navigator.credentials nunca resolve). Desabilitamos a integração
// com a API nativa do Windows e o autofill condicional para que o LinkedIn caia
// sempre no fluxo de senha, sem disparar o modal nativo que congela a janela.
app.commandLine.appendSwitch(
  'disable-features',
  'WebAuthenticationUseNativeWinApi,WebAuthenticationConditionalUI'
)

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
  ipcMain.handle('embedded:reload-login', () => {
    console.log('[IPC embedded:reload-login]')
    return embeddedManager.reloadLogin()
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

// Garante uma única instância. Sem isso, abrir o app duas vezes (comum quando o
// usuário acha que "não abriu" e clica de novo) faz instâncias concorrerem pelo
// mesmo userData/GPUCache (erro de cache → tela não abre) e pela porta de debug
// fixa 9223. A segunda instância apenas foca a janela existente.
const gotSingleInstanceLock = app.requestSingleInstanceLock()
if (!gotSingleInstanceLock) {
  app.quit()
} else {
  app.on('second-instance', () => {
    if (mainWindow && !mainWindow.isDestroyed()) {
      if (mainWindow.isMinimized()) mainWindow.restore()
      mainWindow.show()
      mainWindow.focus()
    }
  })

  app.whenReady().then(() => {
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

    ipcMain.handle('system:clean-run', async () => {
      console.log('[IPC system:clean-run] iniciando limpeza de processos concorrentes')
      const report = await cleanCompetingProcesses(process.pid, sidecar?.process.pid ?? null)
      console.log('[IPC system:clean-run] relatório=', JSON.stringify(report))

      // Encerra nossos próprios recursos antes de reiniciar para que a nova
      // instância consiga fazer bind da porta 9223 e do cache de userData.
      try {
        embeddedManager.destroy()
      } catch (error) {
        console.warn('[clean-run] falha ao destruir browser embutido:', error)
      }
      try {
        await sidecar?.shutdown()
      } catch (error) {
        console.warn('[clean-run] falha ao encerrar sidecar:', error)
      }
      sidecar = null

      app.relaunch()
      // Pequeno atraso para o IPC responder ao renderer antes de sairmos.
      setTimeout(() => app.exit(0), 400)
      return report
    })

    registerEmbeddedBrowserHandlers()

    // Abre a janela IMEDIATAMENTE e inicia o sidecar em paralelo. Antes, a janela
    // só era criada após `await bootSidecar()` (timeout de 20s); um cold start
    // lento do exe (varredura de antivírus/SmartScreen) fazia o app parecer que
    // "não abria". O renderer faz polling de sidecar:status até o backend subir.
    createWindow()
    embeddedManager.attach(mainWindow!)

    void bootSidecar().catch((error) => {
      sidecarError = error instanceof Error ? error.message : String(error)
      console.error('Falha ao iniciar o sidecar Python:', error)
    })

    app.on('activate', () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow()
    })
  })
}

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
