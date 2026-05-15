// TypeScript types mirroring the FastAPI sidecar contract.
// Update both sides when changing models in beautiful_linkedin/server/app.py.

export type ScrapeMode = 'api' | 'serp' | 'cookie' | 'people_search' | 'browser'

export type Seniority =
  | 'c_level'
  | 'vp'
  | 'director'
  | 'head'
  | 'manager'
  | 'senior'
  | 'mid'
  | 'junior'
  | 'intern'

export type JobFunction =
  | 'marketing'
  | 'sales'
  | 'engineering'
  | 'product'
  | 'design'
  | 'data'
  | 'finance'
  | 'hr'
  | 'operations'
  | 'legal'
  | 'customer_success'
  | 'executive'

export interface FilterPayload {
  seniority_in: Seniority[]
  functions_in: JobFunction[]
  locations_in: string[]
  exclude_titles: string[]
  min_confidence_score: number | null
  drop_unclassified: boolean
}

export interface ApiKeyOverrides {
  brave_search_api_key?: string
  google_custom_search_api_key?: string
  google_custom_search_cx?: string
  serper_api_key?: string
  searxng_base_url?: string
  people_data_labs_api_key?: string
  coresignal_api_key?: string
  apollo_api_key?: string
  lusha_api_key?: string
  snovio_client_id?: string
  snovio_client_secret?: string
  apify_api_key?: string
  linkedin_li_at_cookie?: string
  linkedin_cookie_browser?: string
}

export type LeadValidationStatus = 'valid' | 'maybe_incorrect'

export interface SearchRequest {
  company_name: string
  company_domain?: string
  linkedin_url?: string
  titles: string[]
  general_search?: boolean
  max_results: number
  scrape_mode: ScrapeMode
  lead_providers?: string[]
  search_engines?: string[]
  search_depth?: 'standard' | 'deep'
  official_sites?: boolean
  include_uncertain?: boolean
  output_path: string
  output_format?: 'csv' | 'xlsx'
  use_cache?: boolean
  web_query_limit?: number
  parallelism?: number
  provider_timeout_seconds?: number
  linkedin_cookie?: string
  linkedin_cookie_browser?: string
  playwright_headless?: boolean
  accept_risk?: boolean
  filters?: FilterPayload
  api_keys?: ApiKeyOverrides
  cards_per_cycle?: number
}

export interface Lead {
  company_name: string
  company_domain?: string | null
  person_name?: string | null
  title?: string | null
  linkedin_url?: string | null
  email?: string | null
  phone?: string | null
  source_url: string
  source_type: string
  snippet: string
  matched_title?: string | null
  validation_status?: LeadValidationStatus | null
  validation_note?: string | null
  confidence_score: number
  previously_consulted_at?: string | null
  consultation_note?: string | null
}

export interface ProspectingSummary {
  total_companies_processed: number
  total_raw_leads: number
  total_deduplicated_leads: number
  total_previously_consulted_leads: number
  total_maybe_incorrect_leads: number
  output_file: string
  top_sources: Record<string, number>
}

export interface ProviderDiagnostic {
  provider: string
  company_name?: string | null
  raw_records: number
  leads_returned: number
  dropped_company_evidence: number
  dropped_title_filter: number
  last_error?: string | null
  notes: string[]
}

export interface SearchResponse {
  leads: Lead[]
  summary: ProspectingSummary
  provider_diagnostics?: ProviderDiagnostic[]
}

export interface ApiKeySource {
  field: string
  env_var: string
  configured: boolean
  source: 'env' | 'ui_override' | 'missing'
  preview?: string | null
}

export interface ProviderConfigStatus {
  name: string
  configured: boolean
  requires: string[]
}

export interface DiagnosticsResponse {
  settings: ApiKeySource[]
  lead_providers: ProviderConfigStatus[]
  search_engines: ProviderConfigStatus[]
}

export interface CookieBackendAttempt {
  backend: 'rookiepy' | 'browser_cookie3' | string
  browser: string
  found: boolean
  error?: string | null
}

export interface CookieDiagnosticResponse {
  found: boolean
  source: string
  browser_priority: string[]
  default_browser: string | null
  rookiepy_available: boolean
  browser_cookie3_available: boolean
  attempts: CookieBackendAttempt[]
  hints: string[]
  preview: string | null
  cdp_endpoint: string | null
  cdp_alive: boolean
}

