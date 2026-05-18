import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ApiClient, ApiError } from '../../shared/api'
import {
  applyRolePresetToForm,
  buildSearchRequestFromForm,
  emptyFormState,
  isRiskyScrapeMode,
  validateSearchForm,
  type SearchFormState,
  type ValidationErrors
} from '../../shared/validation'
import type {
  ApiKeyOverrides,
  Lead,
  ProspectingSummary,
  ProviderDiagnostic,
  ScrapeMode,
  TaxonomiesResponse
} from '../../shared/types'
import SearchForm from './components/SearchForm'
import FilterPanel from './components/FilterPanel'
import ResultsTable from './components/ResultsTable'
import RiskWarning from './components/RiskWarning'
import SettingsPanel from './components/SettingsPanel'
import DiagnosticPanel from './components/DiagnosticPanel'
import ChromeBootstrapModal from './components/ChromeBootstrapModal'
import SavedLeadsLibrary from './components/SavedLeadsLibrary'
import SmallCompanyDialog from './components/SmallCompanyDialog'
import ProbeProgress from './components/ProbeProgress'
import InternalEnrichProgress from './components/InternalEnrichProgress'
import LiveActivityBubbles, {
  type LiveActivityItem
} from './components/LiveActivityBubbles'
import { EnrichmentRunnerProvider, useEnrichmentRunner } from './enrichment/EnrichmentRunnerContext'
import EnrichmentRunPill from './enrichment/EnrichmentRunPill'
import type {
  InternalEnrichLeadEvent,
  PeopleSearchProbeResponse,
  ProbeEvent
} from '../../shared/types'

interface RunResult {
  leads: Lead[]
  summary: ProspectingSummary
}

type ThemeMode = 'light' | 'dark'
type View = 'search' | 'leads' | 'providers'

const API_KEYS_STORAGE_KEY = 'beautiful-linkedin.api-keys'
const THEME_STORAGE_KEY = 'beautiful-linkedin.theme'

