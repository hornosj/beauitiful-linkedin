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
  enrichment_source?: string | null
  enrichment_status?: string | null
  enrichment_confidence?: number | null
  email_type?: string | null
  email_validation_status?: string | null
  enriched_at?: string | null
  /**
   * Sources that confirmed the primary `email` independently.
   * Length >= 2 ⇒ the UI shows a "Verificado" badge — at least two
   * different layers (internal + Apollo, internal + Lusha, etc.)
   * landed on the same address.
   */
  email_verified_by?: string[]
  /**
   * E-mails that other providers proposed but disagreed with the
   * primary. Kept so the user can audit "Apollo sugeriu X" without
   * losing the trail. Each entry is the contract used by the backend
   * in storage/saved_leads.py::_merge_email_verification.
   */
  email_alternatives?: EmailAlternative[]
}

export interface EmailAlternative {
  email: string
  source: string
  confidence?: number | null
  found_at?: string | null
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
export type EnrichmentProvider = 'apollo' | 'lusha' | 'snovio' | 'pdl'

export interface EnrichmentPricingItem {
  provider: string
  brl_per_credit: number
  source: 'default' | 'env_override' | string
  env_var: string
}

export interface EnrichmentPricingResponse {
  items: EnrichmentPricingItem[]
  currency: string
  note: string
}

export interface InternalEnrichRequest {
  lead_refs?: string[]
  fields: 'email'
  confirmed: boolean
  company_domain?: string | null
}

export interface InternalEnrichSummary {
  requested_leads: number
  enriched_leads: number
  skipped_existing_email: number
  failed_missing_domain: number
  no_change: number
}

export interface InternalEnrichResponse {
  status: 'completed'
  summary: InternalEnrichSummary
  table?: SavedLeadTable
  leads: Lead[]
}

/**
 * Streaming protocol for the SSE variant of internal enrichment.
 *
 * Phases run in order: ``discovering`` (crt.sh + SPF/DMARC + ccTLD
 * variants), ``harvesting`` (per-domain HTTP), ``validating`` (per-lead
 * MX/SMTP), and ``completed``. Per-lead events arrive during the
 * validating phase; per-company ``discovery`` events arrive during the
 * discovering phase; ``done`` carries the full summary + refreshed
 * table so the UI doesn't need a follow-up GET.
 */
export type InternalEnrichPhase =
  | 'discovering'
  | 'harvesting'
  | 'validating'
  | 'completed'
  | 'cancelled'

export type InternalEnrichLeadStatus =
  | 'enriched'
  | 'skipped_existing_email'
  | 'failed_missing_domain'
  | 'failed'
  | 'no_change'

export interface InternalEnrichStartEvent {
  type: 'start'
  total: number
  unique_domains: number
}

export interface InternalEnrichPhaseEvent {
  type: 'phase'
  phase: InternalEnrichPhase
}

export interface InternalEnrichDomainEvent {
  type: 'domain'
  domain: string
  harvested: number
  pattern: string | null
}

export interface InternalEnrichDiscoveryEvent {
  type: 'discovery'
  /** Display name of the company whose seed was probed. */
  company: string
  /** The strongest seed domain used to probe crt.sh/SPF/DMARC. */
  seed: string
  /** Count of new domains accepted by the MX gate. */
  discovered: number
  /** Each accepted new domain, ranked by source order. */
  domains: string[]
  /** Per-source breakdown so we can credit `crt_sh`, `spf_dmarc`, `cctld`. */
  sources: Record<string, string[]>
}

export interface InternalEnrichLeadEvent {
  type: 'lead'
  lead_ref: string | null
  person_name: string | null
  company_name: string | null
  status: InternalEnrichLeadStatus
  email: string | null
  confidence: number
  /**
   * Domain whose candidate eventually validated, or `null` when the
   * orchestrator gave up. Always one of `tested_domains` when set.
   */
  chosen_domain?: string | null
  /**
   * Full ordered list of domains the orchestrator attempted before
   * settling on `chosen_domain` (or failing). Useful to show the user
   * which fallbacks were tried — e.g. `["acme.com", "acme.io"]` when
   * the lead's column had `acme.com` but only `acme.io` validated.
   */
  tested_domains?: string[]
}

export interface InternalEnrichProgressEvent {
  type: 'progress'
  completed: number
  total: number
}

export interface InternalEnrichStreamDoneEvent {
  type: 'done'
  summary: InternalEnrichSummary
  table: SavedLeadTable
  leads: Lead[]
}

export interface InternalEnrichStreamErrorEvent {
  type: 'error'
  message: string
}

export type InternalEnrichStreamEvent =
  | InternalEnrichStartEvent
  | InternalEnrichPhaseEvent
  | InternalEnrichDiscoveryEvent
  | InternalEnrichDomainEvent
  | InternalEnrichLeadEvent
  | InternalEnrichProgressEvent
  | InternalEnrichStreamDoneEvent
  | InternalEnrichStreamErrorEvent

export interface EnrichLeadTableRequest {
  lead_refs: string[]
  fields: EnrichmentFields
  providers: EnrichmentProvider[]
  /** @deprecated Servidor ignora — pricing vem de GET /enrichment/pricing. */
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
  provider_logs: EnrichmentProviderRunLog[]
}

export interface EnrichmentProviderRunLog {
  provider: string
  requested_leads: number
  matched_leads: number
  updated_leads: number
  estimated_credits: number
  estimated_brl: number
  status: 'updated' | 'no_data' | 'error' | 'skipped' | string
  message: string
}

export interface EnrichLeadTableResponse {
  status: 'estimated' | 'completed' | string
  estimate: EnrichmentEstimate
  summary: EnrichmentSummary
  table?: SavedLeadTable | null
  leads: Lead[]
}
