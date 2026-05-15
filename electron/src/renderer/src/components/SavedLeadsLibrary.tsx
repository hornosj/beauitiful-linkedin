import { useEffect, useMemo, useState } from 'react'
import { ApiClient, ApiError } from '../../../shared/api'
import type {
  EnrichLeadTableRequest,
  EnrichLeadTableResponse,
  EnrichmentFields,
  EnrichmentProvider,
  ExperimentalSearchResponse,
  Lead,
  SavedLeadTable,
  SavedLeadTableDetail
} from '../../../shared/types'

type PresetValue = 'all' | 'rh' | 'people' | 'tech' | 'sales' | 'marketing' | 'custom'

interface Preset {
  value: PresetValue
  label: string
  aliases: string[]
}

const PRESETS: Preset[] = [
  { value: 'all', label: 'Tabela completa', aliases: [] },
  {
    value: 'rh',
    label: 'Tabela de RH',
    aliases: ['rh', 'recursos humanos', 'human resources']
  },
  {
    value: 'people',
    label: 'Tabela de People',
    aliases: ['people', 'people operations', 'talent', 'talent acquisition']
  },
  {
    value: 'tech',
    label: 'Tabela de Tecnologia',
    aliases: [
      'software',
      'developer',
      'desenvolvedor',
      'engineer',
      'engenharia',
      'tech',
      'tecnologia',
      'backend',
      'frontend',
      'full stack',
      'fullstack',
      'devops'
    ]
  },
  {
    value: 'sales',
    label: 'Tabela de Vendas',
    aliases: ['sales', 'vendas', 'comercial', 'account executive', 'sdr', 'bdr']
  },
  {
    value: 'marketing',
    label: 'Tabela de Marketing',
    aliases: [
      'marketing',
      'growth',
      'demand generation',
      'performance marketing',
      'social media',
      'content',
      'cmo',
      'brand'
    ]
  },
  { value: 'custom', label: 'Tabela customizada', aliases: [] }
]

interface Props {
  client: ApiClient | null
  currentLeads: Lead[]
  currentKeywords: string[]
  currentSearchRequest: Record<string, unknown> | null
  onFeedback(kind: 'error' | 'success', message: string): void
}

type AsyncStatus = 'idle' | 'loading'