export default function App() {
  const [form, setForm] = useState<SearchFormState>(emptyFormState)
  const [errors, setErrors] = useState<ValidationErrors>({})
  const [taxonomies, setTaxonomies] = useState<TaxonomiesResponse | null>(null)
  const [baseUrl, setBaseUrl] = useState<string | null>(null)
  const [sidecarError, setSidecarError] = useState<string | null>(null)
  const [apiKeys, setApiKeys] = useState<ApiKeyOverrides>(() => loadApiKeys())
  const [showSettings, setShowSettings] = useState(false)
  const [showDiagnostics, setShowDiagnostics] = useState(false)
  const [searchDiagnostics, setSearchDiagnostics] = useState<ProviderDiagnostic[]>([])
  const [theme, setTheme] = useState<ThemeMode>(() => loadTheme())
  const [view, setView] = useState<View>('search')
  const [running, setRunning] = useState(false)
  const [revealing, setRevealing] = useState(false)
  const [visibleLeadCount, setVisibleLeadCount] = useState(0)
  const [showRiskModal, setShowRiskModal] = useState(false)
  const [showChromeBootstrap, setShowChromeBootstrap] = useState(false)
  const [smallCompanyProbe, setSmallCompanyProbe] =
    useState<PeopleSearchProbeResponse | null>(null)
  const [probeEvents, setProbeEvents] = useState<ProbeEvent[]>([])
  const [probeStatus, setProbeStatus] = useState<
    'idle' | 'running' | 'completed' | 'failed'
  >('idle')
  const [result, setResult] = useState<RunResult | null>(null)
  const [feedback, setFeedback] = useState<{ kind: 'error' | 'success'; message: string } | null>(
    null
  )
  const [liveActivities, setLiveActivities] = useState<LiveActivityItem[]>([])
  const [tableQuery, setTableQuery] = useState('')
  const [tableFilter, setTableFilter] = useState<'all' | 'head' | 'growth'>('all')
  const liveActivitySeqRef = useRef(0)
  const liveActivityTimersRef = useRef<number[]>([])
  const lastSearchActivityRef = useRef<string | null>(null)

  const client = useMemo(() => (baseUrl ? new ApiClient(baseUrl) : null), [baseUrl])
  const allLeads = useMemo(
    () => (result ? result.leads.slice(0, visibleLeadCount) : []),
    [result, visibleLeadCount]
  )
  const filteredLeads = useMemo(() => {
    let r = allLeads
    if (tableFilter === 'head')
      r = r.filter((l) => /head|director/i.test(l.title ?? ''))
    if (tableFilter === 'growth')
      r = r.filter((l) => /growth/i.test(l.title ?? ''))
    if (tableQuery)
      r = r.filter((l) =>
        `${l.person_name ?? ''}${l.title ?? ''}`.toLowerCase().includes(tableQuery.toLowerCase())
      )
    return r
  }, [allLeads, tableFilter, tableQuery])

  const pendingLeadCount = result ? Math.max(result.leads.length - visibleLeadCount, 0) : 0
  const configuredApiKeyCount = countConfiguredApiKeys(apiKeys)
  const totalLeads = result?.leads.length ?? 0
  const matchRate = result?.summary
    ? Math.round(
        (result.summary.total_deduplicated_leads /
          Math.max(result.summary.total_raw_leads, 1)) *
          100
      )
    : null

  useEffect(() => {
    document.documentElement.classList.toggle('dark', theme === 'dark')
    safeSetStorage(THEME_STORAGE_KEY, theme)
  }, [theme])

  useEffect(() => {
    safeSetStorage(API_KEYS_STORAGE_KEY, JSON.stringify(apiKeys))
  }, [apiKeys])

  useEffect(() => {
    const configuredBaseUrl = import.meta.env.VITE_BEAUTIFUL_LINKEDIN_BASE_URL as string | undefined
    if (window.beautifulLinkedIn) {
      void window.beautifulLinkedIn.getStatus().then((status) => {
        setBaseUrl(status.baseUrl)
        setSidecarError(status.error)
        if (!status.running) {
          setFeedback({
            kind: 'error',
            message: status.error
              ? `Sidecar Python offline: ${status.error}`
              : 'Sidecar Python offline. Abra pelo Electron ou configure a API local para testar no navegador.'
          })
        }
      })
      return
    }
    if (configuredBaseUrl) {
      setBaseUrl(configuredBaseUrl)
      return
    }
    setFeedback({
      kind: 'error',
      message:
        'Bridge do Electron indisponível. Para testar no navegador, defina VITE_BEAUTIFUL_LINKEDIN_BASE_URL apontando para o sidecar.'
    })
  }, [])

  useEffect(() => {
    if (!client) return
    let cancelled = false
    void client
      .taxonomies()
      .then((response) => {
        if (!cancelled) setTaxonomies(response)
      })
      .catch((error: unknown) => {
        if (cancelled) return
        const message = error instanceof Error ? error.message : 'Falha ao carregar taxonomias.'
        setFeedback({ kind: 'error', message })
      })
    return () => {
      cancelled = true
    }
  }, [client])

  useEffect(() => {
    if (!result) {
      setRevealing(false)
      return
    }
    if (visibleLeadCount >= result.leads.length) {
      setRevealing(false)
      return
    }
    setRevealing(true)
    const timer = window.setTimeout(
      () => {
        setVisibleLeadCount((count) => Math.min(count + 1, result.leads.length))
      },
      visibleLeadCount === 0 ? 120 : 75
    )
    return () => window.clearTimeout(timer)
  }, [result, visibleLeadCount])

  useEffect(() => {
    if (!feedback || feedback.kind !== 'success') return
    const t = window.setTimeout(() => setFeedback(null), 3500)
    return () => window.clearTimeout(t)
  }, [feedback])

  useEffect(() => {
    return () => {
      liveActivityTimersRef.current.forEach((timer) => window.clearTimeout(timer))
      liveActivityTimersRef.current = []
    }
  }, [])

  const pushLiveActivity = useCallback(
    (activity: Omit<LiveActivityItem, 'id'>) => {
      const id = `activity-${Date.now()}-${liveActivitySeqRef.current++}`
      setLiveActivities((current) => [{ id, ...activity }, ...current].slice(0, 4))
      const timer = window.setTimeout(() => {
        setLiveActivities((current) => current.filter((item) => item.id !== id))
      }, 3600)
      liveActivityTimersRef.current.push(timer)
    },
    []
  )

  useEffect(() => {
    if (!result || visibleLeadCount === 0) return
    const index = visibleLeadCount - 1
    const lead = result.leads[index]
    if (!lead) return
    const key = `${result.summary.output_file}:${index}:${lead.linkedin_url ?? lead.source_url ?? lead.person_name ?? ''}`
    if (lastSearchActivityRef.current === key) return
    lastSearchActivityRef.current = key
    pushLiveActivity({
      title: 'Lead encontrado',
      detail: formatLeadActivity(lead),
      tone: 'success'
    })
  }, [result, visibleLeadCount, pushLiveActivity])

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const command = event.ctrlKey || event.metaKey
      if (!command) return
      if (event.key === 'Enter') {
        event.preventDefault()
        document.querySelector<HTMLFormElement>('form')?.requestSubmit()
      } else if (event.key.toLowerCase() === 'n') {
        event.preventDefault()
        setView('search')
        setForm(emptyFormState)
        setErrors({})
      } else if (event.key === ',') {
        event.preventDefault()
        setShowSettings(true)
      } else if (event.shiftKey && event.key.toLowerCase() === 'd') {
        event.preventDefault()
        setTheme((value) => (value === 'light' ? 'dark' : 'light'))
      } else if (event.key.toLowerCase() === 'f') {
        event.preventDefault()
        const selector =
          view === 'leads'
            ? 'input[aria-label="Filtrar leads salvos"]'
            : 'input[aria-label="Filtrar leads"]'
        document.querySelector<HTMLInputElement>(selector)?.focus()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [view])

  const updateForm = (patch: Partial<SearchFormState>) => {
    setForm((prev) => ({ ...prev, ...patch }))
  }

  const updateFilters = (patch: Partial<SearchFormState['filters']>) => {
    setForm((prev) => ({ ...prev, filters: { ...prev.filters, ...patch } }))
  }

  const updateApiKeys = (patch: Partial<ApiKeyOverrides>) => {
    setApiKeys((prev) => ({ ...prev, ...patch }))
  }

  const handleRolePresetChange = (value: string) => {
    if (value === 'custom') {
      setForm((prev) => applyRolePresetToForm(prev, null))
      return
    }
    const preset = taxonomies?.role_presets.find((item) => item.value === value) ?? null
    setForm((prev) => applyRolePresetToForm(prev, preset))
  }

  const handleScrapeModeChange = (mode: ScrapeMode) => {
    updateForm({ scrapeMode: mode, acceptRisk: false })
    if (isRiskyScrapeMode(mode)) {
      setShowRiskModal(true)
    }
  }

  const runSearch = async (formOverride?: SearchFormState) => {
    if (!client) {
      setFeedback({
        kind: 'error',
        message: 'Sidecar offline. A busca precisa de uma API local ativa.'
      })
      return
    }
    const effectiveForm = formOverride ?? form
    setErrors({})
    setFeedback(null)
    setRunning(true)
    setRevealing(false)
    setVisibleLeadCount(0)
    setResult(null)
    lastSearchActivityRef.current = null
    try {
      const response = await client.search(buildSearchRequestFromForm(effectiveForm, apiKeys))
      setResult(response)
      setVisibleLeadCount(0)
      setRevealing(response.leads.length > 0)
      const diagnostics = response.provider_diagnostics ?? []
      setSearchDiagnostics(diagnostics)
      if (response.leads.length === 0) {
        const message = explainEmptyResult(diagnostics, apiKeys)
        setFeedback({ kind: 'error', message })
      } else {
        setFeedback({
          kind: 'success',
          message: `Busca finalizada · ${response.summary.total_deduplicated_leads} leads únicos · ${response.summary.output_file}`
        })
      }
    } catch (error) {
      const message =
        error instanceof ApiError
          ? `${error.status}: ${error.message}`
          : error instanceof Error
            ? error.message
            : 'Erro desconhecido.'
      setFeedback({ kind: 'error', message })
    } finally {
      setRunning(false)
    }
  }

  const saveCurrentSearch = async (name: string) => {
    if (!client) {
      setFeedback({
        kind: 'error',
        message: 'Sidecar offline. Não dá para salvar a busca atual.'
      })
      return
    }
    const leads = result?.leads ?? []
    if (leads.length === 0) {
      setFeedback({ kind: 'error', message: 'Nenhum lead na busca atual para salvar.' })
      return
    }
    const request = buildPersistedSearchRequest(form)
    if (!name || !name.trim()) return
    try {
      const detail = await client.createLeadTable({
        name: name.trim(),
        leads,
        keywords: parseKeywords(form.titles),
        search_request: request,
        generate_queries: true
      })
      setFeedback({
        kind: 'success',
        message: `Tabela "${detail.table.name}" salva com ${detail.leads.length} leads.`
      })
    } catch (error) {
      const message =
        error instanceof ApiError
          ? `${error.status}: ${error.message}`
          : error instanceof Error
            ? error.message
            : 'Erro desconhecido.'
      setFeedback({ kind: 'error', message })
    }
  }

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault()
    if (!client) {
      setFeedback({
        kind: 'error',
        message: 'Sidecar offline. A busca precisa de uma API local ativa.'
      })
      return
    }
    const validation = validateSearchForm(form)
    if (!validation.ok) {
      setErrors(validation.errors)
      return
    }
    if (form.scrapeMode === 'people_search') {
      setErrors({})
      setFeedback(null)
      // Step-by-step probe: stream events into the UI while CDP+Playwright
      // navigate to the company People page in the user's open Chrome.
      setProbeEvents([])
      setProbeStatus('running')
      try {
        const finalState = await client.followPeopleSearchProbe(
          {
            company_name: form.companyName,
            company_domain: form.companyDomain || undefined,
            linkedin_url: form.linkedinUrl || undefined
          },
          (_event, state) => setProbeEvents([...state.events])
        )
        setProbeStatus(finalState.status === 'failed' ? 'failed' : 'completed')
        const classification = finalState.classification
        if (classification?.should_offer_general_search) {
          setSmallCompanyProbe(classification)
          return
        }
      } catch {
        setProbeStatus('failed')
        // Probe is best-effort; fall through to the legacy chrome bootstrap
        // path below so the user can still run a search.
      }

      if (window.beautifulLinkedIn?.chrome) {
        // Fast-path: if CDP is already alive and the user isn't on a login
        // wall, skip the bootstrap modal.
        try {
          const chrome = window.beautifulLinkedIn.chrome
          const probe = await chrome.probe()
          const linkedinState = probe.alive ? await chrome.checkLinkedIn() : null
          if (probe.alive && linkedinState?.state !== 'logged_out') {
            void runSearch()
            return
          }
        } catch {
          // fall through to bootstrap modal
        }
        setShowChromeBootstrap(true)
        return
      }
    }
    void runSearch()
  }

  const sidecarOk = baseUrl !== null

  return (
    <EnrichmentRunnerProvider
      client={client}
      onError={(message) => setFeedback({ kind: 'error', message })}
      onSuccess={(message) => setFeedback({ kind: 'success', message })}
    >
    <div className="flex flex-col h-screen w-screen overflow-hidden bg-bg text-ink font-sans">
      {/* Title bar */}
      <div className="h-11 bg-surface/60 backdrop-blur-xl border-b border-line flex items-center px-4 gap-3 relative z-50 shrink-0">
        <div className="flex items-center gap-2.5 text-[13px] font-semibold text-ink tracking-tight select-none">
          <div className="w-5 h-5 rounded-[6px] bg-gradient-to-br from-accent to-[#5856d6] flex items-center justify-center text-white text-[11px] font-bold shadow-[0_1px_2px_rgba(0,122,255,0.3)]">B</div>
          Beautiful LinkedIn
        </div>
        {form.companyName && (
          <div className="text-[12px] font-normal text-ink-3 pl-3 ml-1 border-l border-line select-none">
            {form.companyName}
            {form.rolePreset && form.rolePreset !== 'custom'
              ? ` · ${labelForPreset(taxonomies, form.rolePreset)}`
              : ''}
          </div>
        )}
        <div className="flex items-center gap-1 ml-auto select-none">
          <button
            type="button"
            className="tb-btn"
            aria-label="Alternar tema"
            onClick={() => setTheme(theme === 'light' ? 'dark' : 'light')}
            title="Alternar tema"
          >
            {theme === 'light' ? '🌙' : '☀'}
          </button>
          <button
            type="button"
            className="tb-btn"
            aria-label="Abrir diagnóstico"
            onClick={() => setShowDiagnostics(true)}
            title="Diagnóstico"
          >
            ⚕
          </button>
          <button
            type="button"
            className="tb-btn"
            aria-label="Abrir preferências"
            onClick={() => setShowSettings(true)}
            title="Preferências"
          >
            ⚙
          </button>
        </div>
      </div>

      <div className="flex flex-1 overflow-hidden max-md:flex-col">
        {/* Sidebar */}
        <aside className="w-[220px] bg-surface-2/40 backdrop-blur-md border-r border-line overflow-y-auto py-3.5 px-2.5 flex flex-col shrink-0 max-md:w-full max-md:max-h-[220px] max-md:border-r-0 max-md:border-b">
          <nav className="mb-4.5" aria-label="Workspace">
            <div className="text-[11px] font-semibold text-ink-3 px-2 pb-1.5 tracking-wide select-none">Workspace</div>
            <button
              type="button"
              className={`sidebar-nav-btn ${view === 'search' ? 'bg-accent text-white shadow-sm' : 'text-ink-2 hover:bg-surface-3'}`}
              aria-current={view === 'search' ? 'page' : undefined}
              onClick={() => setView('search')}
            >
              <span className="w-4 h-4 grid place-items-center shrink-0">⌕</span> Nova busca
            </button>
            <button
              type="button"
              className={`sidebar-nav-btn ${view === 'leads' ? 'bg-accent text-white shadow-sm' : 'text-ink-2 hover:bg-surface-3'}`}
              aria-current={view === 'leads' ? 'page' : undefined}
              onClick={() => setView('leads')}
            >
              <span className="w-4 h-4 grid place-items-center shrink-0">◴</span> Leads salvos
              {totalLeads > 0 && <span className={`ml-auto rounded-full px-1.5 py-0.5 text-[10px] font-mono leading-none flex items-center ${view === 'leads' ? 'bg-white/20 text-white' : 'bg-surface-3 text-ink-3'}`}>{totalLeads}</span>}
            </button>
            <button
              type="button"
              className={`sidebar-nav-btn ${view === 'providers' ? 'bg-accent text-white shadow-sm' : 'text-ink-2 hover:bg-surface-3'}`}
              aria-current={view === 'providers' ? 'page' : undefined}
              onClick={() => {
                setView('providers')
                setShowSettings(true)
              }}
            >
              <span className="w-4 h-4 grid place-items-center shrink-0">⌬</span> Provedores
              {configuredApiKeyCount > 0 && <span className={`ml-auto rounded-full px-1.5 py-0.5 text-[10px] font-mono leading-none flex items-center ${view === 'providers' ? 'bg-white/20 text-white' : 'bg-surface-3 text-ink-3'}`}>{configuredApiKeyCount}</span>}
            </button>
          </nav>

          <nav className="mb-4.5" aria-label="Modos de coleta">
            <div className="text-[11px] font-semibold text-ink-3 px-2 pb-1.5 tracking-wide select-none">Modos de coleta</div>
            {(['people_search', 'api'] as ScrapeMode[]).map((m) => (
              <button
                key={m}
                type="button"
                className={`sidebar-nav-btn ${form.scrapeMode === m ? 'bg-surface border border-line shadow-sm text-ink font-medium' : 'text-ink-2 hover:bg-surface-3'}`}
                aria-pressed={form.scrapeMode === m}
                onClick={() => handleScrapeModeChange(m)}
                title={
                  m === 'people_search'
                    ? 'Usa funcionários visíveis na aba People do LinkedIn, sem visitar perfis.'
                    : 'Usa as APIs estruturadas configuradas (Apollo, PDL, Coresignal etc.).'
                }
              >
                <span className="w-4 h-4 grid place-items-center shrink-0">
                  <span className="w-2 h-2 rounded-full bg-success"></span>
                </span>
                {m === 'people_search' ? 'People Search' : 'API'}
              </button>
            ))}
          </nav>

          <div className="mt-auto px-2 pt-2.5 border-t border-line flex items-center gap-2 text-[11px] text-ink-3">
            <span className={`w-1.5 h-1.5 rounded-full ${sidecarOk ? 'bg-success shadow-[0_0_0_rgba(52,199,89,0.5)] animate-[pulse_2s_infinite]' : 'bg-ink-4'}`} />
            <div>
              <div className="font-medium text-ink-2">
                {sidecarOk ? 'Sidecar conectado' : 'Sidecar offline'}
              </div>
              <div className="font-mono text-[10px] leading-tight mt-0.5 max-w-[170px] truncate">
                {sidecarOk ? baseUrl : sidecarError ?? 'inicie o app pelo Electron'}
              </div>
            </div>
          </div>
        </aside>

        {/* Main */}
        <main className="flex-1 min-w-0 overflow-y-auto overflow-x-hidden bg-bg relative">
          <div className="sticky top-0 z-40 bg-bg/70 backdrop-blur-[14px] border-b border-line px-5 py-2.5 flex items-center gap-3 flex-wrap">
            <span className="text-[13px] text-ink-2 font-medium">
              Workspace <span className="text-ink-4 mx-1.5">›</span>{' '}
              <strong className="font-semibold text-ink">
                {view === 'search' ? 'Nova busca' : view === 'leads' ? 'Leads salvos' : 'Workspace'}
              </strong>
            </span>
            <span className="flex-1" />
          <button className="pill-btn max-sm:hidden">
            <span
              style={{
                width: 6,
                height: 6,
                borderRadius: 99,
                background: sidecarOk ? 'var(--success)' : 'var(--ink-4)'
              }}
            />
            v0.1.0
          </button>
          <button className="pill-btn max-sm:hidden" onClick={() => setShowSettings(true)}>
            ⚙ Preferências{configuredApiKeyCount ? ` · ${configuredApiKeyCount}` : ''}
          </button>
          <button className="pill-btn primary" onClick={() => setView('search')}>
            + Nova
          </button>
          </div>

          <div className="px-5 pt-6 pb-16 max-w-[1240px] mx-auto max-sm:px-3">
          <div className="hero">
            <div>
              <h1>Bem-vindo de volta.</h1>
              <p>
                {result
                  ? `Última busca: ${result.summary.total_deduplicated_leads} leads únicos · arquivo ${result.summary.output_file}.`
                  : 'Configure a empresa, escolha o perfil de cargo e rode a busca. Os resultados aparecem ao lado.'}
              </p>
            </div>
            <div className="stat-strip">
              <div className="stat">
                <div className="v">{totalLeads || '—'}</div>
                <div className="l">Leads totais</div>
              </div>
              <div className="stat">
                <div className="v">{result?.summary.total_raw_leads ?? '—'}</div>
                <div className="l">Brutos</div>
              </div>
              <div className="stat">
                <div className="v">
                  {matchRate !== null ? matchRate : '—'}
                  {matchRate !== null && (
                    <span style={{ fontSize: 14, color: 'var(--ink-3)' }}>%</span>
                  )}
                </div>
                <div className="l">Taxa de match</div>
              </div>
              <div className="stat">
                <div className="v" style={{ color: 'var(--success)' }}>
                  {configuredApiKeyCount}
                </div>
                <div className="l">APIs ativas</div>
              </div>
            </div>
          </div>

          {probeStatus !== 'idle' && probeEvents.length > 0 && view === 'search' && (
            <ProbeProgress
              companyName={form.companyName}
              events={probeEvents}
              status={probeStatus}
            />
          )}

          {feedback && (
            <div
              style={{
                marginBottom: 14,
                padding: '10px 14px',
                borderRadius: 10,
                fontSize: 12,
                background:
                  feedback.kind === 'error'
                    ? 'rgba(255,59,48,0.08)'
                    : 'rgba(52,199,89,0.10)',
                color: feedback.kind === 'error' ? 'var(--risky)' : '#1d6f3f',
                border:
                  feedback.kind === 'error'
                    ? '0.5px solid rgba(255,59,48,0.25)'
                    : '0.5px solid rgba(52,199,89,0.25)'
              }}
            >
              {feedback.message}
            </div>
          )}

          <div key={view} className="animate-fade-in-up duration-300">
            {view === 'leads' ? (
              <SavedLeadsLibrary
                client={client}
                currentLeads={result?.leads ?? []}
                currentKeywords={parseKeywords(form.titles)}
                currentSearchRequest={buildPersistedSearchRequest(form)}
                onFeedback={(kind, message) => setFeedback({ kind, message })}
              />
            ) : (
              <div className="grid grid-cols-1 xl:grid-cols-[360px_minmax(0,1fr)] gap-[18px] items-start">
                <div className="flex flex-col gap-[18px]">
                  <SearchForm
                    form={form}
                    errors={errors}
                    rolePresets={taxonomies?.role_presets ?? []}
                    seniority={taxonomies?.seniority ?? []}
                    scrapeModes={taxonomies?.scrape_modes ?? []}
                    onChange={updateForm}
                    onFilterChange={updateFilters}
                    onRolePresetChange={handleRolePresetChange}
                    onScrapeModeChange={handleScrapeModeChange}
                    onSubmit={handleSubmit}
                    running={running}
                  />
                  <FilterPanel
                    filters={form.filters}
                    functions={taxonomies?.functions ?? []}
                    onChange={updateFilters}
                  />
                </div>

                <ResultsTable
                  leads={filteredLeads}
                  total={allLeads.length}
                  summary={result?.summary ?? null}
                  loading={running || revealing}
                  pendingLeadCount={pendingLeadCount}
                  query={tableQuery}
                  onQueryChange={setTableQuery}
                  filter={tableFilter}
                  onFilterChange={setTableFilter}
                  onSaveCurrent={saveCurrentSearch}
                  saveCurrentDisabled={!client || !result?.leads.length}
                  suggestedSaveName={form.companyName || 'Leads salvos'}
                />
              </div>
            )}
          </div>
        </div>

        {smallCompanyProbe && (
          <SmallCompanyDialog
            probe={smallCompanyProbe}
            onGeneralSearch={() => {
              setSmallCompanyProbe(null)
              const next = { ...form, generalSearch: true }
              setForm(next)
              void runSearch(next)
            }}
            onContinueWithKeywords={() => {
              setSmallCompanyProbe(null)
              const next = { ...form, generalSearch: false }
              setForm(next)
              void runSearch(next)
            }}
            onCancel={() => setSmallCompanyProbe(null)}
          />
        )}

        {showRiskModal && (
          <RiskWarning
            onCancel={() => {
              setShowRiskModal(false)
              updateForm({ scrapeMode: 'people_search', acceptRisk: false })
            }}
            onConfirm={() => {
              setShowRiskModal(false)
              updateForm({ acceptRisk: true })
            }}
          />
        )}

        {showChromeBootstrap && (() => {
          const { url, slug } = buildPeopleTarget(form.linkedinUrl, form.companyName)
          return (
            <ChromeBootstrapModal
              targetUrl={url}
              targetSlug={slug}
              onReady={() => {
                setShowChromeBootstrap(false)
                void runSearch()
              }}
              onCancel={() => {
                setShowChromeBootstrap(false)
                setFeedback({
                  kind: 'error',
                  message: 'Busca cancelada antes de preparar o Chrome.'
                })
              }}
            />
          )
        })()}

        {showSettings && (
          <SettingsPanel
            apiKeys={apiKeys}
            onChange={updateApiKeys}
            onClear={() => setApiKeys({})}
            onClose={() => setShowSettings(false)}
            theme={theme}
            setTheme={setTheme}
            client={client}
          />
        )}

        {showDiagnostics && (
          <DiagnosticPanel
            client={client}
            apiKeys={apiKeys}
            searchDiagnostics={searchDiagnostics}
            onClose={() => setShowDiagnostics(false)}
          />
        )}
        </main>
      </div>
      <EnrichmentBackground onActivity={pushLiveActivity} />
      <LiveActivityBubbles items={liveActivities} />
    </div>
    </EnrichmentRunnerProvider>
  )
}

