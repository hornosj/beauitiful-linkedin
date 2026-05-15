import type {
  ApiKeyOverrides,
  CookieDiagnosticRequest,
  CookieDiagnosticResponse,
  DiagnosticsResponse,
  EnrichLeadTableRequest,
  EnrichLeadTableResponse,
  ExperimentalSearchResponse,
  ExportLeadTableRequest,
  ExportLeadTableResponse,
  HealthResponse,
  ImportLeadTableRequest,
  MergeLeadTablesRequest,
  PeopleSearchProbeRequest,
  PeopleSearchProbeResponse,
  ProbeEvent,
  ProbeStartResponse,
  ProbeStateResponse,
  RunStateResponse,
  SaveLeadTableRequest,
  SavedLeadTable,
  SavedLeadTableDetail,
  SearchRequest,
  SearchResponse,
  StartRunResponse,
  TaxonomiesResponse
} from './types'

export class ApiError extends Error {
  constructor(public readonly status: number, message: string, public readonly body?: unknown) {
    super(message)
    this.name = 'ApiError'
  }
}

interface PollOptions {
  intervalMs?: number
  timeoutMs?: number
}

const TERMINAL_STATUSES = new Set(['completed', 'failed', 'cancelled'])

export class ApiClient {
  constructor(private readonly baseUrl: string) {}

  health(): Promise<HealthResponse> {
    return this.get<HealthResponse>('/health')
  }

  taxonomies(): Promise<TaxonomiesResponse> {
    return this.get<TaxonomiesResponse>('/taxonomies')
  }

  search(request: SearchRequest): Promise<SearchResponse> {
    return this.post<SearchResponse>('/search', request)
  }

  startRun(request: SearchRequest): Promise<StartRunResponse> {
    return this.post<StartRunResponse>('/search/start', request)
  }

  runState(runId: string): Promise<RunStateResponse> {
    return this.get<RunStateResponse>(`/runs/${encodeURIComponent(runId)}`)
  }

  cancelRun(runId: string): Promise<RunStateResponse> {
    return this.delete<RunStateResponse>(`/runs/${encodeURIComponent(runId)}`)
  }

  diagnostics(apiKeys?: ApiKeyOverrides): Promise<DiagnosticsResponse> {
    return this.post<DiagnosticsResponse>('/diagnostics', apiKeys ?? {})
  }

  diagnoseCookie(payload?: CookieDiagnosticRequest): Promise<CookieDiagnosticResponse> {
    return this.post<CookieDiagnosticResponse>('/diagnostics/cookie', payload ?? {})
  }

  listLeadTables(): Promise<SavedLeadTable[]> {
    return this.get<SavedLeadTable[]>('/lead-tables')
  }

  getLeadTable(tableId: string): Promise<SavedLeadTableDetail> {
    return this.get<SavedLeadTableDetail>(`/lead-tables/${encodeURIComponent(tableId)}`)
  }

  createLeadTable(payload: SaveLeadTableRequest): Promise<SavedLeadTableDetail> {
    return this.post<SavedLeadTableDetail>('/lead-tables', payload)
  }

  importLeadTable(payload: ImportLeadTableRequest): Promise<SavedLeadTableDetail> {
    return this.post<SavedLeadTableDetail>('/lead-tables/import', payload)
  }

  exportLeadTable(
    tableId: string,
    payload?: ExportLeadTableRequest
  ): Promise<ExportLeadTableResponse> {
    return this.post<ExportLeadTableResponse>(
      `/lead-tables/${encodeURIComponent(tableId)}/export`,
      payload ?? {}
    )
  }

  enrichLeadTable(
    tableId: string,
    payload: EnrichLeadTableRequest
  ): Promise<EnrichLeadTableResponse> {
    return this.post<EnrichLeadTableResponse>(
      `/lead-tables/${encodeURIComponent(tableId)}/enrich`,
      payload
    )
  }

  mergeLeadTables(payload: MergeLeadTablesRequest): Promise<SavedLeadTableDetail> {
    return this.post<SavedLeadTableDetail>('/lead-tables/merge', payload)
  }

