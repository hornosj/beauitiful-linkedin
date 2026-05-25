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
  SavedLeadTableDetail,
  TelegramConsult,
  TelegramConsultCandidate,
  TelegramPhoneLeadResult,
  TelegramPhoneRankedCandidate,
  TelegramPhoneSummary,
  TelethonPipelineResponse
} from '../../../shared/types'
import { useEnrichmentRunner } from '../enrichment/EnrichmentRunnerContext'
import CpfPickerTelethonDialog from './CpfPickerTelethonDialog'
import { CpfReviewList, cpfAllowedForReview } from './CpfReviewList'
import ChromeBootstrapModal from './ChromeBootstrapModal'
import TelethonAuthDialog from './TelethonAuthDialog'

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

function providerOrder(provider?: string | null): number {
  if (!provider) return 3
  if (provider === 'finder' || provider === 'finder_cpf') return 0
  if (provider === 'gon' || provider === 'gon_cpf') return 1
  if (provider === 'unix') return 2
  return 3
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
  onTelethonAuthSuccess?(): void
}

const TELEGRAM_WEB_URL = 'https://web.telegram.org/k/'

/**
 * Garante que o Chrome com `--remote-debugging-port=9222` esteja vivo
 * antes de qualquer fluxo Telegram. Se o CDP já responder, retorna
 * imediatamente. Caso contrário, spawna o Chrome dedicado (com perfil
 * próprio do app, abrindo Telegram Web na primeira aba) e aguarda até
 * 30s o CDP atender. Toda a tentativa é melhor-esforço — falhas viram
 * `onFeedback('error', ...)` para o caller abortar a ação.
 *
 * O perfil do Chrome é dedicado (`BeautifulLinkedIn/ChromeProfile`),
 * então a sessão do Telegram Web persiste entre launches.
 */
async function ensureChromeReady(
  onFeedback: (kind: 'error' | 'success', message: string) => void
): Promise<boolean> {
  const bridge = window.beautifulLinkedIn?.chrome
  if (!bridge) {
    // Bridge ausente significa: rodando fora do Electron (tests/browser).
    // Não há como auto-iniciar o Chrome aqui — devolvemos ``true`` para
    // deixar o caller seguir; o backend ainda vai validar que o CDP
    // está vivo quando tentar conectar. Em produção o preload SEMPRE
    // injeta o bridge, então esse caminho só é exercitado por tests.
    return true
  }
  try {
    const probe = await bridge.probe()
    if (probe.alive) return true
  } catch {
    // probe failed — assume CDP is down and try to launch
  }
  onFeedback(
    'success',
    'Chrome (CDP) não estava aberto — iniciando agora com Telegram Web…'
  )
  const launch = await bridge.launch(TELEGRAM_WEB_URL)
  if (!launch.launched) {
    onFeedback(
      'error',
      launch.error ??
        'Não foi possível iniciar o Chrome com a porta 9222. Verifique se o Chrome está instalado.'
    )
    return false
  }
  const alive = await bridge.waitForCdp(30000)
  if (!alive) {
    onFeedback(
      'error',
      'Chrome abriu mas não expôs a porta 9222 em 30s. Feche outras instâncias do Chrome e tente de novo.'
    )
    return false
  }
  return true
}

type AsyncStatus = 'idle' | 'loading'

interface EnrichmentNotice {
  id: string
  kind: 'success' | 'warning' | 'error'
  title: string
  message: string
  cost: number
  provider?: string
}

type TelethonLogStatus = 'pending' | 'active' | 'done' | 'error'