/**
 * Inner component that consumes the runner context — kept inside the
 * provider so the modal and the floating chip share the same single
 * source of truth across the whole app, regardless of which view is
 * mounted at the time.
 */
function EnrichmentBackground(props: {
  onActivity(activity: Omit<LiveActivityItem, 'id'>): void
}) {
  const { onActivity } = props
  const { run, modalOpen, cancel, closeModal, dismiss } = useEnrichmentRunner()
  const lastLeadEventRef = useRef<string | null>(null)
  const lastDomainCountRef = useRef(0)

  useEffect(() => {
    if (!run?.running || run.domainsDone <= 0) return
    if (run.domainsDone === lastDomainCountRef.current) return
    lastDomainCountRef.current = run.domainsDone
    onActivity({
      title: 'Site da empresa analisado',
      detail: `${run.domainsDone}/${run.uniqueDomains || '…'} domínios verificados`,
      tone: 'neutral'
    })
  }, [run?.running, run?.domainsDone, run?.uniqueDomains, onActivity])

  useEffect(() => {
    const latest = run?.recentLeads[0]
    if (!latest) return
    const key = `${run?.meta.startedAt ?? 0}:${latest.lead_ref ?? latest.person_name ?? ''}:${latest.status}:${latest.email ?? ''}`
    if (lastLeadEventRef.current === key) return
    lastLeadEventRef.current = key
    onActivity(formatEnrichmentActivity(latest))
  }, [run?.meta.startedAt, run?.recentLeads, onActivity])

  return (
    <>
      <InternalEnrichProgress
        open={Boolean(run) && modalOpen}
        running={Boolean(run?.running)}
        phase={run?.phase ?? null}
        totalLeads={run?.totalLeads ?? 0}
        completed={run?.completed ?? 0}
        domainsDone={run?.domainsDone ?? 0}
        uniqueDomains={run?.uniqueDomains ?? 0}
        recentLeads={run?.recentLeads ?? []}
        summary={run?.summary ?? null}
        errorMessage={run?.errorMessage ?? null}
        onCancel={cancel}
        onClose={() => {
          if (run?.running) {
            // Closing during a run minimises to the chip; the work keeps
            // going in the background.
            closeModal()
          } else {
            dismiss()
          }
        }}
      />
      <EnrichmentRunPill />
    </>
  )
}

