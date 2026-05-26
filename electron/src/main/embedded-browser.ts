import { BrowserWindow, WebContentsView, session, shell, type WebContents } from 'electron'
import { LINKEDIN_AUTH_COOKIE_NAMES, parseLinkedInCookieInput } from './linkedin-cookies'

const LINKEDIN_SESSION = 'persist:linkedin-scrape'
const LINKEDIN_UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
  '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
const AUTH_WALL_HINTS = ['/login', '/checkpoint', '/uas/login', '/authwall', '/signup']
const LINKEDIN_FEED_URL = 'https://www.linkedin.com/feed/'
const LINKEDIN_LOGIN_URL = 'https://www.linkedin.com/login'
const IGNORED_LOAD_ERROR_CODES = new Set([-3])

const tag = '[EmbeddedBrowser]'

export interface EmbeddedPrepareResult {
  ready: boolean
  url: string | null
  onAuthwall: boolean
  /** True when li_at was rejected and the user must log in via the in-app panel. */
  needsLogin?: boolean
  error: string | null
}

export interface EmbeddedStatus {
  url: string | null
  onAuthwall: boolean
  visible: boolean
}

export interface EmbeddedLoginResult {
  ready: boolean
  url: string | null
  error: string | null
}

export interface EmbeddedBounds {
  x: number
  y: number
  width: number
  height: number
}

export class EmbeddedBrowserManager {
  private view: WebContentsView | null = null
  private window: BrowserWindow | null = null
  private loginWindow: BrowserWindow | null = null
  private _visible = false
  private _lastUrl: string | null = null
  private _onAuthwall = false

  attach(win: BrowserWindow): void {
    console.log(`${tag} attach — criando WebContentsView com session="${LINKEDIN_SESSION}"`)
    this.window = win

    const ses = session.fromPartition(LINKEDIN_SESSION)
    ses.setUserAgent(LINKEDIN_UA)
    ses.setPermissionRequestHandler((_webContents, permission, callback) => {
      if (['hid', 'usb', 'serial', 'bluetooth'].includes(permission)) {
        callback(false)
        return
      }
      callback(true)
    })
    console.log(`${tag} UserAgent definido: ${LINKEDIN_UA.slice(0, 60)}…`)

    this.view = new WebContentsView({
      webPreferences: {
        session: ses,
        nodeIntegration: false,
        contextIsolation: true,
        sandbox: true
      }
    })
    console.log(`${tag} WebContentsView criado. webContents.id=${this.view.webContents.id}`)

    this.view.webContents.on('did-navigate', (_event, url) => {
      const wasAuthwall = this._onAuthwall
      this._lastUrl = url
      this._onAuthwall = AUTH_WALL_HINTS.some((h) => url.includes(h))
      if (this._onAuthwall && !wasAuthwall) {
        console.warn(`${tag} AUTHWALL detectado após navegação → ${url}`)
      } else {
        console.log(`${tag} did-navigate → ${url}`)
      }
    })
    this.view.webContents.on('did-navigate-in-page', (_event, url) => {
      this._lastUrl = url
      this._onAuthwall = AUTH_WALL_HINTS.some((h) => url.includes(h))
      console.log(`${tag} did-navigate-in-page → ${url}`)
    })
    this.view.webContents.on('did-fail-load', (_event, code, desc, failedUrl) => {
      console.error(`${tag} did-fail-load code=${code} desc="${desc}" url=${failedUrl}`)
    })
    this.view.webContents.on('did-finish-load', () => {
      this._focusVisibleView()
    })
    this.view.webContents.on('dom-ready', () => {
      void this._disablePasskeyPrompts()
    })
  }

