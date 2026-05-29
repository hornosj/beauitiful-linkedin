import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from '../src/renderer/src/App'

const searchResponse = {
  leads: [
    {
      company_name: 'Nubank',
      person_name: 'Ana Silva',
      title: 'Head of Marketing',
      linkedin_url: 'https://www.linkedin.com/in/ana-silva/',
      source_url: 'https://www.linkedin.com/in/ana-silva/',
      source_type: 'linkedin_people_search',
      snippet: '',
      matched_title: 'marketing',
      confidence_score: 88
    }
  ],
  summary: {
    total_companies_processed: 1,
    total_raw_leads: 1,
    total_deduplicated_leads: 1,
    total_previously_consulted_leads: 0,
    total_maybe_incorrect_leads: 0,
    output_file: 'output/leads.csv',
    top_sources: { linkedin_people_search: 1 }
  },
  provider_diagnostics: []
}

const savedTableResponse = {
  table: {
    id: 'saved-1',
    name: 'Nubank Marketing',
    created_at: '2026-05-14T00:00:00+00:00',
    updated_at: '2026-05-14T00:00:00+00:00',
    source_type: 'search',
    keywords: ['marketing'],
    search_queries: [],
    search_request: { company_name: 'Nubank' },
    enrichment_status: 'not_enriched',
    lead_count: 1
  },
  leads: searchResponse.leads
}

describe('App save current search flow', () => {
  let originalFetch: typeof fetch
  let originalBridge: unknown

  beforeEach(() => {
    originalFetch = globalThis.fetch
    originalBridge = window.beautifulLinkedIn
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
        probe: () => Promise.resolve({ alive: true, endpoint: 'http://127.0.0.1:9222' }),
        isRunning: () => Promise.resolve(true),
        kill: () => Promise.resolve({ killed: true }),
        launch: () => Promise.resolve({ launched: true, executable: null, error: null }),
        waitForCdp: () => Promise.resolve(true),
        checkLinkedIn: () => Promise.resolve({ state: 'unknown', url: null }),
        openUrl: () => Promise.resolve({ opened: true }),
        listTabs: () => Promise.resolve([])
      }
    }
  })

  afterEach(() => {
    globalThis.fetch = originalFetch
    window.beautifulLinkedIn = originalBridge as typeof window.beautifulLinkedIn
    vi.restoreAllMocks()
  })

  it('saves the latest search from the results panel with an in-app name field', async () => {
    const fetchMock = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      const path = String(url)
      if (path.endsWith('/taxonomies')) {
        return Promise.resolve(jsonResponse({ seniority: [], functions: [], role_presets: [], scrape_modes: [] }))
      }
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
      if (path.endsWith('/lead-tables')) {
        return Promise.resolve(jsonResponse(savedTableResponse, 201))
      }
      return Promise.resolve(jsonResponse({ status: 'ok', version: '0.1.3' }))
    })
    globalThis.fetch = fetchMock as unknown as typeof fetch

    render(<App />)

    fireEvent.change(await screen.findByLabelText(/empresa/i), {
      target: { value: 'Nubank' }
    })
    fireEvent.click(screen.getByRole('button', { name: /buscar leads/i }))
    await screen.findByText(/Ana Silva/i)

    fireEvent.click(screen.getByRole('button', { name: /^salvar busca atual$/i }))
    fireEvent.change(screen.getByLabelText(/nome da tabela salva/i), {
      target: { value: 'Nubank Marketing' }
    })
    fireEvent.click(screen.getByRole('button', { name: /^salvar$/i }))

    await waitFor(() => {
      const saveCall = fetchMock.mock.calls.find(([url]) => String(url).endsWith('/lead-tables'))
      expect(saveCall).toBeTruthy()
      expect(JSON.parse(saveCall![1]?.body as string)).toMatchObject({
        name: 'Nubank Marketing',
        leads: [{ person_name: 'Ana Silva' }],
        keywords: expect.arrayContaining(['marketing'])
      })
    })
  })
})

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' }
  })
}