function labelForPreset(taxonomies: TaxonomiesResponse | null, value: string): string {
  const item = taxonomies?.role_presets.find((p) => p.value === value)
  return item?.label ?? value
}

function loadTheme(): ThemeMode {
  const stored = safeGetStorage(THEME_STORAGE_KEY)
  if (stored === 'light' || stored === 'dark') return stored
  if (window.matchMedia?.('(prefers-color-scheme: dark)').matches) return 'dark'
  return 'light'
}

function explainEmptyResult(
  diagnostics: ProviderDiagnostic[],
  apiKeys: ApiKeyOverrides
): string {
  if (diagnostics.length === 0) {
    const hasAnyKey = countConfiguredApiKeys(apiKeys) > 0
    return hasAnyKey
      ? 'Busca concluída sem leads e sem diagnóstico do sidecar — atualize o app e rode novamente.'
      : 'Busca concluída sem leads. Configure chaves em Preferências ou suba o SearxNG local (docker compose -f docker-compose.searxng.yml up -d).'
  }
  const errored = diagnostics.filter((d) => d.last_error)
  const droppedAll = diagnostics.filter(
    (d) => d.raw_records > 0 && d.leads_returned === 0
  )
  const zero = diagnostics.filter((d) => d.raw_records === 0 && !d.last_error)
  const parts: string[] = []
  if (errored.length > 0) {
    parts.push(
      `${errored.length} provider(s) com erro: ${errored
        .map((d) => `${d.provider} (${d.last_error})`)
        .slice(0, 3)
        .join('; ')}`
    )
  }
  if (droppedAll.length > 0) {
    parts.push(
      `${droppedAll.length} provider(s) tiveram todos os registros descartados pelo filtro de empresa/cargo`
    )
  }
  if (zero.length > 0) {
    parts.push(`${zero.length} provider(s) retornaram 0 registros`)
  }
  return `Busca sem leads. ${parts.join(' · ') || 'Veja o painel de Diagnóstico para detalhes.'} Abra Diagnóstico ↗`
}

