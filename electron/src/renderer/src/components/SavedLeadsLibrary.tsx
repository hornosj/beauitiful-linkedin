import React, { useEffect, useMemo, useRef, useState } from 'react'
import { ApiClient, ApiError } from '../../../shared/api'
import type {
  EnrichLeadTableRequest,
  EnrichLeadTableResponse,
  EnrichmentFields,
  EnrichmentPricingItem,
  EnrichmentProvider,
  EnrichmentProviderRunLog,
  ExperimentalSearchResponse,
  InternalEnrichField,
  Lead,
  SavedLeadTable,
  SavedLeadTableDetail
} from '../../../shared/types'
import { useEnrichmentRunner } from '../enrichment/EnrichmentRunnerContext'
import ChromeBootstrapModal from './ChromeBootstrapModal'

function parseArrayField<T>(field: any): T[] {
  if (Array.isArray(field)) return field
  if (typeof field === 'string') {
    try {
      const parsed = JSON.parse(field)
      return Array.isArray(parsed) ? parsed : []
    } catch {
      return []
    }
  }
  return []
}

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

/**
 * Gera um nome de arquivo seguro a partir do nome da tabela, usado apenas como
 * sugestão inicial no diálogo nativo "Salvar como" do export de CSV.
 */
export function exportFileSlug(name: string): string {
  const slug = (name || '')
    .normalize('NFKD')
    .replace(/[̀-ͯ]/g, '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
  return slug || 'leads'
}

interface EnrichmentNotice {
  id: string
  kind: 'success' | 'warning' | 'error'
  title: string
  message: string
  cost: number
  provider?: string
}






type LibraryDialogState =
  | {
      kind: 'text'
      title: string
      label: string
      defaultValue: string
      confirmLabel: string
      resolve(value: string | null): void
    }
  | {
      kind: 'confirm'
      title: string
      message: string
      confirmLabel: string
      danger?: boolean
      resolve(value: boolean): void
    }

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
  const [profileValidationRunning, setProfileValidationRunning] = useState(false)
  const [profileValidationBootstrap, setProfileValidationBootstrap] = useState<{
    refs: string[]
    targetUrl: string
    targetSlug: string
  } | null>(null)
  const [experimentalResult, setExperimentalResult] =
    useState<ExperimentalSearchResponse | null>(null)
  const [selectedLeadRefs, setSelectedLeadRefs] = useState<Set<string>>(new Set())
  const [enrichmentOpen, setEnrichmentOpen] = useState(false)
  const [enrichmentFields, setEnrichmentFields] = useState<EnrichmentFields>('email')
  // Defaults: Lusha + Apollo + PDL (chaves disponíveis em .env). Snov.io é opt-in.
  const [enrichmentProviders, setEnrichmentProviders] = useState<Set<EnrichmentProvider>>(
    new Set(['lusha', 'apollo', 'pdl'])
  )
  // R$ por crédito — pricing canônico vem do servidor (GET /enrichment/pricing).
  // As APIs públicas não expõem preço; o servidor tabela os valores e permite
  // override via env (ENRICHMENT_COST_BRL_<PROVIDER>). UI sempre read-only.
  const [pricing, setPricing] = useState<EnrichmentPricingItem[]>([])
  const creditCosts = useMemo<Record<EnrichmentProvider, string>>(() => {
    const fallback: Record<EnrichmentProvider, string> = {
      lusha: '0',
      apollo: '0',
      snovio: '0',
      pdl: '0'
    }
    for (const item of pricing) {
      if (item.provider in fallback) {
        fallback[item.provider as EnrichmentProvider] = String(item.brl_per_credit)
      }
    }
    return fallback
  }, [pricing])
  const [apolloWebhookUrl, setApolloWebhookUrl] = useState('')
  const [enrichmentEstimate, setEnrichmentEstimate] =
    useState<EnrichLeadTableResponse | null>(null)
  const [enrichmentRunning, setEnrichmentRunning] = useState(false)
  const enricher = useEnrichmentRunner()
  const internalEnrichRunning =
    enricher.run?.running === true && enricher.run.meta.tableId === activeId
  const internalRunningFields = internalEnrichRunning
    ? enricher.run?.meta.fields ?? 'email'
    : null
  const [enrichmentNotices, setEnrichmentNotices] = useState<EnrichmentNotice[]>([])
  // Auto-dismiss bookkeeping for the enrichment notices. Without a timer
  // these banners stayed on screen indefinitely (só sumiam ao trocar de
  // tabela ou iniciar outra run), o que dava a impressão de "alerta que
  // não some". Guardamos os timers num ref para limpá-los no unmount e
  // evitar setState em componente desmontado.
  const noticeTimersRef = useRef<number[]>([])
  const [inspectedApiLead, setInspectedApiLead] = useState<Lead | null>(null)
  const [dialog, setDialog] = useState<LibraryDialogState | null>(null)
  const dialogActiveRef = useRef(false)
  // 'cascade' tenta o provider mais barato primeiro e pergunta antes de
  // passar para o próximo. 'parallel' dispara todos os providers selecionados
  // de uma vez (gasta o orçamento cheio mesmo quando o barato já resolveu).
  const [enrichmentMode, setEnrichmentMode] = useState<'cascade' | 'parallel'>('cascade')

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

  // Limpa os timers de auto-dismiss das notices ao desmontar para não
  // chamar setState depois que o componente saiu da árvore.
  useEffect(
    () => () => {
      noticeTimersRef.current.forEach((timer) => window.clearTimeout(timer))
      noticeTimersRef.current = []
    },
    []
  )

  const askText = (
    title: string,
    label: string,
    defaultValue: string,
    confirmLabel = 'Confirmar'
  ) =>
    new Promise<string | null>((resolve) => {
      dialogActiveRef.current = true
      setDialog({ kind: 'text', title, label, defaultValue, confirmLabel, resolve })
    })

  const askConfirm = (
    title: string,
    message: string,
    confirmLabel = 'Confirmar',
    danger = false
  ) =>
    new Promise<boolean>((resolve) => {
      dialogActiveRef.current = true
      setDialog({ kind: 'confirm', title, message, confirmLabel, danger, resolve })
    })

  const closeDialog = (value: string | boolean | null) => {
    if (!dialog || !dialogActiveRef.current) return
    dialogActiveRef.current = false
    if (dialog.kind === 'text') {
      dialog.resolve(typeof value === 'string' ? value : null)
    } else {
      dialog.resolve(value === true)
    }
    setDialog(null)
  }

  useEffect(() => {
    void refresh()
  }, [refresh])

  useEffect(() => {
    if (!client) return
    let cancelled = false
    void client
      .getEnrichmentPricing()
      .then((response) => {
        if (!cancelled) setPricing(response.items)
      })
      .catch(() => {
        // pricing endpoint is best-effort — UI just won't show R$ values
      })
    return () => {
      cancelled = true
    }
  }, [client])

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
          setInspectedApiLead(null)
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
    const name = await askText('Salvar busca atual', 'Nome da tabela salva', suggested, 'Salvar')
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
    const filePath = await askText(
      'Importar leads',
      'Caminho local do arquivo CSV/XLSX',
      '',
      'Continuar'
    )
    if (!filePath || !filePath.trim()) return
    const name = await askText('Importar leads', 'Nome da nova tabela', 'Tabela importada', 'Importar')
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
      const defaultName = `${exportFileSlug(activeDetail.table.name)}.csv`
      const picker = window.beautifulLinkedIn?.saveCsvDialog
      const choice = picker ? await picker(defaultName) : null
      // Sem o bridge nativo (ex.: ambiente de teste) cai no comportamento antigo.
      if (choice && choice.canceled) return
      const outputPath = choice?.filePath ?? undefined
      const response = await client.exportLeadTable(
        activeId,
        outputPath ? { output_path: outputPath } : undefined
      )
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
    // Chaves de API e pricing (R$/crédito) vêm do .env do servidor; a UI
    // nunca envia overrides — o backend é a fonte da verdade.
    return {
      lead_refs: leadRefs,
      fields: enrichmentFields,
      providers,
      confirmed,
      apollo_webhook_url: apolloWebhookUrl.trim() || null
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
    if (!client || !activeId) return
    if (selectedLeadRefs.size === 0) {
      onFeedback('error', 'Selecione ao menos um lead para enriquecer.')
      return
    }
    if (enrichmentProviders.size === 0) {
      onFeedback('error', 'Selecione ao menos uma API de enriquecimento.')
      return
    }
    if (enrichmentMode === 'parallel') {
      await runParallelEnrichment()
    } else {
      await runCascadeEnrichment()
    }
  }

  const runParallelEnrichment = async () => {
    if (!client || !activeId || !enrichmentEstimate) {
      onFeedback('error', 'Calcule o gasto antes de confirmar (modo paralelo).')
      return
    }
    const payload = buildEnrichmentPayload(true)
    if (!payload) return
    setEnrichmentNotices([])
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
        `${response.summary.updated_leads} lead(s) atualizados por enriquecimento pago.`
      )
      pushProviderNotices(response.summary.provider_logs)
      await refresh()
    } catch (error) {
      onFeedback('error', formatError(error))
    } finally {
      setEnrichmentRunning(false)
    }
  }

  const runCascadeEnrichment = async () => {
    if (!client || !activeId) return
    const ordered = sortProvidersByCost(
      Array.from(enrichmentProviders),
      creditCosts,
      enrichmentFields
    )
    if (ordered.length === 0) {
      onFeedback(
        'error',
        'Nenhum provider compatível com o tipo de dado selecionado. (Snov.io só faz e-mail.)'
      )
      return
    }
    let pending = Array.from(selectedLeadRefs)
    let totalSpent = 0
    let updatedTotal = 0
    const stagesUsed: string[] = []
    setEnrichmentNotices([])
    setEnrichmentRunning(true)
    try {
      for (let i = 0; i < ordered.length; i += 1) {
        const provider = ordered[i]
        if (pending.length === 0) break

        const stageBase = {
          lead_refs: pending,
          fields: enrichmentFields,
          providers: [provider],
          apollo_webhook_url: apolloWebhookUrl.trim() || null
        }

        const estimateResp = await client.enrichLeadTable(activeId, {
          ...stageBase,
          confirmed: false
        })
        const stageCost = estimateResp.estimate.total_estimated_brl

        const runResp = await client.enrichLeadTable(activeId, {
          ...stageBase,
          confirmed: true
        })
        totalSpent += stageCost
        updatedTotal += runResp.summary.updated_leads
        stagesUsed.push(provider)
        setEnrichmentEstimate(runResp)
        if (runResp.table) {
          setActiveDetail({ table: runResp.table, leads: runResp.leads })
        }
        const stageErrors = runResp.summary.errors
        pushProviderNotices(runResp.summary.provider_logs)
        if (stageErrors.length > 0) {
          onFeedback(
            'error',
            `${provider.toUpperCase()}: ${stageErrors.join(' | ')}`
          )
        }
        pending = filterPending(runResp.leads, new Set(pending), enrichmentFields)
        onFeedback(
          'success',
          `${provider.toUpperCase()}: ${formatCurrency(stageCost)} estimado · ` +
            `+${runResp.summary.updated_leads} atualizado(s) · restam ${pending.length}.`
        )
      }
      onFeedback(
        'success',
        `Cascata finalizada. Estágios: ${
          stagesUsed.map((p) => p.toUpperCase()).join(' → ') || 'nenhum'
        }. Atualizados: ${updatedTotal}. Pendentes: ${pending.length}. ` +
          `Gasto total: ${formatCurrency(totalSpent)}.`
      )
      await refresh()
    } catch (error) {
      onFeedback('error', formatError(error))
    } finally {
      setEnrichmentRunning(false)
    }
  }

  const handleInternalEnrich = async (fields: InternalEnrichField = 'email') => {
    if (!client || !activeId || !activeDetail) return
    const refs =
      selectedLeadRefs.size > 0 ? Array.from(selectedLeadRefs) : undefined
    const targetLeads = refs
      ? activeDetail.leads.filter((lead) => refs.includes(leadRef(lead)))
      : activeDetail.leads
    const targetCount = refs?.length ?? activeDetail.leads.length
    if (targetCount === 0) {
      onFeedback('error', 'Nenhum lead disponível para enriquecer.')
      return
    }
    let companyDomain = domainFromSearchRequest(activeDetail.table.search_request)
    const missingDomainCount = targetLeads.filter((lead) => !cleanDomain(lead.company_domain)).length
    if (missingDomainCount > 0 && !companyDomain) {
      const answer = await askText(
        'Domínio da empresa',
        `${missingDomainCount} lead(s) não têm domínio. Informe o domínio corporativo para tentar ${internalFieldObjectLabel(fields)}.`,
        suggestDomainFromCompanyName(activeDetail.table.name || targetLeads[0]?.company_name),
        'Continuar'
      )
      companyDomain = cleanDomain(answer)
      if (!companyDomain) {
        onFeedback(
          'error',
          `Para achar ${internalFieldObjectLabel(fields)}, informe um domínio corporativo válido (ex.: empresa.com.br).`
        )
        return
      }
    }
    const proceed = await askConfirm(
      'Enriquecimento interno',
      `${internalEnrichDescription(fields)} ` +
        `Leads alvo: ${refs ? refs.length : 'todos da tabela'}. ` +
        `${companyDomain ? `Domínio usado quando faltar nos leads: ${companyDomain}. ` : ''}` +
        `Não sobrescreve contatos existentes. Você pode continuar navegando enquanto roda.`,
      'Enriquecer em background'
    )
    if (!proceed) return

    enricher.start({
      tableId: activeId,
      tableName: activeDetail.table.name,
      leadRefs: refs,
      totalLeads: targetCount,
      companyDomain,
      fields
    })
  }

  const runLinkedInProfileValidation = async (refs: string[], cdpEndpoint?: string) => {
    if (!client || !activeId) return
    setProfileValidationRunning(true)
    try {
      const response = await client.validateLinkedInProfiles(activeId, {
        lead_refs: refs,
        max_leads: 40,
        ...(cdpEndpoint ? { cdp_endpoint: cdpEndpoint } : {})
      })
      if (response.table) {
        setActiveDetail({ table: response.table, leads: response.leads })
      } else {
        const refreshed = await client.getLeadTable(activeId)
        setActiveDetail(refreshed)
      }
      onFeedback(
        'success',
        `Validação LinkedIn concluída: ${response.summary.validated_leads} lead(s) com dados atualizados.`
      )
      await refresh()
    } catch (error) {
      onFeedback('error', formatError(error))
    } finally {
      setProfileValidationRunning(false)
    }
  }

  const handleLinkedInProfileValidation = async () => {
    if (!client || !activeId || !activeDetail) return
    const refs = Array.from(selectedLeadRefs)
    if (refs.length === 0) {
      onFeedback('error', 'Selecione ao menos um lead para validar LinkedIn.')
      return
    }
    if (refs.length > 40) {
      onFeedback('error', 'Validar LinkedIn aceita no máximo 40 leads por rodada.')
      return
    }
    const proceed = await askConfirm(
      'Validar LinkedIn',
      `O app vai abrir cada perfil selecionado via Chrome CDP, ler a experiência atual e a área de informações de contato. Leads alvo: ${refs.length}. Não sobrescreve cargo, empresa, e-mail ou telefone já salvos.`,
      'Validar LinkedIn'
    )
    if (!proceed) return

    const embedded = window.beautifulLinkedIn?.embeddedBrowser
    if (embedded) {
      const session = await embedded.checkSession()
      if (!session.hasLiAt || !session.hasJsessionid) {
        onFeedback(
          'error',
          'Clique em "Logar LinkedIn" na barra superior e conclua o login antes de validar perfis.'
        )
        return
      }
      void runLinkedInProfileValidation(refs, 'http://127.0.0.1:9223')
      return
    }

    const firstTargetLead = activeDetail.leads.find(
      (lead) => refs.includes(leadRef(lead)) && profileExperienceUrl(lead.linkedin_url)
    )
    const targetUrl = profileExperienceUrl(firstTargetLead?.linkedin_url)
    const targetSlug = targetUrl ? linkedinProfilePath(targetUrl) : null
    if (targetUrl && targetSlug) {
      setProfileValidationBootstrap({ refs, targetUrl, targetSlug })
      return
    }

    void runLinkedInProfileValidation(refs)
  }

  // When a run finishes for the table currently open, refresh the leads
  // without forcing the user to switch away and back.
  useEffect(() => {
    const unsubscribe = enricher.onCompleted((meta, done) => {
      if (meta.tableId !== activeId) return
      if (done.table) {
        setActiveDetail({ table: done.table, leads: done.leads })
      } else if (client) {
        void client
          .getLeadTable(meta.tableId)
          .then(setActiveDetail)
          .catch(() => undefined)
      }
      void refresh()
    })
    return unsubscribe
  }, [enricher, activeId, client, refresh])

  const handleExperimentalSearch = async () => {
    if (!client || !activeId) return
    const proceed = await askConfirm(
      'Puxar mais leads',
      'Essa busca usa buscadores públicos e pode trazer resultados incompletos ou menos precisos. Os novos leads serão comparados com a tabela atual antes de serem adicionados.',
      'Buscar leads'
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
    const proceed = await askConfirm(
      'Excluir tabela',
      `Excluir a tabela "${activeDetail.table.name}"? Esta ação remove a tabela salva localmente.`,
      'Excluir',
      true
    )
    if (!proceed) return
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
    const name = await askText('Juntar tabelas', 'Nome da tabela resultante', 'Tabelas combinadas', 'Juntar')
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
    setInspectedApiLead(null)
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

  // Remove a single notice (manual ✕ ou expiração do timer). Idempotente:
  // se o id já saiu por slice/dismiss, o filter é um no-op.
  const dismissNotice = (id: string) => {
    setEnrichmentNotices((prev) => prev.filter((notice) => notice.id !== id))
  }

  const pushProviderNotices = (logs: EnrichmentProviderRunLog[]) => {
    if (logs.length === 0) return
    const nextNotices: EnrichmentNotice[] = logs.map((log) => {
      const kind: EnrichmentNotice['kind'] =
        log.status === 'error' ? 'error' : log.status === 'no_data' ? 'warning' : 'success'
      return {
        id: `${Date.now()}:${log.provider}:${Math.random().toString(16).slice(2)}`,
        kind,
        title:
          log.status === 'no_data'
            ? `${log.provider.toUpperCase()} consultou e não encontrou`
            : `${log.provider.toUpperCase()} utilizado`,
        message: log.message,
        cost: log.estimated_brl,
        provider: log.provider
      }
    })
    setEnrichmentNotices((prev) => [...nextNotices, ...prev].slice(0, 6))
    // Auto-dismiss: erros ficam mais tempo (info técnica), sucesso some
    // antes. Mesma filosofia do toast global. O timer apenas remove o
    // próprio id, então é seguro mesmo após slice/dismiss manual.
    for (const notice of nextNotices) {
      const ttl = notice.kind === 'error' ? 9000 : notice.kind === 'warning' ? 7000 : 5000
      const timer = window.setTimeout(() => dismissNotice(notice.id), ttl)
      noticeTimersRef.current.push(timer)
    }
  }

  return (
    <div className="saved-library grid grid-cols-1 lg:grid-cols-[320px_minmax(0,1fr)] gap-6 min-w-0">
      <aside className="saved-library-sidebar rounded-xl border border-line bg-surface p-3 min-h-[360px] flex flex-col min-w-0">
        <div className="flex gap-1.5 flex-wrap mb-3">
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
        <div className="text-[11px] text-ink-3 mb-1.5">
          {status === 'loading' ? 'Carregando…' : `${tables.length} tabelas salvas`}
        </div>
        <ul className="list-none p-0 m-0 flex flex-col gap-1.5 overflow-y-auto">
          {tables.map((table) => {
            const isActive = table.id === activeId
            return (
              <li
                key={table.id}
                className={`rounded-[10px] p-2.5 transition-all duration-200 border ${isActive ? 'border-accent bg-accent/5' : 'border-line hover:bg-surface-2'}`}
              >
                <div className="flex items-start gap-2 min-w-0">
                  <button
                    type="button"
                    aria-pressed={selection.has(table.id)}
                    aria-label={`Selecionar tabela ${table.name}`}
                    onClick={(event) => {
                      event.stopPropagation()
                      toggleSelected(table.id)
                    }}
                    className={`saved-table-check ${selection.has(table.id) ? 'on' : ''}`}
                  />
                  <button
                    type="button"
                    className="min-w-0 flex-1 text-left bg-transparent border-0 font-sans cursor-pointer"
                    aria-current={isActive ? 'page' : undefined}
                    onClick={() => setActiveId(table.id)}
                  >
                    <div className="flex items-center gap-2 min-w-0">
                      <strong className="text-[13px] text-ink font-semibold truncate min-w-0">{table.name}</strong>
                      <span className="ml-auto text-[11px] text-ink-3 font-mono">
                        {table.lead_count}
                      </span>
                    </div>
                    <div className="flex gap-1 flex-wrap mt-1.5">
                      {table.keywords.slice(0, 4).map((keyword) => (
                        <span
                          key={keyword}
                          className="text-[10px] px-1.5 py-0.5 rounded-full bg-surface-3 text-ink-2"
                        >
                          {keyword}
                        </span>
                      ))}
                    </div>
                    <div className="mt-1.5 text-[10px] text-ink-3 leading-tight">
                      {table.source_type} · {formatRelativeDate(table.updated_at)} · {enrichmentLabel(table.enrichment_status)}
                    </div>
                  </button>
                </div>
              </li>
            )
          })}
          {tables.length === 0 && (
            <li className="library-empty-mini" aria-live="polite">
              <div className="library-empty-mini-icon" aria-hidden="true">◴</div>
              <div className="library-empty-mini-text">
                Nenhuma tabela salva
                <span>Rode uma busca e clique em <strong>Salvar busca atual</strong></span>
              </div>
            </li>
          )}
        </ul>
      </aside>

      <section className="saved-library-panel rounded-xl border border-line bg-surface p-4 min-h-[360px] flex flex-col relative min-w-0">
        {!activeDetail ? (
          <div className="library-empty" role="status" aria-live="polite">
            <div className="library-empty-art" aria-hidden="true">
              <svg width="56" height="56" viewBox="0 0 56 56" fill="none">
                <rect x="8" y="14" width="40" height="32" rx="6" stroke="currentColor" strokeWidth="1.2" opacity="0.55" />
                <path d="M14 22h28M14 28h22M14 34h18M14 40h12" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" opacity="0.45" />
                <circle cx="42" cy="40" r="7" stroke="currentColor" strokeWidth="1.4" fill="var(--surface)" />
                <path d="M42 37v6M39 40h6" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
              </svg>
            </div>
            <h3 className="library-empty-title">
              {tables.length === 0
                ? 'Nenhuma tabela ainda'
                : 'Selecione uma tabela para começar'}
            </h3>
            <p className="library-empty-sub">
              {tables.length === 0
                ? 'Rode uma busca em Nova busca e clique em "+ Salvar busca atual" para guardar a tabela aqui.'
                : 'Escolha uma tabela na lista à esquerda para visualizar leads, enriquecer e exportar.'}
            </p>
            {tables.length === 0 && currentLeads.length > 0 && (
              <button
                type="button"
                className="pill-btn primary"
                onClick={handleSaveCurrent}
                disabled={!client}
              >
                + Salvar busca atual ({currentLeads.length} leads)
              </button>
            )}
          </div>
        ) : (
          <div key={activeDetail.table.id} className="saved-library-detail animate-fade-in-up flex flex-col h-full duration-300">
            <header className="flex flex-col gap-3 mb-4 min-w-0">
              <div className="max-w-full min-w-0">
                <h2 className="m-0 text-[20px] font-semibold text-ink tracking-tight break-words">{activeDetail.table.name}</h2>
                <div className="mt-1 text-[13px] text-ink-3 flex flex-wrap items-center gap-1.5">
                  <span className="font-medium text-ink-2">{activeDetail.leads.length} leads</span>
                  <span>·</span>
                  <span>{enrichmentLabel(activeDetail.table.enrichment_status)}</span>
                  <span>·</span>
                  <span title={formatDate(activeDetail.table.updated_at)}>
                    atualizado {formatRelativeDate(activeDetail.table.updated_at)}
                  </span>
                </div>
                <div className="flex flex-wrap gap-1.5 mt-2.5">
                  {activeDetail.table.keywords.map((keyword) => (
                    <span
                      key={keyword}
                      className="text-[10px] px-1.5 py-0.5 rounded-full bg-surface-3 text-ink-2"
                    >
                      {keyword}
                    </span>
                  ))}
                </div>
              </div>
              <div className="library-action-bar">
                {/* Grupo 1 — Enriquecimento (CTAs principais) */}
                <button
                  type="button"
                  className="pill-btn primary"
                  onClick={() => void handleInternalEnrich('email')}
                  disabled={internalEnrichRunning || !activeDetail}
                  title="Inferência local de e-mail profissional (padrões + MX + SMTP). Grátis, não usa API paga."
                >
                  {internalEnrichRunning && internalRunningFields === 'email' ? (
                    <>
                      <span className="enrich-inline-dot" aria-hidden="true" />
                      Procurando e-mails…
                    </>
                  ) : (
                    <>✉ Achar e-mails {selectedLeadRefs.size > 0 ? `(${selectedLeadRefs.size})` : ''}</>
                  )}
                </button>

                <div className="action-group-divider" aria-hidden="true" />

                {/* Grupo 2 — Validação */}
                <button
                  type="button"
                  className="pill-btn"
                  onClick={() => void handleLinkedInProfileValidation()}
                  disabled={profileValidationRunning || !activeDetail}
                  title="Valida o LinkedIn: cargo real na aba Experiência e dados do modal Informações de contato. Usa Chrome CDP já logado."
                >
                  {profileValidationRunning
                    ? 'Validando LinkedIn…'
                    : `Validar LinkedIn ${selectedLeadRefs.size > 0 ? `(${selectedLeadRefs.size})` : ''}`}
                </button>

                <div className="action-group-spacer" aria-hidden="true" />
                <div className="action-group-divider" aria-hidden="true" />

                {/* Grupo 3 — Tabela / utilitários / destrutivos */}
                <button
                  type="button"
                  className={enrichmentOpen ? 'pill-btn primary' : 'pill-btn'}
                  onClick={() => setEnrichmentOpen((value) => !value)}
                  title="Enriquecimento via APIs pagas (Apollo, Lusha, PDL, Snov)."
                >
                  ✦ APIs pagas
                </button>
                <button
                  type="button"
                  className="pill-btn"
                  onClick={handleExperimentalSearch}
                  disabled={experimentalRunning}
                  title="Usa buscadores públicos (SearxNG/DuckDuckGo) para tentar achar mais leads relacionados a essa tabela."
                >
                  {experimentalRunning ? '… buscando' : '⚗ Puxar mais leads'}
                </button>
                <button className="pill-btn" type="button" onClick={handleExport}>
                  ⤓ Exportar CSV
                </button>
                <button className="pill-btn danger" type="button" onClick={handleDelete}>
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
                    gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))',
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
                    </select>
                  </label>

                  <label style={fieldLabel}>
                    Modo de execução
                    <select
                      className="search-input"
                      value={enrichmentMode}
                      onChange={(event) => {
                        setEnrichmentMode(event.target.value as 'cascade' | 'parallel')
                        setEnrichmentEstimate(null)
                      }}
                    >
                      <option value="cascade">
                        Cascata — tenta a API mais barata primeiro
                      </option>
                      <option value="parallel">
                        Paralelo — chama todas as APIs ao mesmo tempo
                      </option>
                    </select>
                  </label>

                  <div style={{ ...fieldLabel, minWidth: 0 }}>
                    APIs
                    <div
                      style={{
                        display: 'flex',
                        gap: 6,
                        flexWrap: 'wrap',
                        alignItems: 'center',
                        minHeight: 32
                      }}
                    >
                      {(['lusha', 'apollo', 'snovio', 'pdl'] as EnrichmentProvider[]).map((provider) => (
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
                </div>

                <div
                  style={{
                    display: 'grid',
                    gap: 6
                  }}
                >
                  <div
                    style={{
                      display: 'flex',
                      alignItems: 'baseline',
                      gap: 8,
                      fontSize: 11,
                      color: 'var(--ink-3)'
                    }}
                  >
                    <strong style={{ fontSize: 11, color: 'var(--ink-2)' }}>
                      Tabela de R$/crédito
                    </strong>
                    <span>
                      tabelada pelo servidor — para ajustar, defina{' '}
                      <code>ENRICHMENT_COST_BRL_&lt;PROVIDER&gt;</code> no <code>.env</code>
                    </span>
                  </div>
                  <div
                    style={{
                      display: 'grid',
                      gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))',
                      gap: 8
                    }}
                  >
                    {(['lusha', 'apollo', 'snovio', 'pdl'] as EnrichmentProvider[]).map((provider) => {
                      const item = pricing.find((p) => p.provider === provider)
                      const cost = item ? item.brl_per_credit : Number.parseFloat(creditCosts[provider]) || 0
                      const isOverride = item?.source === 'env_override'
                      return (
                        <div
                          key={provider}
                          style={{
                            display: 'flex',
                            alignItems: 'center',
                            justifyContent: 'space-between',
                            gap: 8,
                            padding: '6px 10px',
                            borderRadius: 8,
                            border: '0.5px solid var(--ink-5, rgba(0,0,0,0.1))',
                            background: 'var(--surface-1, rgba(0,0,0,0.02))',
                            minWidth: 0
                          }}
                          title={
                            item
                              ? `${isOverride ? 'Override via env' : 'Default do servidor'} — ${item.env_var}`
                              : 'Carregando pricing do servidor…'
                          }
                        >
                          <span style={{ fontSize: 11, color: 'var(--ink-3)' }}>
                            {provider.toUpperCase()}
                          </span>
                          <span
                            style={{
                              fontSize: 13,
                              fontVariantNumeric: 'tabular-nums',
                              color: 'var(--ink-1)',
                              fontWeight: 500
                            }}
                          >
                            {formatCurrency(cost)}
                            {isOverride && (
                              <span
                                style={{
                                  marginLeft: 6,
                                  fontSize: 10,
                                  color: 'var(--ink-3)'
                                }}
                              >
                                env
                              </span>
                            )}
                          </span>
                        </div>
                      )
                    })}
                  </div>
                </div>

                <CostExplainer
                  fields={enrichmentFields}
                  mode={enrichmentMode}
                  providers={Array.from(enrichmentProviders)}
                  costs={creditCosts}
                  selectedCount={selectedLeadRefs.size}
                />

                {enrichmentNotices.length > 0 && (
                  <div style={{ display: 'grid', gap: 6 }}>
                    {enrichmentNotices.map((notice) => (
                      <div
                        key={notice.id}
                        style={{
                          borderRadius: 8,
                          padding: '8px 10px',
                          background:
                            notice.kind === 'error'
                              ? 'rgba(255,59,48,0.08)'
                              : notice.kind === 'warning'
                              ? 'rgba(255,149,0,0.10)'
                              : 'rgba(52,199,89,0.10)',
                          border:
                            notice.kind === 'error'
                              ? '0.5px solid rgba(255,59,48,0.24)'
                              : notice.kind === 'warning'
                              ? '0.5px solid rgba(255,149,0,0.28)'
                              : '0.5px solid rgba(52,199,89,0.24)',
                          color: 'var(--ink-2)',
                          fontSize: 12
                        }}
                      >
                        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                          <strong>{notice.title}</strong>
                          <span style={{ marginLeft: 'auto', fontVariantNumeric: 'tabular-nums' }}>
                            {formatCurrency(notice.cost)}
                          </span>
                          <button
                            type="button"
                            className="notice-close"
                            aria-label="Fechar aviso"
                            onClick={() => dismissNotice(notice.id)}
                          >
                            ✕
                          </button>
                        </div>
                        <div style={{ color: 'var(--ink-3)', marginTop: 2 }}>
                          {notice.message}
                        </div>
                      </div>
                    ))}
                  </div>
                )}

                <details>
                  <summary style={{ cursor: 'pointer', fontSize: 12, color: 'var(--ink-2)' }}>
                    Opções avançadas
                  </summary>
                  <div
                    style={{
                      display: 'grid',
                      gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))',
                      gap: 8,
                      marginTop: 8
                    }}
                  >
                    <div style={{ ...fieldLabel, gridColumn: '1 / -1', color: 'var(--ink-3)' }}>
                      As chaves de API são lidas automaticamente do arquivo <code>.env</code> do servidor.
                      Defina <code>APOLLO_API_KEY</code>, <code>LUSHA_API_KEY</code>,
                      <code> PEOPLE_DATA_LABS_API_KEY</code> e/ou
                      <code> SNOVIO_CLIENT_ID/SNOVIO_CLIENT_SECRET</code> lá.
                    </div>
                    <label style={fieldLabel}>
                      Webhook Apollo para telefone (obrigatório p/ phone via Apollo)
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
                    {enrichmentEstimate.summary.errors.length > 0 && (
                      <div
                        style={{
                          marginTop: 6,
                          padding: '6px 8px',
                          borderRadius: 6,
                          background: 'rgba(255,59,48,0.10)',
                          color: 'var(--ink-2)'
                        }}
                      >
                        <strong style={{ fontSize: 11 }}>Erros das APIs:</strong>
                        <ul style={{ margin: '4px 0 0 16px', padding: 0, fontSize: 11 }}>
                          {enrichmentEstimate.summary.errors.map((err, idx) => (
                            <li key={`${err}:${idx}`}>{err}</li>
                          ))}
                        </ul>
                      </div>
                    )}
                  </div>
                )}

                <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
                  <button
                    type="button"
                    className="pill-btn"
                    onClick={handleEstimateEnrichment}
                    disabled={enrichmentRunning}
                    title={
                      selectedLeadRefs.size === 0
                        ? 'Marque os checkboxes dos leads que deseja enriquecer'
                        : `Calcular teto máximo de gasto para ${selectedLeadRefs.size} lead(s)`
                    }
                  >
                    {enrichmentRunning
                      ? 'Calculando…'
                      : `Calcular teto máximo (${selectedLeadRefs.size} lead${
                          selectedLeadRefs.size === 1 ? '' : 's'
                        })`}
                  </button>
                  <button
                    type="button"
                    className="pill-btn primary"
                    onClick={handleConfirmEnrichment}
                    disabled={
                      enrichmentRunning ||
                      (enrichmentMode === 'parallel' &&
                        (!enrichmentEstimate ||
                          enrichmentEstimate.status === 'completed'))
                    }
                    title={
                      enrichmentMode === 'cascade'
                        ? 'Inicia a cascata: pergunta antes de chamar cada API, começando pela mais barata.'
                        : 'Dispara todas as APIs selecionadas de uma vez.'
                    }
                  >
                    {enrichmentRunning
                      ? 'Enriquecendo…'
                      : enrichmentMode === 'cascade'
                      ? '▶ Iniciar cascata'
                      : 'Confirmar (paralelo)'}
                  </button>
                  {selectedLeadRefs.size === 0 && (
                    <span style={{ fontSize: 11, color: 'var(--ink-3)' }}>
                      Marque os checkboxes dos leads que deseja enriquecer.
                    </span>
                  )}
                </div>
              </div>
            )}

            <div className="preset-bar" role="tablist" aria-label="Predefinições de tabela">
              {PRESETS.map((option) => (
                <button
                  key={option.value}
                  type="button"
                  role="tab"
                  aria-selected={preset === option.value}
                  onClick={() => setPreset(option.value)}
                  className={`preset-tab ${preset === option.value ? 'on' : ''}`}
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

            {inspectedApiLead && (
              <ApiConsultationPanel
                lead={inspectedApiLead}
                onClose={() => setInspectedApiLead(null)}
              />
            )}

            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              aria-label="Filtrar leads salvos"
              placeholder="Filtrar por nome, cargo ou empresa"
              className="search-input library-filter-input"
            />

            <DualScrollTable>
              <table className="saved-leads-table">
                <thead>
                  <tr style={{ textAlign: 'left', color: 'var(--ink-3)' }}>
                    <th style={{ ...th, width: 42 }}></th>
                    <th style={th}>Pessoa</th>
                    <th style={th}>Cargo</th>
                    <th style={th}>Contato</th>
                    <th style={th}>Consulta</th>
                    <th style={th}>Empresa</th>
                    <th style={th}>Score</th>
                    <th style={th}>LinkedIn</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredLeads.map((lead, idx) => {
                    const ref = leadRef(lead)
                    return (
                      <React.Fragment key={`${lead.linkedin_url ?? lead.source_url}:${idx}`}>
                        <tr>
                          <td style={td}>
                            <input
                              type="checkbox"
                              checked={selectedLeadRefs.has(ref)}
                              onChange={() => toggleLeadRef(lead)}
                              aria-label={`Selecionar ${lead.person_name ?? 'lead'}`}
                            />
                          </td>
                          <td style={td}><span className="saved-cell-strong">{lead.person_name ?? '—'}</span></td>
                          <td style={td}><LeadTitleCell lead={lead} /></td>
                          <td style={td}>
                            {(!lead.email && !lead.phone && !lead.endereco && !lead.linkedin_contact_email && !lead.linkedin_contact_website && !lead.linkedin_contact_phone && parseArrayField<any>(lead.email_alternatives).filter(a => a?.email).length === 0 && parseArrayField<any>(lead.phone_alternatives).filter(a => a?.phone).length === 0) ? (
                              '—'
                            ) : (
                              <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                                <LeadEmailCell lead={lead} hideFallback={true} />
                                <LinkedInContactCell lead={lead} />
                                <LeadPhoneCell lead={lead} hideFallback={true} />
                                <LeadAddressCell lead={lead} />
                              </div>
                            )}
                          </td>
                          <td style={td}>
                            {(!lead.enrichment_source || lead.enrichment_source === 'internal') ? (
                              '—'
                            ) : (
                              <div style={{ display: 'flex', flexDirection: 'column', gap: 4, alignItems: 'flex-start' }}>
                                <ApiConsultationButton
                                  lead={lead}
                                  onInspect={() => setInspectedApiLead(lead)}
                                />
                              </div>
                            )}
                          </td>
                          <td style={td}><LeadCompanyCell lead={lead} /></td>
                          <td style={{ ...td, textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>{lead.confidence_score}</td>
                          <td style={td}>
                            {lead.linkedin_url ? (
                              <a
                                className="li-open-btn"
                                href={lead.linkedin_url}
                                target="_blank"
                                rel="noreferrer"
                              >
                                <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
                                  <path d="M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337 7.433a2.062 2.062 0 01-2.063-2.065 2.064 2.064 0 112.063 2.065zm1.782 13.019H3.555V9h3.564v11.452zM22.225 0H1.771C.792 0 0 .774 0 1.729v20.542C0 23.227.792 24 1.771 24h20.451C23.2 24 24 23.227 24 22.271V1.729C24 .774 23.2 0 22.222 0h.003z" />
                                </svg>
                                Abrir
                              </a>
                            ) : (
                              '—'
                            )}
                          </td>
                        </tr>
                      </React.Fragment>
                    )
                  })}
                  {filteredLeads.length === 0 && (
                    <tr>
                      <td colSpan={8} style={{ padding: 12, color: 'var(--ink-3)', textAlign: 'center' }}>
                        Nenhum lead corresponde ao filtro.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </DualScrollTable>
          </div>
        )}
      </section>
      {dialog && (
        <LibraryDialog
          dialog={dialog}
          onCancel={() => closeDialog(null)}
          onConfirm={(value) => closeDialog(value)}
        />
      )}
      {profileValidationBootstrap && (
        <ChromeBootstrapModal
          targetUrl={profileValidationBootstrap.targetUrl}
          targetSlug={profileValidationBootstrap.targetSlug}
          targetKind="profile"
          onCancel={() => setProfileValidationBootstrap(null)}
          onReady={() => {
            const refsToValidate = profileValidationBootstrap.refs
            setProfileValidationBootstrap(null)
            void runLinkedInProfileValidation(refsToValidate)
          }}
        />
      )}
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

function internalFieldObjectLabel(_fields: InternalEnrichField): string {
  return 'e-mails internos'
}

function internalEnrichDescription(_fields: InternalEnrichField): string {
  return 'Sem APIs pagas. O app tenta inferir e-mails profissionais pelo domínio da empresa e padrões comuns, validando MX e SMTP de forma conservadora.'
}

function sanitizeTitle(text: string | null | undefined): string {
  if (!text) return ''
  return text
    .replace(/pular\s+para\s+o?\s*conteúdo\s+principal/gi, '')
    .replace(/skip\s+to\s+main\s+content/gi, '')
    .replace(/^\s*[-·•|/\\,]\s*|\s*[-·•|/\\,]\s*$/g, '')
    .replace(/\s+/g, ' ')
    .trim()
}

function LeadTitleCell({ lead }: { lead: Lead }) {
  const titleClean = sanitizeTitle(lead.title)
  const expTitleClean = sanitizeTitle(lead.linkedin_experience_title)
  const hasValidated = Boolean(expTitleClean || lead.linkedin_experience_company)
  return (
    <div style={{ display: 'grid', gap: 4, minWidth: 180 }}>
      <span className="saved-cell-wrap">{titleClean || '—'}</span>
      {hasValidated && (
        <div style={{ display: 'grid', gap: 2 }}>
          {expTitleClean && (
            <span className="saved-cell-wrap" style={{ color: 'var(--ink-1)', fontWeight: 500 }}>
              {expTitleClean}
            </span>
          )}
          <span style={{ fontSize: 11, color: 'var(--ink-3)' }}>
            Validado no LinkedIn
            {lead.linkedin_experience_checked_at
              ? ` · ${formatDate(lead.linkedin_experience_checked_at)}`
              : ''}
          </span>
        </div>
      )}
    </div>
  )
}

function LeadCompanyCell({ lead }: { lead: Lead }) {
  const companyClean = sanitizeTitle(lead.company_name)
  const expCompanyClean = sanitizeTitle(lead.linkedin_experience_company)
  return (
    <div style={{ display: 'grid', gap: 4, minWidth: 150 }}>
      <span className="saved-cell-wrap">{companyClean || '—'}</span>
      {expCompanyClean && (
        <span style={{ fontSize: 11, color: 'var(--ink-3)' }}>
          Experiência: {expCompanyClean}
        </span>
      )}
    </div>
  )
}

function LinkedInContactCell({ lead }: { lead: Lead }) {
  const email = lead.linkedin_contact_email?.trim()
  const website = lead.linkedin_contact_website?.trim()
  const phone = lead.linkedin_contact_phone?.trim()
  if (!email && !website && !phone) return null
  return (
    <div style={{ display: 'grid', gap: 3, marginTop: 6 }}>
      {email && (
        <div className="email-cell-primary">
          <span className="email-cell-text">{email}</span>
          <span className="email-pill suggested">
            LinkedIn · {emailKindLabel(email)}
          </span>
        </div>
      )}
      {phone && (
        <div className="email-cell-primary">
          <a className="email-cell-text" href={`tel:${phone}`}>
            {phone}
          </a>
          <span className="email-pill suggested">LinkedIn</span>
        </div>
      )}
      {website && (
        <div className="email-cell-primary">
          <a className="email-cell-text" href={website} target="_blank" rel="noreferrer">
            {displayWebsite(website)}
          </a>
          <span className="email-pill suggested">site</span>
        </div>
      )}
    </div>
  )
}

type ConfidenceTone = 'green' | 'yellow' | 'red'

/**
 * Snov.io-style traffic light for an enriched e-mail, derived objectively
 * from the validation status + confidence score we already compute:
 *  🟢 verde  — mailbox confirmado (SMTP valid), publicado, ou score ≥ 85
 *  🟡 amarelo — provável (MX ok, padrão da empresa/catch-all), score 55–84
 *  🔴 vermelho — improvável (sem MX, domínio pessoal, RCPT rejeitado), < 55
 * Retorna null quando não há sinal de enriquecimento (nada a mostrar).
 */
function emailConfidenceBand(
  lead: Lead
): { tone: ConfidenceTone; color: string; label: string; title: string } | null {
  const status = lead.email_validation_status
  const score = lead.enrichment_confidence
  if (status == null && typeof score !== 'number') return null

  const why: string[] = []
  if (typeof score === 'number') why.push(`confiança ${score}/100`)
  if (status) why.push(`validação: ${status}`)
  if (lead.email_type && lead.email_type !== 'unknown') why.push(lead.email_type)

  let tone: ConfidenceTone
  if (status === 'risky') tone = 'red'
  else if (status === 'valid' || (typeof score === 'number' && score >= 85)) tone = 'green'
  else if (status === 'probable' || (typeof score === 'number' && score >= 55)) tone = 'yellow'
  else tone = 'red'

  const meta: Record<ConfidenceTone, { color: string; label: string }> = {
    green: { color: 'var(--success)', label: '~90% de confiança' },
    yellow: { color: 'var(--warn)', label: 'talvez' },
    red: { color: 'var(--risky)', label: 'improvável' }
  }
  const { color, label } = meta[tone]
  return { tone, color, label, title: why.length ? `${label} — ${why.join(' · ')}` : label }
}

function LeadEmailCell({ lead, hideFallback }: { lead: Lead; hideFallback?: boolean }) {
  const [expanded, setExpanded] = useState(false)
  const verifiedBy = parseArrayField<string>(lead.email_verified_by).filter(Boolean)
  const alternatives = parseArrayField<any>(lead.email_alternatives).filter((alt) => alt && alt.email)
  const hasBadge = verifiedBy.length >= 2
  const hasAlternatives = alternatives.length > 0
  const band = emailConfidenceBand(lead)

  if (!lead.email) {
    if (hasAlternatives) {
      const [primary, ...rest] = alternatives
      return (
        <div className="email-cell">
          <div className="email-cell-primary">
            <span className="email-cell-text">{primary.email}</span>
            <span className="email-pill suggested" title={`Sugerido por ${primary.source}`}>
              sugestão · {primary.source}
            </span>
          </div>
          {rest.length > 0 && (
            <AlternativesToggle
              expanded={expanded}
              setExpanded={setExpanded}
              alternatives={rest}
            />
          )}
        </div>
      )
    }
    if (hideFallback) return null
    return <div className="email-cell-empty">—</div>
  }

  return (
    <div className="email-cell">
      <div className="email-cell-primary">
        {band && (
          <span
            className="confidence-dot"
            data-tone={band.tone}
            style={{ background: band.color }}
            title={band.title}
            aria-label={`Confiança do e-mail: ${band.label}`}
          />
        )}
        <span className="email-cell-text">{lead.email}</span>
        <span className="email-pill" title="Tipo de e-mail">
          {emailTypeLabel(lead.email_type, lead.email)}
        </span>
        {hasBadge && (
          <span
            className="email-pill verified"
            title={`Verificado por: ${verifiedBy.join(', ')}`}
            aria-label={`Verificado por ${verifiedBy.join(', ')}`}
          >
            <svg
              width="9"
              height="9"
              viewBox="0 0 12 12"
              fill="none"
              aria-hidden="true"
            >
              <path
                d="M2.5 6.5L4.8 8.8L9.5 3.5"
                stroke="currentColor"
                strokeWidth="1.6"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
            Verificado
          </span>
        )}
      </div>
      {hasAlternatives && (
        <AlternativesToggle
          expanded={expanded}
          setExpanded={setExpanded}
          alternatives={alternatives}
        />
      )}
    </div>
  )
}

function AlternativesToggle({
  expanded,
  setExpanded,
  alternatives
}: {
  expanded: boolean
  setExpanded: (next: boolean) => void
  alternatives: NonNullable<Lead['email_alternatives']>
}) {
  const count = alternatives.length
  return (
    <>
      <button
        type="button"
        className="email-alt-toggle"
        onClick={() => setExpanded(!expanded)}
        aria-expanded={expanded}
      >
        <span
          className="email-alt-caret"
          data-open={expanded ? 'true' : 'false'}
          aria-hidden="true"
        >
          ▸
        </span>
        {count === 1
          ? `outro provider sugeriu 1 e-mail`
          : `outros providers sugeriram ${count} e-mails`}
      </button>
      {expanded && (
        <ul className="email-alt-list">
          {alternatives.map((alt, index) => (
            <li key={`${alt.email}-${alt.source}-${index}`} className="email-alt-row">
              <span className="email-alt-source">{alt.source}</span>
              <span className="email-alt-email">{alt.email}</span>
            </li>
          ))}
        </ul>
      )}
    </>
  )
}

function LeadPhoneCell({ lead, hideFallback }: { lead: Lead; hideFallback?: boolean }) {
  const [expanded, setExpanded] = useState(false)
  const verifiedBy = parseArrayField<string>(lead.phone_verified_by).filter(Boolean)
  const verifiedByLabels = verifiedBy.map(formatPhoneSourceLabel)
  const alternatives = parseArrayField<any>(lead.phone_alternatives).filter(
    (alt) => alt && alt.phone
  )
  const hasBadge = verifiedBy.length >= 2
  const hasAlternatives = alternatives.length > 0
  const whatsAppActive = false

  if (!lead.phone) {
    if (hasAlternatives) {
      const [primary, ...rest] = alternatives
      return (
        <div className="email-cell" style={{ marginTop: 6 }}>
          <div className="email-cell-primary">
            <a className="email-cell-text" href={`tel:${primary.phone}`}>
              {primary.phone}
            </a>
            <span
              className="email-pill suggested"
              title={`Sugerido por ${formatPhoneSourceLabel(primary.source)}`}
            >
              sugestão · {formatPhoneSourceLabel(primary.source)}
            </span>
          </div>
          {rest.length > 0 && (
            <PhoneAlternativesToggle
              expanded={expanded}
              setExpanded={setExpanded}
              alternatives={rest}
            />
          )}
        </div>
      )
    }
    if (hideFallback) return null
    return <div style={{ color: 'var(--ink-3)', marginTop: 6 }}>—</div>
  }

  return (
    <div className="email-cell" style={{ marginTop: 6 }}>
      <div className="email-cell-primary">
        <a className="email-cell-text" href={`tel:${lead.phone}`}>
          {lead.phone}
        </a>
        {lead.phone_type && (
          <span
            className="email-pill"
            style={{ color: 'var(--ink-3)' }}
            title={`Tipo: ${lead.phone_type}`}
          >
            {lead.phone_type === 'mobile'
              ? '📱'
              : lead.phone_type === 'fixed'
                ? '☎'
                : lead.phone_type}
          </span>
        )}
        {hasBadge && (
          <span
            className="email-pill verified"
            title={`Verificado por: ${verifiedByLabels.join(', ')}`}
            aria-label={`Verificado por ${verifiedByLabels.join(', ')}`}
          >
            <svg width="9" height="9" viewBox="0 0 12 12" fill="none" aria-hidden="true">
              <path
                d="M2.5 6.5L4.8 8.8L9.5 3.5"
                stroke="currentColor"
                strokeWidth="1.6"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
            Verificado
          </span>
        )}
        {whatsAppActive && (
          <span className="email-pill" title="WhatsApp confirmado">
            WhatsApp
          </span>
        )}
      </div>
      {lead.phone_source && (
        <div style={{ fontSize: 11, color: 'var(--ink-3)' }}>
          via {formatPhoneSourceLabel(lead.phone_source)}
          {lead.phone_carrier ? ` · ${lead.phone_carrier}` : ''}
          {lead.phone_region ? ` · ${lead.phone_region}` : ''}
        </div>
      )}
      {hasAlternatives && (
        <PhoneAlternativesToggle
          expanded={expanded}
          setExpanded={setExpanded}
          alternatives={alternatives}
        />
      )}
    </div>
  )
}

/**
 * Endereço residencial já presente nos dados do lead. Exibição read-only:
 * linha discreta no card de contato, com tooltip para o endereço completo.
 * Não expomos a fonte interna — só o 📍.
 */
function LeadAddressCell({ lead }: { lead: Lead }): JSX.Element | null {
  const address = (lead.endereco ?? '').trim()
  if (!address) return null
  return (
    <div
      className="email-cell"
      style={{ marginTop: 6 }}
      title={`Endereço da consulta: ${address}`}
    >
      <div
        className="email-cell-primary"
        style={{
          color: 'var(--ink-2)',
          fontSize: 12,
          display: 'flex',
          alignItems: 'flex-start',
          gap: 4
        }}
      >
        <span aria-hidden="true">📍</span>
        <span
          style={{
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            display: '-webkit-box',
            WebkitLineClamp: 2,
            WebkitBoxOrient: 'vertical'
          }}
        >
          {address}
        </span>
      </div>
    </div>
  )
}

function formatPhoneSourceLabel(source?: string | null): string {
  const normalized = (source || '').trim().toLowerCase()
  if (!normalized) return 'Consulta por telefone'
  if (normalized.includes('linkedin')) return 'Contato informado no perfil'
  if (normalized.includes('receita') || normalized.includes('cnpj')) return 'Base pública'
  if (normalized.includes('pdf') || normalized.includes('serp')) return 'Fonte pública'
  if (normalized.includes('internal')) return 'Enriquecimento interno'
  return 'Consulta por telefone'
}

function PhoneAlternativesToggle({
  expanded,
  setExpanded,
  alternatives
}: {
  expanded: boolean
  setExpanded: (next: boolean) => void
  alternatives: NonNullable<Lead['phone_alternatives']>
}) {
  const count = alternatives.length
  const preview = alternatives.slice(0, 2)
  const hiddenCount = alternatives.length - preview.length
  return (
    <>
      <button
        type="button"
        className="email-alt-toggle"
        onClick={() => setExpanded(!expanded)}
        aria-expanded={expanded}
      >
        <span
          className="email-alt-caret"
          data-open={expanded ? 'true' : 'false'}
          aria-hidden="true"
        >
          ▸
        </span>
        {count === 1
          ? `outra consulta sugeriu 1 telefone`
          : `outras consultas sugeriram ${count} telefones`}
      </button>
      {expanded && (
        <ul className="email-alt-list">
          {preview.map((alt, index) => (
            <li
              key={`${alt.phone}-${alt.source}-${index}`}
              className="email-alt-row"
            >
              <span className="email-alt-source">{formatPhoneSourceLabel(alt.source)}</span>
              <span className="email-alt-email">{alt.phone}</span>
            </li>
          ))}
          {hiddenCount > 0 && (
            <li className="email-alt-row">
              <span className="email-alt-source">mais</span>
              <span className="email-alt-email">+{hiddenCount} telefone(s)</span>
            </li>
          )}
        </ul>
      )}
    </>
  )
}

function DualScrollTable({ children }: { children: React.ReactNode }) {
  const topRef = useRef<HTMLDivElement>(null)
  const botRef = useRef<HTMLDivElement>(null)
  const innerRef = useRef<HTMLDivElement>(null)
  const syncing = useRef(false)

  useEffect(() => {
    const top = topRef.current
    const bot = botRef.current
    const inner = innerRef.current
    if (!top || !bot || !inner) return

    const updateWidth = () => {
      const tbl = bot.querySelector('table')
      if (tbl) inner.style.width = tbl.scrollWidth + 'px'
    }
    updateWidth()
    const ro = new ResizeObserver(updateWidth)
    ro.observe(bot)

    const onTop = () => {
      if (syncing.current) return
      syncing.current = true
      bot.scrollLeft = top.scrollLeft
      syncing.current = false
    }
    const onBot = () => {
      if (syncing.current) return
      syncing.current = true
      top.scrollLeft = bot.scrollLeft
      syncing.current = false
    }

    top.addEventListener('scroll', onTop)
    bot.addEventListener('scroll', onBot)
    return () => {
      top.removeEventListener('scroll', onTop)
      bot.removeEventListener('scroll', onBot)
      ro.disconnect()
    }
  }, [])

  return (
    <div className="table-scroll-wrap">
      <div className="table-top-scroll" ref={topRef}>
        <div className="table-top-scroll-inner" ref={innerRef} />
      </div>
      <div className="table-bottom-scroll" ref={botRef}>
        {children}
      </div>
    </div>
  )
}

function LibraryDialog(props: {
  dialog: LibraryDialogState
  onCancel(): void
  onConfirm(value: string | boolean): void
}) {
  const { dialog, onCancel, onConfirm } = props
  const [value, setValue] = useState(dialog.kind === 'text' ? dialog.defaultValue : '')

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onCancel()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [onCancel])

  return (
    <div
      className="sheet-overlay"
      role="dialog"
      aria-modal="true"
      aria-labelledby="library-dialog-title"
      onClick={(event) => {
        if (event.target === event.currentTarget) onCancel()
      }}
    >
      <div className="card" style={{ width: 'min(440px, 100%)', borderRadius: 14 }}>
        <div className="card-body" style={{ padding: 18 }}>
          <h3 id="library-dialog-title" style={{ margin: 0, fontSize: 16 }}>
            {dialog.title}
          </h3>
          {dialog.kind === 'text' ? (
            <label style={{ ...fieldLabel, marginTop: 12 }}>
              {dialog.label}
              <input
                className="input"
                value={value}
                onChange={(event) => setValue(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' && value.trim()) onConfirm(value)
                }}
                autoFocus
              />
            </label>
          ) : (
            <p style={{ margin: '10px 0 0', fontSize: 13, color: 'var(--ink-2)', lineHeight: 1.5 }}>
              {dialog.message}
            </p>
          )}
          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 16 }}>
            <button type="button" className="pill-btn" onClick={onCancel}>
              Cancelar
            </button>
            <button
              type="button"
              className={dialog.kind === 'confirm' && dialog.danger ? 'pill-btn danger' : 'pill-btn primary'}
              onClick={() => onConfirm(dialog.kind === 'text' ? value : true)}
              disabled={dialog.kind === 'text' && !value.trim()}
            >
              {dialog.confirmLabel}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

function CostExplainer(props: {
  fields: EnrichmentFields
  mode: 'cascade' | 'parallel'
  providers: EnrichmentProvider[]
  costs: Record<EnrichmentProvider, string>
  selectedCount: number
}) {
  const { fields, mode, providers, costs, selectedCount } = props
  const ordered = sortProvidersByCost(providers, costs, fields)
  const fieldsPerProvider = (_provider: EnrichmentProvider): string[] => {
    return ['email']
  }
  const rows = ordered.map((provider) => {
    const perCredit = Number.parseFloat(costs[provider] || '0') || 0
    const fieldsCount = fieldsPerProvider(provider).length
    const credits = selectedCount * fieldsCount
    return {
      provider,
      perCredit,
      fields: fieldsPerProvider(provider).join(' + ') || '—',
      credits,
      total: credits * perCredit
    }
  })
  const grandTotal = rows.reduce((acc, r) => acc + r.total, 0)
  const cheapest = rows[0]?.provider
  return (
    <div
      style={{
        marginTop: 4,
        padding: 10,
        borderRadius: 8,
        background: 'rgba(0,122,255,0.05)',
        border: '0.5px solid rgba(0,122,255,0.2)',
        fontSize: 12
      }}
    >
      <div style={{ marginBottom: 6 }}>
        <strong>Como o gasto é calculado:</strong> R$ = (leads × campos por API) ×
        R$/crédito da API.
        {mode === 'cascade' ? (
          <>
            {' '}No modo <strong>cascata</strong>, tentamos a API mais barata
            primeiro{cheapest ? ` (${cheapest.toUpperCase()})` : ''} e só passamos
            para a próxima nos leads <em>que não vieram</em>, com sua confirmação
            antes de cada estágio. Você só gasta o teto máximo se nenhuma API
            anterior resolver.
          </>
        ) : (
          <>
            {' '}No modo <strong>paralelo</strong>, todas as APIs selecionadas
            rodam para todos os leads — você paga por todas mesmo que a mais
            barata já tenha resolvido.
          </>
        )}
      </div>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11 }}>
        <thead>
          <tr style={{ color: 'var(--ink-3)' }}>
            <th style={{ textAlign: 'left', padding: '2px 4px' }}>
              Ordem
            </th>
            <th style={{ textAlign: 'left', padding: '2px 4px' }}>API</th>
            <th style={{ textAlign: 'left', padding: '2px 4px' }}>Campos</th>
            <th style={{ textAlign: 'right', padding: '2px 4px' }}>R$/crédito</th>
            <th style={{ textAlign: 'right', padding: '2px 4px' }}>Créditos</th>
            <th style={{ textAlign: 'right', padding: '2px 4px' }}>Subtotal</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, idx) => (
            <tr key={row.provider}>
              <td style={{ padding: '2px 4px' }}>{idx + 1}º</td>
              <td style={{ padding: '2px 4px' }}>
                <strong>{row.provider.toUpperCase()}</strong>
              </td>
              <td style={{ padding: '2px 4px' }}>{row.fields}</td>
              <td style={{ padding: '2px 4px', textAlign: 'right' }}>
                {formatCurrency(row.perCredit)}
              </td>
              <td style={{ padding: '2px 4px', textAlign: 'right' }}>{row.credits}</td>
              <td style={{ padding: '2px 4px', textAlign: 'right' }}>
                {formatCurrency(row.total)}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td colSpan={6} style={{ padding: 4, color: 'var(--ink-3)' }}>
                Selecione ao menos uma API.
              </td>
            </tr>
          )}
        </tbody>
        <tfoot>
          <tr>
            <td colSpan={5} style={{ padding: '4px', textAlign: 'right' }}>
              <strong>
                {mode === 'cascade' ? 'Teto máximo (pior caso):' : 'Total estimado:'}
              </strong>
            </td>
            <td style={{ padding: '4px', textAlign: 'right' }}>
              <strong>{formatCurrency(grandTotal)}</strong>
            </td>
          </tr>
        </tfoot>
      </table>
    </div>
  )
}