  async checkSession(): Promise<{ hasLiAt: boolean; hasJsessionid: boolean }> {
    const ses = session.fromPartition(LINKEDIN_SESSION)
    const cookies = await ses.cookies.get({ url: 'https://www.linkedin.com' })
    const hasLiAt = cookies.some((c) => c.name === 'li_at')
    const hasJsessionid = cookies.some((c) => c.name === 'JSESSIONID')
    console.log(
      `${tag} checkSession: li_at=${hasLiAt} JSESSIONID=${hasJsessionid} (${cookies.length} cookie(s) total para linkedin.com)`
    )
    return { hasLiAt, hasJsessionid }
  }

  async awaitLogin(timeoutMs: number = 120_000): Promise<boolean> {
    console.log(`${tag} awaitLogin: aguardando JSESSIONID por até ${timeoutMs}ms`)
    const deadline = Date.now() + timeoutMs
    while (Date.now() < deadline) {
      const { hasJsessionid } = await this.checkSession()
      if (hasJsessionid) {
        console.log(`${tag} awaitLogin: JSESSIONID detectado ✓`)
        return true
      }
      await new Promise<void>((resolve) => setTimeout(resolve, 2000))
    }
    console.warn(`${tag} awaitLogin: timeout sem JSESSIONID`)
    return false
  }

  private async _injectLinkedInCookies(input: string): Promise<void> {
    const ses = session.fromPartition(LINKEDIN_SESSION)
    const parsed = parseLinkedInCookieInput(input)
    if (!parsed.liAt) {
      throw new Error('Cookie li_at não encontrado na entrada configurada.')
    }
    try {
      await ses.cookies.set({
        url: 'https://www.linkedin.com',
        name: 'li_at',
        value: parsed.liAt,
        domain: '.linkedin.com',
        path: '/',
        secure: true,
        httpOnly: true,
        expirationDate: Math.floor(Date.now() / 1000) + 60 * 60 * 24 * 30
      })
      if (parsed.jsessionid) {
        await ses.cookies.set({
          url: 'https://www.linkedin.com',
          name: 'JSESSIONID',
          value: parsed.jsessionid,
          domain: '.linkedin.com',
          path: '/',
          secure: true,
          httpOnly: false,
          expirationDate: Math.floor(Date.now() / 1000) + 60 * 60 * 24
        })
      }
      console.log(
        `${tag} Cookie li_at injetado na session "${LINKEDIN_SESSION}" ✓ ` +
          `(JSESSIONID=${parsed.jsessionid ? 'fornecido' : 'ausente'})`
      )
    } catch (cookieErr) {
      console.error(`${tag} Falha ao injetar cookies LinkedIn:`, cookieErr)
      throw cookieErr
    }
  }

  private async _clearLinkedInAuthCookies(reason: string): Promise<void> {
    const ses = session.fromPartition(LINKEDIN_SESSION)
    await Promise.all(
      LINKEDIN_AUTH_COOKIE_NAMES.map(async (name) => {
        try {
          await ses.cookies.remove('https://www.linkedin.com', name)
        } catch (error) {
          console.warn(`${tag} Falha ao remover cookie ${name}:`, error)
        }
      })
    )
    console.log(`${tag} Cookies de autenticação LinkedIn removidos (${reason})`)
  }

