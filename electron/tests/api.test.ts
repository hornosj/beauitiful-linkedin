import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiClient, ApiError } from '../src/shared/api'
import type { SearchRequest } from '../src/shared/types'

const baseRequest: SearchRequest = {
  company_name: 'Nubank',
  titles: ['marketing'],
  max_results: 25,
  scrape_mode: 'serp',
  output_path: 'output/leads.csv'
}

describe('ApiClient', () => {
  let originalFetch: typeof fetch

  beforeEach(() => {
    originalFetch = globalThis.fetch
  })

  afterEach(() => {
    globalThis.fetch = originalFetch
    vi.restoreAllMocks()
  })

  it('builds requests against the configured base URL', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ status: 'ok', version: '0.1.0' }), {
        status: 200,
        headers: { 'content-type': 'application/json' }
      })
    )
    globalThis.fetch = fetchMock as unknown as typeof fetch
    const client = new ApiClient('http://127.0.0.1:39712')

    const result = await client.health()

    expect(result.status).toBe('ok')
    expect(fetchMock).toHaveBeenCalledOnce()
    const [url, init] = fetchMock.mock.calls[0]
    expect(String(url)).toBe('http://127.0.0.1:39712/health')
    expect(init).toMatchObject({ method: 'GET' })
  })

  it('serializes a search request as JSON with content-type header', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ leads: [], summary: {} }), {
        status: 200,
        headers: { 'content-type': 'application/json' }
      })
    )
    globalThis.fetch = fetchMock as unknown as typeof fetch
    const client = new ApiClient('http://127.0.0.1:39712')

    await client.search(baseRequest)

    const [, init] = fetchMock.mock.calls[0]
    expect(init.method).toBe('POST')
    expect(init.headers).toMatchObject({ 'content-type': 'application/json' })
    expect(JSON.parse(init.body as string)).toMatchObject({
      company_name: 'Nubank',
      titles: ['marketing'],
      scrape_mode: 'serp'
    })
  })

  it('throws ApiError with detail on 4xx responses', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: 'Modo ARRISCADO requer accept_risk' }), {
        status: 412,
        headers: { 'content-type': 'application/json' }
      })
    )
    globalThis.fetch = fetchMock as unknown as typeof fetch
    const client = new ApiClient('http://127.0.0.1:39712')

    await expect(client.search({ ...baseRequest, scrape_mode: 'browser' })).rejects.toMatchObject({
      status: 412,
      message: expect.stringContaining('ARRISCADO')
    })
  })

  it('exposes ApiError as a typed instance', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response('boom', { status: 500 })
    )
    globalThis.fetch = fetchMock as unknown as typeof fetch
    const client = new ApiClient('http://127.0.0.1:39712')

    await expect(client.health()).rejects.toBeInstanceOf(ApiError)
  })

  it('hits /diagnostics/cookie with the configured cookie and browser', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          found: false,
          source: 'missing',
          browser_priority: ['chrome', 'edge'],
          default_browser: 'chrome',
          rookiepy_available: true,
          browser_cookie3_available: true,
          attempts: [{ backend: 'rookiepy', browser: 'chrome', found: false }],
          hints: ['Faça login no LinkedIn'],
          preview: null
        }),
        { status: 200, headers: { 'content-type': 'application/json' } }
      )
    )
    globalThis.fetch = fetchMock as unknown as typeof fetch
    const client = new ApiClient('http://127.0.0.1:39712')

    const result = await client.diagnoseCookie({ cookie: null, browser: 'auto' })

    expect(result.found).toBe(false)
    expect(result.default_browser).toBe('chrome')
    const [url, init] = fetchMock.mock.calls[0]
    expect(String(url)).toBe('http://127.0.0.1:39712/diagnostics/cookie')
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body as string)).toEqual({ cookie: null, browser: 'auto' })
  })

  it('hits the lead-tables endpoints with typed payloads', async () => {
    const tableBody = {
      table: {
        id: 't1',
        name: 'Marketing',
        created_at: '2026-05-13T00:00:00+00:00',
        updated_at: '2026-05-13T00:00:00+00:00',
        source_type: 'search',
        keywords: ['marketing'],
        search_queries: [],
        search_request: { company_name: 'Nubank' },
        enrichment_status: 'not_enriched',
        lead_count: 1
      },
      leads: []
    }
    const fetchMock = vi.fn().mockImplementation((_url: string, init: RequestInit) =>
      Promise.resolve(
        new Response(JSON.stringify(init?.method === 'GET' ? [tableBody.table] : tableBody), {
          status: 200,
          headers: { 'content-type': 'application/json' }
        })
      )
    )
    globalThis.fetch = fetchMock as unknown as typeof fetch
    const client = new ApiClient('http://127.0.0.1:39712')

    const list = await client.listLeadTables()
    expect(list).toHaveLength(1)
    expect(list[0].id).toBe('t1')

    const created = await client.createLeadTable({
      name: 'Marketing',
      leads: [],
      keywords: ['marketing']
    })
    expect(created.table.name).toBe('Marketing')

    await client.exportLeadTable('t1', { output_path: '/tmp/out.csv' })
    const exportCall = fetchMock.mock.calls.find(([url]) =>
      String(url).endsWith('/lead-tables/t1/export')
    )!
    expect(exportCall[1].method).toBe('POST')
    expect(JSON.parse(exportCall[1].body as string)).toEqual({ output_path: '/tmp/out.csv' })

    await client.mergeLeadTables({ name: 'Merged', table_ids: ['a', 'b'] })
    const mergeCall = fetchMock.mock.calls.find(([url]) =>
      String(url).endsWith('/lead-tables/merge')
    )!
    expect(JSON.parse(mergeCall[1].body as string)).toEqual({
      name: 'Merged',
      table_ids: ['a', 'b']
    })

    await client.enrichLeadTable('t1', {
      lead_refs: ['https://www.linkedin.com/in/ana/'],
      fields: 'both',
      providers: ['lusha'],
      credit_costs_brl: { lusha: 4.5 },
      confirmed: false
    })
    const enrichCall = fetchMock.mock.calls.find(([url]) =>
      String(url).endsWith('/lead-tables/t1/enrich')
    )!
    expect(enrichCall[1].method).toBe('POST')
    expect(JSON.parse(enrichCall[1].body as string)).toMatchObject({
      lead_refs: ['https://www.linkedin.com/in/ana/'],
      fields: 'both',
      providers: ['lusha'],
      confirmed: false
    })
  })

  it('posts Telegram Telethon experimental consults to the dedicated route', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          status: 'completed',
          summary: { requested_leads: 1, succeeded: 1, failed: 0 },
          consults: []
        }),
        { status: 200, headers: { 'content-type': 'application/json' } }
      )
    )
    globalThis.fetch = fetchMock as unknown as typeof fetch
    const client = new ApiClient('http://127.0.0.1:39712')

    await client.telegramConsultTelethonExperimental('t1', {
      lead_refs: ['https://www.linkedin.com/in/ana/'],
      max_leads: 10
    })

    const [url, init] = fetchMock.mock.calls[0]
    expect(String(url)).toBe(
      'http://127.0.0.1:39712/lead-tables/t1/telegram-consult/telethon-experimental'
    )
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body as string)).toEqual({
      lead_refs: ['https://www.linkedin.com/in/ana/'],
      max_leads: 10
    })
  })

  it('polls run state until terminal status', async () => {
    const responses = [
      new Response(JSON.stringify({ run_id: 'r1', status: 'running' }), {
        status: 200,
        headers: { 'content-type': 'application/json' }
      }),
      new Response(JSON.stringify({ run_id: 'r1', status: 'running' }), {
        status: 200,
        headers: { 'content-type': 'application/json' }
      }),
      new Response(
        JSON.stringify({
          run_id: 'r1',
          status: 'completed',
          result: { leads: [], summary: { output_file: 'output/test.csv' } }
        }),
        { status: 200, headers: { 'content-type': 'application/json' } }
      )
    ]
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(responses.shift()!))
    globalThis.fetch = fetchMock as unknown as typeof fetch
    const client = new ApiClient('http://127.0.0.1:39712')

    const final = await client.waitForRun('r1', { intervalMs: 1, timeoutMs: 1000 })

    expect(final.status).toBe('completed')
    expect(fetchMock).toHaveBeenCalledTimes(3)
  })
})