  deleteLeadTable(tableId: string): Promise<{ ok: boolean }> {
    return this.delete<{ ok: boolean }>(`/lead-tables/${encodeURIComponent(tableId)}`)
  }

  probePeopleSearch(payload: PeopleSearchProbeRequest): Promise<PeopleSearchProbeResponse> {
    return this.post<PeopleSearchProbeResponse>('/people-search/probe', payload)
  }

  startPeopleSearchProbe(payload: PeopleSearchProbeRequest): Promise<ProbeStartResponse> {
    return this.post<ProbeStartResponse>('/people-search/probe/start', payload)
  }

  peopleSearchProbeState(probeId: string): Promise<ProbeStateResponse> {
    return this.get<ProbeStateResponse>(
      `/people-search/probe/${encodeURIComponent(probeId)}`
    )
  }

  /**
   * Stream-like helper that polls the probe registry, calling ``onEvent``
   * for each new event as it arrives. Resolves with the final state when
   * the probe reaches a terminal status.
   */
  async followPeopleSearchProbe(
    payload: PeopleSearchProbeRequest,
    onEvent: (event: ProbeEvent, state: ProbeStateResponse) => void,
    options: { intervalMs?: number; timeoutMs?: number } = {}
  ): Promise<ProbeStateResponse> {
    const interval = options.intervalMs ?? 300
    const timeout = options.timeoutMs ?? 30_000
    const start = await this.startPeopleSearchProbe(payload)
    const deadline = Date.now() + timeout
    let delivered = 0
    while (true) {
      const state = await this.peopleSearchProbeState(start.probe_id)
      while (delivered < state.events.length) {
        onEvent(state.events[delivered], state)
        delivered += 1
      }
      if (state.status === 'completed' || state.status === 'failed') {
        return state
      }
      if (Date.now() > deadline) {
        throw new ApiError(408, `Probe ${start.probe_id} excedeu o tempo de espera.`)
      }
      await new Promise((resolve) => setTimeout(resolve, interval))
    }
  }

  experimentalSearch(tableId: string): Promise<ExperimentalSearchResponse> {
    return this.post<ExperimentalSearchResponse>(
      `/lead-tables/${encodeURIComponent(tableId)}/experimental-search`,
      {}
    )
  }

  async waitForRun(runId: string, options: PollOptions = {}): Promise<RunStateResponse> {
    const interval = options.intervalMs ?? 500
    const timeout = options.timeoutMs ?? 5 * 60_000
    const deadline = Date.now() + timeout
    while (true) {
      const state = await this.runState(runId)
      if (TERMINAL_STATUSES.has(state.status)) {
        return state
      }
      if (Date.now() > deadline) {
        throw new ApiError(408, `Run ${runId} excedeu o tempo de espera (${timeout}ms).`)
      }
      await sleep(interval)
    }
  }

  private async get<T>(path: string): Promise<T> {
    const response = await fetch(this.url(path), { method: 'GET' })
    return this.parse<T>(response)
  }

  private async post<T>(path: string, body: unknown): Promise<T> {
    const response = await fetch(this.url(path), {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body)
    })
    return this.parse<T>(response)
  }

  private async delete<T>(path: string): Promise<T> {
    const response = await fetch(this.url(path), { method: 'DELETE' })
    return this.parse<T>(response)
  }

  private url(path: string): string {
    return `${this.baseUrl.replace(/\/$/, '')}${path}`
  }

  private async parse<T>(response: Response): Promise<T> {
    if (response.ok) {
      return (await response.json()) as T
    }
    const text = await response.text()
    let detail = text
    try {
      const parsed = JSON.parse(text)
      if (parsed && typeof parsed === 'object' && 'detail' in parsed) {
        detail = String((parsed as { detail: unknown }).detail)
      }
    } catch {
      // not JSON — fall through to raw text
    }
    throw new ApiError(response.status, detail || response.statusText, text)
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}
