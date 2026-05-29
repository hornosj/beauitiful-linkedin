import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from '../src/renderer/src/App'

const searchResponse = {
  leads: [],
  summary: {
    total_companies_processed: 1,
    total_raw_leads: 0,
    total_deduplicated_leads: 0,
    total_previously_consulted_leads: 0,
    total_maybe_incorrect_leads: 0,
    output_file: 'output/leads.csv',
    top_sources: { linkedin_people_search: 0 }
  },
  provider_diagnostics: []
}

describe('App LinkedIn embedded session flow', () => {
  let originalFetch: typeof fetch
  let originalBridge: typeof window.beautifulLinkedIn
  const openLogin = vi.fn()
  const reloadLogin = vi.fn()
  const hide = vi.fn()
  const prepare = vi.fn()
  const checkSession = vi.fn()

  beforeEach(() => {
    originalFetch = globalThis.fetch
    originalBridge = window.beautifulLinkedIn
    openLogin.mockResolvedValue({ ready: true, url: 'https://www.linkedin.com/login', error: null })
    reloadLogin.mockResolvedValue({ ready: true, url: 'https://www.linkedin.com/login', error: null })
    hide.mockResolvedValue(undefined)
    prepare.mockResolvedValue({ ready: true, url: null, onAuthwall: false, error: null })
    checkSession.mockResolvedValue({ hasLiAt: true, hasJsessionid: true })
    window.beautifulLinkedIn = {
      getBaseUrl: () => Promise.resolve('http://127.0.0.1:39712'),
      getStatus: () =>
        Promise.resolve({
          running: true,
          baseUrl: 'http://127.0.0.1:39712',
          port: 39712,
          error: null
        }),
      cleanRun: () => Promise.resolve({ killedPids: [], ports: [], errors: [] }),
      chrome: {
        probe: () => Promise.resolve({ alive: false, endpoint: 'http://127.0.0.1:9222' }),
        isRunning: () => Promise.resolve(false),
        kill: () => Promise.resolve({ killed: true }),
        launch: () => Promise.resolve({ launched: false, executable: null, error: null }),
        waitForCdp: () => Promise.resolve(false),
        checkLinkedIn: () => Promise.resolve({ state: 'unknown', url: null }),
        openUrl: () => Promise.resolve({ opened: false }),
        listTabs: () => Promise.resolve([])
      },
      embeddedBrowser: {
        prepare,
        openLogin,
        reloadLogin,
        show: () => Promise.resolve(),
        hide,
        status: () => Promise.resolve({ url: null, onAuthwall: false, visible: false }),
        getCdpEndpoint: () => Promise.resolve({ endpoint: 'http://127.0.0.1:9223', port: 9223 }),
        checkSession,
        awaitLogin: () => Promise.resolve(true)
      }
    }
  })

  afterEach(() => {
    vi.useRealTimers()
    globalThis.fetch = originalFetch
    window.beautifulLinkedIn = originalBridge
    vi.restoreAllMocks()
  })

  it('opens the manual LinkedIn login panel from the title bar', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(jsonResponse({ seniority: [], functions: [], role_presets: [], scrape_modes: [] })) as unknown as typeof fetch

    render(<App />)

    fireEvent.click(await screen.findByRole('button', { name: /logar linkedin/i }))

    await waitFor(() => expect(openLogin).toHaveBeenCalledOnce())
  })

  it('shows the app logo lockup and footer author credits', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(jsonResponse({ seniority: [], functions: [], role_presets: [], scrape_modes: [] })) as unknown as typeof fetch

    render(<App />)

    expect(await screen.findByAltText('Beautiful LinkedIn logo')).toBeTruthy()
    expect(screen.getByText('Beautiful')).toBeTruthy()
    expect(screen.getByText('Linked')).toBeTruthy()
    expect(screen.getByText('in')).toBeTruthy()
    expect(screen.getByText(/made by jpAoH/i)).toBeTruthy()
    expect(screen.getByText('jpAoH Software Solutions © 2026')).toBeTruthy()
  })

  it('shows a close action after the embedded LinkedIn session is logged in', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(jsonResponse({ seniority: [], functions: [], role_presets: [], scrape_modes: [] })) as unknown as typeof fetch

    render(<App />)

    fireEvent.click(await screen.findByRole('button', { name: /logar linkedin/i }))
    await waitFor(() => expect(openLogin).toHaveBeenCalledOnce())

    await act(async () => {
      await new Promise((resolve) => window.setTimeout(resolve, 1700))
    })
    const closeButton = await screen.findByRole('button', {
      name: /linkedin logado.*pode fechar essa aba/i
    })
    fireEvent.click(closeButton)

    await waitFor(() => expect(hide).toHaveBeenCalledOnce())
  })

  it('offers refresh and close controls when the embedded LinkedIn login is open', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(jsonResponse({ seniority: [], functions: [], role_presets: [], scrape_modes: [] })) as unknown as typeof fetch

    render(<App />)

    fireEvent.click(await screen.findByRole('button', { name: /logar linkedin/i }))
    await waitFor(() => expect(openLogin).toHaveBeenCalledOnce())

    const refreshButton = await screen.findByRole('button', { name: /recarregar linkedin/i })
    fireEvent.click(refreshButton)
    await waitFor(() => expect(reloadLogin).toHaveBeenCalledOnce())

    fireEvent.click(screen.getByRole('button', { name: /fechar janela linkedin/i }))
    await waitFor(() => expect(hide).toHaveBeenCalledOnce())
  })

  it('uses the existing embedded LinkedIn session when running people search', async () => {
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      const path = String(url)
      if (path.endsWith('/taxonomies')) {
        return Promise.resolve(jsonResponse({ seniority: [], functions: [], role_presets: [], scrape_modes: [] }))
      }
      // Async run flow: start returns a run id, polling returns the result.
      if (path.endsWith('/search/start')) {
        return Promise.resolve(jsonResponse({ run_id: 'run-1', status: 'pending' }))
      }
      if (path.includes('/runs/')) {
        return Promise.resolve(
          jsonResponse({
            run_id: 'run-1',
            status: 'completed',
            result: searchResponse,
            found_leads: [],
            found_count: 0
          })
        )
      }
      return Promise.resolve(jsonResponse({ status: 'ok' }))
    })
    globalThis.fetch = fetchMock as unknown as typeof fetch

    render(<App />)

    fireEvent.change(await screen.findByLabelText(/empresa/i), {
      target: { value: 'Fullstack Labs' }
    })
    fireEvent.click(screen.getByRole('button', { name: /buscar leads/i }))

    await waitFor(() => {
      const searchCall = fetchMock.mock.calls.find(([url]) => String(url).endsWith('/search/start'))
      expect(searchCall).toBeTruthy()
      expect(JSON.parse(searchCall![1]?.body as string)).toMatchObject({
        company_name: 'Fullstack Labs',
        scrape_mode: 'people_search',
        cdp_endpoint: 'http://127.0.0.1:9223'
      })
    })
    expect(prepare).not.toHaveBeenCalled()
    expect(hide).toHaveBeenCalled()
  })

  it('authenticates the Telethon Telegram session from the title bar', async () => {
    const fetchMock = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      const path = String(url)
      if (path.endsWith('/taxonomies')) {
        return Promise.resolve(jsonResponse({ seniority: [], functions: [], role_presets: [], scrape_modes: [] }))
      }
      if (path.endsWith('/telegram/telethon/auth/status')) {
        return Promise.resolve(
          jsonResponse({
            authorized: false,
            configured: true,
            session_name: 'telegram_phone_lookup'
          })
        )
      }
      if (path.endsWith('/telegram/telethon/auth/send-code')) {
        expect(JSON.parse(String(init?.body))).toMatchObject({ phone: '+5511999999999' })
        return Promise.resolve(
          jsonResponse({
            phone_code_hash: 'hash-123',
            next_type: null,
            timeout: null
          })
        )
      }
      if (path.endsWith('/telegram/telethon/auth/sign-in')) {
        expect(JSON.parse(String(init?.body))).toMatchObject({
          phone: '+5511999999999',
          phone_code_hash: 'hash-123',
          code: '12345'
        })
        return Promise.resolve(
          jsonResponse({
            authorized: true,
            requires_password: false,
            user_id: 42,
            username: 'operator',
            first_name: 'Operator'
          })
        )
      }
      return Promise.resolve(jsonResponse({ status: 'ok' }))
    })
    globalThis.fetch = fetchMock as unknown as typeof fetch

    render(<App />)

    fireEvent.click(await screen.findByRole('button', { name: /telegram não logado/i }))
    fireEvent.change(await screen.findByLabelText('Telefone'), {
      target: { value: '+5511999999999' }
    })
    fireEvent.click(screen.getByRole('button', { name: /enviar código/i }))

    fireEvent.change(await screen.findByLabelText('Código'), {
      target: { value: '12345' }
    })
    fireEvent.click(screen.getByRole('button', { name: /confirmar código/i }))

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /telegram logado/i })).toHaveClass('ready')
    })
  })
})

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' }
  })
}
