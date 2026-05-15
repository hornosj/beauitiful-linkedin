// Renderer-side ambient type for the API exposed by preload via contextBridge.

export {}

declare global {
  interface ChromeBridge {
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

  interface BeautifulLinkedInBridge {
    getBaseUrl(): Promise<string | null>
    getStatus(): Promise<{
      running: boolean
      baseUrl: string | null
      port: number | null
      error: string | null
    }>
    chrome: ChromeBridge
  }

  interface Window {
    beautifulLinkedIn: BeautifulLinkedInBridge
  }
}