function formatLeadActivity(lead: Lead): string {
  const name = lead.person_name || 'Lead sem nome'
  const title = lead.title ? ` · ${lead.title}` : ''
  return `${name}${title}`
}

function formatEnrichmentActivity(
  event: InternalEnrichLeadEvent
): Omit<LiveActivityItem, 'id'> {
  const person = event.person_name || 'Lead sem nome'
  if (event.email) {
    return {
      title: 'E-mail encontrado',
      detail: `${person} · ${event.email}`,
      tone: 'success'
    }
  }
  const label: Record<InternalEnrichLeadEvent['status'], string> = {
    enriched: 'E-mail encontrado',
    skipped_existing_email: 'Lead já tinha e-mail',
    failed_missing_domain: 'Sem domínio para validar',
    failed: 'Sem e-mail válido',
    no_change: 'Sem mudança'
  }
  return {
    title: label[event.status] ?? 'Lead processado',
    detail: person,
    tone:
      event.status === 'failed' || event.status === 'failed_missing_domain'
        ? 'warning'
        : 'neutral'
  }
}

function loadApiKeys(): ApiKeyOverrides {
  const stored = safeGetStorage(API_KEYS_STORAGE_KEY)
  if (!stored) return {}
  try {
    const parsed = JSON.parse(stored) as unknown
    if (!parsed || typeof parsed !== 'object') return {}
    return parsed as ApiKeyOverrides
  } catch {
    return {}
  }
}

