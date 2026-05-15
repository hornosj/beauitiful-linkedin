import { contextBridge, ipcRenderer } from 'electron'

export interface BeautifulLinkedInBridge {
  getBaseUrl(): Promise<string | null>
  getStatus(): Promise<{
    running: boolean
    baseUrl: string | null
    port: number | null
    error: string | null
  }>
  chrome: ChromeBridge
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

const bridge: BeautifulLinkedInBridge = {
  getBaseUrl: () => ipcRenderer.invoke('sidecar:get-base-url'),
  getStatus: () => ipcRenderer.invoke('sidecar:status'),
  chrome
}

contextBridge.exposeInMainWorld('beautifulLinkedIn', bridge)

declare global {
  interface Window {
    beautifulLinkedIn: BeautifulLinkedInBridge
  }
}
