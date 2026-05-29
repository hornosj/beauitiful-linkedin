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

  interface EmbeddedBrowserBridge {
    prepare(liAt: string, url: string): Promise<{
      ready: boolean
      url: string | null
      onAuthwall: boolean
      needsLogin?: boolean
      error: string | null
    }>
    openLogin(): Promise<{ ready: boolean; url: string | null; error: string | null }>
    reloadLogin(): Promise<{ ready: boolean; url: string | null; error: string | null }>
    show(bounds?: { x: number; y: number; width: number; height: number }): Promise<void>
    hide(): Promise<void>
    status(): Promise<{ url: string | null; onAuthwall: boolean; visible: boolean }>
    getCdpEndpoint(): Promise<{ endpoint: string; port: number }>
    checkSession(): Promise<{ hasLiAt: boolean; hasJsessionid: boolean }>
    awaitLogin(timeoutMs?: number): Promise<boolean>
  }

  interface CleanRunReport {
    killedPids: number[]
    ports: { port: number; pid: number | null; killed: boolean }[]
    errors: string[]
  }

  interface BeautifulLinkedInBridge {
    getBaseUrl(): Promise<string | null>
    getStatus(): Promise<{
      running: boolean
      baseUrl: string | null
      port: number | null
      error: string | null
    }>
    cleanRun(): Promise<CleanRunReport>
    chrome: ChromeBridge
    embeddedBrowser?: EmbeddedBrowserBridge
  }

  interface Window {
    beautifulLinkedIn: BeautifulLinkedInBridge
  }
}