function ApiConsultationPanel(props: {
  lead: Lead
  onClose(): void
}) {
  const { lead, onClose } = props
  const provider = lead.enrichment_source ?? 'api'
  const noData = lead.enrichment_status === 'api_consulted_no_data'
  const returned = [
    lead.email ? `E-mail: ${lead.email}` : null,
    lead.phone ? `Telefone: ${lead.phone}` : null,
    lead.email_type ? `Tipo de e-mail: ${lead.email_type}` : null,
    lead.email_validation_status
      ? `Validação: ${lead.email_validation_status}`
      : null
  ].filter(Boolean)
  return (
    <div
      style={{
        marginBottom: 10,
        padding: 12,
        borderRadius: 10,
        border: '0.5px solid var(--line)',
        background: 'var(--surface)',
        boxShadow: '0 1px 2px rgba(0,0,0,0.04)',
        display: 'grid',
        gap: 8
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        <strong style={{ fontSize: 13 }}>
          Consulta via API {provider.toUpperCase()}
        </strong>
        <span
          style={{
            fontSize: 11,
            padding: '2px 7px',
            borderRadius: 999,
            background: noData
              ? 'rgba(255,149,0,0.12)'
              : 'rgba(52,199,89,0.12)',
            color: 'var(--ink-2)'
          }}
        >
          {noData ? 'sem dado novo' : 'dados retornados'}
        </span>
        <button
          type="button"
          className="pill-btn"
          style={{ marginLeft: 'auto' }}
          onClick={onClose}
        >
          Fechar
        </button>
      </div>
      <div style={{ fontSize: 12, color: 'var(--ink-2)' }}>
        <strong>{lead.person_name ?? 'Lead sem nome'}</strong>
        {lead.title ? ` · ${sanitizeTitle(lead.title)}` : ''} · {sanitizeTitle(lead.company_name)}
      </div>
      <div style={{ fontSize: 12, color: 'var(--ink-3)' }}>
        {returned.length > 0
          ? returned.join(' · ')
          : 'A API foi consultada, mas não retornou e-mail ou telefone útil para este lead.'}
      </div>
      {(lead.enriched_at || lead.consultation_note) && (
        <div style={{ fontSize: 11, color: 'var(--ink-3)' }}>
          {lead.enriched_at ? `Consultado em ${formatDate(lead.enriched_at)}. ` : ''}
          {lead.consultation_note ?? ''}
        </div>
      )}
    </div>
  )
}

function ApiConsultationButton(props: {
  lead: Lead
  onInspect(): void
}) {
  const { lead, onInspect } = props
  const provider = lead.enrichment_source
  if (!provider || provider === 'internal') {
    return null
  }
  const label = provider.toUpperCase()
  const noData = lead.enrichment_status === 'api_consulted_no_data'
  return (
    <button
      type="button"
      className={`api-consult-btn ${noData ? 'empty' : ''}`}
      title={`Já consultado via API ${label} — clique para verificar`}
      onClick={onInspect}
    >
      <span className="idle">Já consultado via API {label}</span>
      <span className="hover">Verificar consulta</span>
    </button>
  )
}

function leadRef(lead: Lead): string {
  return lead.linkedin_url || lead.source_url || lead.person_name || ''
}


function profileExperienceUrl(value: string | null | undefined): string | null {
  if (!value) return null
  try {
    const parsed = new URL(value)
    if (!parsed.hostname.toLowerCase().includes('linkedin.com')) return null
    const path = parsed.pathname.replace(/\/+$/, '')
    const profilePath = path.split('/details/')[0]
    if (!profilePath || !profilePath.startsWith('/in/')) return null
    parsed.pathname = `${profilePath}/details/experience/`
    parsed.search = ''
    parsed.hash = ''
    return parsed.toString()
  } catch {
    return null
  }
}

function linkedinProfilePath(value: string): string | null {
  try {
    const parsed = new URL(value)
    const profilePath = parsed.pathname.split('/details/')[0].replace(/\/+$/, '').toLowerCase()
    return profilePath || null
  } catch {
    return null
  }
}

function domainFromSearchRequest(searchRequest: Record<string, unknown> | undefined): string | null {
  const domain = cleanDomain(searchRequest?.company_domain)
  return domain
}

function cleanDomain(value: unknown): string | null {
  if (typeof value !== 'string') return null
  let raw = value.trim().toLowerCase()
  if (!raw) return null
  if (raw.includes('://')) {
    try {
      raw = new URL(raw).hostname
    } catch {
      return null
    }
  }
  raw = raw.split('/')[0]?.split('?')[0]?.trim() ?? ''
  if (raw.startsWith('www.')) raw = raw.slice(4)
  if (!raw || raw.includes('linkedin.com') || !raw.includes('.')) return null
  if (!/^[a-z0-9.-]+$/.test(raw)) return null
  return raw.replace(/^\.+|\.+$/g, '') || null
}

function suggestDomainFromCompanyName(value: string | undefined): string {
  if (!value) return ''
  const slug = value
    .normalize('NFD')
    .replace(/[̀-ͯ]/g, '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '')
  return slug ? `${slug}.com.br` : ''
}

const PERSONAL_EMAIL_DOMAINS = new Set([
  'gmail.com',
  'googlemail.com',
  'hotmail.com',
  'hotmail.com.br',
  'outlook.com',
  'outlook.com.br',
  'live.com',
  'yahoo.com',
  'yahoo.com.br',
  'icloud.com',
  'uol.com.br',
  'bol.com.br',
  'terra.com.br',
  'protonmail.com',
  'proton.me'
])

function emailKindLabel(email: string): string {
  const domain = email.split('@')[1]?.toLowerCase().trim()
  if (!domain) return 'e-mail'
  return PERSONAL_EMAIL_DOMAINS.has(domain) ? 'pessoal' : 'corporativo'
}

function emailTypeLabel(type: string | null | undefined, email: string): string {
  const normalized = (type ?? '').toLowerCase()
  if (normalized === 'work') return 'corporativo'
  if (normalized === 'personal') return 'pessoal'
  return emailKindLabel(email)
}

function displayWebsite(url: string): string {
  try {
    const parsed = new URL(url)
    return parsed.hostname.replace(/^www\./, '')
  } catch {
    return url.replace(/^https?:\/\//, '').replace(/^www\./, '').split('/')[0] || url
  }
}

function sortProvidersByCost(
  providers: EnrichmentProvider[],
  costs: Record<EnrichmentProvider, string>,
  _fields: EnrichmentFields
): EnrichmentProvider[] {
  return [...providers].sort((a, b) => {
    const ca = Number.parseFloat(costs[a] || '0') || 0
    const cb = Number.parseFloat(costs[b] || '0') || 0
    return ca - cb
  })
}

function filterPending(
  allLeads: Lead[],
  wantedRefs: Set<string>,
  _fields: EnrichmentFields
): string[] {
  const out: string[] = []
  for (const lead of allLeads) {
    const ref = leadRef(lead)
    if (!wantedRefs.has(ref)) continue
    if (!lead.email) out.push(ref)
  }
  return out
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

/**
 * Tempo relativo curto pt-BR ("há 2 min", "ontem", "12 mai"). Usado nos
 * cards densos da sidebar e no header da tabela ativa — `toLocaleString()`
 * cheio (15 chars) gerava ruído.
 */
function formatRelativeDate(iso: string): string {
  try {
    const date = new Date(iso)
    const now = Date.now()
    const diffMs = now - date.getTime()
    const diffMin = Math.floor(diffMs / 60_000)
    if (diffMin < 1) return 'agora'
    if (diffMin < 60) return `há ${diffMin} min`
    const diffHour = Math.floor(diffMin / 60)
    if (diffHour < 24) return `há ${diffHour} h`
    const diffDay = Math.floor(diffHour / 24)
    if (diffDay === 1) return 'ontem'
    if (diffDay < 7) return `há ${diffDay} dias`
    return date.toLocaleDateString('pt-BR', { day: '2-digit', month: 'short' })
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
