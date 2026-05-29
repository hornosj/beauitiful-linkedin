import type {
  ApiKeyOverrides,
  FilterPayload,
  JobFunction,
  ScrapeMode,
  SearchRequest,
  Seniority,
  TaxonomyItem
} from './types'

export interface SearchFormFilters {
  seniority: Seniority[]
  functions: JobFunction[]
  locations: string
  excludeTitles: string
  minConfidence: string
  dropUnclassified: boolean
}

export interface SearchFormState {
  companyName: string
  companyDomain: string
  linkedinUrl: string
  /** Empresas adicionais (nome OU URL do LinkedIn). Cada entrada dispara
   *  sua própria busca. Vazio = busca de empresa única (legado). */
  extraCompanies: string[]
  /** Com 2+ empresas: 'single' agrega tudo numa tabela (coluna identifica
   *  a empresa) e 'separate' cria uma tabela por empresa. */
  tableMode: 'single' | 'separate'
  rolePreset: string
  titles: string
  generalSearch: boolean
  maxResults: number
  cardsPerCycle: number
  scrapeMode: ScrapeMode
  searchDepth: NonNullable<SearchRequest['search_depth']>
  outputPath: string
  includeUncertain: boolean
  acceptRisk: boolean
  filters: SearchFormFilters
}

export type ValidationErrors = Partial<Record<keyof SearchFormState, string>>

export type ValidationResult =
  | { ok: true }
  | { ok: false; errors: ValidationErrors }

export function isRiskyScrapeMode(mode: ScrapeMode): boolean {
  return mode === 'browser'
}

export function validateSearchForm(form: SearchFormState): ValidationResult {
  const errors: ValidationErrors = {}

  if (!form.companyName.trim()) {
    errors.companyName = 'Nome da empresa é obrigatório.'
  }
  const titles = parseTitles(form.titles)
  if (titles.length === 0 && !form.generalSearch) {
    errors.titles =
      'Informe pelo menos um cargo ou termo, ou marque a busca geral.'
  }
  if (!form.outputPath.trim()) {
    errors.outputPath = 'Defina um caminho de saída (.csv ou .xlsx).'
  }
  if (form.maxResults <= 0 || form.maxResults > 500) {
    errors.maxResults = 'Use um valor entre 1 e 500.'
  }
  if (
    form.scrapeMode === 'people_search' &&
    (form.cardsPerCycle <= 0 || form.cardsPerCycle > 200)
  ) {
    errors.cardsPerCycle = 'Cards por ciclo deve estar entre 1 e 200.'
  }
  if (isRiskyScrapeMode(form.scrapeMode) && !form.acceptRisk) {
    errors.acceptRisk =
      'Você precisa aceitar o risco antes de rodar o modo navegador (Playwright).'
  }

  if (Object.keys(errors).length > 0) {
    return { ok: false, errors }
  }
  return { ok: true }
}

const API_KEY_FIELDS: (keyof ApiKeyOverrides)[] = [
  'brave_search_api_key',
  'google_custom_search_api_key',
  'google_custom_search_cx',
  'serper_api_key',
  'searxng_base_url',
  'people_data_labs_api_key',
  'coresignal_api_key',
  'apollo_api_key',
  'lusha_api_key',
  'apify_api_key',
  'linkedin_li_at_cookie',
  'linkedin_cookie_browser'
]

export function buildSearchRequestFromForm(
  form: SearchFormState,
  apiKeys?: ApiKeyOverrides
): SearchRequest {
  const titles = parseTitles(form.titles)
  const filters = buildFilterPayload(form.filters)
  const configuredApiKeys = buildApiKeyOverridesPayload(apiKeys)
  const request: SearchRequest = {
    company_name: form.companyName.trim(),
    titles: form.generalSearch ? [] : titles,
    general_search: form.generalSearch,
    max_results: form.maxResults,
    scrape_mode: form.scrapeMode,
    search_depth: form.searchDepth,
    output_path: form.outputPath.trim(),
    include_uncertain: form.includeUncertain,
    accept_risk: form.acceptRisk,
    filters
  }
  if (form.companyDomain.trim()) request.company_domain = form.companyDomain.trim()
  if (form.linkedinUrl.trim()) request.linkedin_url = form.linkedinUrl.trim()
  if (configuredApiKeys) request.api_keys = configuredApiKeys
  if (form.scrapeMode === 'people_search' && form.cardsPerCycle > 0) {
    request.cards_per_cycle = form.cardsPerCycle
  }
  return request
}

/** A single company to search, normalized from the form's primary fields
 *  or from a free-text "extra company" entry (name or LinkedIn URL). */
export interface CompanyDraft {
  name: string
  domain?: string
  linkedinUrl?: string
}