export default function SavedLeadsLibrary(props: Props) {
  const { client, currentLeads, currentKeywords, currentSearchRequest, onFeedback } = props
  const [tables, setTables] = useState<SavedLeadTable[]>([])
  const [activeId, setActiveId] = useState<string | null>(null)
  const [activeDetail, setActiveDetail] = useState<SavedLeadTableDetail | null>(null)
  const [status, setStatus] = useState<AsyncStatus>('idle')
  const [selection, setSelection] = useState<Set<string>>(new Set())
  const [query, setQuery] = useState('')
  const [preset, setPreset] = useState<PresetValue>('all')
  const [experimentalRunning, setExperimentalRunning] = useState(false)
  const [experimentalResult, setExperimentalResult] =
    useState<ExperimentalSearchResponse | null>(null)
  const [selectedLeadRefs, setSelectedLeadRefs] = useState<Set<string>>(new Set())
  const [enrichmentOpen, setEnrichmentOpen] = useState(false)
  const [enrichmentFields, setEnrichmentFields] = useState<EnrichmentFields>('email')
  const [enrichmentProviders, setEnrichmentProviders] = useState<Set<EnrichmentProvider>>(
    new Set(['lusha'])
  )
  const [creditCosts, setCreditCosts] = useState<Record<EnrichmentProvider, string>>({
    lusha: '',
    apollo: '',
    snovio: ''
  })
  const [enrichmentKeys, setEnrichmentKeys] = useState({
    lusha_api_key: '',
    apollo_api_key: '',
    snovio_client_id: '',
    snovio_client_secret: ''
  })
  const [apolloWebhookUrl, setApolloWebhookUrl] = useState('')
  const [enrichmentEstimate, setEnrichmentEstimate] =
    useState<EnrichLeadTableResponse | null>(null)
  const [enrichmentRunning, setEnrichmentRunning] = useState(false)

  const refresh = useMemo(
    () => async () => {
      if (!client) return
      setStatus('loading')
      try {
        const list = await client.listLeadTables()
        setTables(list)
        if (activeId && !list.some((t) => t.id === activeId)) {
          setActiveId(null)
          setActiveDetail(null)
        }
      } catch (error) {
        onFeedback('error', formatError(error))
      } finally {
        setStatus('idle')
      }
    },
    [client, activeId, onFeedback]
  )

  useEffect(() => {
    void refresh()
  }, [refresh])

  useEffect(() => {
    if (!client || !activeId) {
      setActiveDetail(null)
      return
    }
    let cancelled = false
    void client
      .getLeadTable(activeId)
      .then((detail) => {
        if (!cancelled) {
          setActiveDetail(detail)
          setSelectedLeadRefs(new Set())
          setEnrichmentEstimate(null)
        }
      })
      .catch((error) => {
        if (!cancelled) onFeedback('error', formatError(error))
      })
    return () => {
      cancelled = true
    }
  }, [client, activeId, onFeedback])

  const filteredLeads = useMemo(() => {
    if (!activeDetail) return []
    let leads = activeDetail.leads
    if (preset !== 'all' && preset !== 'custom') {
      const aliases = (PRESETS.find((p) => p.value === preset)?.aliases ?? []).map(
        (a) => a.toLowerCase()
      )
      leads = leads.filter((lead) => {
        const headline = (lead.title ?? '').toLowerCase()
        return aliases.some((alias) => headline.includes(alias))
      })
    }
    if (!query) return leads
    const needle = query.toLowerCase()
    return leads.filter((lead) =>
      `${lead.person_name ?? ''} ${lead.title ?? ''} ${lead.company_name ?? ''}`
        .toLowerCase()
        .includes(needle)
    )
  }, [activeDetail, query, preset])

  const handleSaveCurrent = async () => {
    if (!client) return
    if (currentLeads.length === 0) {
      onFeedback('error', 'Nenhum lead na busca atual para salvar.')
      return
    }
    const suggested =
      (currentSearchRequest?.company_name as string | undefined) ||
      'Leads salvos'
    const name = window.prompt('Nome da tabela salva:', suggested)
    if (!name || !name.trim()) return
    try {
      const detail = await client.createLeadTable({
        name: name.trim(),
        leads: currentLeads,
        keywords: currentKeywords,
        search_request: currentSearchRequest ?? {},
        generate_queries: true
      })
      onFeedback('success', `Tabela "${detail.table.name}" salva com ${detail.leads.length} leads.`)
      await refresh()
      setActiveId(detail.table.id)
    } catch (error) {
      onFeedback('error', formatError(error))
    }
  }

  const handleImport = async () => {
    if (!client) return
    const filePath = window.prompt(
      'Caminho local do arquivo CSV/XLSX a importar:',
      ''
    )
    if (!filePath || !filePath.trim()) return
    const name = window.prompt('Nome da nova tabela:', 'Tabela importada')
    if (!name || !name.trim()) return
    try {
      const detail = await client.importLeadTable({
        name: name.trim(),
        file_path: filePath.trim()
      })
      onFeedback('success', `Importados ${detail.leads.length} leads em "${detail.table.name}".`)
      await refresh()
      setActiveId(detail.table.id)
    } catch (error) {
      onFeedback('error', formatError(error))
    }
  }

  const handleExport = async () => {
    if (!client || !activeId || !activeDetail) return
    try {
      const response = await client.exportLeadTable(activeId)
      onFeedback('success', `CSV exportado em ${response.output_path}.`)
    } catch (error) {
      onFeedback('error', formatError(error))
    }
  }

  const buildEnrichmentPayload = (confirmed: boolean): EnrichLeadTableRequest | null => {
    const providers = Array.from(enrichmentProviders)
    const leadRefs = Array.from(selectedLeadRefs)
    if (leadRefs.length === 0) {
      onFeedback('error', 'Selecione ao menos um lead para enriquecer.')
      return null
    }
    if (providers.length === 0) {
      onFeedback('error', 'Selecione ao menos uma API de enriquecimento.')
      return null
    }
    const costs = Object.fromEntries(
      Object.entries(creditCosts)
        .map(([provider, value]) => [provider, Number.parseFloat(value || '0')] as const)
        .filter(([, value]) => Number.isFinite(value) && Number(value) >= 0)
    )
    const api_keys = {
      lusha_api_key: enrichmentKeys.lusha_api_key.trim() || undefined,
      apollo_api_key: enrichmentKeys.apollo_api_key.trim() || undefined,
      snovio_client_id: enrichmentKeys.snovio_client_id.trim() || undefined,
      snovio_client_secret: enrichmentKeys.snovio_client_secret.trim() || undefined
    }
    return {
      lead_refs: leadRefs,
      fields: enrichmentFields,
      providers,
      credit_costs_brl: costs,
      confirmed,
      apollo_webhook_url: apolloWebhookUrl.trim() || null,
      api_keys
    }
  }

  const handleEstimateEnrichment = async () => {
    if (!client || !activeId) return
    const payload = buildEnrichmentPayload(false)
    if (!payload) return
    setEnrichmentRunning(true)
    try {
      const response = await client.enrichLeadTable(activeId, payload)
      setEnrichmentEstimate(response)
    } catch (error) {
      onFeedback('error', formatError(error))
    } finally {
      setEnrichmentRunning(false)
    }
  }

  const handleConfirmEnrichment = async () => {
    if (!client || !activeId || !enrichmentEstimate) return
    const total = formatCurrency(enrichmentEstimate.estimate.total_estimated_brl)
    const proceed = window.confirm(
      `Você está prestes a enriquecer ${enrichmentEstimate.estimate.selected_leads} lead(s).\n` +
        `Custo estimado máximo: ${total}.\n\n` +
        'Isso pode consumir créditos reais das APIs selecionadas. Deseja continuar?'
    )
    if (!proceed) return
    const payload = buildEnrichmentPayload(true)
    if (!payload) return
    setEnrichmentRunning(true)
    try {
      const response = await client.enrichLeadTable(activeId, payload)
      setEnrichmentEstimate(response)
      if (response.table) {
        setActiveDetail({ table: response.table, leads: response.leads })
      } else {
        const refreshed = await client.getLeadTable(activeId)
        setActiveDetail(refreshed)
      }
      onFeedback(
        'success',
        `${response.summary.updated_leads} lead(s) atualizados por enriquecimento.`
      )
      await refresh()
    } catch (error) {
      onFeedback('error', formatError(error))
    } finally {
      setEnrichmentRunning(false)
    }
  }

  const handleExperimentalSearch = async () => {
    if (!client || !activeId) return
    const proceed = window.confirm(
      'Essa busca usa buscadores públicos e pode trazer resultados ' +
        'incompletos ou menos precisos. Os novos leads serão comparados com ' +
        'a tabela atual antes de serem adicionados.\n\nContinuar?'
    )
    if (!proceed) return
    setExperimentalRunning(true)
    setExperimentalResult(null)
    try {
      const response = await client.experimentalSearch(activeId)
      setExperimentalResult(response)
      if (response.new_leads.length === 0) {
        onFeedback(
          'success',
          response.note
            ? response.note
            : 'Nenhum lead novo encontrado nos buscadores.'
        )
      } else {
        onFeedback(
          'success',
          `${response.new_leads.length} lead(s) novos adicionados via busca experimental.`
        )
        const refreshed = await client.getLeadTable(activeId)
        setActiveDetail(refreshed)
      }
    } catch (error) {
      onFeedback('error', formatError(error))
    } finally {
      setExperimentalRunning(false)
    }
  }

  const handleDelete = async () => {
    if (!client || !activeId || !activeDetail) return
    if (!window.confirm(`Excluir a tabela "${activeDetail.table.name}"?`)) return
    try {
      await client.deleteLeadTable(activeId)
      onFeedback('success', 'Tabela excluída.')
      setActiveId(null)
      setActiveDetail(null)
      await refresh()
    } catch (error) {
      onFeedback('error', formatError(error))
    }
  }

  const handleMerge = async () => {
    if (!client) return
    if (selection.size < 2) {
      onFeedback('error', 'Selecione ao menos duas tabelas para juntar.')
      return
    }
    const name = window.prompt('Nome da tabela resultante:', 'Tabelas combinadas')
    if (!name || !name.trim()) return
    try {
      const detail = await client.mergeLeadTables({
        name: name.trim(),
        table_ids: Array.from(selection)
      })
      onFeedback(
        'success',
        `Tabela "${detail.table.name}" criada com ${detail.leads.length} leads únicos.`
      )
      setSelection(new Set())
      await refresh()
      setActiveId(detail.table.id)
    } catch (error) {
      onFeedback('error', formatError(error))
    }
  }

  const toggleSelected = (id: string) => {
    setSelection((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const toggleLeadRef = (lead: Lead) => {
    const ref = leadRef(lead)
    if (!ref) return
    setSelectedLeadRefs((prev) => {
      const next = new Set(prev)
      if (next.has(ref)) next.delete(ref)
      else next.add(ref)
      return next
    })
    setEnrichmentEstimate(null)
  }

  const selectFilteredLeads = () => {
    setSelectedLeadRefs(new Set(filteredLeads.map(leadRef).filter(Boolean)))
    setEnrichmentEstimate(null)
  }

  const clearSelectedLeads = () => {
    setSelectedLeadRefs(new Set())
    setEnrichmentEstimate(null)
  }

  const toggleProvider = (provider: EnrichmentProvider) => {
    setEnrichmentProviders((prev) => {
      const next = new Set(prev)
      if (next.has(provider)) next.delete(provider)
      else next.add(provider)
      return next
    })
    setEnrichmentEstimate(null)
  }

  return (
    <div style={{ display: 'grid', gridTemplateColumns: '320px 1fr', gap: 16 }}>
      <aside
        style={{
          borderRadius: 12,
          border: '0.5px solid var(--ink-5, rgba(0,0,0,0.08))',
          padding: 12,
          background: 'var(--surface, #fff)',
          minHeight: 360
        }}
      >
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 12 }}>
          <button className="pill-btn primary" onClick={handleSaveCurrent} disabled={!client}>
            + Salvar busca atual
          </button>
          <button className="pill-btn" onClick={handleImport} disabled={!client}>
            ↥ Importar
          </button>
          <button
            className="pill-btn"
            onClick={handleMerge}
            disabled={!client || selection.size < 2}
            title="Selecione ao menos duas tabelas"
          >
            ⇄ Juntar ({selection.size})
          </button>
        </div>
        <div style={{ fontSize: 11, color: 'var(--ink-3)', marginBottom: 6 }}>
          {status === 'loading' ? 'Carregando…' : `${tables.length} tabelas salvas`}
        </div>
        <ul style={{ listStyle: 'none', padding: 0, margin: 0, display: 'flex', flexDirection: 'column', gap: 6 }}>
          {tables.map((table) => {
            const isActive = table.id === activeId
            return (
              <li
                key={table.id}
                onClick={() => setActiveId(table.id)}
                style={{
                  border: isActive
                    ? '1px solid var(--accent, #007aff)'
                    : '0.5px solid var(--ink-5, rgba(0,0,0,0.08))',
                  borderRadius: 10,
                  padding: 10,
                  cursor: 'pointer',
                  background: isActive ? 'rgba(0,122,255,0.06)' : 'transparent'
                }}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <input
                    type="checkbox"
                    checked={selection.has(table.id)}
                    onChange={(event) => {
                      event.stopPropagation()
                      toggleSelected(table.id)
                    }}
                    onClick={(event) => event.stopPropagation()}
                  />
                  <strong style={{ fontSize: 13 }}>{table.name}</strong>
                  <span style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--ink-3)' }}>
                    {table.lead_count}
                  </span>
                </div>
                <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap', marginTop: 6 }}>
                  {table.keywords.slice(0, 4).map((keyword) => (
                    <span
                      key={keyword}
                      style={{
                        fontSize: 10,
                        padding: '2px 6px',
                        borderRadius: 99,
                        background: 'rgba(0,0,0,0.04)',
                        color: 'var(--ink-2)'
                      }}
                    >
                      {keyword}
                    </span>
                  ))}
                </div>
                <div style={{ marginTop: 6, fontSize: 10, color: 'var(--ink-3)' }}>
                  {table.source_type} · {formatDate(table.updated_at)} · {enrichmentLabel(table.enrichment_status)}
                </div>
              </li>
            )
          })}
          {tables.length === 0 && (
            <li style={{ fontSize: 12, color: 'var(--ink-3)', padding: 12 }}>
              Nenhuma tabela salva ainda. Rode uma busca e clique em "Salvar busca atual".
            </li>
          )}
        </ul>
      </aside>

      <section
        style={{
          borderRadius: 12,
          border: '0.5px solid var(--ink-5, rgba(0,0,0,0.08))',
          padding: 16,
          background: 'var(--surface, #fff)',
          minHeight: 360
        }}
      >
        {!activeDetail ? (
          <div style={{ color: 'var(--ink-3)', fontSize: 13 }}>
            Selecione uma tabela à esquerda para visualizar os leads.
          </div>
        ) : (
          <>
            <header style={{ display: 'flex', alignItems: 'flex-start', gap: 12, marginBottom: 14 }}>
              <div style={{ flex: 1 }}>
                <h2 style={{ margin: 0, fontSize: 18 }}>{activeDetail.table.name}</h2>
                <div style={{ marginTop: 4, fontSize: 12, color: 'var(--ink-3)' }}>
                  {activeDetail.leads.length} leads · {enrichmentLabel(activeDetail.table.enrichment_status)} ·
                  atualizado em {formatDate(activeDetail.table.updated_at)}
                </div>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, marginTop: 6 }}>
                  {activeDetail.table.keywords.map((keyword) => (
                    <span
                      key={keyword}
                      style={{
                        fontSize: 10,
                        padding: '2px 6px',
                        borderRadius: 99,
                        background: 'rgba(0,0,0,0.04)',
                        color: 'var(--ink-2)'
                      }}
                    >
                      {keyword}
                    </span>
                  ))}
                </div>
              </div>
              <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                <button className="pill-btn" onClick={handleExport}>
                  ⤓ Exportar CSV
                </button>
                <button
                  className={enrichmentOpen ? 'pill-btn primary' : 'pill-btn'}
                  onClick={() => setEnrichmentOpen((value) => !value)}
                >
                  ✦ Enriquecer leads
                </button>
                <button
                  className="pill-btn"
                  onClick={handleExperimentalSearch}
                  disabled={experimentalRunning}
                  title="Usa buscadores públicos (SearxNG/DuckDuckGo) para tentar achar mais leads relacionados a essa tabela."
                >
                  {experimentalRunning
                    ? '… buscando'
                    : '⚗ Puxar mais leads (EXPERIMENTAL)'}
                </button>
                <button className="pill-btn" onClick={handleDelete}>
                  🗑 Excluir
                </button>
              </div>
            </header>

            {enrichmentOpen && (
              <div
                style={{
                  marginBottom: 12,
                  padding: 12,
                  borderRadius: 10,
                  border: '0.5px solid var(--ink-5, rgba(0,0,0,0.1))',
                  background: 'var(--surface-2, rgba(0,0,0,0.03))',
                  display: 'grid',
                  gap: 10
                }}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                  <strong style={{ fontSize: 13 }}>Enriquecer leads selecionados</strong>
                  <span style={{ fontSize: 12, color: 'var(--ink-3)' }}>
                    {selectedLeadRefs.size} selecionado(s)
                  </span>
                  <span style={{ flex: 1 }} />
                  <button type="button" className="pill-btn" onClick={selectFilteredLeads}>
                    Selecionar filtrados
                  </button>
                  <button type="button" className="pill-btn" onClick={clearSelectedLeads}>
                    Limpar
                  </button>
                </div>

                <div
                  style={{
                    display: 'grid',
                    gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))',
                    gap: 10
                  }}
                >
                  <label style={fieldLabel}>
                    Dados a buscar
                    <select
                      className="search-input"
                      value={enrichmentFields}
                      onChange={(event) => {
                        setEnrichmentFields(event.target.value as EnrichmentFields)
                        setEnrichmentEstimate(null)
                      }}
                    >
                      <option value="email">Somente e-mail</option>
                      <option value="phone">Somente telefone</option>
                      <option value="both">E-mail + telefone</option>
                    </select>
                  </label>

                  <div style={fieldLabel}>
                    APIs
                    <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                      {(['lusha', 'apollo', 'snovio'] as EnrichmentProvider[]).map((provider) => (
                        <button
                          key={provider}
                          type="button"
                          className={enrichmentProviders.has(provider) ? 'pill-btn primary' : 'pill-btn'}
                          onClick={() => toggleProvider(provider)}
                        >
                          {provider.toUpperCase()}
                        </button>
                      ))}
                    </div>
                  </div>

                  {(['lusha', 'apollo', 'snovio'] as EnrichmentProvider[]).map((provider) => (
                    <label key={provider} style={fieldLabel}>
                      R$ por crédito {provider.toUpperCase()}
                      <input
                        className="search-input"
                        value={creditCosts[provider]}
                        placeholder="0,00"
                        inputMode="decimal"
                        onChange={(event) => {
                          setCreditCosts((prev) => ({ ...prev, [provider]: event.target.value.replace(',', '.') }))
                          setEnrichmentEstimate(null)
                        }}
                      />
                    </label>
                  ))}
                </div>

                <details>
                  <summary style={{ cursor: 'pointer', fontSize: 12, color: 'var(--ink-2)' }}>
                    Chaves e opções avançadas desta execução
                  </summary>
                  <div
                    style={{
                      display: 'grid',
                      gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))',
                      gap: 8,
                      marginTop: 8
                    }}
                  >
                    <SecretInput
                      label="Lusha API key"
                      value={enrichmentKeys.lusha_api_key}
                      onChange={(value) => setEnrichmentKeys((prev) => ({ ...prev, lusha_api_key: value }))}
                    />
                    <SecretInput
                      label="Apollo API key"
                      value={enrichmentKeys.apollo_api_key}
                      onChange={(value) => setEnrichmentKeys((prev) => ({ ...prev, apollo_api_key: value }))}
                    />
                    <SecretInput
                      label="Snov.io client ID"
                      value={enrichmentKeys.snovio_client_id}
                      onChange={(value) => setEnrichmentKeys((prev) => ({ ...prev, snovio_client_id: value }))}
                    />
                    <SecretInput
                      label="Snov.io client secret"
                      value={enrichmentKeys.snovio_client_secret}
                      onChange={(value) => setEnrichmentKeys((prev) => ({ ...prev, snovio_client_secret: value }))}
                    />
                    <label style={fieldLabel}>
                      Webhook Apollo para telefone
                      <input
                        className="search-input"
                        value={apolloWebhookUrl}
                        placeholder="https://..."
                        onChange={(event) => {
                          setApolloWebhookUrl(event.target.value)
                          setEnrichmentEstimate(null)
                        }}
                      />
                    </label>
                  </div>
                </details>

                {enrichmentEstimate && (
                  <div
                    style={{
                      borderRadius: 8,
                      padding: '8px 10px',
                      background: 'rgba(255,149,0,0.09)',
                      color: 'var(--ink-2)',
                      fontSize: 12
                    }}
                  >
                    Estimativa máxima: <strong>{formatCurrency(enrichmentEstimate.estimate.total_estimated_brl)}</strong> ·{' '}
                    {enrichmentEstimate.estimate.total_estimated_credits} crédito(s) estimado(s)
                    {enrichmentEstimate.estimate.provider_estimates.length > 0 && (
                      <div style={{ marginTop: 4 }}>
                        {enrichmentEstimate.estimate.provider_estimates
                          .map((item) => `${item.provider}: ${item.estimated_credits} crédito(s) / ${formatCurrency(item.estimated_brl)}`)
                          .join(' · ')}
                      </div>
                    )}
                    {enrichmentEstimate.estimate.warnings.map((warning) => (
                      <div key={warning} style={{ marginTop: 4 }}>
                        {warning}
                      </div>
                    ))}
                    {enrichmentEstimate.status === 'completed' && (
                      <div style={{ marginTop: 4 }}>
                        Atualizados: {enrichmentEstimate.summary.updated_leads} · provedores usados:{' '}
                        {enrichmentEstimate.summary.providers_used.join(', ') || 'nenhum'}
                      </div>
                    )}
                  </div>
                )}

                <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                  <button
                    type="button"
                    className="pill-btn"
                    onClick={handleEstimateEnrichment}
                    disabled={enrichmentRunning || selectedLeadRefs.size === 0}
                  >
                    Calcular gasto
                  </button>
                  <button
                    type="button"
                    className="pill-btn primary"
                    onClick={handleConfirmEnrichment}
                    disabled={
                      enrichmentRunning ||
                      selectedLeadRefs.size === 0 ||
                      !enrichmentEstimate ||
                      enrichmentEstimate.status === 'completed'
                    }
                  >
                    {enrichmentRunning ? 'Enriquecendo…' : 'Confirmar e enriquecer'}
                  </button>
                </div>
              </div>
            )}

            <div
              style={{
                display: 'flex',
                gap: 6,
                flexWrap: 'wrap',
                marginBottom: 10
              }}
            >
              {PRESETS.map((option) => (
                <button
                  key={option.value}
                  type="button"
                  onClick={() => setPreset(option.value)}
                  className={preset === option.value ? 'pill-btn primary' : 'pill-btn'}
                >
                  {option.label}
                </button>
              ))}
            </div>

            {experimentalResult && (
              <div
                style={{
                  marginBottom: 10,
                  padding: '8px 10px',
                  borderRadius: 8,
                  background: 'rgba(0,122,255,0.06)',
                  fontSize: 12,
                  color: 'var(--ink-2)'
                }}
              >
                Busca experimental: {experimentalResult.candidates_total}{' '}
                candidato(s) ·{' '}
                {experimentalResult.duplicates_skipped} ignorado(s) por
                duplicidade ·{' '}
                <strong>{experimentalResult.new_leads.length} novo(s)</strong>
                {experimentalResult.engines_used.length > 0 && (
                  <> · buscadores: {experimentalResult.engines_used.join(', ')}</>
                )}
                {experimentalResult.note && (
                  <div style={{ marginTop: 4 }}>{experimentalResult.note}</div>
                )}
              </div>
            )}

            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Filtrar por nome, cargo ou empresa"
              style={{
                width: '100%',
                padding: '8px 10px',
                border: '0.5px solid var(--ink-5, rgba(0,0,0,0.12))',
                borderRadius: 8,
                marginBottom: 12,
                fontSize: 13
              }}
            />

            <div style={{ overflowX: 'auto' }}>
              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
                <thead>
                  <tr style={{ textAlign: 'left', color: 'var(--ink-3)' }}>
                    <th style={{ ...th, width: 42 }}></th>
                    <th style={th}>Pessoa</th>
                    <th style={th}>Cargo</th>
                    <th style={th}>Contato</th>
                    <th style={th}>Empresa</th>
                    <th style={th}>Score</th>
                    <th style={th}>LinkedIn</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredLeads.map((lead, idx) => (
                    <tr key={`${lead.linkedin_url ?? lead.source_url}:${idx}`}>
                      <td style={td}>
                        <input
                          type="checkbox"
                          checked={selectedLeadRefs.has(leadRef(lead))}
                          onChange={() => toggleLeadRef(lead)}
                          aria-label={`Selecionar ${lead.person_name ?? 'lead'}`}
                        />
                      </td>
                      <td style={td}>{lead.person_name ?? '—'}</td>
                      <td style={td}>{lead.title ?? '—'}</td>
                      <td style={td}>
                        <div>{lead.email ?? '—'}</div>
                        {lead.phone && <div style={{ color: 'var(--ink-3)' }}>{lead.phone}</div>}
                      </td>
                      <td style={td}>{lead.company_name}</td>
                      <td style={td}>{lead.confidence_score}</td>
                      <td style={td}>
                        {lead.linkedin_url ? (
                          <a href={lead.linkedin_url} target="_blank" rel="noreferrer">
                            abrir ↗
                          </a>
                        ) : (
                          '—'
                        )}
                      </td>
                    </tr>
                  ))}
                  {filteredLeads.length === 0 && (
                    <tr>
                      <td colSpan={7} style={{ padding: 12, color: 'var(--ink-3)', textAlign: 'center' }}>
                        Nenhum lead corresponde ao filtro.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </>
        )}
      </section>
    </div>
  )
}