  private _loadPageWithTimeout(url: string, timeoutMs = 30_000): Promise<EmbeddedPrepareResult> {
    return new Promise((resolve) => {
      if (!this.view) {
        resolve({ ready: false, url: null, onAuthwall: false, error: 'View não inicializada.' })
        return
      }
      const wc = this.view.webContents
      const timeout = setTimeout(() => {
        wc.removeListener('did-finish-load', onLoad)
        wc.removeListener('did-fail-load', onFail)
        const currentUrl = wc.getURL() || null
        console.error(
          `${tag} _loadPageWithTimeout TIMEOUT (${timeoutMs}ms). URL atual: ${currentUrl}`
        )
        resolve({
          ready: false,
          url: currentUrl,
          onAuthwall: this._onAuthwall,
          error: 'Timeout ao carregar a página do LinkedIn.'
        })
      }, timeoutMs)

      const onLoad = () => {
        clearTimeout(timeout)
        wc.removeListener('did-fail-load', onFail)
        const finalUrl = wc.getURL()
        const aw = this._onAuthwall
        console.log(`${tag} _loadPageWithTimeout ✓ → "${finalUrl}" | onAuthwall=${aw}`)
        resolve({ ready: true, url: finalUrl, onAuthwall: aw, error: null })
      }

      const onFail = (_e: unknown, code: number, desc: string, failedUrl: string) => {
        if (IGNORED_LOAD_ERROR_CODES.has(code)) {
          console.warn(
            `${tag} _loadPageWithTimeout ignorando load abortado code=${code} "${desc}" url=${failedUrl}`
          )
          return
        }
        clearTimeout(timeout)
        wc.removeListener('did-finish-load', onLoad)
        console.error(
          `${tag} _loadPageWithTimeout FAIL code=${code} "${desc}" url=${failedUrl}`
        )
        resolve({ ready: false, url: failedUrl, onAuthwall: false, error: desc })
      }

      wc.once('did-finish-load', onLoad)
      wc.once('did-fail-load', onFail)
      void wc.loadURL(url)
    })
  }

  private async _disablePasskeyPrompts(): Promise<void> {
    if (!this.view) return
    return this._disablePasskeyPromptsFor(this.view.webContents)
  }

  private async _disablePasskeyPromptsFor(webContents: WebContents): Promise<void> {
    try {
      await webContents.executeJavaScript(
        `(() => {
          try {
            const credentials = navigator.credentials;
            if (!credentials || credentials.__beautifulLinkedInPatched) return;
            const reject = () => Promise.reject(new DOMException('Passkey desabilitada neste browser embutido.', 'NotAllowedError'));
            Object.defineProperty(credentials, 'get', { value: reject, configurable: true });
            Object.defineProperty(credentials, 'create', { value: reject, configurable: true });
            Object.defineProperty(credentials, '__beautifulLinkedInPatched', { value: true, configurable: true });
          } catch (_) {}
        })()`,
        true
      )
    } catch (error) {
      console.debug(`${tag} Falha ao aplicar bloqueio de passkey:`, error)
    }
  }

  async openLogin(): Promise<EmbeddedLoginResult> {
    console.log(`${tag} openLogin() chamado`)
    if (!this.view || !this.window) {
      return { ready: false, url: null, error: 'Browser não inicializado.' }
    }
    const existing = await this.checkSession()
    const targetUrl = existing.hasLiAt && existing.hasJsessionid ? LINKEDIN_FEED_URL : LINKEDIN_LOGIN_URL
    if (!existing.hasLiAt || !existing.hasJsessionid) {
      await this._clearLinkedInAuthCookies('login manual solicitado')
    }
    return this._openLoginWindow(targetUrl)
  }

  async reloadLogin(): Promise<EmbeddedLoginResult> {
    console.log(`${tag} reloadLogin() chamado`)
    if (!this.view || !this.window) {
      return { ready: false, url: null, error: 'Browser não inicializado.' }
    }
    if (this.loginWindow && !this.loginWindow.isDestroyed()) {
      const currentUrl = this.loginWindow.webContents.getURL()
      const targetUrl = isLinkedInUrl(currentUrl) ? currentUrl : LINKEDIN_LOGIN_URL
      this._focusLoginWindow()
      void this.loginWindow.loadURL(targetUrl)
      return { ready: true, url: targetUrl, error: null }
    }
    // Sem janela de login aberta: abrimos uma BrowserWindow dedicada em vez de
    // exibir a WebContentsView. A view embutida perde mouse/teclado em algumas
    // máquinas Windows (tela "congelada"); a janela separada sempre recebe input.
    return this._openLoginWindow(LINKEDIN_LOGIN_URL)
  }

