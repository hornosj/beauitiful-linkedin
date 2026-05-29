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
  cdp_endpoint?: string
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
  // Phone enrichment metadata. Mirrors the e-mail trail so the UI can
  // render a "Verificado" badge + an "alternatives" expander for phones.
  // ``phone`` itself stays as the primary E.164-ish value; the columns
  // below describe how it was found and which sources agreed.
  phone_type?: string | null
  phone_country?: string | null
  phone_carrier?: string | null
  phone_region?: string | null
  phone_validation_status?: string | null
  phone_confidence?: number | null
  phone_source?: string | null
  phone_source_url?: string | null
  phone_verified_by?: string[]
  phone_alternatives?: PhoneAlternative[]
  linkedin_profile_validation_status?: string | null
  linkedin_experience_title?: string | null
  linkedin_experience_company?: string | null
  linkedin_experience_start_year?: number | null
  linkedin_experience_end_year?: number | null
  linkedin_experience_checked_at?: string | null
  linkedin_contact_email?: string | null
  linkedin_contact_website?: string | null
  linkedin_contact_phone?: string | null
  linkedin_location?: string | null
  linkedin_education?: Array<Record<string, unknown>>
  /**
   * Residential address recovered from a Telegram CPF (SISREG-III)
   * consult — the same lookup that finds the phone. Informational only;
   * never overwrites and never affects confidence. May be absent when
   * the report had no address ("Sem informação").
   */
  endereco?: string | null
}

export interface EmailAlternative {
  email: string
  source: string
  confidence?: number | null
  found_at?: string | null
}

