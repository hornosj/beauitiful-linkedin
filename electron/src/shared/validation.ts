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
  rolePreset: 'marketing_growth',
  titles: 'marketing, growth, cmo, head of marketing, demand generation, performance marketing',
  generalSearch: false,
  maxResults: 25,
  cardsPerCycle: 10,
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