  async prepare(liAt: string, url: string): Promise<EmbeddedPrepareResult> {
    console.log(
      `${tag} prepare() chamado. url="${url}" liAt=${liAt ? `"${liAt.slice(0, 8)}…" (len=${liAt.length})` : 'VAZIO'}`
    )

    if (!this.view || !this.window) {
      console.error(`${tag} prepare() FALHOU — view=${!!this.view} window=${!!this.window}`)
      return { ready: false, url: null, onAuthwall: false, error: 'Browser não inicializado.' }
    }

    // 1. If the session already has both li_at AND JSESSIONID, skip warm-up entirely.
    //    This happens on re-runs after the user has already logged in once.
    const existing = await this.checkSession()
    if (existing.hasLiAt && existing.hasJsessionid) {
      console.log(`${tag} Sessão completa existente (li_at + JSESSIONID) — pulando warm-up → ${url}`)
      this._onAuthwall = false
      this._lastUrl = null
      return this._loadPageWithTimeout(url)
    }

    // 2. Inject li_at into the persistent partition so the warm-up request is authenticated.
    try {
      await this._injectLinkedInCookies(liAt)
    } catch (cookieErr) {
      const message = cookieErr instanceof Error ? cookieErr.message : String(cookieErr)
      return { ready: false, url: null, onAuthwall: false, error: message }
    }
    this._onAuthwall = false
    this._lastUrl = null

    // 3. Warm-up: navigate to /feed/ so LinkedIn's server mints JSESSIONID (required for
    //    Voyager XHR calls that load the People-tab cards). Without JSESSIONID the cards
    //    come back empty even though the page HTML renders correctly.
    console.log(`${tag} Iniciando warm-up → ${LINKEDIN_FEED_URL}`)
    const warmupResult = await this._loadPageWithTimeout(LINKEDIN_FEED_URL, 25_000)
    console.log(
      `${tag} Warm-up concluído: ready=${warmupResult.ready} url="${warmupResult.url}" onAuthwall=${warmupResult.onAuthwall}`
    )

    // 4. Verify JSESSIONID was issued after warm-up.
    const afterWarmup = await this.checkSession()
    console.log(
      `${tag} Após warm-up: li_at=${afterWarmup.hasLiAt} JSESSIONID=${afterWarmup.hasJsessionid} onAuthwall=${warmupResult.onAuthwall}`
    )

    if (!afterWarmup.hasJsessionid || warmupResult.onAuthwall) {
      console.warn(
        `${tag} JSESSIONID ausente após warm-up — li_at inválido ou expirado. ` +
          'Exibindo painel de login in-app.'
      )
      await this._clearLinkedInAuthCookies('li_at rejeitado no warm-up')
      // Open a normal child window for one-time login. A WebContentsView can render
      // LinkedIn but lose pointer/keyboard input on some Windows machines.
      // The modal will poll checkSession() until JSESSIONID appears, then re-call prepare().
      await this._openLoginWindow(LINKEDIN_LOGIN_URL)
      return {
        ready: false,
        url: LINKEDIN_LOGIN_URL,
        onAuthwall: true,
        needsLogin: true,
        error: null
      }
    }

    // 5. Session is authenticated — navigate to the actual target URL.
    console.log(`${tag} JSESSIONID ok — navegando para URL alvo: ${url}`)
    this._onAuthwall = false
    this._lastUrl = null
    return this._loadPageWithTimeout(url)
  }

  show(bounds?: EmbeddedBounds): void {
    if (!this.view || !this.window) {
      console.warn(`${tag} show() ignorado — view ou window não disponível`)
      return
    }
    const b = bounds ?? this._defaultBounds()
    if (!this._visible) {
      this.window.contentView.addChildView(this.view)
      this._visible = true
      console.log(`${tag} Painel EXIBIDO. bounds=${JSON.stringify(b)}`)
    } else {
      this.view.setBounds(b)
      console.log(`${tag} Painel já visível, bounds atualizados: ${JSON.stringify(b)}`)
    }
    this.view.setBounds(b)
    this._focusVisibleView()
  }