const th: React.CSSProperties = {
  padding: '8px 10px',
  borderBottom: '0.5px solid var(--ink-5, rgba(0,0,0,0.08))',
  fontWeight: 500
}

const td: React.CSSProperties = {
  padding: '8px 10px',
  borderBottom: '0.5px solid var(--ink-5, rgba(0,0,0,0.04))'
}

const fieldLabel: React.CSSProperties = {
  display: 'grid',
  gap: 4,
  fontSize: 11,
  color: 'var(--ink-3)'
}

function SecretInput(props: {
  label: string
  value: string
  onChange(value: string): void
}) {
  return (
    <label style={fieldLabel}>
      {props.label}
      <input
        className="search-input"
        type="password"
        value={props.value}
        autoComplete="off"
        onChange={(event) => props.onChange(event.target.value)}
      />
    </label>
  )
}

function leadRef(lead: Lead): string {
  return lead.linkedin_url || lead.source_url || lead.person_name || ''
}

function enrichmentLabel(status: string): string {
  if (status === 'enriched') return 'enriquecido'
  if (status === 'not_implemented') return 'enriquecimento pendente'
  return 'sem enriquecimento'
}

function formatDate(iso: string): string {
  try {
    return new Date(iso).toLocaleString()
  } catch {
    return iso
  }
}

function formatCurrency(value: number): string {
  return new Intl.NumberFormat('pt-BR', {
    style: 'currency',
    currency: 'BRL'
  }).format(Number.isFinite(value) ? value : 0)
}

function formatError(error: unknown): string {
  if (error instanceof ApiError) return `${error.status}: ${error.message}`
  if (error instanceof Error) return error.message
  return 'Erro desconhecido.'
}