function countConfiguredApiKeys(apiKeys: ApiKeyOverrides): number {
  return Object.values(apiKeys).filter((value) => typeof value === 'string' && value.trim()).length
}

function safeGetStorage(key: string): string | null {
  try {
    return window.localStorage.getItem(key)
  } catch {
    return null
  }
}

function safeSetStorage(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value)
  } catch {
    // Local storage may be unavailable in hardened browser contexts.
  }
}

function buildPeopleTarget(linkedinUrl: string, companyName: string): { url: string; slug: string } {
  const slug = extractCompanySlug(linkedinUrl) ?? slugifyName(companyName)
  const url = `https://www.linkedin.com/company/${slug}/people/`
  return { url, slug: `/company/${slug}/` }
}

function extractCompanySlug(linkedinUrl: string): string | null {
  if (!linkedinUrl) return null
  const match = linkedinUrl.match(/linkedin\.com\/company\/([^/?#]+)/i)
  return match ? match[1].toLowerCase() : null
}

function parseKeywords(titles: string): string[] {
  return titles
    .split(',')
    .map((value) => value.trim())
    .filter(Boolean)
}

function buildPersistedSearchRequest(form: SearchFormState): Record<string, unknown> {
  return {
    company_name: form.companyName,
    company_domain: form.companyDomain || undefined,
    linkedin_url: form.linkedinUrl || undefined,
    titles: parseKeywords(form.titles),
    scrape_mode: form.scrapeMode,
    search_depth: form.searchDepth,
    max_results: form.maxResults
  }
}

function slugifyName(name: string): string {
  return (
    name
      .toLowerCase()
      .normalize('NFD')
      .replace(/[̀-ͯ]/g, '')
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-+|-+$/g, '') || 'unknown'
  )
}