export interface CookieDiagnosticRequest {
  cookie?: string | null
  browser?: string
}

export type RunStatus = 'pending' | 'running' | 'completed' | 'failed' | 'cancelled'

export interface StartRunResponse {
  run_id: string
  status: RunStatus
}

export interface RunStateResponse {
  run_id: string
  status: RunStatus
  error?: string | null
  result?: SearchResponse | null
}

export interface TaxonomyItem {
  value: string
  label: string
  aliases?: string[]
}

export interface TaxonomiesResponse {
  seniority: TaxonomyItem[]
  functions: TaxonomyItem[]
  role_presets: TaxonomyItem[]
  scrape_modes: TaxonomyItem[]
}

export interface HealthResponse {
  status: string
  version: string
}

export type SavedLeadTableSourceType = 'search' | 'imported' | 'merged'
export type SavedLeadEnrichmentStatus = 'not_enriched' | 'not_implemented' | 'enriched'

export interface SavedLeadTable {
  id: string
  name: string
  created_at: string
  updated_at: string
  source_type: SavedLeadTableSourceType | string
  keywords: string[]
  search_queries: string[]
  search_request: Record<string, unknown>
  enrichment_status: SavedLeadEnrichmentStatus | string
  lead_count: number
}

export interface SavedLeadTableDetail {
  table: SavedLeadTable
  leads: Lead[]
}

export interface SaveLeadTableRequest {
  name: string
  leads: Lead[]
  keywords?: string[]
  search_request?: Record<string, unknown>
  generate_queries?: boolean
}

export interface ImportLeadTableRequest {
  name: string
  file_path: string
}

export interface ExportLeadTableRequest {
  output_path?: string
}

export interface ExportLeadTableResponse {
  output_path: string
}

export interface MergeLeadTablesRequest {
  name: string
  table_ids: string[]
  keywords?: string[]
}

export interface PeopleSearchProbeRequest {
  company_name: string
  company_domain?: string
  linkedin_url?: string
}

export interface PeopleSearchProbeResponse {
  is_small: boolean | null
  employee_count: number | null
  source: 'exact' | 'range' | 'heuristic' | 'unknown' | string
  note?: string | null
  should_offer_general_search: boolean
}

export type ProbeStatus = 'pending' | 'running' | 'completed' | 'failed'

export interface ProbeEvent {
  event: string
  data: Record<string, unknown>
}

export interface ProbeStartResponse {
  probe_id: string
  status: ProbeStatus
}

export interface ProbeStateResponse {
  probe_id: string
  status: ProbeStatus
  events: ProbeEvent[]
  classification?: PeopleSearchProbeResponse | null
  error?: string | null
}

export interface ExperimentalSearchResponse {
  new_leads: Lead[]
  duplicates_skipped: number
  candidates_total: number
  engines_used: string[]
  note?: string | null
}

export type EnrichmentFields = 'email' | 'phone' | 'both'
export type EnrichmentProvider = 'apollo' | 'lusha' | 'snovio'

export interface EnrichLeadTableRequest {
  lead_refs: string[]
  fields: EnrichmentFields
  providers: EnrichmentProvider[]
  credit_costs_brl?: Record<string, number>
  confirmed?: boolean
  apollo_webhook_url?: string | null
  api_keys?: ApiKeyOverrides
}

export interface EnrichmentProviderEstimate {
  provider: string
  selected_leads: number
  estimated_credits: number
  estimated_brl: number
  fields: string[]
}

export interface EnrichmentEstimate {
  selected_leads: number
  provider_estimates: EnrichmentProviderEstimate[]
  total_estimated_credits: number
  total_estimated_brl: number
  warnings: string[]
}

export interface EnrichmentSummary {
  requested_leads: number
  enriched_leads: number
  updated_leads: number
  providers_used: string[]
  errors: string[]
}

export interface EnrichLeadTableResponse {
  status: 'estimated' | 'completed' | string
  estimate: EnrichmentEstimate
  summary: EnrichmentSummary
  table?: SavedLeadTable | null
  leads: Lead[]
}
