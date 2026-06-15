import { contextBridge, ipcRenderer } from 'electron'

export interface EmbeddedPrepareResult {
  ready: boolean
  url: string | null
  onAuthwall: boolean
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

export interface EmbeddedBrowserBridge {
  prepare(liAt: string, url: string): Promise<EmbeddedPrepareResult>
  openLogin(): Promise<EmbeddedLoginResult>
  reloadLogin(): Promise<EmbeddedLoginResult>
  show(bounds?: EmbeddedBounds): Promise<void>
  hide(): Promise<void>
  status(): Promise<EmbeddedStatus>
  getCdpEndpoint(): Promise<{ endpoint: string; port: number }>
  checkSession(): Promise<{ hasLiAt: boolean; hasJsessionid: boolean }>
  getLiAt(): Promise<string | null>
  awaitLogin(timeoutMs?: number): Promise<boolean>
}

export interface CleanRunReport {
  killedPids: number[]
  ports: { port: number; pid: number | null; killed: boolean }[]
  errors: string[]
}

export interface BeautifulLinkedInBridge {
  getBaseUrl(): Promise<string | null>
  getStatus(): Promise<{
    running: boolean
    baseUrl: string | null
    port: number | null
    error: string | null
  }>
  /**
   * Encerra processos travados do app (Chromium embutido, Chrome de scraping e
   * sidecars órfãos) que disputam as portas de debug CDP e reinicia o app para
   * uma execução limpa. O app reinicia logo após resolver.
   */
  cleanRun(): Promise<CleanRunReport>
  /**
   * Abre o diálogo nativo "Salvar como" para o export de CSV e devolve o
   * caminho absoluto escolhido pelo usuário (ou ``canceled``).
   */
  saveCsvDialog(defaultName?: string): Promise<{ canceled: boolean; filePath: string | null }>
  chrome: ChromeBridge
  embeddedBrowser?: EmbeddedBrowserBridge
}

export interface ChromeBridge {
  probe(): Promise<{ alive: boolean; endpoint: string }>
  isRunning(): Promise<boolean>
  kill(): Promise<{ killed: boolean }>
  launch(initialUrl?: string): Promise<{
    launched: boolean
    executable: string | null
    error: string | null
  }>
  waitForCdp(timeoutMs?: number): Promise<boolean>
  checkLinkedIn(): Promise<{
    state: 'logged_in' | 'logged_out' | 'no_tab' | 'unknown'
    url: string | null
  }>
  openUrl(url: string): Promise<{ opened: boolean }>
  listTabs(): Promise<{ url: string; title?: string }[]>
}

const chrome: ChromeBridge = {
  probe: () => ipcRenderer.invoke('chrome:probe'),
  isRunning: () => ipcRenderer.invoke('chrome:is-running'),
  kill: () => ipcRenderer.invoke('chrome:kill'),
  launch: (initialUrl) => ipcRenderer.invoke('chrome:launch', initialUrl),
  waitForCdp: (timeoutMs) => ipcRenderer.invoke('chrome:wait-cdp', timeoutMs),
  checkLinkedIn: () => ipcRenderer.invoke('chrome:check-linkedin'),
  openUrl: (url) => ipcRenderer.invoke('chrome:open-url', url),
  listTabs: () => ipcRenderer.invoke('chrome:list-tabs')
}

const embeddedBrowser: EmbeddedBrowserBridge = {
  prepare: (liAt, url) => ipcRenderer.invoke('embedded:prepare', liAt, url),
  openLogin: () => ipcRenderer.invoke('embedded:open-login'),
  reloadLogin: () => ipcRenderer.invoke('embedded:reload-login'),
  show: (bounds) => ipcRenderer.invoke('embedded:show', bounds),
  hide: () => ipcRenderer.invoke('embedded:hide'),
  status: () => ipcRenderer.invoke('embedded:status'),
  getCdpEndpoint: () => ipcRenderer.invoke('embedded:cdp-endpoint'),
  checkSession: () => ipcRenderer.invoke('embedded:check-session'),
  getLiAt: () => ipcRenderer.invoke('embedded:get-li-at'),
  awaitLogin: (timeoutMs) => ipcRenderer.invoke('embedded:await-login', timeoutMs)
}

const bridge: BeautifulLinkedInBridge = {
  getBaseUrl: () => ipcRenderer.invoke('sidecar:get-base-url'),
  getStatus: () => ipcRenderer.invoke('sidecar:status'),
  cleanRun: () => ipcRenderer.invoke('system:clean-run'),
  saveCsvDialog: (defaultName) => ipcRenderer.invoke('dialog:save-csv', defaultName),
  chrome,
  embeddedBrowser
}

contextBridge.exposeInMainWorld('beautifulLinkedIn', bridge)

declare global {
  interface Window {
    beautifulLinkedIn: BeautifulLinkedInBridge
  }
}