  hide(): void {
    this._closeLoginWindow()
    if (!this.view || !this.window || !this._visible) return
    this.window.contentView.removeChildView(this.view)
    this._visible = false
    console.log(`${tag} Painel OCULTADO`)
  }

  getStatus(): EmbeddedStatus {
    const loginVisible = Boolean(this.loginWindow && !this.loginWindow.isDestroyed())
    const url =
      this._lastUrl ?? this.loginWindow?.webContents.getURL() ?? this.view?.webContents.getURL() ?? null
    return { url, onAuthwall: this._onAuthwall, visible: this._visible || loginVisible }
  }

  destroy(): void {
    this._closeLoginWindow()
    this.hide()
    if (this.view) {
      try {
        this.view.webContents.close()
      } catch {
        // best-effort
      }
      this.view = null
    }
    this.window = null
  }

  private _defaultBounds(): EmbeddedBounds {
    if (!this.window) return { x: 0, y: 44, width: 1280, height: 776 }
    const cb = this.window.getContentBounds()
    return { x: 0, y: 44, width: cb.width, height: cb.height - 44 }
  }

  private _focusVisibleView(): void {
    if (!this.view || !this.window || !this._visible) return
    this.window.focus()
    const focus = () => {
      if (!this.view || this.view.webContents.isDestroyed()) return
      this.view.webContents.focus()
    }
    focus()
    setTimeout(focus, 50)
  }

  private _loginResultFromLoad(result: EmbeddedPrepareResult): EmbeddedLoginResult {
    if (result.ready) {
      return { ready: true, url: result.url, error: null }
    }
    if (result.url && isLinkedInUrl(result.url) && result.error?.toLowerCase().includes('timeout')) {
      console.warn(
        `${tag} LinkedIn não finalizou o load, mas a URL já está aberta. Mantendo painel interativo: ${result.url}`
      )
      return { ready: true, url: result.url, error: null }
    }
    return { ready: false, url: result.url, error: result.error }
  }