export interface PhoneAlternative {
  phone: string
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

export interface PlaywrightDiagnosticStep {
  name: string
  ok: boolean
  elapsed_ms: number
  detail?: string | null
}

export interface PlaywrightDiagnosticRequest {
  cdp_endpoint?: string | null
}

export interface PlaywrightDiagnosticResponse {
  overall_ok: boolean
  cdp_endpoint: string
  sidecar_version: string
  playwright_version?: string | null
  log_path?: string | null
  steps: PlaywrightDiagnosticStep[]
}

export type RunStatus = 'pending' | 'running' | 'completed' | 'failed' | 'cancelled'

export interface StartRunResponse {
  run_id: string
  status: RunStatus
}

export interface FoundLead {
  person_name?: string | null
  title?: string | null
  company_name?: string | null
  location?: string | null
  linkedin_url?: string | null
  confidence_score: number
  matched_title?: string | null
  validation_status?: string | null
  note?: string | null
}

export interface RunStateResponse {
  run_id: string
  status: RunStatus
  error?: string | null
  result?: SearchResponse | null
  /** Leads descobertos progressivamente enquanto a busca roda. */
  found_leads?: FoundLead[]
  found_count?: number
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

export type InternalEnrichField = 'email' | 'phone' | 'both'

export interface InternalEnrichRequest {
  lead_refs?: string[]
  fields: InternalEnrichField
  confirmed: boolean
  company_domain?: string | null
  phone_sources?: string[]
}

export interface InternalEnrichSummary {
  requested_leads: number
  enriched_leads: number
  skipped_existing_email: number
  failed_missing_domain: number
  no_change: number
  // Phone-side counters live on the same summary so the UI can render
  // both pipelines with one shape. Zero when ``fields="email"``.
  enriched_phone_leads?: number
  skipped_existing_phone?: number
  failed_no_phone_candidate?: number
}

export interface InternalEnrichResponse {
  status: 'completed'
  summary: InternalEnrichSummary
  table?: SavedLeadTable
  leads: Lead[]
}

export interface LinkedInProfileValidationRequest {
  lead_refs: string[]
  max_leads?: number
  cdp_endpoint?: string
}

export interface LinkedInProfileValidationSummary {
  requested_leads: number
  validated_leads: number
  failed_leads: number
  no_linkedin_url: number
  no_change: number
}

export interface LinkedInProfileValidationResponse {
  status: 'completed' | string
  summary: LinkedInProfileValidationSummary
  table?: SavedLeadTable | null
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
  | 'lookup'
  | 'validating'
  | 'completed'
  | 'cancelled'

export type InternalEnrichLeadStatus =
  | 'enriched'
  | 'skipped_existing_email'
  | 'skipped_existing_phone'
  | 'failed_missing_domain'
  | 'failed_no_candidate'
  | 'failed_existing_phone'
  | 'failed'
  | 'no_change'

/**
 * Demultiplexer for ``fields="both"`` SSE streams.
 *
 * Events without a ``channel`` field default to the e-mail pipeline
 * (the historical contract). Phone-side events arrive tagged
 * ``channel: "phone"`` so the renderer can route them to a separate
 * progress bucket.
 */
export type InternalEnrichChannel = 'email' | 'phone'

export interface InternalEnrichStartEvent {
  type: 'start'
  total: number
  unique_domains: number
  fields?: InternalEnrichField
}

export interface InternalEnrichPhaseEvent {
  type: 'phase'
  phase: InternalEnrichPhase
  channel?: InternalEnrichChannel
}

export interface InternalEnrichDomainEvent {
  type: 'domain'
  domain: string
  harvested: number
  pattern?: string | null
  channel?: InternalEnrichChannel
}

/**
 * One ``lookup`` event per lead, emitted during the phone pipeline's
 * Bucket B phase. ``candidates`` is the count of external phone
 * candidates returned by all configured lookup providers (SERP, etc.)
 * for this lead. The UI uses it to show "found N candidates from
 * external sources" alongside the harvested count.
 */
export interface InternalEnrichLookupEvent {
  type: 'lookup'
  lead_ref: string | null
  candidates: number
  providers: string[]
  channel?: InternalEnrichChannel
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
  email?: string | null
  phone?: string | null
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
  /** Source label of the chosen candidate (phone only). */
  source?: string | null
  /** Active-presence probe verdict (phone only). */
  whatsapp_status?: 'active' | 'inactive' | 'unknown' | 'invalid' | null
  hlr_status?: 'reachable' | 'unreachable' | 'absent' | 'unknown' | null
  channel?: InternalEnrichChannel
}

export interface InternalEnrichProgressEvent {
  type: 'progress'
  completed: number
  total: number
  channel?: InternalEnrichChannel
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
  | InternalEnrichLookupEvent
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

export interface TelegramConsultCandidate {
  cpf: string
  nome: string | null
  data_nascimento: string | null
  endereco: string | null
  match_score?: number
  signals_used?: string[]
  breakdown?: Record<string, unknown>
}

export interface TelegramConsult {
  id: number
  table_id: string
  lead_ref: string
  provider: 'finder' | 'finder_cpf' | 'gon' | 'unix' | 'gon_cpf' | string
  lead_name: string
  query: string
  raw_text: string | null
  source_url: string | null
  downloaded_at: string | null
  error: string | null
  extracted_nome: string | null
  extracted_cpf: string | null
  extracted_birth_date: string | null
  extracted_address: string | null
  extracted_candidates: TelegramConsultCandidate[]
  match_score: number | null
  match_details: Record<string, unknown>
  created_at: string
  // Pipeline fields (Phase 3). ``query_type`` defaults to ``"name"`` for
  // rows persisted before the schema bump. The CPF-stage follow-up
  // produces rows with ``query_type === "cpf"`` and ``blocked_reason``
  // set when the lead was skipped (e.g. cargo divergente).
  run_id?: string | null
  query_type?: 'name' | 'cpf' | 'phone' | 'email' | string
  query_value?: string | null
  blocked_reason?: string | null
}

export interface TelegramConsultRequest {
  lead_refs: string[]
  max_leads?: number
}

export interface TelegramConsultSummary {
  requested_leads: number
  succeeded: number
  failed: number
}

export interface TelegramConsultResponse {
  status: string
  summary: TelegramConsultSummary
  consults: TelegramConsult[]
}

export interface TelegramConsultListResponse {
  consults: TelegramConsult[]
}

export interface TelethonAuthStatusResponse {
  authorized: boolean
  configured: boolean
  session_name: string | null
}

export interface TelethonAuthSendCodeRequest {
  phone: string
}

export interface TelethonAuthSendCodeResponse {
  phone_code_hash: string
  next_type: string | null
  timeout: number | null
}

export interface TelethonAuthSignInRequest {
  phone: string
  phone_code_hash: string
  code: string
  password?: string | null
}

export interface TelethonAuthSignInResponse {
  authorized: boolean
  requires_password: boolean
  user_id: number | null
  username: string | null
  first_name: string | null
}

export interface TelethonAuthLogoutResponse {
  authorized: boolean
  logged_out: boolean
}

export interface TelethonConfigRequest {
  api_id: string
  api_hash: string
}

export interface TelethonPipelineRequest {
  lead_refs: string[]
  max_leads?: number
  min_score?: number
  max_cpf_candidates?: number
  target_titles?: string[] | null
}

export interface TelethonCpfStageRequest {
  lead_ref: string
  cpf: string
}

export interface TelethonPipelineLeadResult {
  lead_ref: string
  lead_name: string | null
  name_consults: TelegramConsult[]
  cpf_consults: TelegramConsult[]
  blocked_reason: string | null
  candidates: TelegramFollowupPhoneCandidate[]
}

export interface TelethonPipelineSummary {
  requested_leads: number
  name_consults: number
  cpf_consults: number
  leads_with_phone: number
  phones_persisted: number
}

export interface TelethonPipelineResponse {
  status: string
  summary: TelethonPipelineSummary
  leads: TelethonPipelineLeadResult[]
}

// ---- Telegram phone follow-up (CPF stage) -------------------------------
//
// The follow-up is gated by the LinkedIn-cargo rule and the matcher
// score threshold (default 65). Each returned phone carries the
// originating CPF's match_score 1:1 in ``confidence`` — the UI must
// surface this number as-is and never recompute it.

export interface TelegramFollowupPhoneCandidate {
  phone_raw: string
  phone_digits: string
  cpf: string
  confidence: number
  source_provider: string
  nome?: string | null
  provenance: Record<string, unknown>
}

export interface TelegramFollowupLeadResult {
  lead_ref: string
  lead_name?: string | null
  blocked_reason?: string | null
  candidates: TelegramFollowupPhoneCandidate[]
  consults: TelegramConsult[]
}

export interface TelegramFollowupPhoneSummary {
  requested_leads: number
  leads_with_phone: number
  leads_blocked: number
  phones_persisted: number
  skipped_existing_phone: number
}

export interface TelegramFollowupPhoneResponse {
  status: string
  summary: TelegramFollowupPhoneSummary
  leads: TelegramFollowupLeadResult[]
}

export interface TelegramFollowupPhoneRequest {
  lead_refs: string[]
  target_titles?: string[] | null
  max_leads?: number
}

// ---- Unified Telegram phone flow ----------------------------------------
//
// The user-facing "Pegar telefone via Telegram" button calls
// ``POST /lead-tables/{id}/telegram-phone``. Per lead it first reuses a
// verified persisted CPF when available, otherwise runs /nome → /cpf.
// If no CPF is found and the lead has a Mail Finder e-mail, the backend
// can return the Findex /email e-mail fallback as an ``email`` consult row.

export interface TelegramPhoneRequest {
  lead_refs: string[]
  target_titles?: string[] | null
  max_leads?: number
}

export interface TelegramPhoneStageEvent {
  stage: string
  timestamp: string
  detail: Record<string, unknown>
}

export interface TelegramPhoneLeadResult {
  lead_ref: string
  lead_name?: string | null
  blocked_reason?: string | null
  candidates: TelegramFollowupPhoneCandidate[]
  name_consult?: TelegramConsult | null
  cpf_consult?: TelegramConsult | null
  stages?: TelegramPhoneStageEvent[]
  last_stage?: string | null
}

export interface TelegramPhoneSummary {
  requested_leads: number
  leads_with_phone: number
  leads_blocked: number
  phones_persisted: number
  skipped_existing_phone: number
}

export interface TelegramPhoneResponse {
  status: string
  summary: TelegramPhoneSummary
  leads: TelegramPhoneLeadResult[]
}

// Resumable per-lead runs. The UI uses these to pause between consults
// (anti-ban dos bots Telegram): /start cria o run, /next processa um
// lead por chamada, /cancel encerra. Cada /next devolve o resultado do
// lead processado e o cursor atualizado para o cliente decidir
// continuar manualmente.

export interface TelegramPhoneStartRequest {
  lead_refs: string[]
  target_titles?: string[] | null
  max_leads?: number
}

export interface TelegramPhoneStartResponse {
  run_id: string
  table_id: string
  total_leads: number
  next_index: number
  lead_refs: string[]
  status: string
}

export interface TelegramPhoneNextResponse {
  run_id: string
  status: string
  next_index: number
  total_leads: number
  summary: TelegramPhoneSummary
  last_lead?: TelegramPhoneLeadResult | null
}

export interface TelegramPhoneCancelResponse {
  run_id: string
  status: string
  next_index: number
  total_leads: number
}

// Two-step interactive flow. /extract-cpfs roda /nome + matcher e
// devolve os CPFs candidatos pra UI exibir checkboxes. O operador
// confirma quais vão pra /cpf via /run-cpf-stage (ou pula via
// /skip-current-lead). Anti-ban + transparência sobre quais CPFs
// vão consumir quota de bot.

export interface TelegramPhoneRankedCandidate {
  cpf: string
  nome?: string | null
  data_nascimento?: string | null
  endereco?: string | null
  match_score: number
  signals_used: string[]
  breakdown: Record<string, unknown>
  eligible: boolean
}

export interface TelegramPhoneExtractCpfsResponse {
  run_id: string
  status: string
  next_index: number
  total_leads: number
  lead_ref: string
  lead_name?: string | null
  blocked_reason?: string | null
  eligible_cpfs: string[]
  candidates: TelegramPhoneRankedCandidate[]
  name_consult?: TelegramConsult | null
}