interface TelethonRunLogEntry {
  id: string
  label: string
  detail: string
  status: TelethonLogStatus
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
  // `telegramPhoneRunning` ainda existe (drasticamente menor escopo): só
  // gateia o handler `handleTelegramPhoneDirectExtract` que dispara
  // `telegramPhoneTelethonCpfStage` por CPF clicado no picker / no
  // painel de evidências. Todos os fluxos Playwright/CDP foram removidos.
  const [telegramPhoneRunning, setTelegramPhoneRunning] = useState(false)
  const [telegramTelethonPipelineRunning, setTelegramTelethonPipelineRunning] = useState(false)
  const [telegramPhonePipelineRunning, setTelegramPhonePipelineRunning] = useState(false)
  const [telethonPipelineLastResult, setTelethonPipelineLastResult] =
    useState<TelethonPipelineResponse | null>(null)
  const [telethonRunLogs, setTelethonRunLogs] = useState<TelethonRunLogEntry[]>([])
  const [telethonAuthOpen, setTelethonAuthOpen] = useState(false)
  const telethonAuthPendingRefsRef = useRef<string[] | null>(null)
  const telethonAuthPendingFlowRef =
    useRef<'experimental' | 'pipeline' | 'phone-pipeline' | 'cpf' | null>(null)
  const telethonAuthPendingCpfRef = useRef<{ leadRef: string; cpf: string } | null>(null)
  // Map keyed by ``leadRef(lead)`` → list of consult rows (one per
  // provider — default "gon" + "unix"; experimental also adds Finder).
  // Loaded once per active table
  // and updated optimistically after each batch.
  const [telegramConsults, setTelegramConsults] = useState<Record<string, TelegramConsult[]>>({})
  const [expandedTelegramRefs, setExpandedTelegramRefs] = useState<Set<string>>(new Set())
  // Picker dialog for CPFs already extracted via the Telethon pipeline.
  // Opens the "Revisar CPFs" screen (see ctx_images/sinais.png) without
  // touching the Playwright/CDP flow — phone lookups go through Telethon
  // via ``telegramPhoneTelethonCpfStage``.
  const [cpfPicker, setCpfPicker] = useState<{
    leadRef: string
    leadName: string | null
    candidates: TelegramPhoneRankedCandidate[]
    eligibleCpfs: string[]
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
      setTelegramConsults({})
      setExpandedTelegramRefs(new Set())
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
    // Load existing Telegram consults for this table so the expand
    // controls show up immediately without a fresh consult run.
    const listTelegramConsults = client.listTelegramConsults?.bind(client)
    if (listTelegramConsults) {
      void listTelegramConsults(activeId)
        .then((response) => {
          if (cancelled) return
          const map: Record<string, TelegramConsult[]> = {}
          for (const consult of response.consults) {
            ;(map[consult.lead_ref] ??= []).push(consult)
          }
          // Sort each lead's rows: gon first, then unix.
          for (const ref of Object.keys(map)) {
            map[ref].sort((a, b) => providerOrder(a.provider) - providerOrder(b.provider))
          }
          setTelegramConsults(map)
          setExpandedTelegramRefs(new Set())
        })
        .catch(() => {
          // Listing is best-effort — a missing table or transient error
          // shouldn't block the leads view.
          if (!cancelled) setTelegramConsults({})
        })
    }
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

    // Phone discovery is currently surfaced in the UI exclusively as the
    // Telegram-group lookup ("Buscar via Telegram"). The button label is
    // explicit, so we always restrict the backend to that source — the
    // other phone providers (site harvest, Receita CNPJ, PDFs, bot 1:1)
    // would surprise the user given the button copy.
    const phoneSources =
      fields === 'phone' || fields === 'both' ? ['telegram_group'] : undefined

    enricher.start({
      tableId: activeId,
      tableName: activeDetail.table.name,
      leadRefs: refs,
      totalLeads: targetCount,
      companyDomain,
      fields,
      phoneSources
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

  const openCpfPickerForLead = (lead: Lead) => {
    const ref = leadRef(lead)
    if (!ref) return
    const consults = telegramConsults[ref] ?? []
    const candidates = collectCpfCandidatesFromConsults(consults)
    if (candidates.length === 0) {
      onFeedback(
        'error',
        'Nenhum CPF extraído ainda — rode a extração de CPFs antes de buscar telefone.'
      )
      return
    }
    const eligibleCpfs = candidates.filter((c) => c.eligible).map((c) => c.cpf)
    setCpfPicker({
      leadRef: ref,
      leadName: lead.person_name ?? null,
      candidates,
      eligibleCpfs:
        eligibleCpfs.length > 0 ? eligibleCpfs : candidates.slice(0, 1).map((c) => c.cpf)
    })
  }

  const handleCpfPickerResult = async (response: TelethonPipelineResponse): Promise<void> => {
    if (!client || !activeId) return
    const leadResult = response.leads?.[0] ?? null
    if (leadResult) {
      mergeConsultsForLead({
        lead_ref: leadResult.lead_ref,
        name_consult: null,
        cpf_consult: leadResult.cpf_consults?.[0] ?? null,
        candidates: leadResult.candidates,
        blocked_reason: leadResult.blocked_reason
      })
    }
    try {
      const refreshed = await client.getLeadTable(activeId)
      setActiveDetail(refreshed)
    } catch {
      // best-effort
    }
    onFeedback(
      'success',
      `Busca concluída: ${response.summary.leads_with_phone}/${response.summary.requested_leads} lead(s) com telefone.`
    )
    setCpfPicker(null)
  }

  const handleCpfPickerAuthRequired = (cpfs: string[]): void => {
    if (!cpfPicker) return
    const fallbackCpf = cpfs[0] ?? cpfPicker.candidates[0]?.cpf
    if (!fallbackCpf) {
      setCpfPicker(null)
      return
    }
    telethonAuthPendingCpfRef.current = { leadRef: cpfPicker.leadRef, cpf: fallbackCpf }
    telethonAuthPendingFlowRef.current = 'cpf'
    setCpfPicker(null)
    setTelethonAuthOpen(true)
  }

  const handleTelegramPhoneDirectExtract = async (ref: string, cpfToRun?: string) => {
    if (!client || !activeId) return
    const cpf = (cpfToRun ?? '').trim()
    if (!cpf) {
      onFeedback('error', 'Nenhum CPF extraído para buscar telefone.')
      return
    }
    setTelegramPhoneRunning(true)
    beginTelethonRunLogs('Consultando telefone para o CPF selecionado.')
    try {
      markTelethonRunLog('consult', 'active')
      const response = await client.telegramPhoneTelethonCpfStage(activeId, {
        lead_ref: ref,
        cpf
      })
      markTelethonRunLog('match', 'active')
      const lead = response.leads[0]
      if (lead) {
        mergeConsultsForLead({
          lead_ref: lead.lead_ref,
          name_consult: null,
          cpf_consult: lead.cpf_consults[0] ?? null,
          candidates: lead.candidates,
          blocked_reason: lead.blocked_reason
        })
      }
      try {
        markTelethonRunLog('save', 'active')
        const refreshed = await client.getLeadTable(activeId)
        setActiveDetail(refreshed)
      } catch {}
      finishTelethonRunLogs('done')
      onFeedback(
        'success',
        `Busca concluída: ${response.summary.leads_with_phone}/${response.summary.requested_leads} lead(s) com telefone.`
      )
    } catch (error) {
      if (error instanceof ApiError && isTelethonAuthError(error)) {
        finishTelethonRunLogs('error')
        telethonAuthPendingCpfRef.current = { leadRef: ref, cpf }
        telethonAuthPendingFlowRef.current = 'cpf'
        setTelethonAuthOpen(true)
      } else {
        finishTelethonRunLogs('error')
        onFeedback('error', formatError(error))
      }
    } finally {
      setTelegramPhoneRunning(false)
    }
  }









  const handleTelethonAuthSuccess = (): void => {
    const refs = telethonAuthPendingRefsRef.current
    const pendingCpf = telethonAuthPendingCpfRef.current
    const flow = telethonAuthPendingFlowRef.current ?? 'pipeline'
    telethonAuthPendingRefsRef.current = null
    telethonAuthPendingCpfRef.current = null
    telethonAuthPendingFlowRef.current = null
    setTelethonAuthOpen(false)
    props.onTelethonAuthSuccess?.()
    onFeedback('success', 'Telegram autenticado. Retomando o fluxo...')
    if (flow === 'cpf' && pendingCpf) {
      void handleTelegramPhoneDirectExtract(pendingCpf.leadRef, pendingCpf.cpf)
    } else if (flow === 'phone-pipeline' && refs && refs.length > 0) {
      void executeTelegramTelethonPipeline(refs, { mode: 'phone' })
    } else if (refs && refs.length > 0) {
      void executeTelegramCpfExtraction(refs)
    }
  }

  const handleTelethonAuthClose = (): void => {
    telethonAuthPendingRefsRef.current = null
    telethonAuthPendingCpfRef.current = null
    telethonAuthPendingFlowRef.current = null
    setTelethonAuthOpen(false)
  }

  const beginTelethonRunLogs = (detail: string): void => {
    setTelethonRunLogs([
      {
        id: 'session',
        label: 'Preparando sessão Telegram',
        detail: 'Validando a sessão nativa antes de consultar.',
        status: 'active'
      },
      {
        id: 'consult',
        label: 'Consultando dados via sessão nativa',
        detail,
        status: 'pending'
      },
      {
        id: 'match',
        label: 'Comparando sinais do lead',
        detail: 'Cruzando retornos com nome, localização e idade quando houver sinais.',
        status: 'pending'
      },
      {
        id: 'save',
        label: 'Consolidando resultados',
        detail: 'Atualizando evidências, CPFs e telefones sem expor fontes internas.',
        status: 'pending'
      }
    ])
  }

  const markTelethonRunLog = (id: string, status: TelethonLogStatus): void => {
    setTelethonRunLogs((prev) =>
      prev.map((entry) => {
        if (entry.id === id) return { ...entry, status }
        if (id === 'consult' && entry.id === 'session' && entry.status === 'active') {
          return { ...entry, status: 'done' }
        }
        if (id === 'match' && entry.id === 'consult' && entry.status !== 'error') {
          return { ...entry, status: 'done' }
        }
        if (id === 'save' && entry.id === 'match' && entry.status !== 'error') {
          return { ...entry, status: 'done' }
        }
        return entry
      })
    )
  }

  const finishTelethonRunLogs = (status: 'done' | 'error'): void => {
    setTelethonRunLogs((prev) =>
      prev.map((entry) => ({
        ...entry,
        status: entry.status === 'error' ? 'error' : status
      }))
    )
  }



  const handleRetryTelethonForLead = async (ref: string): Promise<void> => {
    if (!client || !activeId) return
    try {
      if (typeof client.getTelethonAuthStatus === 'function') {
        const status = await client.getTelethonAuthStatus()
        if (!status.configured) {
          onFeedback(
            'error',
            'Telegram não configurado: defina BEAUTIFUL_LINKEDIN_TELEGRAM_API_ID e BEAUTIFUL_LINKEDIN_TELEGRAM_API_HASH no .env.'
          )
          return
        }
        if (!status.authorized) {
          telethonAuthPendingRefsRef.current = [ref]
          telethonAuthPendingFlowRef.current = 'pipeline'
          setTelethonAuthOpen(true)
          return
        }
      }
    } catch (error) {
      console.warn('getTelethonAuthStatus falhou', error)
    }
    await executeTelegramCpfExtraction([ref])
  }

  const executeTelegramCpfExtraction = async (refs: string[]): Promise<void> => {
    if (!client || !activeId) return
    setTelegramTelethonPipelineRunning(true)
    setTelethonPipelineLastResult(null)
    beginTelethonRunLogs(
      refs.length === 1
        ? 'Rodando nome e comparação de CPFs para um lead.'
        : `Rodando nome e comparação de CPFs para ${refs.length} leads.`
    )
    try {
      markTelethonRunLog('consult', 'active')
      const response = await client.telegramConsultTelethonExperimental(activeId, {
        lead_refs: refs,
        max_leads: 10
      })
      markTelethonRunLog('match', 'active')
      setTelegramConsults((prev) => {
        const next: Record<string, TelegramConsult[]> = { ...prev }
        for (const consult of response.consults) {
          const existing = (next[consult.lead_ref] ?? []).slice()
          const idx = existing.findIndex(
            (row) =>
              row.provider === consult.provider &&
              (row.query_type ?? 'name') === (consult.query_type ?? 'name')
          )
          if (idx >= 0) existing[idx] = consult
          else existing.push(consult)
          existing.sort(
            (a, b) =>
              providerOrder(a.provider) - providerOrder(b.provider) ||
              ((a.query_type ?? 'name') === 'name' ? -1 : 1)
          )
          next[consult.lead_ref] = existing
        }
        return next
      })
      setExpandedTelegramRefs((prev) => {
        const next = new Set(prev)
        for (const consult of response.consults) next.add(consult.lead_ref)
        return next
      })
      markTelethonRunLog('save', 'active')
      onFeedback(
        'success',
        `Consulta de CPF concluída: ${response.summary.succeeded}/${response.summary.requested_leads} lead(s) com CPFs processados.`
      )
      finishTelethonRunLogs('done')
    } catch (error) {
      if (error instanceof ApiError && isTelethonAuthError(error)) {
        finishTelethonRunLogs('error')
        telethonAuthPendingRefsRef.current = refs
        telethonAuthPendingFlowRef.current = 'pipeline'
        setTelethonAuthOpen(true)
        onFeedback('error', 'Sessão Telegram não autenticada. Faça login para continuar.')
        return
      }
      finishTelethonRunLogs('error')
      onFeedback('error', formatError(error))
    } finally {
      setTelegramTelethonPipelineRunning(false)
    }
  }

  const executeTelegramTelethonPipeline = async (
    refs: string[],
    options: { mode?: 'cpf' | 'phone' } = {}
  ): Promise<void> => {
    if (!client || !activeId) return
    const isPhonePipeline = options.mode === 'phone'
    if (isPhonePipeline) setTelegramPhonePipelineRunning(true)
    else setTelegramTelethonPipelineRunning(true)
    setTelethonPipelineLastResult(null)
    beginTelethonRunLogs(
      refs.length === 1
        ? isPhonePipeline
          ? 'Rodando nome, comparação e telefone para o CPF de maior score.'
          : 'Rodando nome e comparação de CPFs para um lead.'
        : isPhonePipeline
          ? `Rodando nome, comparação e telefone pelo melhor CPF para ${refs.length} leads.`
          : `Rodando nome e comparação de CPFs para ${refs.length} leads.`
    )
    try {
      markTelethonRunLog('consult', 'active')
      const response = await client.telegramConsultTelethonPipeline(activeId, {
        lead_refs: refs,
        max_leads: 10,
        ...(isPhonePipeline ? { max_cpf_candidates: 1 } : {})
      })
      setTelethonPipelineLastResult(response)
      markTelethonRunLog('match', 'active')
      // Merge name + cpf consults into the per-lead Telegram evidence map so
      // the existing expander surfaces them without an extra fetch.
      setTelegramConsults((prev) => {
        const next: Record<string, TelegramConsult[]> = { ...prev }
        for (const leadResult of response.leads) {
          const ref = leadResult.lead_ref
          const existing = (next[ref] ?? []).slice()
          const incoming: TelegramConsult[] = [
            ...leadResult.name_consults,
            ...leadResult.cpf_consults
          ]
          for (const consult of incoming) {
            const idx = existing.findIndex(
              (row) =>
                row.provider === consult.provider &&
                (row.query_type ?? 'name') === (consult.query_type ?? 'name')
            )
            if (idx >= 0) existing[idx] = consult
            else existing.push(consult)
          }
          existing.sort(
            (a, b) =>
              providerOrder(a.provider) - providerOrder(b.provider) ||
              ((a.query_type ?? 'name') === 'name' ? -1 : 1)
          )
          next[ref] = existing
        }
        return next
      })
      setExpandedTelegramRefs((prev) => {
        const next = new Set(prev)
        for (const leadResult of response.leads) next.add(leadResult.lead_ref)
        return next
      })
      markTelethonRunLog('save', 'active')
      // Refresh the lead detail so phone updates land in the table.
      if (activeId) {
        try {
          const refreshed = await client.getLeadTable(activeId)
          setActiveDetail(refreshed)
        } catch (err) {
          console.warn('Falha ao atualizar tabela após pipeline Telethon', err)
        }
      }
      onFeedback(
        'success',
        isPhonePipeline
          ? `Busca de telefones concluída: ${response.summary.leads_with_phone}/${response.summary.requested_leads} lead(s) com telefone (` +
              `${response.summary.phones_persisted} persistido(s)).`
          : `Consulta de CPF concluída: ${response.summary.name_consults} consulta(s) de nome e ${response.summary.cpf_consults} consulta(s) de CPF registradas.`
      )
      finishTelethonRunLogs('done')
    } catch (error) {
      if (error instanceof ApiError && isTelethonAuthError(error)) {
        finishTelethonRunLogs('error')
        telethonAuthPendingRefsRef.current = refs
        telethonAuthPendingFlowRef.current = isPhonePipeline ? 'phone-pipeline' : 'pipeline'
        setTelethonAuthOpen(true)
        onFeedback('error', 'Sessão Telegram não autenticada. Faça login para continuar.')
        return
      }
      finishTelethonRunLogs('error')
      onFeedback('error', formatError(error))
    } finally {
      if (isPhonePipeline) setTelegramPhonePipelineRunning(false)
      else setTelegramTelethonPipelineRunning(false)
    }
  }

  const handleTelegramTelethonPipeline = async (): Promise<void> => {
    if (!client || !activeId || !activeDetail) return
    const refs = Array.from(selectedLeadRefs)
    if (refs.length === 0) {
      onFeedback('error', 'Selecione ao menos um lead para extrair CPFs.')
      return
    }
    if (refs.length > 10) {
      onFeedback(
        'error',
        'A extração aceita no máximo 10 leads por rodada — o intervalo de segurança entre consultas estende muito o tempo total.'
      )
      return
    }
    const proceed = await askConfirm(
      'Extração completa de CPFs',
      `Vou consultar a sessão Telegram nativa, comparar sinais do LinkedIn, consolidar CPFs e buscar telefones quando houver CPF confiável. ` +
        `Cada ação respeita o intervalo mínimo conservador de 15s (anti-bloqueio). ` +
        `Tempo estimado: ~1 min ou mais por telefone encontrado. Leads alvo: ${refs.length}.`,
      'Rodar pipeline'
    )
    if (!proceed) return

    try {
      if (typeof client.getTelethonAuthStatus === 'function') {
        const status = await client.getTelethonAuthStatus()
        if (!status.configured) {
          onFeedback(
            'error',
            'Telegram não configurado: defina BEAUTIFUL_LINKEDIN_TELEGRAM_API_ID e BEAUTIFUL_LINKEDIN_TELEGRAM_API_HASH no .env.'
          )
          return
        }
        if (!status.authorized) {
          telethonAuthPendingRefsRef.current = refs
          telethonAuthPendingFlowRef.current = 'pipeline'
          setTelethonAuthOpen(true)
          return
        }
      }
    } catch (error) {
      console.warn('getTelethonAuthStatus falhou', error)
    }

    await executeTelegramCpfExtraction(refs)
  }

  const handleFindPhonesViaTelethon = async (): Promise<void> => {
    if (!client || !activeId || !activeDetail) return
    const refs = Array.from(selectedLeadRefs)
    if (refs.length === 0) {
      onFeedback('error', 'Selecione ao menos um lead para encontrar telefones.')
      return
    }
    if (refs.length > 10) {
      onFeedback(
        'error',
        'A busca de telefones aceita no máximo 10 leads por rodada para manter o intervalo seguro entre ações.'
      )
      return
    }
    const averageSecondsPerLead = 60
    const estimatedMinutes = Math.max(1, Math.ceil((refs.length * averageSecondsPerLead) / 60))
    const leadLabel = refs.length === 1 ? 'lead selecionado' : 'leads selecionados'
    const proceed = await askConfirm(
      'Encontrar telefones',
      `Vou rodar CPF e telefone pela sessão Telegram nativa e consultar telefone só para o CPF com maior score de qualidade em cada lead. ` +
        `As ações usam intervalo mínimo conservador de 15s para reduzir risco de bloqueio. ` +
        `Média usada: ~1 min por lead. Tempo estimado: cerca de ${estimatedMinutes} min ou mais para ${refs.length} ${leadLabel}.`,
      'Encontrar telefones'
    )
    if (!proceed) return

    try {
      if (typeof client.getTelethonAuthStatus === 'function') {
        const status = await client.getTelethonAuthStatus()
        if (!status.configured) {
          onFeedback(
            'error',
            'Telegram não configurado: defina BEAUTIFUL_LINKEDIN_TELEGRAM_API_ID e BEAUTIFUL_LINKEDIN_TELEGRAM_API_HASH no .env.'
          )
          return
        }
        if (!status.authorized) {
          telethonAuthPendingRefsRef.current = refs
          telethonAuthPendingFlowRef.current = 'phone-pipeline'
          setTelethonAuthOpen(true)
          return
        }
      }
    } catch (error) {
      console.warn('getTelethonAuthStatus falhou', error)
    }

    await executeTelegramTelethonPipeline(refs, { mode: 'phone' })
  }

  // Reusado pela merge de consults na lista de cada lead.
  const mergeConsultsForLead = (lead: {
    lead_ref: string
    name_consult?: TelegramConsult | null
    cpf_consult?: TelegramConsult | null
    candidates?: readonly unknown[]
    blocked_reason?: string | null
  }) => {
    setTelegramConsults((prev) => {
      const next: Record<string, TelegramConsult[]> = { ...prev }
      const existing = (next[lead.lead_ref] ?? []).slice()
      for (const consult of [lead.name_consult, lead.cpf_consult]) {
        if (!consult) continue
        const idx = existing.findIndex(
          (row) =>
            row.provider === consult.provider &&
            (row.query_type ?? 'name') === (consult.query_type ?? 'name')
        )
        if (idx >= 0) existing[idx] = consult
        else existing.push(consult)
      }
      existing.sort(
        (a, b) =>
          providerOrder(a.provider) - providerOrder(b.provider) ||
          ((a.query_type ?? 'name') === 'name' ? -1 : 1)
      )
      next[lead.lead_ref] = existing
      return next
    })
    const candidatesLength = lead.candidates?.length ?? 0
    if (candidatesLength > 0 || lead.blocked_reason || lead.cpf_consult?.error) {
      setExpandedTelegramRefs((prev) => {
        const next = new Set(prev)
        next.add(lead.lead_ref)
        return next
      })
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
    setEnrichmentNotices((prev) => [
      ...nextNotices,
      ...prev
    ].slice(0, 6))
  }

  return (
    <div className="grid grid-cols-1 lg:grid-cols-[320px_minmax(0,1fr)] gap-6 min-w-0">
      <aside className="rounded-xl border border-line bg-surface p-3 min-h-[360px] flex flex-col min-w-0">
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

      <section className="rounded-xl border border-line bg-surface p-4 min-h-[360px] flex flex-col relative min-w-0">
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
          <div key={activeDetail.table.id} className="animate-fade-in-up flex flex-col h-full duration-300">
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
                <button
                  type="button"
                  className="pill-btn primary"
                  onClick={() => void handleTelegramTelethonPipeline()}
                  disabled={
                    telegramTelethonPipelineRunning ||
                    telegramPhonePipelineRunning ||
                    !activeDetail ||
                    selectedLeadRefs.size === 0
                  }
                  aria-label="Extrair CPFs via Telegram"
                  title="Roda a sessão Telegram nativa para extrair CPFs, pontuar sinais do lead e consolidar evidências. Sem Chrome, sem Playwright. Máx. 10 leads."
                >
                  {telegramTelethonPipelineRunning ? (
                    <>
                      <span className="enrich-inline-dot" aria-hidden="true" />
                      Extraindo CPFs…
                    </>
                  ) : (
                    <>🪪 Extrair CPFs via Telegram {selectedLeadRefs.size > 0 ? `(${selectedLeadRefs.size})` : ''}</>
                  )}
                </button>
                <button
                  type="button"
                  className="pill-btn primary"
                  onClick={() => void handleFindPhonesViaTelethon()}
                  disabled={
                    telegramPhonePipelineRunning ||
                    telegramTelethonPipelineRunning ||
                    !activeDetail ||
                    selectedLeadRefs.size === 0
                  }
                  aria-label="Encontrar telefones via Telegram"
                  title="Roda a pipeline Telethon completa e consulta telefone apenas para o CPF com maior score de qualidade. Máx. 10 leads."
                >
                  {telegramPhonePipelineRunning ? (
                    <>
                      <span className="enrich-inline-dot" aria-hidden="true" />
                      Encontrando telefones…
                    </>
                  ) : (
                    <>☎ Encontrar telefones {selectedLeadRefs.size > 0 ? `(${selectedLeadRefs.size})` : ''}</>
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
              <TelethonRunLogPanel entries={telethonRunLogs} />
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
                      {/*
                        Pipeline de telefone ocultado da UI a pedido.
                        Backend continua wired (Receita CNPJ + PDF +
                        site harvester + WhatsApp + Telegram quando
                        configurado). Para reabrir, basta descomentar
                        as duas linhas abaixo.
                      */}
                      {/* <option value="phone">Somente telefone</option> */}
                      {/* <option value="both">E-mail + telefone</option> */}
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
                    const consults = ref ? telegramConsults[ref] ?? [] : []
                    const expanded = !!ref && expandedTelegramRefs.has(ref)
                    const anyError = consults.some((c) => c.error)
                    const cpfCandidates = collectCpfCandidatesFromConsults(consults)
                    const hasCpfCandidates = cpfCandidates.length > 0
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
                            {(!lead.email && !lead.phone && !lead.linkedin_contact_email && !lead.linkedin_contact_website && !lead.linkedin_contact_phone && parseArrayField<any>(lead.email_alternatives).filter(a => a?.email).length === 0 && parseArrayField<any>(lead.phone_alternatives).filter(a => a?.phone).length === 0) ? (
                              '—'
                            ) : (
                              <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                                <LeadEmailCell lead={lead} hideFallback={true} />
                                <LinkedInContactCell lead={lead} />
                                <LeadPhoneCell lead={lead} hideFallback={true} />
                              </div>
                            )}
                          </td>
                          <td style={td}>
                            {(!lead.enrichment_source || lead.enrichment_source === 'internal') && consults.length === 0 ? (
                              '—'
                            ) : (
                              <div style={{ display: 'flex', flexDirection: 'column', gap: 4, alignItems: 'flex-start' }}>
                                <ApiConsultationButton
                                  lead={lead}
                                  onInspect={() => setInspectedApiLead(lead)}
                                />
                                {hasCpfCandidates && (
                                  <button
                                    type="button"
                                    className="pill-btn primary"
                                    style={{
                                      fontSize: 11,
                                      maxWidth: '100%',
                                      whiteSpace: 'normal',
                                      textAlign: 'left',
                                      lineHeight: 1.2,
                                      padding: '6px 8px'
                                    }}
                                    onClick={() => openCpfPickerForLead(lead)}
                                    title="Abre a tela de revisão de CPFs. A consulta de telefone usa o Telegram."
                                  >
                                    🪪 CPFs encontrados ({cpfCandidates.length}) — Achar telefone?
                                  </button>
                                )}
                                {consults.length > 0 && (
                                  <button
                                    type="button"
                                    className="pill-btn"
                                    style={{ fontSize: 11, maxWidth: '100%' }}
                                    onClick={() => {
                                      setExpandedTelegramRefs((prev) => {
                                        const next = new Set(prev)
                                        if (next.has(ref)) next.delete(ref)
                                        else next.add(ref)
                                        return next
                                      })
                                    }}
                                    title={
                                      anyError
                                        ? `Algum provider falhou — abra para ver detalhes`
                                        : `${consults.length} consulta(s) salvas no Telegram`
                                    }
                                  >
                                    {anyError ? '⚠' : '📄'} Telegram · {consults.length} {expanded ? '▾' : '▸'}
                                  </button>
                                )}
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
                        {expanded && consults.length > 0 && (
                          <tr key={`telegram:${ref}`}>
                            <td colSpan={8} style={{ padding: 0, background: 'var(--surface-2)' }}>
                              <TelegramConsultMultiProviderPanel
                                consults={consults}
                                onStartPhoneRun={(cpf) => handleTelegramPhoneDirectExtract(ref, cpf)}
                                onRetryTelethon={() => void handleRetryTelethonForLead(ref)}
                                retryBusy={telegramTelethonPipelineRunning}
                              />
                            </td>
                          </tr>
                        )}
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
      {client && (
        <TelethonAuthDialog
          open={telethonAuthOpen}
          client={client}
          onSuccess={handleTelethonAuthSuccess}
          onClose={handleTelethonAuthClose}
        />
      )}
      <CpfPickerTelethonDialog
        open={cpfPicker !== null}
        client={client}
        tableId={activeId}
        leadRef={cpfPicker?.leadRef ?? ''}
        leadName={cpfPicker?.leadName ?? null}
        candidates={cpfPicker?.candidates ?? []}
        eligibleCpfs={cpfPicker?.eligibleCpfs ?? []}
        onClose={() => setCpfPicker(null)}
        onResult={handleCpfPickerResult}
        onAuthRequired={handleCpfPickerAuthRequired}
        onError={(message) => onFeedback('error', message)}
      />
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

function isTelethonAuthError(error: ApiError): boolean {
  const body = error.body
  if (body && typeof body === 'object' && 'detail' in body) {
    const detail = (body as { detail?: unknown }).detail
    if (typeof detail === 'string') {
      return detail.includes('telethon_session_not_authorized')
    }
  }
  return error.message.includes('telethon_session_not_authorized')
}

function TelethonRunLogPanel({ entries }: { entries: TelethonRunLogEntry[] }) {
  if (entries.length === 0) return null
  return (
    <div
      role="status"
      aria-live="polite"
      style={{
        display: 'grid',
        gap: 8,
        padding: '10px 12px',
        border: '0.5px solid var(--line)',
        borderRadius: 10,
        background: 'var(--surface-2)',
        maxWidth: '100%'
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 10 }}>
        <strong style={{ fontSize: 12, color: 'var(--ink)' }}>Trilha da consulta</strong>
        <span style={{ fontSize: 11, color: 'var(--ink-3)' }}>
          Sem nomes de fontes internas
        </span>
      </div>
      <ol
        style={{
          listStyle: 'none',
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))',
          gap: 8,
          padding: 0,
          margin: 0
        }}
      >
        {entries.map((entry) => (
          <li
            key={entry.id}
            style={{
              display: 'grid',
              gridTemplateColumns: '10px minmax(0, 1fr)',
              gap: 8,
              alignItems: 'start',
              padding: '8px 10px',
              borderRadius: 8,
              background: 'var(--surface)',
              border: '0.5px solid var(--line)'
            }}
          >
            <span
              aria-hidden="true"
              style={{
                width: 8,
                height: 8,
                borderRadius: 999,
                marginTop: 4,
                background:
                  entry.status === 'done'
                    ? 'var(--success)'
                    : entry.status === 'error'
                      ? 'var(--risky)'
                      : entry.status === 'active'
                        ? 'var(--accent)'
                        : 'var(--ink-5, rgba(0,0,0,0.16))',
                boxShadow:
                  entry.status === 'active'
                    ? '0 0 0 3px color-mix(in srgb, var(--accent) 16%, transparent)'
                    : undefined
              }}
            />
            <span style={{ minWidth: 0, display: 'grid', gap: 2 }}>
              <strong style={{ fontSize: 11.5, color: 'var(--ink)', fontWeight: 600 }}>
                {entry.label}
              </strong>
              <span style={{ fontSize: 11, color: 'var(--ink-3)', lineHeight: 1.25 }}>
                {entry.detail}
              </span>
            </span>
          </li>
        ))}
      </ol>
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

function internalFieldObjectLabel(fields: InternalEnrichField): string {
  if (fields === 'phone') return 'telefones via grupo Telegram'
  if (fields === 'both') return 'e-mails internos + telefones via grupo Telegram'
  return 'e-mails internos'
}

function internalEnrichDescription(fields: InternalEnrichField): string {
  if (fields === 'phone') {
    return 'Sem APIs pagas. O fluxo operacional de telefone via Telegram fica no botão "Pegar telefone via Telegram", que cruza as consultas disponíveis (inclusive por e-mail quando houver).'
  }
  if (fields === 'both') {
    return 'Sem APIs pagas. O app tenta inferir e-mails profissionais pelo domínio da empresa; para telefones, use o botão dedicado "Pegar telefone via Telegram".'
  }
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

function LeadEmailCell({ lead, hideFallback }: { lead: Lead; hideFallback?: boolean }) {
  const [expanded, setExpanded] = useState(false)
  const verifiedBy = parseArrayField<string>(lead.email_verified_by).filter(Boolean)
  const alternatives = parseArrayField<any>(lead.email_alternatives).filter((alt) => alt && alt.email)
  const hasBadge = verifiedBy.length >= 2
  const hasAlternatives = alternatives.length > 0

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
              title={`Sugerido por ${primary.source}`}
            >
              sugestão · {primary.source}
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
            title={`Verificado por: ${verifiedBy.join(', ')}`}
            aria-label={`Verificado por ${verifiedBy.join(', ')}`}
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
          via {lead.phone_source}
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
          ? `outro provider sugeriu 1 telefone`
          : `outros providers sugeriram ${count} telefones`}
      </button>
      {expanded && (
        <ul className="email-alt-list">
          {preview.map((alt, index) => (
            <li
              key={`${alt.phone}-${alt.source}-${index}`}
              className="email-alt-row"
            >
              <span className="email-alt-source">{alt.source}</span>
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
  const fieldsPerProvider = (provider: EnrichmentProvider): string[] => {
    const out: string[] = []
    if (fields === 'email' || fields === 'both') out.push('email')
    if ((fields === 'phone' || fields === 'both') && provider !== 'snovio') out.push('phone')
    return out
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

function TelegramConsultMultiProviderPanel({
  consults,
  onStartPhoneRun,
  onRetryTelethon,
  retryBusy = false
}: {
  consults: TelegramConsult[]
  onStartPhoneRun?: (cpf?: string) => void
  onRetryTelethon?: () => void
  retryBusy?: boolean
}) {
  const rows = buildTelegramComparisonRows(consults)
  const errorCount = consults.filter((consult) => consult.error || consult.blocked_reason).length
  const hasStructuredCpf = rows.some((row) => !!row.cpf)
  const canRetryTelethon = consults.length > 0 && !hasStructuredCpf && !!onRetryTelethon
  // Findex expira o link `api.fdxapis.us/temp/...` em poucos minutos.
  // Detecta tanto blocked_reason canônico quanto texto bruto — alguns
  // providers só anotam o HTML retornado.
  const findexExpired = consults.some(
    (c) =>
      isFindexPageExpired(c.error) ||
      isFindexPageExpired(c.blocked_reason) ||
      isFindexPageExpired(c.raw_text)
  )
  return (
    <div
      style={{
        padding: '12px 14px 14px',
        display: 'grid',
        gap: 10,
        maxWidth: '100%',
        overflow: 'hidden',
        borderTop: '1px solid var(--line)',
        borderBottom: '1px solid var(--line)'
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
        <div style={{ display: 'grid', gap: 2 }}>
          <strong style={{ fontSize: 12, color: 'var(--ink)' }}>Comparação Telegram</strong>
          <span style={{ fontSize: 11, color: 'var(--ink-3)' }}>
            {rows.length} CPF(s) consolidado(s) · {consults.length} evidência(s){errorCount ? ` · ${errorCount} alerta(s)` : ''}
          </span>
        </div>
      </div>
      {findexExpired && (
        <div
          role="alert"
          style={{
            display: 'flex',
            alignItems: 'flex-start',
            gap: 10,
            padding: '10px 12px',
            border: '0.5px solid rgba(220,38,38,0.28)',
            borderRadius: 8,
            background: 'rgba(220,38,38,0.06)',
            color: 'var(--ink-2)'
          }}
        >
          <span aria-hidden="true" style={{ fontSize: 16, lineHeight: 1 }}>⌛</span>
          <div style={{ display: 'grid', gap: 4, flex: 1, minWidth: 0 }}>
            <strong style={{ fontSize: 12.5, color: '#b91c1c' }}>
              Resultado da consulta expirou
            </strong>
            <span style={{ fontSize: 11.5, color: 'var(--ink-3)', lineHeight: 1.4 }}>
              O resultado temporário tem validade curta e foi fechado antes do app
              capturar o telefone. Rode a consulta de novo para gerar um resultado novo.
            </span>
          </div>
          {onRetryTelethon && (
            <button
              type="button"
              className="pill-btn primary"
              style={{ flexShrink: 0 }}
              onClick={(event) => {
                event.stopPropagation()
                onRetryTelethon()
              }}
              disabled={retryBusy}
              title="Repete a consulta de telefone para este lead — gera um resultado novo."
            >
              {retryBusy ? 'Tentando…' : 'Tentar novamente'}
            </button>
          )}
        </div>
      )}
      {canRetryTelethon && !findexExpired && (
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            gap: 12,
            padding: '10px 12px',
            border: '0.5px solid rgba(14,116,144,0.22)',
            borderRadius: 8,
            background: 'rgba(14,116,144,0.07)',
            color: 'var(--ink-2)',
            flexWrap: 'wrap'
          }}
        >
          <div style={{ display: 'grid', gap: 2, minWidth: 220 }}>
            <strong style={{ fontSize: 12, color: 'var(--ink)' }}>Sem CPF estruturado nesta busca</strong>
            <span style={{ fontSize: 11, color: 'var(--ink-3)', lineHeight: 1.35 }}>
              A consulta ficou salva, mas não trouxe CPF aproveitável. Você pode repetir pela sessão nativa sem abrir Chrome.
            </span>
          </div>
          <button
            type="button"
            className="pill-btn primary"
            onClick={(event) => {
              event.stopPropagation()
              onRetryTelethon?.()
            }}
            disabled={retryBusy}
            title="Repete a busca de telefone para este lead."
          >
            {retryBusy ? 'Tentando…' : 'Tentar novamente'}
          </button>
        </div>
      )}
      {rows.length > 0 ? (
        <div style={{ overflow: 'auto', maxHeight: 320, border: '1px solid var(--line)', borderRadius: 8, background: 'var(--surface)', maxWidth: '100%' }}>
          <table style={{ width: '100%', minWidth: 0, borderCollapse: 'collapse', tableLayout: 'fixed', fontSize: 12 }}>
            <thead>
              <tr style={{ background: 'var(--surface-2)', color: 'var(--ink-2)', textAlign: 'left' }}>
                <th style={{ padding: '8px 8px', width: '17%', fontWeight: 600 }}>CPF</th>
                <th style={{ padding: '8px 8px', width: '24%', fontWeight: 600 }}>Nome</th>
                <th style={{ padding: '8px 8px', width: '13%', fontWeight: 600 }}>Nasc.</th>
                <th style={{ padding: '8px 8px', width: '8%', fontWeight: 600 }}>Score</th>
                <th style={{ padding: '8px 8px', width: '10%', fontWeight: 600 }}>Evid.</th>
                <th style={{ padding: '8px 8px', width: '18%', fontWeight: 600 }}>Obs.</th>
                <th style={{ padding: '8px 8px', width: '10%', fontWeight: 600 }}></th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const score = row.scores.length > 0 ? Math.max(...row.scores) : null
                const conflicts = row.nomes.length > 1 || row.births.length > 1 || row.addresses.length > 1
                return (
                  <tr key={row.key} style={{ borderTop: '1px solid var(--line)', verticalAlign: 'top' }}>
                    <td style={{ padding: '9px 8px', fontFamily: 'var(--font-mono)', fontVariantNumeric: 'tabular-nums', color: 'var(--ink)', overflowWrap: 'anywhere' }}>
                      {row.cpf ?? '—'}
                    </td>
                    <td style={{ padding: '9px 8px', color: 'var(--ink)', fontWeight: 500, overflowWrap: 'anywhere' }}>
                      <ValueStack values={row.nomes} />
                    </td>
                    <td style={{ padding: '9px 8px', color: 'var(--ink-2)', fontVariantNumeric: 'tabular-nums', overflowWrap: 'anywhere' }}>
                      <ValueStack values={row.births} />
                    </td>
                    <td style={{ padding: '9px 8px', color: score !== null && score >= 70 ? 'var(--success)' : 'var(--ink-3)', fontWeight: 600 }}>
                      {score ?? '—'}
                    </td>
                    <td style={{ padding: '9px 8px', color: 'var(--ink-2)' }}>
                      {row.sourceCount}
                      {row.queryTypes.has('cpf') ? <span style={{ color: 'var(--ink-3)' }}> · cpf</span> : null}
                    </td>
                    <td style={{ padding: '9px 8px', color: conflicts ? 'var(--warning)' : 'var(--ink-3)', lineHeight: 1.35, overflowWrap: 'anywhere' }}>
                      {row.errors.length > 0 ? (
                        <ValueStack values={row.errors.map(humanizeBlockedReason)} />
                      ) : conflicts ? (
                        'Dados divergentes entre evidências.'
                      ) : row.addresses.length > 0 ? (
                        <ValueStack values={row.addresses} />
                      ) : (
                        'Dados alinhados.'
                      )}
                    </td>
                    <td style={{ padding: '8px 8px', textAlign: 'right' }}>
                      {onStartPhoneRun && row.cpf ? (
                        <button
                          className="pill-btn primary"
                          style={{ whiteSpace: 'normal', maxWidth: '100%', minWidth: 0, height: 'auto', lineHeight: 1.15, padding: '6px 8px' }}
                          onClick={(e) => {
                            e.stopPropagation()
                            onStartPhoneRun(row.cpf ?? undefined)
                          }}
                          title="Buscar telefone usando este CPF"
                        >
                          Achar telefone
                        </button>
                      ) : null}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <div style={{ color: 'var(--ink-3)', fontSize: 12, padding: '8px 10px', border: '1px solid var(--line)', borderRadius: 8, background: 'var(--surface)' }}>
          Nenhum CPF estruturado nas evidências salvas.
        </div>
      )}
    </div>
  )
}

interface TelegramComparisonRow {
  key: string
  cpf: string | null
  nomes: string[]
  births: string[]
  addresses: string[]
  scores: number[]
  errors: string[]
  sourceCount: number
  queryTypes: Set<string>
}

function buildTelegramComparisonRows(consults: TelegramConsult[]): TelegramComparisonRow[] {
  const rows = new Map<string, TelegramComparisonRow>()
  const addRow = (
    key: string,
    payload: {
      cpf?: string | null
      nome?: string | null
      birth?: string | null
      address?: string | null
      score?: number | null
      error?: string | null
      queryType?: string | null
    }
  ) => {
    const existing = rows.get(key) ?? {
      key,
      cpf: payload.cpf ?? null,
      nomes: [],
      births: [],
      addresses: [],
      scores: [],
      errors: [],
      sourceCount: 0,
      queryTypes: new Set<string>()
    }
    existing.sourceCount += 1
    if (payload.cpf && !existing.cpf) existing.cpf = payload.cpf
    pushUnique(existing.nomes, payload.nome)
    pushUnique(existing.births, payload.birth)
    pushUnique(existing.addresses, payload.address)
    if (typeof payload.score === 'number') existing.scores.push(payload.score)
    pushUnique(existing.errors, payload.error)
    if (payload.queryType) existing.queryTypes.add(payload.queryType)
    rows.set(key, existing)
  }

  for (const consult of consults) {
    const queryType = consult.query_type ?? 'name'
    const error = consult.error || consult.blocked_reason || null
    const candidates = Array.isArray(consult.extracted_candidates) ? consult.extracted_candidates : []
    if (error && !consult.extracted_cpf && candidates.length === 0) {
      continue
    }
    if (candidates.length > 0) {
      for (const candidate of candidates) {
        const cpf = candidate.cpf || consult.extracted_cpf || null
        addRow(cpf ? `cpf:${cpf}` : `consult:${consult.id}`, {
          cpf,
          nome: candidate.nome || consult.extracted_nome,
          birth: candidate.data_nascimento || consult.extracted_birth_date,
          address: candidate.endereco || consult.extracted_address,
          score: candidate.match_score ?? consult.match_score,
          error,
          queryType
        })
      }
      continue
    }
    const cpf = consult.extracted_cpf || null
    addRow(cpf ? `cpf:${cpf}` : `consult:${consult.id}`, {
      cpf,
      nome: consult.extracted_nome,
      birth: consult.extracted_birth_date,
      address: consult.extracted_address,
      score: consult.match_score,
      error,
      queryType
    })
  }

  return Array.from(rows.values()).sort((a, b) => {
    const aScore = a.scores.length > 0 ? Math.max(...a.scores) : -1
    const bScore = b.scores.length > 0 ? Math.max(...b.scores) : -1
    return bScore - aScore || b.sourceCount - a.sourceCount || (a.cpf ?? '').localeCompare(b.cpf ?? '')
  })
}

function pushUnique(target: string[], value?: string | null): void {
  const clean = (value ?? '').trim()
  if (clean && !target.includes(clean)) target.push(clean)
}

function ValueStack({ values }: { values: string[] }) {
  if (values.length === 0) return <>—</>
  return (
    <span style={{ display: 'grid', gap: 2 }}>
      {values.slice(0, 3).map((value) => (
        <span key={value}>{value}</span>
      ))}
      {values.length > 3 && <span style={{ color: 'var(--ink-3)' }}>+{values.length - 3}</span>}
    </span>
  )
}

function TelegramConsultProviderBlock({ consult, onStartPhoneRun }: { consult: TelegramConsult, onStartPhoneRun?: (cpf?: string) => void }) {
  const handleCopy = () => {
    if (consult.raw_text) {
      void navigator.clipboard.writeText(consult.raw_text).catch(() => {})
    }
  }
  // Provider names are internal — the UI labels evidence rows by *what the
  // consult does* (name vs CPF lookup), never by the underlying source.
  const providerLabel =
    consult.provider === 'finder_cpf' || consult.provider === 'gon_cpf'
      ? 'Consulta por CPF'
      : 'Consulta por nome'
  const stage = consult.query_type ?? 'name'
  const stageBadge =
    stage === 'cpf'
      ? { label: 'Follow-up CPF', color: 'var(--accent)', bg: 'var(--accent-tint)' }
      : stage === 'phone'
        ? { label: 'Follow-up telefone', color: '#0e7490', bg: 'rgba(14,116,144,0.12)' }
        : null
  const firstCandidateCpf = Array.isArray(consult.extracted_candidates)
    ? consult.extracted_candidates.find((candidate) => candidate?.cpf)?.cpf
    : undefined
  const phoneLookupCpf = consult.extracted_cpf || firstCandidateCpf

  return (
    <div
      style={{
        display: 'grid',
        gap: 8,
        border: '1px solid var(--line)',
        borderRadius: '10px',
        padding: '12px',
        background: 'var(--surface)',
        boxShadow: '0 1px 3px rgba(0,0,0,0.02)'
      }}
    >
      <div style={{ display: 'flex', gap: 12, alignItems: 'center', fontSize: 11, color: 'var(--ink-3)', flexWrap: 'wrap' }}>
        <strong style={{ color: 'var(--ink)', fontSize: '12px', fontWeight: 600 }}>{providerLabel}</strong>
        {stageBadge && (
          <span
            style={{
              padding: '2px 8px',
              borderRadius: '999px',
              fontSize: '10px',
              color: stageBadge.color,
              background: stageBadge.bg,
              fontWeight: 600
            }}
          >
            {stageBadge.label}
          </span>
        )}
        <span style={{ fontSize: '11.5px' }}>Consulta: <code style={{ fontFamily: 'var(--font-mono)', background: 'var(--surface-3)', padding: '2px 4px', borderRadius: '4px' }}>{consult.query || '—'}</code></span>
        {consult.downloaded_at && <span>· {formatDate(consult.downloaded_at)}</span>}
        {consult.source_url && (
          <a href={consult.source_url} target="_blank" rel="noreferrer" style={{ color: 'var(--accent)', textDecoration: 'none', fontWeight: 500 }}>
            Fonte ↗
          </a>
        )}
        {consult.raw_text && (
          <button
            type="button"
            className="pill-btn"
            style={{ fontSize: 10, padding: '2px 6px', height: '20px' }}
            onClick={handleCopy}
          >
            Copiar texto
          </button>
        )}
      </div>
      {consult.blocked_reason && (() => {
        const tone = classifyBlockedReason(consult.blocked_reason)
        return (
          <div
            style={{
              color: tone.color,
              fontSize: 12,
              background: tone.background,
              border: `0.5px solid ${tone.border}`,
              padding: '6px 10px',
              borderRadius: '6px',
              display: 'flex',
              alignItems: 'center',
              gap: 6
            }}
          >
            <span>{tone.icon}</span>{' '}
            <strong>{tone.label}:</strong>{' '}
            {humanizeBlockedReason(consult.blocked_reason)}
          </div>
        )
      })()}
      {consult.error && !consult.blocked_reason && (
        <div style={{ color: 'var(--risky)', fontSize: 12, background: 'rgba(255,59,48,0.06)', border: '0.5px solid rgba(255,59,48,0.15)', padding: '6px 10px', borderRadius: '6px', display: 'flex', alignItems: 'center', gap: 6 }}>
          <span>⚠</span> <strong>Erro:</strong> {consult.error}
        </div>
      )}
      {(consult.extracted_nome || consult.extracted_cpf || consult.extracted_birth_date || consult.extracted_address) && (
        <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 10 }}>
          <div
            style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))',
              gap: 10,
              padding: '10px',
              background: 'var(--surface-2)',
              borderRadius: '8px',
              border: '0.5px solid var(--line)',
              fontSize: 12,
              flex: 1
            }}
          >
            {consult.extracted_nome && (
              <div>
                <span style={{ color: 'var(--ink-3)', fontWeight: 500 }}>Nome Extraído:</span>{' '}
                <strong style={{ color: 'var(--ink)' }}>{consult.extracted_nome}</strong>
              </div>
            )}
            {consult.extracted_cpf && (
              <div>
                <span style={{ color: 'var(--ink-3)', fontWeight: 500 }}>CPF Extraído:</span>{' '}
                <strong style={{ color: 'var(--ink)', fontFamily: 'var(--font-mono)' }}>{consult.extracted_cpf}</strong>
              </div>
            )}
            {consult.extracted_birth_date && (
              <div>
                <span style={{ color: 'var(--ink-3)', fontWeight: 500 }}>Nascimento:</span>{' '}
                <span style={{ color: 'var(--ink)' }}>{consult.extracted_birth_date}</span>
              </div>
            )}
            {consult.extracted_address && (
              <div style={{ gridColumn: '1 / -1' }}>
                <span style={{ color: 'var(--ink-3)', fontWeight: 500 }}>Endereço:</span>{' '}
                <span style={{ color: 'var(--ink)' }}>{consult.extracted_address}</span>
              </div>
            )}
          </div>
          {onStartPhoneRun && phoneLookupCpf && (
            <button
              className="pill-btn primary"
              style={{ whiteSpace: 'nowrap', marginTop: 10 }}
              onClick={(e) => {
                e.stopPropagation()
                onStartPhoneRun(phoneLookupCpf)
              }}
              title="Buscar telefone usando o CPF selecionado"
            >
              Achar telefone
            </button>
          )}
        </div>
      )}
      {Array.isArray(consult.extracted_candidates) && consult.extracted_candidates.length > 1 && (
        <TelegramCandidatesTable candidates={consult.extracted_candidates} />
      )}
      {consult.raw_text ? (
        <details style={{ marginTop: 4 }}>
          <summary style={{ fontSize: 11, color: 'var(--ink-3)', cursor: 'pointer', userSelect: 'none', fontWeight: 500 }}>
            Ver texto completo da consulta Telegram
          </summary>
          <pre
            style={{
              margin: '6px 0 0',
              padding: 10,
              background: 'var(--surface-2)',
              border: '1px solid var(--line)',
              borderRadius: 8,
              fontSize: 11.5,
              fontFamily: 'var(--font-mono)',
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
              maxHeight: 200,
              overflow: 'auto',
              color: 'var(--ink-2)'
            }}
          >
            {consult.raw_text}
          </pre>
        </details>
      ) : !consult.error ? (
        <div style={{ color: 'var(--ink-3)', fontSize: 11, fontStyle: 'italic' }}>Sem texto retornado do bot.</div>
      ) : null}
    </div>
  )
}

interface BlockedReasonTone {
  color: string
  background: string
  border: string
  icon: string
  label: string
}

/**
 * Detecta se o erro/texto bruto de uma consulta indica que a página
 * temporária do Findex (`api.fdxapis.us/temp/...`) expirou — o link
 * tem TTL curto e devolve uma página "Página Expirada / Tempo esgotado"
 * quando o operador (ou o scraper) chega tarde. O backend pode retornar
 * isso como `findex_page_expired` ou texto bruto contendo marcadores
 * conhecidos. Detector tolerante a variações de string.
 */
function isFindexPageExpired(text: string | null | undefined): boolean {
  if (!text) return false
  const t = text.toLowerCase()
  return (
    t.includes('findex_page_expired') ||
    t.includes('página expirada') ||
    t.includes('pagina expirada') ||
    t.includes('tempo esgotado') ||
    t.includes('tempo de acesso terminou') ||
    t.includes('esta página não está mais disponível') ||
    t.includes('esta pagina nao esta mais disponivel')
  )
}

function classifyBlockedReason(reason: string): BlockedReasonTone {
  const normalized = (reason || '').toLowerCase()
  // Findex página expirada → vermelho dedicado, operador precisa
  // rodar de novo (link tem TTL curto).
  if (isFindexPageExpired(normalized)) {
    return {
      color: '#b91c1c',
      background: 'rgba(220,38,38,0.06)',
      border: 'rgba(220,38,38,0.18)',
      icon: '⌛',
      label: 'Resultado expirado'
    }
  }
  // Recuperáveis: o pipeline já tentou outro caminho ou pode tentar de novo.
  // Tom amarelo/info — não é falha do operador.
  if (
    normalized.includes('routed_to_findex') ||
    normalized === 'email_stage_no_phone' ||
    normalized === 'no_cpf_from_name_stage' ||
    normalized === 'no_eligible_cpf' ||
    normalized === 'telethon_group_join_pending' ||
    normalized === 'telethon_result_timeout'
  ) {
    return {
      color: '#b45309',
      background: 'rgba(245,158,11,0.08)',
      border: 'rgba(245,158,11,0.2)',
      icon: 'ℹ️',
      label: 'Sem resultado'
    }
  }
  // Telegram access problems the operator must fix before retrying.
  if (
    normalized === 'telethon_not_in_group' ||
    normalized === 'telethon_bot_not_started' ||
    normalized === 'telethon_blocked_bot' ||
    normalized === 'telethon_session_not_authorized'
  ) {
    return {
      color: '#b91c1c',
      background: 'rgba(220,38,38,0.06)',
      border: 'rgba(220,38,38,0.18)',
      icon: '🔐',
      label: 'Ação necessária'
    }
  }
  // Bloqueios reais: o operador precisa fazer algo (revalidar LinkedIn,
  // esperar cooldown, encontrar um e-mail). Tom vermelho.
  if (
    normalized === 'rate_limited' ||
    normalized === 'no_email_for_findex_fallback' ||
    normalized === 'linkedin_cargo_divergente' ||
    normalized === 'missing_linkedin_signals' ||
    normalized === 'linkedin_titulo_ausente' ||
    normalized.startsWith('name_stage_error') ||
    normalized.startsWith('email_stage_error')
  ) {
    return {
      color: '#b91c1c',
      background: 'rgba(220,38,38,0.06)',
      border: 'rgba(220,38,38,0.18)',
      icon: '🚫',
      label: 'Bloqueado'
    }
  }
  return {
    color: '#b45309',
    background: 'rgba(245,158,11,0.08)',
    border: 'rgba(245,158,11,0.2)',
    icon: '🚫',
    label: 'Bloqueado'
  }
}

function humanizeBlockedReason(reason: string): string {
  const normalized = (reason || '').toLowerCase()
  if (isFindexPageExpired(normalized)) {
    return 'o resultado temporário da consulta expirou antes do app capturar o telefone — rode a consulta de novo.'
  }
  if (normalized.includes('routed_to_findex')) {
    if (normalized.startsWith('linkedin_cargo_divergente')) {
      return 'cargo do LinkedIn não bate com o alvo — tentou a consulta via e-mail.'
    }
    if (normalized.startsWith('linkedin_titulo_ausente')) {
      return 'sem cargo no LinkedIn — tentou a consulta via e-mail.'
    }
    if (normalized.startsWith('missing_linkedin_signals')) {
      return 'sem sinais LinkedIn — tentou a consulta via e-mail.'
    }
    return 'desviado para a consulta via e-mail.'
  }
  const map: Record<string, string> = {
    linkedin_cargo_divergente: 'cargo do LinkedIn diverge do alvo e o lead não tem e-mail para a consulta alternativa.',
    linkedin_titulo_ausente: 'sem título no LinkedIn e sem e-mail para a consulta alternativa.',
    missing_linkedin_signals: 'sinais LinkedIn ausentes e sem e-mail para a consulta alternativa.',
    no_eligible_cpf: 'nenhum CPF passou a comparação com sinais LinkedIn.',
    no_cpf_from_name_stage: 'a consulta por nome não devolveu CPF.',
    no_email_for_findex_fallback: 'sem e-mail disponível para a consulta alternativa.',
    email_stage_no_phone: 'a consulta por e-mail não retornou telefone.',
    rate_limited: 'serviço de consulta em cooldown — tente novamente em alguns minutos.',
    lead_sem_nome_ou_ref: 'lead sem nome ou referência canônica.',
    telethon_not_in_group: 'sua conta não está no grupo de consultas necessário — entre no grupo e tente de novo.',
    telethon_group_join_pending: 'o ingresso no grupo de consultas aguarda aprovação — tente de novo depois de ser aceito.',
    telethon_bot_not_started: 'inicie a conversa com o serviço de consulta (botão Iniciar/Start) e tente de novo.',
    telethon_blocked_bot: 'o serviço de consulta está bloqueado na sua conta do Telegram — desbloqueie e tente de novo.',
    telethon_session_not_authorized: 'sessão do Telegram não autenticada — faça login no Telegram nas configurações.',
    telethon_result_timeout: 'o serviço de consulta não respondeu a tempo — tente novamente.',
    telegram_not_configured: 'integração do Telegram não configurada.'
  }
  return map[normalized] ?? reason
}

function TelegramCandidatesTable({ candidates }: { candidates: TelegramConsult['extracted_candidates'] }) {
  return (
    <div style={{ marginTop: 8, minWidth: 0 }}>
      <div style={{ fontSize: 11, color: 'var(--ink-3)', marginBottom: 6, fontWeight: 500 }}>
        🔍 {candidates.length} candidatos encontrados · ranqueados por correspondência com LinkedIn
      </div>
      <div style={{ overflow: 'auto', maxHeight: 300, border: '1px solid var(--line)', borderRadius: '8px', background: 'var(--surface)' }}>
        <table style={{ width: '100%', minWidth: '760px', borderCollapse: 'collapse', fontSize: 12 }}>
          <thead>
            <tr style={{ background: 'var(--surface-2)', color: 'var(--ink-2)', borderBottom: '1px solid var(--line)', textAlign: 'left' }}>
              <th style={{ padding: '8px 10px', width: '60px', fontWeight: 600 }}>Score</th>
              <th style={{ padding: '8px 10px', width: '120px', fontWeight: 600 }}>CPF</th>
              <th style={{ padding: '8px 10px', minWidth: '180px', fontWeight: 600 }}>Nome</th>
              <th style={{ padding: '8px 10px', width: '100px', fontWeight: 600 }}>Nascimento</th>
              <th style={{ padding: '8px 10px', minWidth: '200px', fontWeight: 600 }}>Endereço</th>
              <th style={{ padding: '8px 10px', minWidth: '120px', fontWeight: 600 }}>Sinais</th>
            </tr>
          </thead>
          <tbody>
            {candidates.map((c, idx) => {
              const isHighMatch = (c.match_score ?? 0) >= 70
              const isMediumMatch = (c.match_score ?? 0) >= 50 && (c.match_score ?? 0) < 70
              return (
                <tr
                  key={`${c.cpf}:${idx}`}
                  style={{
                    borderTop: '0.5px solid var(--line)',
                    background: isHighMatch ? 'rgba(52, 199, 89, 0.03)' : 'transparent',
                    transition: 'background-color 150ms'
                  }}
                  className="hover:bg-surface-2"
                >
                  <td
                    style={{
                      padding: '8px 10px',
                      fontWeight: 600,
                      color: isHighMatch ? 'var(--success)' : isMediumMatch ? 'var(--warning)' : 'var(--ink-3)'
                    }}
                  >
                    {c.match_score ?? '—'}
                  </td>
                  <td style={{ padding: '8px 10px', fontVariantNumeric: 'tabular-nums', letterSpacing: '-0.02em' }}>
                    {c.cpf}
                  </td>
                  <td style={{ padding: '8px 10px', fontWeight: 500, color: 'var(--ink)' }}>
                    {c.nome ?? '—'}
                  </td>
                  <td style={{ padding: '8px 10px', fontVariantNumeric: 'tabular-nums', color: 'var(--ink-2)' }}>
                    {c.data_nascimento ?? '—'}
                  </td>
                  <td style={{ padding: '8px 10px', color: 'var(--ink-2)', lineHeight: 1.3 }}>
                    {c.endereco ?? '—'}
                  </td>
                  <td style={{ padding: '8px 10px', color: 'var(--ink-3)', fontSize: 11 }}>
                    {(c.signals_used ?? []).map(s => (
                      <span
                        key={s}
                        style={{
                          display: 'inline-block',
                          padding: '1px 5px',
                          borderRadius: '4px',
                          background: 'var(--surface-3)',
                          marginRight: '3px',
                          marginBottom: '2px',
                          fontSize: '10px'
                        }}
                      >
                        {s}
                      </span>
                    )) || '—'}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
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

/**
 * Dedupe candidatos de CPF entre as evidências persistidas (Finder, Gon,
 * Unix, etc.) e devolve uma lista ordenada por match_score desc — o shape
 * casa com ``TelegramPhoneRankedCandidate`` esperado pelo CpfReviewList.
 *
 * Regras:
 *  - Linhas com ``query_type === 'cpf'`` são follow-ups do CPF e não
 *    geram cards novos no picker (a UI dessa tela é sobre escolher CPF
 *    pra disparar telefone).
 *  - Quando o mesmo CPF aparece em mais de uma fonte, mantemos o maior
 *    ``match_score`` e completamos campos faltantes (nome, nascimento,
 *    endereço, sinais) com o que tiver na primeira fonte que trouxe.
 *  - ``eligible`` segue o flag explícito do candidato quando vier; senão
 *    usamos score >= 65 como heurística (mesmo limiar do matcher).
 */
function collectCpfCandidatesFromConsults(
  consults: readonly TelegramConsult[]
): TelegramPhoneRankedCandidate[] {
  const byCpf = new Map<string, TelegramPhoneRankedCandidate>()
  for (const consult of consults) {
    if ((consult.query_type ?? 'name') === 'cpf') continue
    const raw: TelegramConsultCandidate[] = Array.isArray(consult.extracted_candidates)
      ? consult.extracted_candidates
      : []
    for (const candidate of raw) {
      if (!candidate || !candidate.cpf) continue
      const cpf = candidate.cpf
      const incomingScore =
        typeof candidate.match_score === 'number' ? candidate.match_score : 0
      const explicitEligible = (candidate as { eligible?: unknown }).eligible
      const eligible =
        typeof explicitEligible === 'boolean' ? explicitEligible : incomingScore >= 65
      const incoming: TelegramPhoneRankedCandidate = {
        cpf,
        nome: candidate.nome ?? null,
        data_nascimento: candidate.data_nascimento ?? null,
        endereco: candidate.endereco ?? null,
        match_score: incomingScore,
        signals_used: Array.isArray(candidate.signals_used) ? candidate.signals_used : [],
        breakdown: (candidate.breakdown as Record<string, unknown>) ?? {},
        eligible
      }
      const existing = byCpf.get(cpf)
      if (!existing) {
        byCpf.set(cpf, incoming)
        continue
      }
      const merged: TelegramPhoneRankedCandidate = {
        ...existing,
        nome: existing.nome ?? incoming.nome ?? null,
        data_nascimento: existing.data_nascimento ?? incoming.data_nascimento ?? null,
        endereco: existing.endereco ?? incoming.endereco ?? null,
        match_score: Math.max(existing.match_score, incoming.match_score),
        signals_used:
          existing.signals_used.length >= incoming.signals_used.length
            ? existing.signals_used
            : incoming.signals_used,
        breakdown:
          Object.keys(existing.breakdown).length >= Object.keys(incoming.breakdown).length
            ? existing.breakdown
            : incoming.breakdown,
        eligible: existing.eligible || incoming.eligible
      }
      byCpf.set(cpf, merged)
    }
  }
  return Array.from(byCpf.values())
    .filter(cpfAllowedForReview)
    .sort((a, b) => b.match_score - a.match_score)
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
    .replace(/[\u0300-\u036f]/g, '')
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
  fields: EnrichmentFields
): EnrichmentProvider[] {
  const usable = providers.filter((p) => {
    if (p === 'snovio' && fields === 'phone') return false
    return true
  })
  return [...usable].sort((a, b) => {
    const ca = Number.parseFloat(costs[a] || '0') || 0
    const cb = Number.parseFloat(costs[b] || '0') || 0
    return ca - cb
  })
}

function filterPending(
  allLeads: Lead[],
  wantedRefs: Set<string>,
  fields: EnrichmentFields
): string[] {
  const out: string[] = []
  for (const lead of allLeads) {
    const ref = leadRef(lead)
    if (!wantedRefs.has(ref)) continue
    let missing = false
    if (fields === 'email') missing = !lead.email
    else if (fields === 'phone') missing = !lead.phone
    else missing = !lead.email || !lead.phone
    if (missing) out.push(ref)
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