  private _openLoginWindow(url: string): EmbeddedLoginResult {
    if (!this.window) {
      return { ready: false, url: null, error: 'Janela principal não encontrada.' }
    }
    const existing = this.loginWindow && !this.loginWindow.isDestroyed() ? this.loginWindow : null
    if (existing) {
      this._focusLoginWindow()
      void existing.loadURL(url)
      return { ready: true, url, error: null }
    }

    const ses = session.fromPartition(LINKEDIN_SESSION)
    const win = new BrowserWindow({
      width: 1120,
      height: 760,
      minWidth: 860,
      minHeight: 620,
      title: 'Login LinkedIn - Beautiful LinkedIn',
      parent: this.window,
      modal: false,
      show: true,
      autoHideMenuBar: true,
      backgroundColor: '#ffffff',
      webPreferences: {
        session: ses,
        nodeIntegration: false,
        contextIsolation: true,
        sandbox: true
      }
    })
    this.loginWindow = win
    const wc = win.webContents
    if (typeof wc.setUserAgent === 'function') {
      wc.setUserAgent(LINKEDIN_UA)
    }
    wc.on('did-navigate', (_event, nextUrl) => {
      this._lastUrl = nextUrl
      this._onAuthwall = AUTH_WALL_HINTS.some((h) => nextUrl.includes(h))
      console.log(`${tag} loginWindow did-navigate → ${nextUrl}`)
    })
    wc.on('did-navigate-in-page', (_event, nextUrl) => {
      this._lastUrl = nextUrl
      this._onAuthwall = AUTH_WALL_HINTS.some((h) => nextUrl.includes(h))
      console.log(`${tag} loginWindow did-navigate-in-page → ${nextUrl}`)
    })
    wc.on('dom-ready', () => {
      void this._disablePasskeyPromptsFor(wc)
    })
    // Garante input após cada carga: foca a janela e o webContents. Algumas
    // versões do Windows entregam a janela sem foco de teclado até este passo.
    wc.on('did-finish-load', () => this._focusLoginWindow())
    // Watchdog de travamento: se o processo de renderização morrer ou ficar
    // irresponsivo (ex.: diálogo nativo de WebAuthn preso), recarrega a página
    // de login em vez de deixar o usuário com a tela congelada.
    wc.on('unresponsive', () => {
      console.warn(`${tag} loginWindow IRRESPONSIVO — recarregando página de login`)
      try {
        wc.reloadIgnoringCache()
      } catch (error) {
        console.warn(`${tag} Falha ao recarregar loginWindow irresponsivo:`, error)
      }
    })
    wc.on('render-process-gone', (_event, details) => {
      console.error(`${tag} loginWindow render-process-gone reason=${details.reason}`)
      if (!win.isDestroyed() && details.reason !== 'clean-exit') {
        try {
          wc.reloadIgnoringCache()
        } catch (error) {
          console.warn(`${tag} Falha ao recarregar após crash:`, error)
        }
      }
    })
    win.on('closed', () => {
      if (this.loginWindow === win) this.loginWindow = null
    })
    // Popups (ex.: "Continue with Google", desafios): abrir na MESMA sessão e UA
    // para não nascerem sem cookies/UA. Domínios não-login vão para o navegador
    // externo. Nunca usamos webPreferences padrão, que quebram a sessão.
    wc.setWindowOpenHandler(({ url: popupUrl }) => {
      if (isLoginPopupUrl(popupUrl)) {
        return {
          action: 'allow',
          overrideBrowserWindowOptions: {
            parent: this.window ?? undefined,
            autoHideMenuBar: true,
            backgroundColor: '#ffffff',
            webPreferences: {
              session: ses,
              nodeIntegration: false,
              contextIsolation: true,
              sandbox: true
            }
          }
        }
      }
      void shell.openExternal(popupUrl)
      return { action: 'deny' }
    })
    wc.on('did-create-window', (childWindow) => {
      const childWc = childWindow.webContents
      if (typeof childWc.setUserAgent === 'function') {
        childWc.setUserAgent(LINKEDIN_UA)
      }
      childWc.on('dom-ready', () => {
        void this._disablePasskeyPromptsFor(childWc)
      })
      childWc.setWindowOpenHandler(({ url: nestedUrl }) => {
        void shell.openExternal(nestedUrl)
        return { action: 'deny' }
      })
    })
    this._focusLoginWindow()
    void win.loadURL(url)
    return { ready: true, url, error: null }
  }

  private _focusLoginWindow(): void {
    const win = this.loginWindow
    if (!win || win.isDestroyed()) return
    if (win.isMinimized()) win.restore()
    win.show()
    win.moveTop()
    win.focus()
    const focusContents = () => {
      if (!win.isDestroyed() && !win.webContents.isDestroyed()) {
        win.webContents.focus()
      }
    }
    focusContents()
    setTimeout(focusContents, 50)
  }

  private _closeLoginWindow(): void {
    if (!this.loginWindow || this.loginWindow.isDestroyed()) {
      this.loginWindow = null
      return
    }
    const win = this.loginWindow
    this.loginWindow = null
    win.close()
  }
}

export const embeddedManager = new EmbeddedBrowserManager()

function isLinkedInUrl(url: string | null | undefined): boolean {
  return Boolean(url && /(^https?:\/\/)?([^/]+\.)?linkedin\.com(\/|$)/i.test(url))
}

/**
 * Popups que devem abrir DENTRO do app (mesma sessão/UA) durante o login:
 * o próprio LinkedIn e o fluxo OAuth do Google ("Continue with Google").
 * Qualquer outro destino vai para o navegador externo.
 */
function isLoginPopupUrl(url: string | null | undefined): boolean {
  if (!url) return false
  if (isLinkedInUrl(url)) return true
  return /(^https?:\/\/)?([^/]+\.)?(google\.com|accounts\.google\.com|gstatic\.com)(\/|$)/i.test(url)
}
