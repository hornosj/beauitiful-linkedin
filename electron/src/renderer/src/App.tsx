import { useEffect, useMemo, useState } from 'react'
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
import type { PeopleSearchProbeResponse, ProbeEvent } from '../../shared/types'

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
  const [tableQuery, setTableQuery] = useState('')
  const [tableFilter, setTableFilter] = useState<'all' | 'head' | 'growth'>('all')

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
    <div className="app-shell">
      {/* Title bar */}
      <div className="titlebar">
        <div className="brand">
          <div className="brand-mark">B</div>
          Beautiful LinkedIn
        </div>
        {form.companyName && (
          <div className="tb-context">
            {form.companyName}
            {form.rolePreset && form.rolePreset !== 'custom'
              ? ` · ${labelForPreset(taxonomies, form.rolePreset)}`
              : ''}
          </div>
        )}
        <div className="tb-actions">
          <button
            type="button"
            className="tb-btn"
            onClick={() => setTheme(theme === 'light' ? 'dark' : 'light')}
            title="Alternar tema"
          >
            {theme === 'light' ? '🌙' : '☀'}
          </button>
          <button
            type="button"
            className="tb-btn"
            onClick={() => setShowDiagnostics(true)}
            title="Diagnóstico"
          >
            ⚕
          </button>
          <button
            type="button"
            className="tb-btn"
            onClick={() => setShowSettings(true)}
            title="Preferências"
          >
            ⚙
          </button>
        </div>
      </div>

      {/* Sidebar */}
      <aside className="sidebar">
        <div className="nav-section">
          <div className="nav-title">Workspace</div>
          <div
            className={`nav-item ${view === 'search' ? 'active' : ''}`}
            onClick={() => setView('search')}
          >
            <span className="ico">⌕</span> Nova busca
          </div>
          <div
            className={`nav-item ${view === 'leads' ? 'active' : ''}`}
            onClick={() => setView('leads')}
          >
            <span className="ico">◴</span> Leads salvos
            {totalLeads > 0 && <span className="badge">{totalLeads}</span>}
          </div>
          <div
            className={`nav-item ${view === 'providers' ? 'active' : ''}`}
            onClick={() => {
              setView('providers')
              setShowSettings(true)
            }}
          >
            <span className="ico">⌬</span> Provedores
            {configuredApiKeyCount > 0 && <span className="badge">{configuredApiKeyCount}</span>}
          </div>
        </div>

        <div className="nav-section">
          <div className="nav-title">Modos de coleta</div>
          {(['people_search', 'api'] as ScrapeMode[]).map((m) => (
            <div
              key={m}
              className={`nav-item ${form.scrapeMode === m ? 'active' : ''}`}
              onClick={() => handleScrapeModeChange(m)}
              title={
                m === 'people_search'
                  ? 'Usa funcionários visíveis na aba People do LinkedIn, sem visitar perfis.'
                  : 'Usa as APIs estruturadas configuradas (Apollo, PDL, Coresignal etc.).'
              }
            >
              <span
                className="ico"
                style={{
                  width: 8,
                  height: 8,
                  borderRadius: 99,
                  background: 'var(--success)'
                }}
              />
              {m === 'people_search' ? 'People Search' : 'API'}
            </div>
          ))}
        </div>

        <div className="sidebar-foot">
          <span className={`pulse ${sidecarOk ? '' : 'off'}`} />
          <div>
            <div style={{ fontWeight: 500, color: 'var(--ink-2)' }}>
              {sidecarOk ? 'Sidecar conectado' : 'Sidecar offline'}
            </div>
            <div style={{ fontFamily: 'var(--mono)', fontSize: 10 }}>
              {sidecarOk ? baseUrl : sidecarError ?? 'inicie o app pelo Electron'}
            </div>
          </div>
        </div>
      </aside>

      {/* Main */}
      <main className="main">
        <div className="toolbar">
          <span className="crumb">
            Workspace <span className="sep">›</span>{' '}
            <strong style={{ fontWeight: 600 }}>
              {view === 'search' ? 'Nova busca' : view === 'leads' ? 'Leads salvos' : 'Workspace'}
            </strong>
          </span>
          <span className="flex-1" />
          <button className="pill-btn">
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
          <button className="pill-btn" onClick={() => setShowSettings(true)}>
            ⚙ Preferências{configuredApiKeyCount ? ` · ${configuredApiKeyCount}` : ''}
          </button>
          <button className="pill-btn primary" onClick={() => setView('search')}>
            + Nova
          </button>
        </div>

        <div style={{ padding: '24px 22px 60px', maxWidth: 1240 }}>
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

          {view === 'leads' ? (
            <SavedLeadsLibrary
              client={client}
              currentLeads={result?.leads ?? []}
              currentKeywords={parseKeywords(form.titles)}
              currentSearchRequest={buildPersistedSearchRequest(form)}
              onFeedback={(kind, message) => setFeedback({ kind, message })}
            />
          ) : (
          <div
            style={{ display: 'grid', gridTemplateColumns: '360px 1fr', gap: 18, alignItems: 'flex-start' }}
          >
            <div style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
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