const LINKEDIN_COMPANY_URL = /linkedin\.com\/company\/([^/?#]+)/i

function companyNameFromLinkedInUrl(url: string): string {
  const match = url.match(LINKEDIN_COMPANY_URL)
  if (!match) return url.trim()
  // slug → readable name: "mercadolivre-com" → "mercadolivre com". Good
  // enough as a label/dedupe key; the URL still drives the actual scrape.
  return match[1].replace(/-/g, ' ').trim() || url.trim()
}

function draftFromEntry(value: string): CompanyDraft | null {
  const trimmed = value.trim()
  if (!trimmed) return null
  if (LINKEDIN_COMPANY_URL.test(trimmed)) {
    return { name: companyNameFromLinkedInUrl(trimmed), linkedinUrl: trimmed }
  }
  return { name: trimmed }
}

/** Collect every company to search: the primary fields first, then each
 *  non-blank extra entry, deduped by URL/name. Returns [] when the form
 *  has no company at all. */
export function collectCompanies(form: SearchFormState): CompanyDraft[] {
  const drafts: CompanyDraft[] = []
  const seen = new Set<string>()
  const push = (draft: CompanyDraft | null): void => {
    if (!draft) return
    const key = (draft.linkedinUrl || draft.name).trim().toLowerCase()
    if (!key || seen.has(key)) return
    seen.add(key)
    drafts.push(draft)
  }
  if (form.companyName.trim()) {
    push({
      name: form.companyName.trim(),
      domain: form.companyDomain.trim() || undefined,
      linkedinUrl: form.linkedinUrl.trim() || undefined
    })
  }
  for (const entry of form.extraCompanies) push(draftFromEntry(entry))
  return drafts
}

/** Build a search request for one specific company, reusing every shared
 *  parameter from the form (titles, filters, max_results, scrape mode, …)
 *  and overriding only the company identity. */
export function buildSearchRequestForCompany(
  form: SearchFormState,
  company: CompanyDraft,
  apiKeys?: ApiKeyOverrides
): SearchRequest {
  const request = buildSearchRequestFromForm(form, apiKeys)
  request.company_name = company.name
  if (company.domain) request.company_domain = company.domain
  else delete request.company_domain
  if (company.linkedinUrl) request.linkedin_url = company.linkedinUrl
  else delete request.linkedin_url
  return request
}

export function applyRolePresetToForm(
  form: SearchFormState,
  preset: TaxonomyItem | null
): SearchFormState {
  if (!preset) {
    return { ...form, rolePreset: 'custom' }
  }
  const terms = (preset.aliases ?? []).map((term) => term.trim()).filter(Boolean)
  return {
    ...form,
    rolePreset: preset.value,
    titles: terms.join(', ')
  }
}

function buildFilterPayload(filters: SearchFormFilters): FilterPayload {
  const minConfidence = filters.minConfidence.trim()
    ? Number.parseInt(filters.minConfidence, 10)
    : null
  return {
    seniority_in: filters.seniority,
    functions_in: filters.functions,
    locations_in: parseCsv(filters.locations),
    exclude_titles: parseCsv(filters.excludeTitles),
    min_confidence_score:
      minConfidence !== null && Number.isFinite(minConfidence) ? minConfidence : null,
    drop_unclassified: filters.dropUnclassified
  }
}

function parseTitles(value: string): string[] {
  return parseCsv(value)
}

function parseCsv(value: string): string[] {
  return value
    .split(',')
    .map((entry) => entry.trim())
    .filter((entry) => entry.length > 0)
}

function buildApiKeyOverridesPayload(apiKeys?: ApiKeyOverrides): ApiKeyOverrides | undefined {
  if (!apiKeys) return undefined
  const payload: ApiKeyOverrides = {}
  for (const field of API_KEY_FIELDS) {
    const value = apiKeys[field]
    if (typeof value !== 'string') continue
    const cleaned = value.trim()
    if (cleaned) {
      payload[field] = cleaned
    }
  }
  return Object.keys(payload).length ? payload : undefined
}

export const emptyFormState: SearchFormState = {
  companyName: '',
  companyDomain: '',
  linkedinUrl: '',
  extraCompanies: [],
  tableMode: 'single',
  rolePreset: 'marketing_growth',
  titles: 'marketing, growth, cmo, head of marketing, demand generation, performance marketing',
  generalSearch: false,
  maxResults: 25,
  cardsPerCycle: 8,
  scrapeMode: 'people_search',
  searchDepth: 'standard',
  outputPath: 'output/leads.csv',
  includeUncertain: false,
  acceptRisk: false,
  filters: {
    seniority: [],
    functions: [],
    locations: '',
    excludeTitles: '',
    minConfidence: '',
    dropUnclassified: false
  }
}
