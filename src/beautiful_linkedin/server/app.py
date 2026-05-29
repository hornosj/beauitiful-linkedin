"""FastAPI app for the local Electron sidecar.

Exposes the existing ``run_prospecting`` pipeline plus the lead filter
taxonomy. The renderer talks to this server over loopback only — never
expose it to the network. Risky modes (Playwright) require explicit
``accept_risk: true`` in the payload as a server-side guard against the
renderer accidentally forwarding the wrong scrape mode.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator

from beautiful_linkedin.cli import build_lead_filter
from beautiful_linkedin.config import (
    Settings,
    clear_telegram_credentials,
    load_settings,
    save_telegram_credentials,
)
from beautiful_linkedin.cookie_resolver import (
    diagnose_li_at_cookie,
    resolve_linkedin_li_at_cookie,
)
from beautiful_linkedin.providers.linkedin_people_search import (
    PeopleSearchOptions,
    looks_like_valid_li_at,
    probe_cdp_endpoint,
    resolve_company_size_via_page,
)
from beautiful_linkedin.models import (
    CompanyInput,
    Lead,
    ProspectingResult,
    ProspectingSummary,
    ProviderDiagnostic,
)
from beautiful_linkedin.processing.company_size import (
    CompanySizeClassification,
    classify_company_size,
)
from beautiful_linkedin.processing.function_taxonomy import (
    FUNCTION_ALIASES,
    JobFunction,
)
from beautiful_linkedin.processing.lead_filter import LeadFilter
from beautiful_linkedin.processing.role_taxonomy import (
    AREAS_MAP,
    get_area_slug,
)
from beautiful_linkedin.processing.seniority_taxonomy import (
    SENIORITY_ALIASES,
    SENIORITY_ORDER,
)
from beautiful_linkedin.runner import run_prospecting
from beautiful_linkedin.scrape_modes import apply_scrape_mode
from beautiful_linkedin.search.duckduckgo_search import DuckDuckGoSearchEngine
from beautiful_linkedin.search.query_builder import build_balanced_queries_for_company
from beautiful_linkedin.search.search_engine import SearchEngine
from beautiful_linkedin.search.searxng_search import SearxngSearchEngine
from beautiful_linkedin.storage.experimental_search import (
    EXPERIMENTAL_SOURCE_TYPE,
    ExperimentalSearchSummary,
    run_experimental_search,
)
from beautiful_linkedin.storage.enrichment import (
    ApolloEnrichmentProvider,
    EnrichmentEstimate,
    EnrichmentOptions,
    EnrichmentRunSummary,
    EnrichmentUpdate,
    LushaEnrichmentProvider,
    PdlEnrichmentProvider,
    ProviderRunLog,
    SnovioEnrichmentProvider,
    estimate_enrichment_cost,
)
from beautiful_linkedin.storage.company_email_harvester import (
    CompanyEmailHarvester,
)
from beautiful_linkedin.storage.internal_phone_enrichment import (
    InternalPhoneEnrichmentOrchestrator,
    InternalPhoneEnrichmentService,
    PhoneEnrichmentUpdate,
    collect_company_domains_from_leads,
    default_harvest_fn as default_phone_harvest_fn,
)
from beautiful_linkedin.storage.phone_hlr import (
    HlrProbeProvider,
    NoopHlrProbe,
    default_hlr_probe,
)
from beautiful_linkedin.storage.phone_linkedin_contact import (
    LinkedInContactInfoLookupProvider,
)
from beautiful_linkedin.storage.phone_lookup import PhoneLookupProvider
from beautiful_linkedin.storage.phone_pdf_extractor import PdfPhoneExtractor
from beautiful_linkedin.storage.phone_receita_cnpj import ReceitaCnpjLookupProvider
from beautiful_linkedin.storage.phone_serp_search import SerpPhoneSearchProvider
from beautiful_linkedin.storage.telegram_group_phone_lookup import (
    TelegramGroupPhoneLookupProvider,
    TelegramVoidPhoneLookupProvider,
)
from beautiful_linkedin.storage.telegram_consult_matcher import (
    MatchScore,
    score_candidate,
)
from beautiful_linkedin.storage.telegram_consult_parser import (
    TelegramCandidate,
    TelegramExtraction,
    parse_telegram_text,
)
from beautiful_linkedin.storage.telegram_group_playwright_lookup import (
    FindexCpfConsult,
    FindexEmailConsult,
    FindexNameConsult,
    GonzalesBotConsult,
    GonzalesCpfConsult,
    TelegramConsultOrchestrator,
    TelegramConsultResult,
    TelegramGroupPlaywrightLookup,
    UnixBotConsult,
)
from beautiful_linkedin.storage.telegram_pipeline import (
    TelegramCpfStageResult,
    TelegramNameStageResult,
    TelegramParsedCandidate,
    TelegramPhoneCandidate,
    TelegramPhoneFlowResult,
    cpf_candidate_allowed_for_review as _pipeline_cpf_candidate_allowed_for_review,
    collect_name_stage_candidates as _pipeline_collect_name_stage_candidates,
    latest_name_stage_consult,
    lead_ref_for as _pipeline_lead_ref_for,
    parse_and_rank as _pipeline_parse_and_rank,
    refresh_linkedin_signals_for_telegram as _pipeline_refresh_linkedin_signals,
    run_cpf_stage_only as _pipeline_run_cpf_stage_only,
    run_extract_phone_via_cpf as _pipeline_run_extract_phone_via_cpf,
    run_name_stage_only as _pipeline_run_name_stage_only,
    run_phone_followup as _pipeline_run_phone_followup,
    run_telegram_consult as _pipeline_run_telegram_consult,
    select_followup_candidates as _pipeline_select_followup_candidates,
)
from beautiful_linkedin.storage.telegram_phone_lookup import (
    TelegramBotPhoneLookupProvider,
)
from beautiful_linkedin.storage.telegram_telethon_lookup import (
    TelethonGonzalesCpfConsult,
    TelethonSerasaCpfConsult,
    TelethonTelegramConsultOrchestrator,
)
from beautiful_linkedin.storage import telegram_telethon_auth
from beautiful_linkedin.storage.phone_validation import PhoneValidator
from beautiful_linkedin.storage.whatsapp_checker import WhatsAppNumberChecker
from beautiful_linkedin.storage.internal_enrichment import (
    CompanyDomainResolver,
    EmailValidator,
    InternalEnrichmentOrchestrator,
    InternalLeadEnrichmentService,
    SmtpMailboxVerifier,
    collect_company_domains,
)
from beautiful_linkedin.storage.linkedin_profile_validation import (
    CdpLinkedInProfilePageFetcher,
    PlaywrightLinkedInProfilePageFetcher,
    run_linkedin_profile_validation as run_profile_validation_with_fetcher,
)
from beautiful_linkedin.storage.saved_leads import (
    ImportColumnError,
    SavedLeadsStore,
    SOURCE_TYPE_SEARCH,
)

VERSION = "0.1.5"

logger = logging.getLogger(__name__)


def _log_telegram_phone_endpoint(event: str, **detail: Any) -> None:
    """Sidecar-visible logs for the Telegram phone API surface.

    Values are deliberately operational: run/table/cursor/counts. Raw
    CPF, e-mail, and phone values stay out of logs.
    """
    clean = {key: value for key, value in detail.items() if value is not None}
    detail_text = " ".join(f"{key}={value}" for key, value in sorted(clean.items()))
    logger.info("telegram_phone_endpoint event=%s%s%s", event, " " if detail_text else "", detail_text)


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class FilterPayload(BaseModel):
    seniority_in: list[str] = Field(default_factory=list)
    functions_in: list[str] = Field(default_factory=list)
    locations_in: list[str] = Field(default_factory=list)
    exclude_titles: list[str] = Field(default_factory=list)
    min_confidence_score: int | None = None
    drop_unclassified: bool = False


class ApiKeyOverrides(BaseModel):
    brave_search_api_key: str | None = None
    google_custom_search_api_key: str | None = None
    google_custom_search_cx: str | None = None
    serper_api_key: str | None = None
    searxng_base_url: str | None = None
    people_data_labs_api_key: str | None = None
    coresignal_api_key: str | None = None
    apollo_api_key: str | None = None
    lusha_api_key: str | None = None
    snovio_client_id: str | None = None
    snovio_client_secret: str | None = None
    apify_api_key: str | None = None
    linkedin_li_at_cookie: str | None = None
    linkedin_cookie_browser: str | None = None


class SearchRequest(BaseModel):
    company_name: str = Field(min_length=1)
    company_domain: str | None = None
    linkedin_url: str | None = None
    titles: list[str] = Field(default_factory=list)
    # Opt-in flag for "busca geral": when true, ``titles`` may be empty and
    # the people_search provider runs without any ?keywords= filter. Default
    # is false so callers that simply forget to include titles still get a
    # 422 explaining the mistake.
    general_search: bool = False
    max_results: int = Field(default=20, ge=1, le=500)
    scrape_mode: str = "api"
    lead_providers: list[str] = Field(default_factory=lambda: ["auto"])
    search_engines: list[str] = Field(default_factory=lambda: ["auto"])
    search_depth: str = "standard"
    official_sites: bool = True
    include_uncertain: bool = False
    output_path: str
    output_format: str | None = None
    use_cache: bool = True
    web_query_limit: int = 48
    parallelism: int = 6
    provider_timeout_seconds: float = 120.0
    linkedin_cookie: str | None = None
    linkedin_cookie_browser: str | None = None
    playwright_headless: bool | None = None
    accept_risk: bool = False
    filters: FilterPayload | None = None
    api_keys: ApiKeyOverrides | None = None
    # Approx cards rendered by each "Exibir mais resultados" click on the
    # LinkedIn People page. Used to derive how many clicks the load-more loop
    # needs to satisfy ``max_results``. None means "keep server-side default".
    cards_per_cycle: int | None = Field(default=None, ge=1, le=200)
    # Override the CDP endpoint used by the people_search provider.
    # When set (e.g. "http://127.0.0.1:9223"), the server will use this
    # endpoint instead of the global linkedin_cdp_endpoint setting.
    # Intended for the in-app embedded browser mode.
    cdp_endpoint: str | None = None

    @model_validator(mode="after")
    def _titles_required_unless_general(self) -> "SearchRequest":
        if not self.general_search and not [t for t in self.titles if t.strip()]:
            raise ValueError(
                "titles é obrigatório quando general_search=false. "
                "Marque general_search=true para rodar uma busca sem keywords."
            )
        return self


class SearchResponse(BaseModel):
    leads: list[Lead]
    summary: ProspectingSummary
    provider_diagnostics: list[ProviderDiagnostic] = Field(default_factory=list)


class ApiKeySource(BaseModel):
    field: str
    env_var: str
    configured: bool
    source: str  # "env", "ui_override", "missing"
    preview: str | None = None  # masked, e.g. "abc1…wxyz"


class ProviderConfigStatus(BaseModel):
    name: str
    configured: bool
    requires: list[str] = Field(default_factory=list)


class DiagnosticsResponse(BaseModel):
    settings: list[ApiKeySource]
    lead_providers: list[ProviderConfigStatus]
    search_engines: list[ProviderConfigStatus]


class CookieBackendAttempt(BaseModel):
    backend: str
    browser: str
    found: bool
    error: str | None = None


class CookieDiagnosticResponse(BaseModel):
    found: bool
    source: str
    browser_priority: list[str] = Field(default_factory=list)
    default_browser: str | None = None
    rookiepy_available: bool = False
    browser_cookie3_available: bool = False
    attempts: list[CookieBackendAttempt] = Field(default_factory=list)
    hints: list[str] = Field(default_factory=list)
    preview: str | None = None
    cdp_endpoint: str | None = None
    cdp_alive: bool = False


class CookieDiagnosticRequest(BaseModel):
    cookie: str | None = None
    browser: str = "auto"


class PlaywrightDiagnosticStep(BaseModel):
    name: str
    ok: bool
    elapsed_ms: int
    detail: str | None = None


class PlaywrightDiagnosticRequest(BaseModel):
    cdp_endpoint: str | None = None


class PlaywrightDiagnosticResponse(BaseModel):
    """Auto-teste de Playwright/CDP usado pelo botão Diagnosticar.

    Cada etapa devolve ok/elapsed_ms para identificar exatamente onde o
    fluxo trava no PC do usuário (CDP probe, sync_playwright start,
    connect_over_cdp, listagem de páginas).
    """
    overall_ok: bool
    cdp_endpoint: str
    sidecar_version: str
    playwright_version: str | None = None
    log_path: str | None = None
    steps: list[PlaywrightDiagnosticStep] = Field(default_factory=list)


class StartRunResponse(BaseModel):
    run_id: str
    status: RunStatus = RunStatus.PENDING


class FoundLeadPayload(BaseModel):
    """Compact, user-facing snapshot of a lead found mid-run.

    Deliberately omits internal provenance (provider names, fetch backend):
    the UI shows only what helps the operator recognise the person.
    """

    person_name: str | None = None
    title: str | None = None
    company_name: str | None = None
    location: str | None = None
    linkedin_url: str | None = None
    confidence_score: int = 0
    matched_title: str | None = None
    validation_status: str | None = None
    note: str | None = None


class RunStateResponse(BaseModel):
    run_id: str
    status: RunStatus
    error: str | None = None
    result: SearchResponse | None = None
    # Progressive feedback: accumulates as leads are discovered so the UI can
    # render them live while ``status`` is still RUNNING.
    found_leads: list[FoundLeadPayload] = Field(default_factory=list)
    found_count: int = 0


class TaxonomyItem(BaseModel):
    value: str
    label: str
    aliases: list[str] = Field(default_factory=list)


class TaxonomiesResponse(BaseModel):
    seniority: list[TaxonomyItem]
    functions: list[TaxonomyItem]
    role_presets: list[TaxonomyItem]
    scrape_modes: list[TaxonomyItem]


class SavedLeadTablePayload(BaseModel):
    id: str
    name: str
    created_at: str
    updated_at: str
    source_type: str
    keywords: list[str] = Field(default_factory=list)
    search_queries: list[str] = Field(default_factory=list)
    search_request: dict[str, Any] = Field(default_factory=dict)
    enrichment_status: str
    lead_count: int = 0


class SavedLeadTableDetail(BaseModel):
    table: SavedLeadTablePayload
    leads: list[Lead] = Field(default_factory=list)


class SaveLeadTableRequest(BaseModel):
    name: str = Field(min_length=1)
    leads: list[Lead] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    search_request: dict[str, Any] = Field(default_factory=dict)
    generate_queries: bool = False


class ImportLeadTableRequest(BaseModel):
    name: str = Field(min_length=1)
    file_path: str = Field(min_length=1)


class ExportLeadTableRequest(BaseModel):
    output_path: str | None = None
    format: str = "tabela"  # "tabela" (padrão) | "oficial"


class ExportLeadTableResponse(BaseModel):
    output_path: str


class MergeLeadTablesRequest(BaseModel):
    name: str = Field(min_length=1)
    table_ids: list[str] = Field(min_length=1)
    keywords: list[str] = Field(default_factory=list)


class PeopleSearchProbeRequest(BaseModel):
    company_name: str = Field(min_length=1)
    company_domain: str | None = None
    linkedin_url: str | None = None


class ExperimentalSearchResponse(BaseModel):
    new_leads: list[Lead] = Field(default_factory=list)
    duplicates_skipped: int = 0
    candidates_total: int = 0
    engines_used: list[str] = Field(default_factory=list)
    note: str | None = None


class EnrichmentProviderEstimatePayload(BaseModel):
    provider: str
    selected_leads: int
    estimated_credits: int
    estimated_brl: float
    fields: list[str] = Field(default_factory=list)


class EnrichmentEstimatePayload(BaseModel):
    selected_leads: int
    provider_estimates: list[EnrichmentProviderEstimatePayload] = Field(
        default_factory=list
    )
    total_estimated_credits: int = 0
    total_estimated_brl: float = 0.0
    warnings: list[str] = Field(default_factory=list)


class EnrichLeadTableRequest(BaseModel):
    lead_refs: list[str] = Field(min_length=1)
    fields: str = "email"  # "email" | "phone" | "both"
    providers: list[str] = Field(
        default_factory=lambda: ["lusha", "apollo", "snovio", "pdl"]
    )
    # DEPRECATED — mantido por compat com clientes antigos. O servidor
    # ignora este campo e usa ``get_enrichment_pricing()`` como fonte da
    # verdade. Veja ``GET /enrichment/pricing``.
    credit_costs_brl: dict[str, float] = Field(default_factory=dict)
    confirmed: bool = False
    apollo_webhook_url: str | None = None
    api_keys: ApiKeyOverrides | None = None

    @model_validator(mode="after")
    def _validate_enrichment_request(self) -> "EnrichLeadTableRequest":
        if self.fields not in {"email", "phone", "both"}:
            raise ValueError("fields deve ser 'email', 'phone' ou 'both'")
        provider_names = {provider.strip().lower() for provider in self.providers}
        if self.confirmed and self.fields in {"phone", "both"} and "apollo" in provider_names:
            if not (self.apollo_webhook_url or "").strip():
                raise ValueError(
                    "Apollo exige apollo_webhook_url HTTPS para enriquecimento de telefone."
                )
        return self


class InternalEnrichRequest(BaseModel):
    """Body for ``POST /lead-tables/{id}/internal-enrich``.

    The first version only enriches e-mail. ``lead_refs`` scopes the run
    to a subset of the table's leads; if omitted, every lead is processed.
    """

    lead_refs: list[str] | None = None
    fields: str = "email"
    confirmed: bool = True
    company_domain: str | None = None
    # When set, restricts the phone pipeline to the named source providers
    # (by ``provider.name``) and disables the company-site harvester.
    # ``["telegram_group"]`` is the canonical alias for
    # ``telegram_group_consultasgratis``. ``None`` keeps the full pipeline.
    phone_sources: list[str] | None = None

    @model_validator(mode="after")
    def _validate(self) -> "InternalEnrichRequest":
        if self.fields not in {"email", "phone", "both"}:
            raise ValueError(
                "fields deve ser 'email', 'phone' ou 'both'."
            )
        if self.company_domain is not None:
            cleaned = _clean_internal_company_domain(self.company_domain)
            if not cleaned:
                raise ValueError(
                    "company_domain deve ser um domínio corporativo válido (ex.: empresa.com.br)."
                )
            self.company_domain = cleaned
        if self.phone_sources is not None:
            cleaned_sources = [s.strip() for s in self.phone_sources if s and s.strip()]
            self.phone_sources = cleaned_sources or None
        return self


class InternalEnrichSummary(BaseModel):
    requested_leads: int = 0
    enriched_leads: int = 0
    skipped_existing_email: int = 0
    failed_missing_domain: int = 0
    no_change: int = 0
    # Phone-side counters live alongside the e-mail ones so the UI can
    # render both with a single response shape. They stay at zero when
    # ``fields="email"``.
    enriched_phone_leads: int = 0
    skipped_existing_phone: int = 0
    failed_no_phone_candidate: int = 0


class InternalEnrichResponse(BaseModel):
    status: str
    summary: InternalEnrichSummary
    table: SavedLeadTablePayload | None = None
    leads: list[Lead] = Field(default_factory=list)


class LinkedInProfileValidationRequest(BaseModel):
    lead_refs: list[str] = Field(min_length=1)
    max_leads: int = Field(default=40, ge=1, le=40)
    cdp_endpoint: str | None = None

    @model_validator(mode="after")
    def _validate_limit(self) -> "LinkedInProfileValidationRequest":
        if len(self.lead_refs) > self.max_leads:
            raise ValueError(
                f"Selecione no máximo {self.max_leads} leads por validação de cargo."
            )
        return self


class TelegramConsultRequest(BaseModel):
    """Body for ``POST /lead-tables/{id}/telegram-consult``.

    The flow runs sequentially over ``lead_refs`` — each one drives the
    Chrome-CDP / Telegram-Web automation through the bot. A small cap
    keeps the operator from accidentally booking the browser for hours
    and signals to the UI that this is not a bulk pipeline.
    """

    lead_refs: list[str] = Field(min_length=1)
    max_leads: int = Field(default=10, ge=1, le=10)


class TelegramConsultPayload(BaseModel):
    id: int
    table_id: str
    lead_ref: str
    provider: str = "unix"
    lead_name: str
    query: str
    raw_text: str | None = None
    source_url: str | None = None
    downloaded_at: str | None = None
    error: str | None = None
    extracted_nome: str | None = None
    extracted_cpf: str | None = None
    extracted_birth_date: str | None = None
    extracted_address: str | None = None
    extracted_candidates: list[dict[str, Any]] = Field(default_factory=list)
    match_score: int | None = None
    match_details: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    # Pipeline columns (Phase 3). Default to legacy values so previously
    # persisted rows render unchanged in the UI.
    run_id: str | None = None
    query_type: str = "name"
    query_value: str | None = None
    blocked_reason: str | None = None


class TelegramConsultSummary(BaseModel):
    requested_leads: int = 0
    succeeded: int = 0
    failed: int = 0


class TelegramConsultResponse(BaseModel):
    status: str
    summary: TelegramConsultSummary
    consults: list[TelegramConsultPayload] = Field(default_factory=list)


class TelegramConsultListResponse(BaseModel):
    consults: list[TelegramConsultPayload] = Field(default_factory=list)


class TelethonAuthStatusResponse(BaseModel):
    authorized: bool
    configured: bool
    session_name: str | None = None


class TelethonAuthSendCodeRequest(BaseModel):
    phone: str = Field(..., min_length=4)


class TelethonAuthSendCodeResponse(BaseModel):
    phone_code_hash: str
    next_type: str | None = None
    timeout: int | None = None


class TelethonAuthSignInRequest(BaseModel):
    phone: str = Field(..., min_length=4)
    phone_code_hash: str = Field(..., min_length=1)
    code: str = Field(..., min_length=1)
    password: str | None = None


class TelethonAuthSignInResponse(BaseModel):
    authorized: bool
    requires_password: bool = False
    user_id: int | None = None
    username: str | None = None
    first_name: str | None = None


class TelethonAuthLogoutResponse(BaseModel):
    authorized: bool = False
    logged_out: bool = False


class TelethonConfigRequest(BaseModel):
    """Body for ``POST /telegram/telethon/config``.

    These are the *application* credentials (api_id/api_hash) the end user
    registers once at my.telegram.org. They are persisted so the operator
    never has to hand-edit a ``.env`` in the packaged app.
    """

    api_id: str = Field(..., min_length=1)
    api_hash: str = Field(..., min_length=1)


class TelegramFollowupPhoneRequest(BaseModel):
    """Body for ``POST /lead-tables/{id}/telegram-followup-phone``.

    Pre-condition: each lead in ``lead_refs`` must already have a
    name-stage Telegram consult row in ``tabela_telegram``. The
    follow-up reads those rows' ranked CPF candidates to decide which
    ``/cpf`` queries to dispatch — it does not consult for an arbitrary
    name.

    ``target_titles`` enables the LinkedIn cargo gate when provided.
    Empty list / ``None`` disables it (mirrors the people-search loop's
    behavior on "busca geral").
    """

    lead_refs: list[str] = Field(min_length=1)
    target_titles: list[str] | None = None
    max_leads: int = Field(default=10, ge=1, le=10)


class TelegramFollowupPhoneCandidate(BaseModel):
    """One harvested phone with its CPF provenance.

    ``confidence`` mirrors the originating CPF's matcher score 1:1.
    Reshaping or capping this number would defeat the audit trail
    encoded in ``provenance``.
    """

    phone_raw: str
    phone_digits: str
    cpf: str
    confidence: int
    source_provider: str
    nome: str | None = None
    provenance: dict[str, Any] = Field(default_factory=dict)


class TelegramFollowupLeadResult(BaseModel):
    """Per-lead summary of one follow-up run."""

    lead_ref: str
    lead_name: str | None = None
    blocked_reason: str | None = None
    candidates: list[TelegramFollowupPhoneCandidate] = Field(default_factory=list)
    consults: list[TelegramConsultPayload] = Field(default_factory=list)


class TelegramFollowupPhoneSummary(BaseModel):
    requested_leads: int = 0
    leads_with_phone: int = 0
    leads_blocked: int = 0
    phones_persisted: int = 0
    skipped_existing_phone: int = 0


class TelegramFollowupPhoneResponse(BaseModel):
    status: str
    summary: TelegramFollowupPhoneSummary
    leads: list[TelegramFollowupLeadResult] = Field(default_factory=list)


class TelethonPipelineRequest(BaseModel):
    """Body for ``POST /lead-tables/{id}/telegram-consult/telethon-pipeline``.

    ``lead_refs`` are the leads the operator selected in the UI. The
    flow runs ``/nome`` on Findex+Gon+Unix (Telethon), parses CPF
    candidates, scores them against the lead's LinkedIn signals (with
    the new ``linkedin_birthday`` carrying the top weight), then runs
    Gonzales ``/cpf`` + SISREG for the top candidates to harvest phones.
    """

    lead_refs: list[str] = Field(default_factory=list)
    max_leads: int = Field(default=10, ge=1, le=20)
    min_score: int = Field(default=65, ge=0, le=100)
    max_cpf_candidates: int = Field(default=1, ge=1, le=10)
    target_titles: list[str] | None = None


class TelethonCpfStageRequest(BaseModel):
    """Body for a single-CPF Telethon phone lookup from a saved consult."""

    lead_ref: str = Field(min_length=1)
    cpf: str = Field(min_length=1)


class TelethonPipelineLeadResult(BaseModel):
    lead_ref: str
    lead_name: str | None = None
    name_consults: list[TelegramConsultPayload] = Field(default_factory=list)
    cpf_consults: list[TelegramConsultPayload] = Field(default_factory=list)
    blocked_reason: str | None = None
    candidates: list[TelegramFollowupPhoneCandidate] = Field(default_factory=list)


class TelethonPipelineSummary(BaseModel):
    requested_leads: int = 0
    name_consults: int = 0
    cpf_consults: int = 0
    leads_with_phone: int = 0
    phones_persisted: int = 0


class TelethonPipelineResponse(BaseModel):
    status: str
    summary: TelethonPipelineSummary
    leads: list[TelethonPipelineLeadResult] = Field(default_factory=list)


# ---- Unified phone flow ---------------------------------------------------
#
# One endpoint that runs ``/nome`` then ``/cpf`` atomically per lead.
# The UI exposes a single button so the operator does not need to track
# the two-stage shape of the underlying pipeline.


class TelegramPhoneRequest(BaseModel):
    """Body for ``POST /lead-tables/{id}/telegram-phone``.

    Atomic per lead: the server dispatches ``/nome`` then, for each CPF
    above the matcher threshold, ``/cpf`` — no pre-condition on already
    having a name-stage row persisted. ``target_titles`` enables the
    LinkedIn cargo gate; when ``None`` or empty, the gate is disabled
    and every lead proceeds (matches the "busca geral" semantics from
    the people-search loop).
    """

    lead_refs: list[str] = Field(min_length=1)
    target_titles: list[str] | None = None
    max_leads: int = Field(default=10, ge=1, le=10)


class TelegramPhoneStageEvent(BaseModel):
    """One observable transition for a single lead's Telegram run.

    The UI keys against ``stage`` to render a timeline, and ``last_stage``
    on the response tells the operator at a glance where each lead stopped.
    """

    stage: str
    timestamp: str
    detail: dict[str, Any] = Field(default_factory=dict)


class TelegramPhoneLeadResult(BaseModel):
    """Per-lead outcome of the unified name → cpf → phone flow."""

    lead_ref: str
    lead_name: str | None = None
    blocked_reason: str | None = None
    candidates: list[TelegramFollowupPhoneCandidate] = Field(default_factory=list)
    name_consult: TelegramConsultPayload | None = None
    cpf_consult: TelegramConsultPayload | None = None
    stages: list[TelegramPhoneStageEvent] = Field(default_factory=list)
    last_stage: str | None = None


class TelegramPhoneSummary(BaseModel):
    requested_leads: int = 0
    leads_with_phone: int = 0
    leads_blocked: int = 0
    phones_persisted: int = 0
    skipped_existing_phone: int = 0


class TelegramPhoneResponse(BaseModel):
    status: str
    summary: TelegramPhoneSummary
    leads: list[TelegramPhoneLeadResult] = Field(default_factory=list)


class TelegramPhoneStartRequest(BaseModel):
    """Body for ``POST /lead-tables/{id}/telegram-phone/start``.

    Cria um run resumível. Nenhuma consulta Telegram acontece aqui —
    o servidor só prepara a sequência ordenada de leads. O cliente
    Electron chama ``/next`` lead-a-lead, confirmando manualmente entre
    cada um (anti-ban dos bots Telegram).
    """

    lead_refs: list[str] = Field(min_length=1)
    target_titles: list[str] | None = None
    max_leads: int = Field(default=10, ge=1, le=10)


class TelegramPhoneStartResponse(BaseModel):
    run_id: str
    table_id: str
    total_leads: int
    next_index: int
    lead_refs: list[str]
    status: str


class TelegramPhoneNextRequest(BaseModel):
    run_id: str


class TelegramPhoneNextResponse(BaseModel):
    run_id: str
    status: str  # in_progress | completed | cancelled
    next_index: int
    total_leads: int
    summary: TelegramPhoneSummary
    last_lead: TelegramPhoneLeadResult | None = None


class TelegramPhoneCancelRequest(BaseModel):
    run_id: str


class TelegramPhoneCancelResponse(BaseModel):
    run_id: str
    status: str
    next_index: int
    total_leads: int


class TelegramPhoneRankedCandidatePayload(BaseModel):
    """Um CPF candidato pra ser exibido pra confirmação humana antes do
    /cpf rodar. Carrega o score do matcher + sinais usados pra UI poder
    mostrar 'porque a gente acha que é essa pessoa'."""

    cpf: str
    nome: str | None = None
    data_nascimento: str | None = None
    endereco: str | None = None
    match_score: int = 0
    signals_used: list[str] = Field(default_factory=list)
    breakdown: dict[str, Any] = Field(default_factory=dict)
    eligible: bool = False  # True se passou min_score E gate de nome


class TelegramPhoneExtractCpfsRequest(BaseModel):
    run_id: str


class TelegramPhoneExtractCpfsResponse(BaseModel):
    """Resposta da etapa 1 (/extract-cpfs). Devolve os CPFs que o /nome
    encontrou + os filtrados pelo matcher pra UI exibir checkboxes."""

    run_id: str
    status: str  # awaiting_cpf_confirmation | in_progress | completed | cancelled
    next_index: int
    total_leads: int
    lead_ref: str
    lead_name: str | None = None
    blocked_reason: str | None = None
    eligible_cpfs: list[str] = Field(default_factory=list)
    candidates: list[TelegramPhoneRankedCandidatePayload] = Field(default_factory=list)
    name_consult: TelegramConsultPayload | None = None


class TelegramPhoneRunCpfStageRequest(BaseModel):
    run_id: str
    cpfs: list[str] = Field(min_length=1)


class TelegramPhoneSkipLeadRequest(BaseModel):
    run_id: str


class LinkedInProfileValidationSummary(BaseModel):
    requested_leads: int = 0
    validated_leads: int = 0
    failed_leads: int = 0
    no_linkedin_url: int = 0
    no_change: int = 0


class LinkedInProfileValidationResponse(BaseModel):
    status: str
    summary: LinkedInProfileValidationSummary
    table: SavedLeadTablePayload | None = None
    leads: list[Lead] = Field(default_factory=list)


class EnrichmentSummaryPayload(BaseModel):
    requested_leads: int = 0
    enriched_leads: int = 0
    updated_leads: int = 0
    providers_used: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    provider_logs: list["ProviderRunLogPayload"] = Field(default_factory=list)


class ProviderRunLogPayload(BaseModel):
    provider: str
    requested_leads: int
    matched_leads: int
    updated_leads: int
    estimated_credits: int
    estimated_brl: float
    status: str
    message: str


class EnrichLeadTableResponse(BaseModel):
    status: str
    estimate: EnrichmentEstimatePayload
    summary: EnrichmentSummaryPayload = Field(default_factory=EnrichmentSummaryPayload)
    table: SavedLeadTablePayload | None = None
    leads: list[Lead] = Field(default_factory=list)


class EnrichmentPricingItem(BaseModel):
    provider: str
    brl_per_credit: float
    source: str  # "default" | "env_override"
    env_var: str


class EnrichmentPricingResponse(BaseModel):
    items: list[EnrichmentPricingItem] = Field(default_factory=list)
    currency: str = "BRL"
    note: str = (
        "As APIs de enriquecimento não expõem preço por crédito. Valores "
        "são tabelados no servidor e podem ser ajustados via env vars "
        "ENRICHMENT_COST_BRL_<PROVIDER>."
    )


class PeopleSearchProbeResponse(BaseModel):
    is_small: bool | None
    employee_count: int | None
    source: str
    note: str | None = None
    should_offer_general_search: bool = False


class ProbeEvent(BaseModel):
    event: str
    data: dict[str, Any] = Field(default_factory=dict)


class ProbeStartResponse(BaseModel):
    probe_id: str
    status: str


class ProbeStateResponse(BaseModel):
    probe_id: str
    status: str  # 'pending' | 'running' | 'completed' | 'failed'
    events: list[ProbeEvent] = Field(default_factory=list)
    classification: PeopleSearchProbeResponse | None = None
    error: str | None = None


@dataclass
class RunRecord:
    run_id: str
    status: RunStatus = RunStatus.PENDING
    error: str | None = None
    result: SearchResponse | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    # Live-feedback sink. ``found_leads`` accumulates as the search discovers
    # people; ``_found_keys`` dedupes the overlap between the per-lead emission
    # (people_search, real time) and the per-batch emission (other providers).
    found_leads: list[FoundLeadPayload] = field(default_factory=list)
    _found_keys: set[str] = field(default_factory=set)
    _found_lock: threading.Lock = field(default_factory=threading.Lock)

    def record_found_lead(self, lead: Lead) -> None:
        key = (
            (lead.linkedin_url or "").strip().lower()
            or f"{(lead.person_name or '').strip().lower()}|"
            f"{(lead.company_name or '').strip().lower()}|"
            f"{(lead.title or '').strip().lower()}"
        )
        with self._found_lock:
            if key in self._found_keys:
                return
            self._found_keys.add(key)
            self.found_leads.append(
                FoundLeadPayload(
                    person_name=lead.person_name,
                    title=lead.title,
                    company_name=lead.company_name,
                    location=lead.linkedin_location,
                    linkedin_url=lead.linkedin_url,
                    confidence_score=int(lead.confidence_score),
                    matched_title=lead.matched_title,
                    validation_status=lead.validation_status,
                    note=lead.consultation_note,
                )
            )

    def found_leads_snapshot(self) -> list[FoundLeadPayload]:
        with self._found_lock:
            return list(self.found_leads)


class RunRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: dict[str, RunRecord] = {}

    def create(self) -> RunRecord:
        run_id = uuid.uuid4().hex
        record = RunRecord(run_id=run_id)
        with self._lock:
            self._runs[run_id] = record
        return record

    def get(self, run_id: str) -> RunRecord | None:
        with self._lock:
            return self._runs.get(run_id)

    def update(self, record: RunRecord) -> None:
        with self._lock:
            self._runs[record.run_id] = record

    def clear(self) -> None:
        with self._lock:
            self._runs.clear()


def get_run_registry(app: FastAPI) -> RunRegistry:
    return app.state.run_registry  # type: ignore[no-any-return]


@dataclass
class ProbeRecord:
    probe_id: str
    status: str = "pending"
    events: list[dict[str, Any]] = field(default_factory=list)
    classification: PeopleSearchProbeResponse | None = None
    error: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


class ProbeRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._probes: dict[str, ProbeRecord] = {}

    def create(self) -> ProbeRecord:
        probe_id = uuid.uuid4().hex
        record = ProbeRecord(probe_id=probe_id)
        with self._lock:
            self._probes[probe_id] = record
        return record

    def get(self, probe_id: str) -> ProbeRecord | None:
        with self._lock:
            return self._probes.get(probe_id)


def get_probe_registry(app: FastAPI) -> ProbeRegistry:
    return app.state.probe_registry  # type: ignore[no-any-return]


@dataclass
class TelegramPhoneRunRecord:
    """Estado em memória de um run resumível de extração de telefone via
    Telegram. Cada ``/next`` processa exatamente um lead e avança o cursor;
    o cliente decide quando chamar o próximo `/next` (pausa anti-ban).

    Não é persistido em SQLite de propósito: o run depende do
    Chrome+Playwright vivo do processo atual. Se o sidecar reinicia, a
    sessão está morta de qualquer jeito — o cliente deve criar um run novo.
    """

    run_id: str
    table_id: str
    lead_refs: list[str] = field(default_factory=list)
    next_index: int = 0
    # status global do run
    # pending: criado mas /next ainda não chamado
    # in_progress: processou pelo menos 1 lead, ainda há mais
    # awaiting_cpf_confirmation: etapa 1 (name) rodou pro lead atual,
    #     servidor está esperando a UI confirmar quais CPFs vão pra /cpf
    # completed: cursor passou de todos os leads
    # cancelled: operador encerrou
    status: str = "pending"
    target_titles: list[str] | None = None
    results: list[dict[str, Any]] = field(default_factory=list)
    # Quando o status é awaiting_cpf_confirmation, este campo guarda os
    # CPFs do lead atual que o matcher pré-selecionou (a UI mostra esses
    # marcados por default). Se o usuário desmarcar todos e clicar
    # "Pular este lead", o cursor avança sem rodar /cpf.
    pending_eligible_cpfs: list[str] = field(default_factory=list)
    pending_lead_ref: str | None = None
    summary: dict[str, int] = field(
        default_factory=lambda: {
            "requested_leads": 0,
            "leads_with_phone": 0,
            "leads_blocked": 0,
            "phones_persisted": 0,
            "skipped_existing_phone": 0,
        }
    )
    lock: threading.Lock = field(default_factory=threading.Lock)


class TelegramPhoneRunRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: dict[str, TelegramPhoneRunRecord] = {}

    def create(
        self,
        *,
        table_id: str,
        lead_refs: list[str],
        target_titles: list[str] | None,
    ) -> TelegramPhoneRunRecord:
        run_id = uuid.uuid4().hex
        record = TelegramPhoneRunRecord(
            run_id=run_id,
            table_id=table_id,
            lead_refs=list(lead_refs),
            target_titles=list(target_titles) if target_titles else None,
        )
        record.summary["requested_leads"] = len(lead_refs)
        with self._lock:
            self._runs[run_id] = record
        return record

    def get(self, run_id: str) -> TelegramPhoneRunRecord | None:
        with self._lock:
            return self._runs.get(run_id)

    def list_for_table(self, table_id: str) -> list[TelegramPhoneRunRecord]:
        with self._lock:
            return [r for r in self._runs.values() if r.table_id == table_id]


def get_telegram_phone_run_registry(app: FastAPI) -> TelegramPhoneRunRegistry:
    return app.state.telegram_phone_run_registry  # type: ignore[no-any-return]


def get_saved_leads_store(app: FastAPI) -> SavedLeadsStore:
    return app.state.saved_leads_store  # type: ignore[no-any-return]


def _table_to_payload(table: Any) -> SavedLeadTablePayload:
    return SavedLeadTablePayload(
        id=table.id,
        name=table.name,
        created_at=table.created_at,
        updated_at=table.updated_at,
        source_type=table.source_type,
        keywords=list(table.keywords),
        search_queries=list(table.search_queries),
        search_request=dict(table.search_request),
        enrichment_status=table.enrichment_status,
        lead_count=int(table.lead_count),
    )


_HUMAN_LABELS_SENIORITY = {
    "c_level": "C-level / Founder",
    "vp": "Vice President",
    "director": "Director / Diretor",
    "head": "Head of",
    "manager": "Manager / Gerente",
    "senior": "Senior / Sênior",
    "mid": "Mid / Pleno",
    "junior": "Junior / Júnior",
    "intern": "Intern / Estagiário",
}

_HUMAN_LABELS_FUNCTION = {
    "marketing": "Marketing & Growth",
    "sales": "Vendas / Sales",
    "engineering": "Engenharia / Engineering",
    "product": "Produto / Product",
    "design": "Design",
    "data": "Dados / Data",
    "finance": "Financeiro / Finance",
    "hr": "RH / People",
    "operations": "Operações / Operations",
    "legal": "Jurídico / Legal",
    "customer_success": "Customer Success",
    "executive": "Executivo / C-suite",
}

_HUMAN_LABELS_SCRAPE_MODE = {
    "api": "APIs externas + busca pública",
    "serp": "Busca pública (sem conta)",
    "cookie": "LinkedIn com cookie (li_at)",
    "people_search": "LinkedIn People (li_at, sem visitar perfis)",
    "browser": "ARRISCADO — Playwright logado",
}


def build_app(*, saved_leads_path: str | None = None) -> FastAPI:
    app = FastAPI(title="Beautiful LinkedIn Sidecar", version=VERSION)
    app.state.run_registry = RunRegistry()
    app.state.probe_registry = ProbeRegistry()
    app.state.telegram_phone_run_registry = TelegramPhoneRunRegistry()
    resolved_path = saved_leads_path or load_settings().saved_leads_path
    app.state.saved_leads_store = SavedLeadsStore(resolved_path)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": VERSION}

    @app.get("/taxonomies", response_model=TaxonomiesResponse)
    def taxonomies() -> TaxonomiesResponse:
        seniority = [
            TaxonomyItem(
                value=level.value,
                label=_HUMAN_LABELS_SENIORITY.get(level.value, level.value),
                aliases=list(SENIORITY_ALIASES.get(level.value, [])),
            )
            for level in SENIORITY_ORDER
        ]
        functions = [
            TaxonomyItem(
                value=func.value,
                label=_HUMAN_LABELS_FUNCTION.get(func.value, func.value),
                aliases=list(FUNCTION_ALIASES.get(func.value, [])),
            )
            for func in JobFunction
        ]
        scrape_modes = [
            TaxonomyItem(value=value, label=label)
            for value, label in _HUMAN_LABELS_SCRAPE_MODE.items()
        ]
        role_presets = [
            TaxonomyItem(value=get_area_slug(label), label=label, aliases=list(terms))
            for label, terms in AREAS_MAP.items()
        ]
        return TaxonomiesResponse(
            seniority=seniority,
            functions=functions,
            role_presets=role_presets,
            scrape_modes=scrape_modes,
        )

    @app.post("/search", response_model=SearchResponse)
    def search(request: SearchRequest) -> SearchResponse:
        _ensure_risky_mode_consent(request)
        result = _execute(
            request,
            exclude_lead_keys=_global_dedupe_keys_for_search(app),
        )
        return SearchResponse(
            leads=result.leads,
            summary=result.summary,
            provider_diagnostics=result.provider_diagnostics,
        )

    @app.post("/diagnostics", response_model=DiagnosticsResponse)
    def diagnostics(payload: ApiKeyOverrides | None = None) -> DiagnosticsResponse:
        return _build_diagnostics(payload)

    @app.get("/diagnostics", response_model=DiagnosticsResponse)
    def diagnostics_get() -> DiagnosticsResponse:
        return _build_diagnostics(None)

    @app.post(
        "/diagnostics/playwright",
        response_model=PlaywrightDiagnosticResponse,
    )
    def playwright_diagnostic(
        payload: PlaywrightDiagnosticRequest | None = None,
    ) -> PlaywrightDiagnosticResponse:
        request = payload or PlaywrightDiagnosticRequest()
        settings = load_settings()
        endpoint = (
            request.cdp_endpoint
            or settings.linkedin_cdp_endpoint
            or "http://127.0.0.1:9223"
        )
        return _run_playwright_diagnostic(endpoint)

    @app.post(
        "/diagnostics/cookie",
        response_model=CookieDiagnosticResponse,
    )
    def cookie_diagnostic(
        payload: CookieDiagnosticRequest | None = None,
    ) -> CookieDiagnosticResponse:
        request = payload or CookieDiagnosticRequest()
        diag = diagnose_li_at_cookie(
            explicit_cookie=request.cookie,
            browser=request.browser or "auto",
        )
        settings = load_settings()
        cdp_endpoint = settings.linkedin_cdp_endpoint
        cdp_alive = (
            probe_cdp_endpoint(cdp_endpoint)
            if cdp_endpoint and settings.linkedin_cdp_enabled
            else False
        )
        return CookieDiagnosticResponse(
            found=diag.found,
            source=diag.source,
            browser_priority=list(diag.browser_priority),
            default_browser=diag.default_browser,
            rookiepy_available=diag.rookiepy_available,
            browser_cookie3_available=diag.browser_cookie3_available,
            attempts=[
                CookieBackendAttempt(
                    backend=a.backend,
                    browser=a.browser,
                    found=a.found,
                    error=a.error,
                )
                for a in diag.attempts
            ],
            hints=list(diag.hints),
            preview=diag.preview,
            cdp_endpoint=cdp_endpoint,
            cdp_alive=cdp_alive,
        )

    @app.post("/search/start", response_model=StartRunResponse, status_code=status.HTTP_202_ACCEPTED)
    def start_run(request: SearchRequest) -> StartRunResponse:
        _ensure_risky_mode_consent(request)
        record = get_run_registry(app).create()
        thread = threading.Thread(
            target=_execute_in_background,
            args=(app, record, request),
            daemon=True,
        )
        thread.start()
        return StartRunResponse(run_id=record.run_id, status=record.status)

    @app.get("/runs/{run_id}", response_model=RunStateResponse)
    def run_state(run_id: str) -> RunStateResponse:
        record = get_run_registry(app).get(run_id)
        if record is None:
            raise HTTPException(status_code=404, detail="run não encontrado")
        found = record.found_leads_snapshot()
        return RunStateResponse(
            run_id=record.run_id,
            status=record.status,
            error=record.error,
            result=record.result,
            found_leads=found,
            found_count=len(found),
        )

    @app.post(
        "/people-search/probe/start",
        response_model=ProbeStartResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def people_search_probe_start(
        payload: PeopleSearchProbeRequest,
    ) -> ProbeStartResponse:
        record = get_probe_registry(app).create()
        # Snapshot the initial status before the worker thread starts so the
        # caller always sees 'pending' here, even if the resolver finishes
        # before we return (which can happen in tests with a synchronous
        # fake resolver).
        thread = threading.Thread(
            target=_run_people_probe,
            args=(app, record, payload),
            daemon=True,
        )
        thread.start()
        return ProbeStartResponse(probe_id=record.probe_id, status="pending")

    @app.get(
        "/people-search/probe/{probe_id}",
        response_model=ProbeStateResponse,
    )
    def people_search_probe_state(probe_id: str) -> ProbeStateResponse:
        record = get_probe_registry(app).get(probe_id)
        if record is None:
            raise HTTPException(status_code=404, detail="probe não encontrado")
        with record.lock:
            return ProbeStateResponse(
                probe_id=record.probe_id,
                status=record.status,
                events=[ProbeEvent(**event) for event in record.events],
                classification=record.classification,
                error=record.error,
            )

    @app.post(
        "/people-search/probe",
        response_model=PeopleSearchProbeResponse,
    )
    def people_search_probe(
        payload: PeopleSearchProbeRequest,
    ) -> PeopleSearchProbeResponse:
        try:
            raw_signals = _probe_people_company(
                company_name=payload.company_name,
                company_domain=payload.company_domain,
                linkedin_url=payload.linkedin_url,
            )
        except Exception:  # network/parsing failures must not 500 the UI
            raw_signals = {}
        classification = classify_company_size(
            employee_count=raw_signals.get("employee_count"),
            employee_count_text=raw_signals.get("employee_count_text"),
            visible_card_count=raw_signals.get("visible_card_count"),
        )
        return _probe_response(classification)

    @app.get(
        "/enrichment/pricing",
        response_model=EnrichmentPricingResponse,
    )
    def enrichment_pricing() -> EnrichmentPricingResponse:
        overrides = _env_override_pricing()
        effective = get_enrichment_pricing()
        items = [
            EnrichmentPricingItem(
                provider=provider,
                brl_per_credit=effective[provider],
                source="env_override" if provider in overrides else "default",
                env_var=f"ENRICHMENT_COST_BRL_{provider.upper()}",
            )
            for provider in sorted(effective.keys())
        ]
        return EnrichmentPricingResponse(items=items)

    @app.get("/lead-tables", response_model=list[SavedLeadTablePayload])
    def list_lead_tables() -> list[SavedLeadTablePayload]:
        tables = get_saved_leads_store(app).list_tables()
        return [_table_to_payload(t) for t in tables]

    @app.get("/lead-tables/{table_id}", response_model=SavedLeadTableDetail)
    def get_lead_table(table_id: str) -> SavedLeadTableDetail:
        store = get_saved_leads_store(app)
        try:
            table = store.get_table(table_id)
            leads = store.list_leads(table_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return SavedLeadTableDetail(table=_table_to_payload(table), leads=leads)

    @app.post(
        "/lead-tables",
        response_model=SavedLeadTableDetail,
        status_code=status.HTTP_201_CREATED,
    )
    def create_lead_table(payload: SaveLeadTableRequest) -> SavedLeadTableDetail:
        store = get_saved_leads_store(app)
        queries: list[str] = []
        if payload.generate_queries:
            queries = _maybe_build_queries(payload.search_request, payload.keywords)
        table = store.create_table(
            name=payload.name,
            source_type=SOURCE_TYPE_SEARCH,
            keywords=payload.keywords,
            search_queries=queries,
            search_request=payload.search_request,
        )
        store.add_leads(table.id, payload.leads)
        refreshed = store.get_table(table.id)
        leads = store.list_leads(table.id)
        return SavedLeadTableDetail(table=_table_to_payload(refreshed), leads=leads)

    @app.post(
        "/lead-tables/import",
        response_model=SavedLeadTableDetail,
        status_code=status.HTTP_201_CREATED,
    )
    def import_lead_table(payload: ImportLeadTableRequest) -> SavedLeadTableDetail:
        store = get_saved_leads_store(app)
        try:
            table = store.import_table(name=payload.name, file_path=payload.file_path)
        except ImportColumnError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        leads = store.list_leads(table.id)
        return SavedLeadTableDetail(table=_table_to_payload(table), leads=leads)

    @app.post(
        "/lead-tables/{table_id}/export",
        response_model=ExportLeadTableResponse,
    )
    def export_lead_table(
        table_id: str, payload: ExportLeadTableRequest | None = None
    ) -> ExportLeadTableResponse:
        store = get_saved_leads_store(app)
        request = payload or ExportLeadTableRequest()
        try:
            path = store.export_csv(
                table_id, request.output_path, format=request.format
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return ExportLeadTableResponse(output_path=str(path))

    @app.post(
        "/lead-tables/{table_id}/enrich",
        response_model=EnrichLeadTableResponse,
    )
    def enrich_lead_table(
        table_id: str, payload: EnrichLeadTableRequest
    ) -> EnrichLeadTableResponse:
        store = get_saved_leads_store(app)
        try:
            table = store.get_table(table_id)
            saved_leads = store.list_leads(table_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        selected = _select_leads(saved_leads, payload.lead_refs)
        if not selected:
            raise HTTPException(
                status_code=422,
                detail="Nenhum lead selecionado foi encontrado na tabela.",
            )

        # Pricing vem SEMPRE do servidor — o ``credit_costs_brl`` do payload
        # é ignorado (mantido no schema só por compat). Operadores que
        # queiram custos diferentes setam ``ENRICHMENT_COST_BRL_<PROVIDER>``
        # no ambiente do sidecar.
        options = EnrichmentOptions(
            fields=payload.fields,  # type: ignore[arg-type]
            providers=payload.providers,
            credit_costs_brl=get_enrichment_pricing(),
            apollo_webhook_url=payload.apollo_webhook_url,
        )
        estimate = estimate_enrichment_cost(selected, options)
        if not payload.confirmed:
            return EnrichLeadTableResponse(
                status="estimated",
                estimate=_enrichment_estimate_payload(estimate),
                table=_table_to_payload(table),
            )

        settings = _settings_with_api_key_overrides(payload.api_keys)
        providers = _build_enrichment_providers(settings, options)
        summary = run_saved_lead_enrichment(
            store=store,
            table_id=table_id,
            selected_leads=selected,
            options=options,
            providers=providers,
        )
        refreshed = store.get_table(table_id)
        leads = store.list_leads(table_id)
        return EnrichLeadTableResponse(
            status="completed",
            estimate=_enrichment_estimate_payload(estimate),
            summary=_enrichment_summary_payload(summary),
            table=_table_to_payload(refreshed),
            leads=leads,
        )

    @app.post(
        "/lead-tables/{table_id}/internal-enrich",
        response_model=InternalEnrichResponse,
    )
    def internal_enrich_lead_table(
        table_id: str, payload: InternalEnrichRequest
    ) -> InternalEnrichResponse:
        store = get_saved_leads_store(app)
        try:
            table = store.get_table(table_id)
            saved_leads = store.list_leads(table_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        # Scope the run. When lead_refs is empty/None, enrich the whole table.
        if payload.lead_refs:
            selected = _select_leads(saved_leads, payload.lead_refs)
        else:
            selected = list(saved_leads)

        if not selected:
            raise HTTPException(
                status_code=422,
                detail="Nenhum lead encontrado para enriquecimento interno.",
            )
        selected = _apply_internal_domain_fallback(
            selected,
            payload.company_domain
            or _clean_internal_company_domain(table.search_request.get("company_domain")),
        )

        existing_company_emails = [
            (lead.person_name or "", lead.email or "")
            for lead in saved_leads
            if lead.email and lead.person_name
        ]
        # Aggregate every domain ever observed for each company across the
        # entire table — including ones discovered by paid providers like
        # Apollo. The orchestrator tries them 1-by-1 for leads whose own
        # company_domain is missing or fails to validate.
        company_domains = collect_company_domains(saved_leads)

        email_counters = {
            "enriched": 0,
            "skipped_existing_email": 0,
            "failed_missing_domain": 0,
            "no_change": 0,
        }
        phone_counters = {
            "enriched": 0,
            "skipped_existing_phone": 0,
            "failed_no_candidate": 0,
            "no_change": 0,
        }
        if payload.fields in {"email", "both"}:
            updates = _run_internal_enrichment(
                leads=selected,
                existing_company_emails=existing_company_emails,
                company_domains=company_domains,
            )
            email_counters = store.apply_internal_enrichment_updates(
                table_id, updates
            )
        if payload.fields in {"phone", "both"}:
            phone_domains = collect_company_domains_from_leads(saved_leads)
            # Merge any e-mail-derived domains into the phone domain
            # ranking — they're still the company's website, just
            # discovered from a different signal.
            for key, domains in company_domains.items():
                bucket = phone_domains.setdefault(key, [])
                for domain in domains:
                    if domain and domain not in bucket:
                        bucket.append(domain)
            phone_updates = _run_internal_phone_enrichment(
                leads=selected,
                company_domains=phone_domains,
                phone_sources=payload.phone_sources,
            )
            phone_counters = store.apply_internal_phone_enrichment_updates(
                table_id, phone_updates
            )
        summary = InternalEnrichSummary(
            requested_leads=len(selected),
            enriched_leads=email_counters["enriched"],
            skipped_existing_email=email_counters["skipped_existing_email"],
            failed_missing_domain=email_counters["failed_missing_domain"],
            no_change=email_counters["no_change"],
            enriched_phone_leads=phone_counters["enriched"],
            skipped_existing_phone=phone_counters["skipped_existing_phone"],
            failed_no_phone_candidate=phone_counters["failed_no_candidate"],
        )
        refreshed = store.get_table(table_id)
        leads = store.list_leads(table_id)
        return InternalEnrichResponse(
            status="completed",
            summary=summary,
            table=_table_to_payload(refreshed),
            leads=leads,
        )

    @app.post(
        "/lead-tables/{table_id}/linkedin-profile-validate",
        response_model=LinkedInProfileValidationResponse,
    )
    def linkedin_profile_validate(
        table_id: str, payload: LinkedInProfileValidationRequest
    ) -> LinkedInProfileValidationResponse:
        store = get_saved_leads_store(app)
        try:
            table = store.get_table(table_id)
            saved_leads = store.list_leads(table_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        selected = _select_leads(saved_leads, payload.lead_refs)
        if not selected:
            raise HTTPException(
                status_code=422,
                detail="Nenhum lead selecionado foi encontrado na tabela.",
            )
        if len(selected) > payload.max_leads:
            raise HTTPException(
                status_code=422,
                detail=f"Selecione no máximo {payload.max_leads} leads por validação de cargo.",
            )

        try:
            validation_settings = load_settings()
            if payload.cdp_endpoint is not None:
                from dataclasses import replace as _dc_replace
                validation_settings = _dc_replace(
                    validation_settings,
                    linkedin_cdp_endpoint=payload.cdp_endpoint,
                    linkedin_cdp_enabled=True,
                )
            updates = _run_linkedin_profile_validation(
                leads=selected,
                settings=validation_settings,
                max_leads=payload.max_leads,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        counters = store.apply_linkedin_profile_validation_updates(
            table_id, updates
        )
        refreshed = store.get_table(table_id)
        leads = store.list_leads(table_id)
        return LinkedInProfileValidationResponse(
            status="completed",
            summary=LinkedInProfileValidationSummary(
                requested_leads=len(selected),
                validated_leads=counters["validated"],
                failed_leads=counters["failed"],
                no_linkedin_url=counters["no_linkedin_url"],
                no_change=counters["no_change"],
            ),
            table=_table_to_payload(refreshed),
            leads=leads,
        )

    @app.post(
        "/lead-tables/{table_id}/telegram-consult",
        response_model=TelegramConsultResponse,
    )
    def telegram_consult(
        table_id: str, payload: TelegramConsultRequest
    ) -> TelegramConsultResponse:
        """Drive Chrome (CDP) through Telegram Web to query each lead in
        the configured name-consult providers. Gonzales now runs in the
        private @ConsultoriaGonzalesbot chat; Unix remains a fallback
        provider. Sequential by design — a single Chrome session can
        only handle one Telegram-Web tab at a time."""
        store = get_saved_leads_store(app)
        try:
            store.get_table(table_id)
            saved_leads = store.list_leads(table_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        selected = _select_leads(saved_leads, payload.lead_refs)
        if not selected:
            raise HTTPException(
                status_code=422,
                detail="Nenhum lead selecionado foi encontrado na tabela.",
            )
        if len(selected) > payload.max_leads:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Selecione no máximo {payload.max_leads} leads por consulta Telegram."
                ),
            )

        settings = load_settings()
        selected = _refresh_linkedin_signals_for_telegram(
            selected=selected,
            selected_refs=payload.lead_refs,
            table_id=table_id,
            store=store,
            settings=settings,
        )

        try:
            lookup = _default_telegram_consult_lookup(settings)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        consults = _run_telegram_consult(
            selected=selected,
            table_id=table_id,
            store=store,
            lookup=lookup,
        )
        succeeded = sum(1 for c in consults if c.error is None and c.raw_text)
        failed = len(consults) - succeeded
        return TelegramConsultResponse(
            status="completed",
            summary=TelegramConsultSummary(
                requested_leads=len(selected),
                succeeded=succeeded,
                failed=failed,
            ),
            consults=consults,
        )

    @app.post(
        "/lead-tables/{table_id}/telegram-consult/multiple-experimental",
        response_model=TelegramConsultResponse,
    )
    def telegram_consult_multiple_experimental(
        table_id: str, payload: TelegramConsultRequest
    ) -> TelegramConsultResponse:
        """Experimental multi-provider evidence flow.

        This intentionally does not replace ``/telegram-consult`` nor
        the production resumable phone flow. It runs Finder, Gon and Unix
        as separate evidence sources, persists one consult row per
        provider, and lets the existing parser/ranker compare the
        extracted candidates.
        """
        store = get_saved_leads_store(app)
        try:
            store.get_table(table_id)
            saved_leads = store.list_leads(table_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        selected = _select_leads(saved_leads, payload.lead_refs)
        if not selected:
            raise HTTPException(
                status_code=422,
                detail="Nenhum lead selecionado foi encontrado na tabela.",
            )
        if len(selected) > payload.max_leads:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Selecione no máximo {payload.max_leads} leads por consulta Telegram."
                ),
            )

        settings = load_settings()
        selected = _refresh_linkedin_signals_for_telegram(
            selected=selected,
            selected_refs=payload.lead_refs,
            table_id=table_id,
            store=store,
            settings=settings,
        )

        try:
            lookup = _default_telegram_multi_experimental_lookup(settings)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        consults = _run_telegram_consult(
            selected=selected,
            table_id=table_id,
            store=store,
            lookup=lookup,
        )
        finder_cpf = _default_findex_cpf_consult(settings)
        consults.extend(
            _run_experimental_finder_cpf_followup(
                selected=selected,
                table_id=table_id,
                store=store,
                finder_cpf=finder_cpf,
            )
        )
        succeeded = sum(1 for c in consults if c.error is None and c.raw_text)
        failed = len(consults) - succeeded
        return TelegramConsultResponse(
            status="completed",
            summary=TelegramConsultSummary(
                requested_leads=len(selected),
                succeeded=succeeded,
                failed=failed,
            ),
            consults=consults,
        )

    @app.post(
        "/lead-tables/{table_id}/telegram-consult/telethon-experimental",
        response_model=TelegramConsultResponse,
    )
    def telegram_consult_telethon_experimental(
        table_id: str, payload: TelegramConsultRequest
    ) -> TelegramConsultResponse:
        """Experimental multi-provider evidence flow via Telethon.

        This route is deliberately separate from both Telegram-Web/CDP
        consult routes. It uses a native Telegram session and persists
        rows in the same evidence table so the parser, matcher, and UI
        can compare results without changing the existing flows.
        """
        store = get_saved_leads_store(app)
        try:
            store.get_table(table_id)
            saved_leads = store.list_leads(table_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        selected = _select_leads(saved_leads, payload.lead_refs)
        if not selected:
            raise HTTPException(
                status_code=422,
                detail="Nenhum lead selecionado foi encontrado na tabela.",
            )
        if len(selected) > payload.max_leads:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Selecione no máximo {payload.max_leads} leads por consulta Telegram."
                ),
            )

        settings = load_settings()
        selected = _refresh_linkedin_signals_for_telegram(
            selected=selected,
            selected_refs=payload.lead_refs,
            table_id=table_id,
            store=store,
            settings=settings,
        )

        try:
            lookup = _default_telegram_telethon_multi_experimental_lookup(settings)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        consults = _run_telegram_consult(
            selected=selected,
            table_id=table_id,
            store=store,
            lookup=lookup,
        )
        succeeded = sum(1 for c in consults if c.error is None and c.raw_text)
        failed = len(consults) - succeeded
        return TelegramConsultResponse(
            status="completed",
            summary=TelegramConsultSummary(
                requested_leads=len(selected),
                succeeded=succeeded,
                failed=failed,
            ),
            consults=consults,
        )

    @app.post(
        "/lead-tables/{table_id}/telegram-consult/telethon-pipeline",
        response_model=TelethonPipelineResponse,
    )
    def telegram_consult_telethon_pipeline(
        table_id: str, payload: TelethonPipelineRequest
    ) -> TelethonPipelineResponse:
        """End-to-end Telethon pipeline: name → CPF → phone, per lead.

        Stages, executed sequentially per lead so the conservative
        Telegram action throttle paces the whole flow (not just within
        one stage):

        1. ``/nome`` on Findex + Gonzales + Unix (Telethon). Persists
           three ``query_type='name'`` rows and parses ranked CPF
           candidates against the lead's LinkedIn signals — including
           the new ``linkedin_birthday`` high-weight DD/MM check.
        2. ``/cpf`` + SISREG-III on Gonzales (Telethon) for the top
           one CPF candidate whose score clears ``min_score``. If the
           client sends a higher ``max_cpf_candidates``, the endpoint
           still clamps the phone stage to one CPF: the highest-scoring
           candidate only.
           Each CPF row is persisted with ``query_type='cpf'``.
        3. Phone harvest from the /cpf raw text. Phones merge into the
           lead's ``phone`` / ``phone_alternatives`` via the existing
           internal-phone enrichment helper — never overwriting an
           existing primary.

        Blocking endpoint by design. Wall-clock cost is dominated by
        the conservative Telegram spacing and the bots' own latency;
        expect about a minute or more per complete phone extraction.
        """
        store = get_saved_leads_store(app)
        try:
            store.get_table(table_id)
            saved_leads = store.list_leads(table_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        selected = _select_leads(saved_leads, payload.lead_refs)
        if not selected:
            raise HTTPException(
                status_code=422,
                detail="Nenhum lead selecionado foi encontrado na tabela.",
            )
        if len(selected) > payload.max_leads:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Selecione no máximo {payload.max_leads} leads por consulta Telegram."
                ),
            )

        settings = load_settings()
        selected = _refresh_linkedin_signals_for_telegram(
            selected=selected,
            selected_refs=payload.lead_refs,
            table_id=table_id,
            store=store,
            settings=settings,
        )

        try:
            name_lookup = _default_telegram_telethon_multi_experimental_lookup(settings)
            cpf_driver = _default_telethon_serasa_cpf_consult(settings)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        leads_out: list[TelethonPipelineLeadResult] = []
        total_phones_persisted = 0
        leads_with_phone = 0
        total_name_consults = 0
        total_cpf_consults = 0

        logger.info(
            "[telethon-pipeline] iniciando — table=%s leads=%d",
            table_id,
            len(selected),
        )

        for idx, lead in enumerate(selected, start=1):
            lead_ref = _lead_ref(lead) or (lead.person_name or "")
            logger.info(
                "[telethon-pipeline] lead %d/%d — %r (%s)",
                idx,
                len(selected),
                lead.person_name or "(sem nome)",
                lead_ref,
            )

            name_consults = _run_telegram_consult(
                selected=[lead],
                table_id=table_id,
                store=store,
                lookup=name_lookup,
            )
            total_name_consults += len(name_consults)

            candidates = _pipeline_run_phone_followup(
                lead=lead,
                table_id=table_id,
                store=store,
                consult_fn=cpf_driver.consult,
                target_titles=payload.target_titles,
                run_id=None,
                provider_name="serasa_cpf",
                min_score=payload.min_score,
                max_candidates=1,
            )

            persisted_cpf = store.list_telegram_consults_for_lead(
                table_id, lead_ref, query_type="cpf"
            )
            total_cpf_consults += len(persisted_cpf)
            blocked_reason: str | None = None
            for row in persisted_cpf:
                if row.blocked_reason:
                    blocked_reason = row.blocked_reason
                    break

            lead_persisted = 0
            if candidates:
                primary = max(candidates, key=lambda c: c.confidence)
                update = _telegram_phone_candidate_to_update(primary)
                counters = store.apply_internal_phone_enrichment_updates(
                    table_id, [(lead, update)]
                )
                lead_persisted = counters.get("enriched", 0)
                total_phones_persisted += lead_persisted
                for extra in (c for c in candidates if c is not primary):
                    store.apply_internal_phone_enrichment_updates(
                        table_id,
                        [(lead, _telegram_phone_candidate_to_update(extra))],
                    )
                _apply_telegram_contact_details(
                    store=store,
                    table_id=table_id,
                    lead=lead,
                    candidates=candidates,
                )
                if lead_persisted > 0 and update.phone:
                    leads_with_phone += 1

            if candidates:
                logger.info(
                    "[telethon-pipeline] lead=%r → %d telefone(s) colhido(s)",
                    lead.person_name or lead_ref,
                    len(candidates),
                )
            else:
                logger.info(
                    "[telethon-pipeline] lead=%r → nenhum telefone encontrado (blocked=%s)",
                    lead.person_name or lead_ref,
                    blocked_reason or "–",
                )

            leads_out.append(
                TelethonPipelineLeadResult(
                    lead_ref=lead_ref,
                    lead_name=lead.person_name,
                    name_consults=name_consults,
                    cpf_consults=[_consult_to_payload(row) for row in persisted_cpf],
                    blocked_reason=blocked_reason,
                    candidates=[
                        _telegram_phone_candidate_to_payload(c) for c in candidates
                    ],
                )
            )

        logger.info(
            "[telethon-pipeline] concluído — leads=%d nome_consults=%d cpf_consults=%d "
            "leads_com_telefone=%d telefones_persistidos=%d",
            len(selected),
            total_name_consults,
            total_cpf_consults,
            leads_with_phone,
            total_phones_persisted,
        )

        return TelethonPipelineResponse(
            status="completed",
            summary=TelethonPipelineSummary(
                requested_leads=len(selected),
                name_consults=total_name_consults,
                cpf_consults=total_cpf_consults,
                leads_with_phone=leads_with_phone,
                phones_persisted=total_phones_persisted,
            ),
            leads=leads_out,
        )

    @app.post(
        "/lead-tables/{table_id}/telegram-phone/telethon-cpf-stage",
        response_model=TelethonPipelineResponse,
    )
    def telegram_phone_telethon_cpf_stage(
        table_id: str, payload: TelethonCpfStageRequest
    ) -> TelethonPipelineResponse:
        """Run one already-extracted CPF through Gonzales /cpf via Telethon.

        This backs the per-consult "Achar telefone" button. It does not
        create a resumable Playwright run and never touches the CDP CPF
        driver; it only uses the native Telegram session configured for
        Telethon.
        """
        store = get_saved_leads_store(app)
        try:
            store.get_table(table_id)
            saved_leads = store.list_leads(table_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        selected = _select_leads(saved_leads, [payload.lead_ref])
        if not selected:
            raise HTTPException(
                status_code=422,
                detail="Lead selecionado não foi encontrado na tabela.",
            )
        lead = selected[0]
        lead_ref = _lead_ref(lead) or payload.lead_ref

        settings = load_settings()
        try:
            cpf_driver = _default_telethon_serasa_cpf_consult(settings)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        stage = _pipeline_run_cpf_stage_only(
            lead=lead,
            table_id=table_id,
            store=store,
            cpf_consult_fn=cpf_driver.consult,
            selected_cpfs=[payload.cpf],
            run_id=f"telethon-cpf-{uuid.uuid4().hex}",
            cpf_provider_name="serasa_cpf",
        )

        phones_persisted = 0
        leads_with_phone = 0
        if stage.phone_candidates:
            ordered = sorted(
                stage.phone_candidates,
                key=lambda c: c.confidence,
                reverse=True,
            )
            primary = ordered[0]
            update = _telegram_phone_candidate_to_update(primary)
            counters = store.apply_internal_phone_enrichment_updates(
                table_id, [(lead, update)]
            )
            phones_persisted += counters.get("enriched", 0)
            if phones_persisted > 0 and update.phone:
                leads_with_phone = 1
            for extra in ordered[1:]:
                extra_counters = store.apply_internal_phone_enrichment_updates(
                    table_id,
                    [(lead, _telegram_phone_candidate_to_update(extra))],
                )
                phones_persisted += extra_counters.get("enriched", 0)
            _apply_telegram_contact_details(
                store=store,
                table_id=table_id,
                lead=lead,
                candidates=ordered,
            )

        cpf_rows = store.list_telegram_consults_for_lead(
            table_id, lead_ref, query_type="cpf"
        )
        return TelethonPipelineResponse(
            status="completed",
            summary=TelethonPipelineSummary(
                requested_leads=1,
                name_consults=0,
                cpf_consults=len(cpf_rows),
                leads_with_phone=leads_with_phone,
                phones_persisted=phones_persisted,
            ),
            leads=[
                TelethonPipelineLeadResult(
                    lead_ref=lead_ref,
                    lead_name=lead.person_name,
                    name_consults=[],
                    cpf_consults=[_consult_to_payload(row) for row in cpf_rows],
                    blocked_reason=stage.blocked_reason,
                    candidates=[
                        _telegram_phone_candidate_to_payload(c)
                        for c in stage.phone_candidates
                    ],
                )
            ],
        )

    @app.post(
        "/telegram/telethon/config",
        response_model=TelethonAuthStatusResponse,
    )
    def telegram_telethon_config(
        payload: TelethonConfigRequest,
    ) -> TelethonAuthStatusResponse:
        """Persist the Telegram API credentials supplied through the UI.

        The end user gets ``api_id``/``api_hash`` once at my.telegram.org and
        pastes them here instead of editing a ``.env``. We validate the
        shape (api_id must be the numeric app id; api_hash a non-empty token)
        and store them durably. ``load_settings()`` re-reads on every request,
        so the very next status/send-code call sees them — no sidecar restart.
        """
        api_id = payload.api_id.strip()
        api_hash = payload.api_hash.strip()
        if not api_id.isdigit():
            raise HTTPException(
                status_code=400,
                detail="O API ID deve conter apenas números (ex: 1234567).",
            )
        if not api_hash:
            raise HTTPException(status_code=400, detail="Informe o API Hash.")
        save_telegram_credentials(api_id, api_hash)
        settings = load_settings()
        return TelethonAuthStatusResponse(
            authorized=False,
            configured=bool(
                settings.telegram_api_id and settings.telegram_api_hash
            ),
            session_name=settings.telegram_session_name,
        )

    @app.delete(
        "/telegram/telethon/config",
        response_model=TelethonAuthStatusResponse,
    )
    def telegram_telethon_config_clear() -> TelethonAuthStatusResponse:
        """Forget the UI-persisted credentials so the operator can re-enter them.

        Used when the saved api_id/api_hash were wrong (login keeps failing).
        Does not touch the ``.session`` — call logout for that.
        """
        clear_telegram_credentials()
        settings = load_settings()
        return TelethonAuthStatusResponse(
            authorized=False,
            configured=bool(
                settings.telegram_api_id and settings.telegram_api_hash
            ),
            session_name=settings.telegram_session_name,
        )

    @app.get(
        "/telegram/telethon/auth/status",
        response_model=TelethonAuthStatusResponse,
    )
    def telegram_telethon_auth_status() -> TelethonAuthStatusResponse:
        settings = load_settings()
        configured = bool(settings.telegram_api_id and settings.telegram_api_hash)
        if not configured:
            return TelethonAuthStatusResponse(
                authorized=False,
                configured=False,
                session_name=settings.telegram_session_name,
            )
        try:
            authorized = telegram_telethon_auth.is_authorized(
                session_name=settings.telegram_session_name,
                api_id=settings.telegram_api_id,
                api_hash=settings.telegram_api_hash,
            )
        except telegram_telethon_auth.TelethonAuthError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return TelethonAuthStatusResponse(
            authorized=authorized,
            configured=True,
            session_name=settings.telegram_session_name,
        )

    @app.post(
        "/telegram/telethon/auth/send-code",
        response_model=TelethonAuthSendCodeResponse,
    )
    def telegram_telethon_auth_send_code(
        payload: TelethonAuthSendCodeRequest,
    ) -> TelethonAuthSendCodeResponse:
        settings = load_settings()
        if not settings.telegram_api_id or not settings.telegram_api_hash:
            raise HTTPException(status_code=503, detail="telegram_not_configured")
        try:
            result = telegram_telethon_auth.send_code(
                phone=payload.phone,
                session_name=settings.telegram_session_name,
                api_id=settings.telegram_api_id,
                api_hash=settings.telegram_api_hash,
            )
        except telegram_telethon_auth.TelethonAuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return TelethonAuthSendCodeResponse(
            phone_code_hash=result.phone_code_hash,
            next_type=result.next_type,
            timeout=result.timeout,
        )

    @app.post(
        "/telegram/telethon/auth/sign-in",
        response_model=TelethonAuthSignInResponse,
    )
    def telegram_telethon_auth_sign_in(
        payload: TelethonAuthSignInRequest,
    ) -> TelethonAuthSignInResponse:
        settings = load_settings()
        if not settings.telegram_api_id or not settings.telegram_api_hash:
            raise HTTPException(status_code=503, detail="telegram_not_configured")
        try:
            result = telegram_telethon_auth.sign_in(
                phone=payload.phone,
                code=payload.code,
                phone_code_hash=payload.phone_code_hash,
                password=payload.password,
                session_name=settings.telegram_session_name,
                api_id=settings.telegram_api_id,
                api_hash=settings.telegram_api_hash,
            )
        except telegram_telethon_auth.TelethonAuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return TelethonAuthSignInResponse(
            authorized=not result.requires_password and result.user_id is not None,
            requires_password=result.requires_password,
            user_id=result.user_id,
            username=result.username,
            first_name=result.first_name,
        )

    @app.post(
        "/telegram/telethon/auth/logout",
        response_model=TelethonAuthLogoutResponse,
    )
    def telegram_telethon_auth_logout() -> TelethonAuthLogoutResponse:
        settings = load_settings()
        if not settings.telegram_api_id or not settings.telegram_api_hash:
            raise HTTPException(status_code=503, detail="telegram_not_configured")
        try:
            logged_out = telegram_telethon_auth.log_out(
                session_name=settings.telegram_session_name,
                api_id=settings.telegram_api_id,
                api_hash=settings.telegram_api_hash,
            )
        except telegram_telethon_auth.TelethonAuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return TelethonAuthLogoutResponse(authorized=False, logged_out=logged_out)

    @app.post(
        "/lead-tables/{table_id}/telegram-followup-phone",
        response_model=TelegramFollowupPhoneResponse,
    )
    def telegram_followup_phone(
        table_id: str, payload: TelegramFollowupPhoneRequest
    ) -> TelegramFollowupPhoneResponse:
        """Run the CPF-stage Telegram follow-up to harvest phones.

        Pre-condition: the name-stage consult must already have run for
        the selected leads — this endpoint reads the persisted CPF
        candidates from ``tabela_telegram`` (rows with
        ``query_type='name'``), filters them by the matcher score
        threshold, and drives ``/cpf <cpf>`` queries via Gonzales for
        the survivors. Phones harvested from the raw response are
        persisted into the lead's verification trail, never overwriting
        an existing ``phone``.

        Confidence policy: each returned phone carries the originating
        CPF's match_score 1:1; ``provenance.score_source`` exposes the
        audit chain so the UI can show "85 (via CPF 111.222.333-44,
        verified by location + education_age)".
        """
        store = get_saved_leads_store(app)
        try:
            store.get_table(table_id)
            saved_leads = store.list_leads(table_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        selected = _select_leads(saved_leads, payload.lead_refs)
        if not selected:
            raise HTTPException(
                status_code=422,
                detail="Nenhum lead selecionado foi encontrado na tabela.",
            )
        if len(selected) > payload.max_leads:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Selecione no máximo {payload.max_leads} leads por follow-up."
                ),
            )

        settings = load_settings()
        # Refresh LinkedIn signals so the title gate sees the freshest
        # cargo. Same best-effort behavior as the /telegram-consult
        # route — a failure here only loses signals, never blocks.
        selected = _refresh_linkedin_signals_for_telegram(
            selected=selected,
            selected_refs=payload.lead_refs,
            table_id=table_id,
            store=store,
            settings=settings,
        )

        try:
            driver = _default_gonzales_cpf_consult(settings)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        lead_results: list[TelegramFollowupLeadResult] = []
        leads_with_phone = 0
        leads_blocked = 0
        phones_persisted = 0
        skipped_existing = 0

        for lead in selected:
            lead_ref = _lead_ref(lead) or (lead.person_name or "")
            candidates = _pipeline_run_phone_followup(
                lead=lead,
                table_id=table_id,
                store=store,
                consult_fn=driver.consult,
                target_titles=payload.target_titles,
                run_id=None,
                provider_name="gon_cpf",
            )

            blocked_reason: str | None = None
            persisted_consults = store.list_telegram_consults_for_lead(
                table_id, lead_ref, query_type="cpf"
            )
            # The first row produced by the workflow carries the block
            # reason when the gate rejected the lead; surface it.
            for row in persisted_consults:
                if row.blocked_reason:
                    blocked_reason = row.blocked_reason
                    break
            if blocked_reason:
                leads_blocked += 1

            if candidates:
                # Pick the strongest candidate as the primary update — the
                # rest still get persisted as alternatives via the merge
                # helper inside the store, so nothing gets lost.
                primary = max(candidates, key=lambda c: c.confidence)
                update = _telegram_phone_candidate_to_update(primary)
                counters = store.apply_internal_phone_enrichment_updates(
                    table_id, [(lead, update)]
                )
                phones_persisted += counters.get("enriched", 0)
                skipped_existing += counters.get("skipped_existing_phone", 0)
                # Apply every other candidate so divergent values join
                # the alternatives trail with their own source label.
                if len(candidates) > 1:
                    extras = [c for c in candidates if c is not primary]
                    for extra in extras:
                        store.apply_internal_phone_enrichment_updates(
                            table_id,
                            [(lead, _telegram_phone_candidate_to_update(extra))],
                        )
                _apply_telegram_contact_details(
                    store=store,
                    table_id=table_id,
                    lead=lead,
                    candidates=candidates,
                )
                if phones_persisted > 0 and update.phone:
                    leads_with_phone += 1

            lead_results.append(
                TelegramFollowupLeadResult(
                    lead_ref=lead_ref,
                    lead_name=lead.person_name,
                    blocked_reason=blocked_reason,
                    candidates=[
                        _telegram_phone_candidate_to_payload(c) for c in candidates
                    ],
                    consults=[_consult_to_payload(row) for row in persisted_consults],
                )
            )

        return TelegramFollowupPhoneResponse(
            status="completed",
            summary=TelegramFollowupPhoneSummary(
                requested_leads=len(selected),
                leads_with_phone=leads_with_phone,
                leads_blocked=leads_blocked,
                phones_persisted=phones_persisted,
                skipped_existing_phone=skipped_existing,
            ),
            leads=lead_results,
        )

    @app.post(
        "/lead-tables/{table_id}/telegram-phone",
        response_model=TelegramPhoneResponse,
    )
    def telegram_phone(
        table_id: str, payload: TelegramPhoneRequest
    ) -> TelegramPhoneResponse:
        """Unified ``/nome`` → ``/cpf`` → phone flow for the selected
        leads.

        Replaces the two earlier endpoints (``/telegram-consult`` and
        ``/telegram-followup-phone``) from the operator's perspective.
        Per lead, the server:

        1. Refreshes LinkedIn signals best-effort so the cargo gate
           sees the freshest title.
        2. Runs the LinkedIn cargo gate if ``target_titles`` is set.
        3. Dispatches ``/nome <lead>`` via Gonzales, parses + ranks
           CPFs against LinkedIn signals.
        4. Dispatches ``/cpf <cpf>`` for the top-K survivors of the
           matcher threshold, harvests phones from each raw response.
        5. Persists the strongest phone via the internal phone
           enrichment update path (existing phones win, divergent
           values join the alternatives trail).

        Unix is intentionally not used — Gonzales is the cheap+fast
        path and adding Unix here would double the Playwright time per
        lead. The Unix-bearing ``/telegram-consult`` endpoint stays
        available for callers that want both providers' raw outputs.

        Confidence policy: each phone inherits its CPF's matcher score
        1:1, with ``provenance.score_source = "telegram_match_score"``
        so the UI can audit it without recomputing anything.
        """
        store = get_saved_leads_store(app)
        try:
            store.get_table(table_id)
            saved_leads = store.list_leads(table_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        selected = _select_leads(saved_leads, payload.lead_refs)
        if not selected:
            raise HTTPException(
                status_code=422,
                detail="Nenhum lead selecionado foi encontrado na tabela.",
            )
        if len(selected) > payload.max_leads:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Selecione no máximo {payload.max_leads} leads por execução."
                ),
            )
        _log_telegram_phone_endpoint(
            "batch_started",
            table_id=table_id,
            selected_leads=len(selected),
            target_titles=len(payload.target_titles or []),
        )

        settings = load_settings()
        selected = _refresh_linkedin_signals_for_telegram(
            selected=selected,
            selected_refs=payload.lead_refs,
            table_id=table_id,
            store=store,
            settings=settings,
        )

        try:
            name_driver = _default_gonzales_consult(settings)
            cpf_driver = _default_gonzales_cpf_consult(settings)
            email_driver = _default_findex_email_consult(settings)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        lead_payloads: list[TelegramPhoneLeadResult] = []
        totals = {
            "leads_with_phone": 0,
            "leads_blocked": 0,
            "phones_persisted": 0,
            "skipped_existing_phone": 0,
        }

        for idx, lead in enumerate(selected):
            _log_telegram_phone_endpoint(
                "batch_lead_started",
                table_id=table_id,
                lead_index=idx,
                total_leads=len(selected),
                lead_ref=_lead_ref(lead),
            )
            if idx > 0:
                # Pausa humana entre dois leads consecutivos. Sem isso o
                # endpoint dispara 10 consultas em ~60s e o Gonzales/Findex
                # rate-limita o operador. Configurável via
                # ``BEAUTIFUL_LINKEDIN_TELEGRAM_INTER_LEAD_MIN/MAX``.
                _telegram_inter_lead_pause(settings)
            lead_payload, counters = _process_one_telegram_phone_lead(
                lead=lead,
                table_id=table_id,
                store=store,
                name_consult_fn=name_driver.consult,
                cpf_consult_fn=cpf_driver.consult,
                email_consult_fn=email_driver.consult,
                target_titles=payload.target_titles,
            )
            for key, value in counters.items():
                totals[key] += value
            lead_payloads.append(lead_payload)
            _log_telegram_phone_endpoint(
                "batch_lead_completed",
                table_id=table_id,
                lead_index=idx,
                lead_ref=lead_payload.lead_ref,
                blocked_reason=lead_payload.blocked_reason,
                phone_candidates=len(lead_payload.candidates),
                last_stage=lead_payload.last_stage,
            )

        _log_telegram_phone_endpoint(
            "batch_completed",
            table_id=table_id,
            selected_leads=len(selected),
            leads_with_phone=totals["leads_with_phone"],
            phones_persisted=totals["phones_persisted"],
            leads_blocked=totals["leads_blocked"],
        )
        return TelegramPhoneResponse(
            status="completed",
            summary=TelegramPhoneSummary(
                requested_leads=len(selected),
                **totals,
            ),
            leads=lead_payloads,
        )

    @app.post(
        "/lead-tables/{table_id}/telegram-phone/start",
        response_model=TelegramPhoneStartResponse,
    )
    def telegram_phone_start(
        table_id: str, payload: TelegramPhoneStartRequest
    ) -> TelegramPhoneStartResponse:
        """Cria um run resumível para extração de telefone via Telegram.

        Não dispara nenhuma consulta — apenas prepara a sequência. O
        cliente chama ``/next`` por lead, intercalando confirmação
        humana para proteger os bots Telegram contra banimento.
        """
        store = get_saved_leads_store(app)
        try:
            store.get_table(table_id)
            saved_leads = store.list_leads(table_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        selected = _select_leads(saved_leads, payload.lead_refs)
        if not selected:
            raise HTTPException(
                status_code=422,
                detail="Nenhum lead selecionado foi encontrado na tabela.",
            )
        if len(selected) > payload.max_leads:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Selecione no máximo {payload.max_leads} leads por execução."
                ),
            )

        registry = get_telegram_phone_run_registry(app)
        # Reordena lead_refs do payload para corresponder à ordem em que
        # ``_select_leads`` devolveu os leads — isso garante que `/next`
        # processe na mesma ordem que o batch endpoint processa.
        ordered_refs = [_lead_ref(lead) for lead in selected]
        record = registry.create(
            table_id=table_id,
            lead_refs=ordered_refs,
            target_titles=payload.target_titles,
        )
        _log_telegram_phone_endpoint(
            "start_created",
            table_id=table_id,
            run_id=record.run_id,
            total_leads=len(record.lead_refs),
            target_titles=len(payload.target_titles or []),
        )
        return TelegramPhoneStartResponse(
            run_id=record.run_id,
            table_id=table_id,
            total_leads=len(record.lead_refs),
            next_index=record.next_index,
            lead_refs=list(record.lead_refs),
            status=record.status,
        )

    @app.post(
        "/lead-tables/{table_id}/telegram-phone/next",
        response_model=TelegramPhoneNextResponse,
    )
    def telegram_phone_next(
        table_id: str, payload: TelegramPhoneNextRequest
    ) -> TelegramPhoneNextResponse:
        """Processa o próximo lead do run. Após responder, o cliente
        decide quando chamar de novo (pausa anti-ban).

        Quando o cursor já passou do fim, devolve ``status=completed``
        sem disparar nada. Quando o run está cancelado, devolve 409 para
        o cliente saber que precisa criar um run novo.
        """
        registry = get_telegram_phone_run_registry(app)
        record = registry.get(payload.run_id)
        if record is None or record.table_id != table_id:
            raise HTTPException(
                status_code=404, detail="Run de telefone Telegram não encontrado."
            )

        with record.lock:
            _log_telegram_phone_endpoint(
                "next_requested",
                table_id=table_id,
                run_id=record.run_id,
                status=record.status,
                next_index=record.next_index,
                total_leads=len(record.lead_refs),
            )
            if record.status == "cancelled":
                _log_telegram_phone_endpoint(
                    "next_rejected_cancelled",
                    table_id=table_id,
                    run_id=record.run_id,
                    next_index=record.next_index,
                )
                raise HTTPException(
                    status_code=409,
                    detail="Run cancelado — crie um run novo para continuar.",
                )
            if record.status == "completed" or record.next_index >= len(record.lead_refs):
                record.status = "completed"
                _log_telegram_phone_endpoint(
                    "next_already_completed",
                    table_id=table_id,
                    run_id=record.run_id,
                    next_index=record.next_index,
                )
                return TelegramPhoneNextResponse(
                    run_id=record.run_id,
                    status="completed",
                    next_index=record.next_index,
                    total_leads=len(record.lead_refs),
                    summary=TelegramPhoneSummary(**record.summary),
                    last_lead=None,
                )

            store = get_saved_leads_store(app)
            try:
                store.get_table(table_id)
                saved_leads = store.list_leads(table_id)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

            target_ref = record.lead_refs[record.next_index]
            _log_telegram_phone_endpoint(
                "next_lead_started",
                table_id=table_id,
                run_id=record.run_id,
                next_index=record.next_index,
                lead_ref=target_ref,
            )
            selected = _select_leads(saved_leads, [target_ref])
            if not selected:
                # Lead foi removido da tabela entre /start e /next.
                # Pulamos com um marker, mantendo o cursor avançando.
                record.next_index += 1
                if record.next_index >= len(record.lead_refs):
                    record.status = "completed"
                else:
                    record.status = "in_progress"
                missing = TelegramPhoneLeadResult(
                    lead_ref=target_ref,
                    lead_name=None,
                    blocked_reason="lead_not_found_in_table",
                )
                record.results.append(missing.model_dump())
                record.summary["leads_blocked"] += 1
                _log_telegram_phone_endpoint(
                    "next_lead_missing",
                    table_id=table_id,
                    run_id=record.run_id,
                    lead_ref=target_ref,
                    next_index=record.next_index,
                    status=record.status,
                )
                return TelegramPhoneNextResponse(
                    run_id=record.run_id,
                    status=record.status,
                    next_index=record.next_index,
                    total_leads=len(record.lead_refs),
                    summary=TelegramPhoneSummary(**record.summary),
                    last_lead=missing,
                )

            settings = load_settings()
            refreshed = _refresh_linkedin_signals_for_telegram(
                selected=selected,
                selected_refs=[target_ref],
                table_id=table_id,
                store=store,
                settings=settings,
            )

            try:
                name_driver = _default_gonzales_consult(settings)
                cpf_driver = _default_gonzales_cpf_consult(settings)
                email_driver = _default_findex_email_consult(settings)
            except RuntimeError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc

            lead = refreshed[0]
            lead_payload, counters = _process_one_telegram_phone_lead(
                lead=lead,
                table_id=table_id,
                store=store,
                name_consult_fn=name_driver.consult,
                cpf_consult_fn=cpf_driver.consult,
                email_consult_fn=email_driver.consult,
                target_titles=record.target_titles,
                run_id=record.run_id,
            )
            _log_telegram_phone_endpoint(
                "next_lead_pipeline_completed",
                table_id=table_id,
                run_id=record.run_id,
                lead_ref=lead_payload.lead_ref,
                blocked_reason=lead_payload.blocked_reason,
                phone_candidates=len(lead_payload.candidates),
                last_stage=lead_payload.last_stage,
            )

            record.next_index += 1
            for key, value in counters.items():
                record.summary[key] += value
            record.results.append(lead_payload.model_dump())
            if record.next_index >= len(record.lead_refs):
                record.status = "completed"
            else:
                record.status = "in_progress"

            _log_telegram_phone_endpoint(
                "next_completed",
                table_id=table_id,
                run_id=record.run_id,
                next_index=record.next_index,
                status=record.status,
                leads_with_phone=record.summary["leads_with_phone"],
                phones_persisted=record.summary["phones_persisted"],
                leads_blocked=record.summary["leads_blocked"],
            )
            return TelegramPhoneNextResponse(
                run_id=record.run_id,
                status=record.status,
                next_index=record.next_index,
                total_leads=len(record.lead_refs),
                summary=TelegramPhoneSummary(**record.summary),
                last_lead=lead_payload,
            )

    @app.post(
        "/lead-tables/{table_id}/telegram-phone/cancel",
        response_model=TelegramPhoneCancelResponse,
    )
    def telegram_phone_cancel(
        table_id: str, payload: TelegramPhoneCancelRequest
    ) -> TelegramPhoneCancelResponse:
        registry = get_telegram_phone_run_registry(app)
        record = registry.get(payload.run_id)
        if record is None or record.table_id != table_id:
            raise HTTPException(
                status_code=404, detail="Run de telefone Telegram não encontrado."
            )
        with record.lock:
            _log_telegram_phone_endpoint(
                "cancel_requested",
                table_id=table_id,
                run_id=record.run_id,
                status=record.status,
                next_index=record.next_index,
            )
            if record.status not in {"completed"}:
                record.status = "cancelled"
            _log_telegram_phone_endpoint(
                "cancel_completed",
                table_id=table_id,
                run_id=record.run_id,
                status=record.status,
                next_index=record.next_index,
            )
        return TelegramPhoneCancelResponse(
            run_id=record.run_id,
            status=record.status,
            next_index=record.next_index,
            total_leads=len(record.lead_refs),
        )

    @app.post(
        "/lead-tables/{table_id}/telegram-phone/extract-cpfs",
        response_model=TelegramPhoneExtractCpfsResponse,
    )
    def telegram_phone_extract_cpfs(
        table_id: str, payload: TelegramPhoneExtractCpfsRequest
    ) -> TelegramPhoneExtractCpfsResponse:
        """Etapa 1 do fluxo interativo de telefone.

        Roda gates + ``/nome`` + matcher para o lead atual do run e
        devolve os CPFs candidatos para a UI revisar. O servidor PARA
        antes de qualquer ``/cpf`` — quem decide é o operador clicando
        ``Buscar telefones`` (que chama ``/run-cpf-stage``) ou
        ``Pular este lead`` (que chama ``/skip-current-lead``).
        """
        registry = get_telegram_phone_run_registry(app)
        record = registry.get(payload.run_id)
        if record is None or record.table_id != table_id:
            raise HTTPException(
                status_code=404, detail="Run de telefone Telegram não encontrado."
            )

        with record.lock:
            _log_telegram_phone_endpoint(
                "extract_cpfs_started",
                table_id=table_id,
                run_id=record.run_id,
                status=record.status,
                next_index=record.next_index,
                total_leads=len(record.lead_refs),
            )
            if record.status == "cancelled":
                _log_telegram_phone_endpoint(
                    "extract_cpfs_rejected_cancelled",
                    table_id=table_id,
                    run_id=record.run_id,
                    next_index=record.next_index,
                )
                raise HTTPException(
                    status_code=409,
                    detail="Run cancelado — crie um run novo para continuar.",
                )
            if record.status == "completed" or record.next_index >= len(record.lead_refs):
                record.status = "completed"
                _log_telegram_phone_endpoint(
                    "extract_cpfs_already_completed",
                    table_id=table_id,
                    run_id=record.run_id,
                    next_index=record.next_index,
                )
                return TelegramPhoneExtractCpfsResponse(
                    run_id=record.run_id,
                    status="completed",
                    next_index=record.next_index,
                    total_leads=len(record.lead_refs),
                    lead_ref="",
                    lead_name=None,
                    blocked_reason=None,
                    eligible_cpfs=[],
                    candidates=[],
                    name_consult=None,
                )

            # Se já estamos awaiting_cpf_confirmation pro mesmo lead,
            # devolve o estado em cache sem rerodar /nome.
            target_ref = record.lead_refs[record.next_index]
            if (
                record.status == "awaiting_cpf_confirmation"
                and record.pending_lead_ref == target_ref
            ):
                _log_telegram_phone_endpoint(
                    "extract_cpfs_cache_hit",
                    table_id=table_id,
                    run_id=record.run_id,
                    next_index=record.next_index,
                    lead_ref=target_ref,
                )
                return _build_extract_cpfs_response_from_persisted(
                    app, record, table_id, target_ref
                )

            store = get_saved_leads_store(app)
            try:
                store.get_table(table_id)
                saved_leads = store.list_leads(table_id)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

            selected = _select_leads(saved_leads, [target_ref])
            if not selected:
                # Lead sumiu da tabela — avança o cursor.
                record.next_index += 1
                record.pending_lead_ref = None
                record.pending_eligible_cpfs = []
                missing = TelegramPhoneLeadResult(
                    lead_ref=target_ref,
                    lead_name=None,
                    blocked_reason="lead_not_found_in_table",
                )
                record.results.append(missing.model_dump())
                record.summary["leads_blocked"] += 1
                if record.next_index >= len(record.lead_refs):
                    record.status = "completed"
                else:
                    record.status = "in_progress"
                _log_telegram_phone_endpoint(
                    "extract_cpfs_lead_missing",
                    table_id=table_id,
                    run_id=record.run_id,
                    lead_ref=target_ref,
                    next_index=record.next_index,
                    status=record.status,
                )
                return TelegramPhoneExtractCpfsResponse(
                    run_id=record.run_id,
                    status=record.status,
                    next_index=record.next_index,
                    total_leads=len(record.lead_refs),
                    lead_ref=target_ref,
                    lead_name=None,
                    blocked_reason="lead_not_found_in_table",
                    eligible_cpfs=[],
                    candidates=[],
                    name_consult=None,
                )

            settings = load_settings()
            refreshed = _refresh_linkedin_signals_for_telegram(
                selected=selected,
                selected_refs=[target_ref],
                table_id=table_id,
                store=store,
                settings=settings,
            )

            try:
                name_driver = _default_gonzales_consult(settings)
            except RuntimeError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc

            lead = refreshed[0]
            stage1: TelegramNameStageResult = _pipeline_run_name_stage_only(
                lead=lead,
                table_id=table_id,
                store=store,
                name_consult_fn=name_driver.consult,
                target_titles=record.target_titles,
                run_id=record.run_id,
            )

            eligible_cpfs = [c.cpf for c in stage1.eligible_candidates]
            candidates_payload = _ranked_candidates_payload(stage1)
            _log_telegram_phone_endpoint(
                "extract_cpfs_name_stage_completed",
                table_id=table_id,
                run_id=record.run_id,
                lead_ref=stage1.lead_ref,
                blocked_reason=stage1.blocked_reason,
                candidates=len(stage1.all_candidates),
                eligible_cpfs=len(eligible_cpfs),
            )

            if stage1.blocked_reason or not eligible_cpfs:
                # Nada pra revisar — registra e avança o cursor sem
                # chamar /cpf nem aguardar confirmação humana.
                lead_payload = TelegramPhoneLeadResult(
                    lead_ref=stage1.lead_ref,
                    lead_name=stage1.lead_name,
                    blocked_reason=stage1.blocked_reason,
                    candidates=[],
                    name_consult=(
                        _consult_to_payload(stage1.name_consult)
                        if stage1.name_consult is not None
                        else None
                    ),
                    cpf_consult=None,
                )
                record.results.append(lead_payload.model_dump())
                if stage1.blocked_reason:
                    record.summary["leads_blocked"] += 1
                record.next_index += 1
                record.pending_lead_ref = None
                record.pending_eligible_cpfs = []
                if record.next_index >= len(record.lead_refs):
                    record.status = "completed"
                else:
                    record.status = "in_progress"
                _log_telegram_phone_endpoint(
                    "extract_cpfs_blocked_or_empty",
                    table_id=table_id,
                    run_id=record.run_id,
                    lead_ref=stage1.lead_ref,
                    blocked_reason=stage1.blocked_reason or "no_eligible_cpf",
                    next_index=record.next_index,
                    status=record.status,
                )
                return TelegramPhoneExtractCpfsResponse(
                    run_id=record.run_id,
                    status=record.status,
                    next_index=record.next_index,
                    total_leads=len(record.lead_refs),
                    lead_ref=stage1.lead_ref,
                    lead_name=stage1.lead_name,
                    blocked_reason=stage1.blocked_reason,
                    eligible_cpfs=[],
                    candidates=candidates_payload,
                    name_consult=(
                        _consult_to_payload(stage1.name_consult)
                        if stage1.name_consult is not None
                        else None
                    ),
                )

            # Estado pendente — aguardando confirmação humana.
            record.pending_lead_ref = stage1.lead_ref
            record.pending_eligible_cpfs = eligible_cpfs
            record.status = "awaiting_cpf_confirmation"
            _log_telegram_phone_endpoint(
                "extract_cpfs_awaiting_confirmation",
                table_id=table_id,
                run_id=record.run_id,
                lead_ref=stage1.lead_ref,
                eligible_cpfs=len(eligible_cpfs),
                candidates=len(candidates_payload),
            )
            return TelegramPhoneExtractCpfsResponse(
                run_id=record.run_id,
                status="awaiting_cpf_confirmation",
                next_index=record.next_index,
                total_leads=len(record.lead_refs),
                lead_ref=stage1.lead_ref,
                lead_name=stage1.lead_name,
                blocked_reason=None,
                eligible_cpfs=eligible_cpfs,
                candidates=candidates_payload,
                name_consult=(
                    _consult_to_payload(stage1.name_consult)
                    if stage1.name_consult is not None
                    else None
                ),
            )

    @app.post(
        "/lead-tables/{table_id}/telegram-phone/run-cpf-stage",
        response_model=TelegramPhoneNextResponse,
    )
    def telegram_phone_run_cpf_stage(
        table_id: str, payload: TelegramPhoneRunCpfStageRequest
    ) -> TelegramPhoneNextResponse:
        """Etapa 2 do fluxo interativo: roda ``/cpf`` SÓ nos CPFs que o
        operador confirmou na UI. O cursor avança e o estado volta para
        ``in_progress`` (ou ``completed`` se era o último lead)."""
        registry = get_telegram_phone_run_registry(app)
        record = registry.get(payload.run_id)
        if record is None or record.table_id != table_id:
            raise HTTPException(
                status_code=404, detail="Run de telefone Telegram não encontrado."
            )

        with record.lock:
            _log_telegram_phone_endpoint(
                "run_cpf_stage_started",
                table_id=table_id,
                run_id=record.run_id,
                status=record.status,
                next_index=record.next_index,
                requested_cpfs=len(payload.cpfs),
            )
            if record.status == "cancelled":
                _log_telegram_phone_endpoint(
                    "run_cpf_stage_rejected_cancelled",
                    table_id=table_id,
                    run_id=record.run_id,
                    next_index=record.next_index,
                )
                raise HTTPException(
                    status_code=409,
                    detail="Run cancelado — crie um run novo para continuar.",
                )
            if record.status != "awaiting_cpf_confirmation":
                _log_telegram_phone_endpoint(
                    "run_cpf_stage_rejected_wrong_status",
                    table_id=table_id,
                    run_id=record.run_id,
                    status=record.status,
                    next_index=record.next_index,
                )
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Etapa /cpf só pode ser disparada depois de"
                        " /extract-cpfs ter retornado candidatos."
                    ),
                )

            target_ref = record.pending_lead_ref or ""
            if (
                not target_ref
                or record.next_index >= len(record.lead_refs)
                or record.lead_refs[record.next_index] != target_ref
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Estado inconsistente do run — o lead pendente não"
                        " bate com o cursor atual."
                    ),
                )

            # Validar que cada CPF está na lista pré-aprovada pelo
            # matcher (defesa contra payload alterado pelo cliente).
            allowed = set(record.pending_eligible_cpfs)
            chosen = [cpf for cpf in payload.cpfs if cpf in allowed]
            if not chosen:
                _log_telegram_phone_endpoint(
                    "run_cpf_stage_rejected_unknown_cpfs",
                    table_id=table_id,
                    run_id=record.run_id,
                    requested_cpfs=len(payload.cpfs),
                    allowed_cpfs=len(allowed),
                )
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "Nenhum CPF da lista informada bate com os"
                        " candidatos persistidos pelo /nome desse lead."
                    ),
                )

            store = get_saved_leads_store(app)
            try:
                store.get_table(table_id)
                saved_leads = store.list_leads(table_id)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

            selected = _select_leads(saved_leads, [target_ref])
            if not selected:
                raise HTTPException(
                    status_code=404,
                    detail="Lead atual não encontrado mais na tabela.",
                )
            lead = selected[0]

            settings = load_settings()
            try:
                cpf_driver = _default_gonzales_cpf_consult(settings)
            except RuntimeError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc

            stage2: TelegramCpfStageResult = _pipeline_run_cpf_stage_only(
                lead=lead,
                table_id=table_id,
                store=store,
                cpf_consult_fn=cpf_driver.consult,
                selected_cpfs=chosen,
                run_id=record.run_id,
            )
            _log_telegram_phone_endpoint(
                "run_cpf_stage_pipeline_completed",
                table_id=table_id,
                run_id=record.run_id,
                lead_ref=stage2.lead_ref,
                selected_cpfs=len(chosen),
                phone_candidates=len(stage2.phone_candidates),
                blocked_reason=stage2.blocked_reason,
            )

            # Aplica os updates de telefone (mesma lógica do batch).
            counters = {
                "leads_with_phone": 0,
                "leads_blocked": 0,
                "phones_persisted": 0,
                "skipped_existing_phone": 0,
            }
            if stage2.blocked_reason:
                counters["leads_blocked"] += 1
            had_new_phone = False
            if stage2.phone_candidates:
                ordered = sorted(
                    stage2.phone_candidates,
                    key=lambda c: c.confidence,
                    reverse=True,
                )
                primary_update = _telegram_phone_candidate_to_update(ordered[0])
                primary_counters = store.apply_internal_phone_enrichment_updates(
                    table_id, [(lead, primary_update)]
                )
                if primary_counters.get("enriched", 0) > 0:
                    counters["phones_persisted"] += primary_counters["enriched"]
                    had_new_phone = True
                counters["skipped_existing_phone"] += primary_counters.get(
                    "skipped_existing_phone", 0
                )
                for extra in ordered[1:]:
                    extras_counter = store.apply_internal_phone_enrichment_updates(
                        table_id,
                        [(lead, _telegram_phone_candidate_to_update(extra))],
                    )
                    counters["phones_persisted"] += extras_counter.get("enriched", 0)
                    counters["skipped_existing_phone"] += extras_counter.get(
                        "skipped_existing_phone", 0
                    )
                _apply_telegram_contact_details(
                    store=store,
                    table_id=table_id,
                    lead=lead,
                    candidates=ordered,
                )
            if had_new_phone:
                counters["leads_with_phone"] += 1

            # Recuperar o name_consult persistido pra payload completo.
            name_consult = latest_name_stage_consult(
                store=store, table_id=table_id, lead_ref=target_ref
            )
            lead_payload = TelegramPhoneLeadResult(
                lead_ref=stage2.lead_ref,
                lead_name=stage2.lead_name,
                blocked_reason=stage2.blocked_reason,
                candidates=[
                    _telegram_phone_candidate_to_payload(c)
                    for c in stage2.phone_candidates
                ],
                name_consult=(
                    _consult_to_payload(name_consult)
                    if name_consult is not None
                    else None
                ),
                cpf_consult=(
                    _consult_to_payload(stage2.cpf_consult)
                    if stage2.cpf_consult is not None
                    else None
                ),
            )

            record.next_index += 1
            for key, value in counters.items():
                record.summary[key] += value
            record.results.append(lead_payload.model_dump())
            record.pending_lead_ref = None
            record.pending_eligible_cpfs = []
            if record.next_index >= len(record.lead_refs):
                record.status = "completed"
            else:
                record.status = "in_progress"

            _log_telegram_phone_endpoint(
                "run_cpf_stage_completed",
                table_id=table_id,
                run_id=record.run_id,
                lead_ref=lead_payload.lead_ref,
                next_index=record.next_index,
                status=record.status,
                phone_candidates=len(lead_payload.candidates),
                phones_persisted=counters["phones_persisted"],
                skipped_existing_phone=counters["skipped_existing_phone"],
                leads_with_phone=counters["leads_with_phone"],
                leads_blocked=counters["leads_blocked"],
            )
            return TelegramPhoneNextResponse(
                run_id=record.run_id,
                status=record.status,
                next_index=record.next_index,
                total_leads=len(record.lead_refs),
                summary=TelegramPhoneSummary(**record.summary),
                last_lead=lead_payload,
            )

    @app.post(
        "/lead-tables/{table_id}/telegram-phone/skip-current-lead",
        response_model=TelegramPhoneNextResponse,
    )
    def telegram_phone_skip_current_lead(
        table_id: str, payload: TelegramPhoneSkipLeadRequest
    ) -> TelegramPhoneNextResponse:
        """Pula o lead atual sem rodar /cpf — usado quando o operador
        olha a lista de CPFs candidatos e decide que nenhum vale a
        consulta."""
        registry = get_telegram_phone_run_registry(app)
        record = registry.get(payload.run_id)
        if record is None or record.table_id != table_id:
            raise HTTPException(
                status_code=404, detail="Run de telefone Telegram não encontrado."
            )

        with record.lock:
            _log_telegram_phone_endpoint(
                "skip_current_started",
                table_id=table_id,
                run_id=record.run_id,
                status=record.status,
                next_index=record.next_index,
            )
            if record.status == "cancelled":
                _log_telegram_phone_endpoint(
                    "skip_current_rejected_cancelled",
                    table_id=table_id,
                    run_id=record.run_id,
                    next_index=record.next_index,
                )
                raise HTTPException(
                    status_code=409,
                    detail="Run cancelado — crie um run novo para continuar.",
                )
            if record.status == "completed":
                _log_telegram_phone_endpoint(
                    "skip_current_already_completed",
                    table_id=table_id,
                    run_id=record.run_id,
                    next_index=record.next_index,
                )
                return TelegramPhoneNextResponse(
                    run_id=record.run_id,
                    status="completed",
                    next_index=record.next_index,
                    total_leads=len(record.lead_refs),
                    summary=TelegramPhoneSummary(**record.summary),
                    last_lead=None,
                )

            target_ref = (
                record.pending_lead_ref
                if record.pending_lead_ref
                else (
                    record.lead_refs[record.next_index]
                    if record.next_index < len(record.lead_refs)
                    else ""
                )
            )
            skipped = TelegramPhoneLeadResult(
                lead_ref=target_ref,
                lead_name=None,
                blocked_reason="skipped_by_operator",
            )
            record.results.append(skipped.model_dump())
            record.next_index += 1
            record.summary["leads_blocked"] += 1
            record.pending_lead_ref = None
            record.pending_eligible_cpfs = []
            if record.next_index >= len(record.lead_refs):
                record.status = "completed"
            else:
                record.status = "in_progress"

            _log_telegram_phone_endpoint(
                "skip_current_completed",
                table_id=table_id,
                run_id=record.run_id,
                lead_ref=target_ref,
                next_index=record.next_index,
                status=record.status,
            )
            return TelegramPhoneNextResponse(
                run_id=record.run_id,
                status=record.status,
                next_index=record.next_index,
                total_leads=len(record.lead_refs),
                summary=TelegramPhoneSummary(**record.summary),
                last_lead=skipped,
            )

    @app.get(
        "/lead-tables/{table_id}/telegram-consults",
        response_model=TelegramConsultListResponse,
    )
    def list_telegram_consults(table_id: str) -> TelegramConsultListResponse:
        store = get_saved_leads_store(app)
        try:
            store.get_table(table_id)
            saved_leads = store.list_leads(table_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        consults = store.list_telegram_consults(table_id)
        lead_by_ref = {_lead_ref(lead): lead for lead in saved_leads if _lead_ref(lead)}
        return TelegramConsultListResponse(
            consults=[
                _consult_to_payload(c, lead=lead_by_ref.get(c.lead_ref))
                for c in consults
            ]
        )

    @app.post("/lead-tables/{table_id}/internal-enrich/stream")
    def internal_enrich_stream(
        table_id: str, payload: InternalEnrichRequest
    ) -> StreamingResponse:
        """Same work as the blocking endpoint, but streams progress events
        as Server-Sent Events. The UI consumes this with ``EventSource``
        and renders per-lead updates live.

        The work runs on a background thread; the response generator pulls
        events off a thread-safe queue and writes them as SSE frames. The
        final frame is ``{"type": "done", ...}`` with the full summary so
        the UI can refresh its lead list without an extra fetch.
        """
        store = get_saved_leads_store(app)
        try:
            table = store.get_table(table_id)
            saved_leads = store.list_leads(table_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        selected = (
            _select_leads(saved_leads, payload.lead_refs)
            if payload.lead_refs
            else list(saved_leads)
        )
        if not selected:
            raise HTTPException(
                status_code=422,
                detail="Nenhum lead encontrado para enriquecimento interno.",
            )
        selected = _apply_internal_domain_fallback(
            selected,
            payload.company_domain
            or _clean_internal_company_domain(table.search_request.get("company_domain")),
        )

        existing_company_emails = [
            (lead.person_name or "", lead.email or "")
            for lead in saved_leads
            if lead.email and lead.person_name
        ]
        company_domains = collect_company_domains(saved_leads)
        # Count every domain we may attempt, not just lead.company_domain.
        # This is the number the UI shows in the "domínios" progress bar.
        attempted_domains: set[str] = set()
        for lead in selected:
            own = (lead.company_domain or "").strip().lower()
            if own:
                attempted_domains.add(own)
            for domain in company_domains.values():
                for entry in domain:
                    attempted_domains.add(entry)
        unique_domain_total = len(attempted_domains)

        events_queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        cancel_event = threading.Event()

        def push(event: dict[str, Any]) -> None:
            events_queue.put(event)

        def worker() -> None:
            try:
                push(
                    {
                        "type": "start",
                        "total": len(selected),
                        "unique_domains": unique_domain_total,
                        "fields": payload.fields,
                    }
                )
                email_counters = {
                    "enriched": 0,
                    "skipped_existing_email": 0,
                    "failed_missing_domain": 0,
                    "no_change": 0,
                }
                phone_counters = {
                    "enriched": 0,
                    "skipped_existing_phone": 0,
                    "failed_no_candidate": 0,
                    "no_change": 0,
                }
                if payload.fields in {"email", "both"}:
                    updates = _run_internal_enrichment(
                        leads=selected,
                        existing_company_emails=existing_company_emails,
                        company_domains=company_domains,
                        on_event=push,
                        cancel_check=cancel_event.is_set,
                    )
                    email_counters = store.apply_internal_enrichment_updates(
                        table_id, updates
                    )
                if payload.fields in {"phone", "both"} and not cancel_event.is_set():
                    phone_domains = collect_company_domains_from_leads(saved_leads)
                    for key, domains in company_domains.items():
                        bucket = phone_domains.setdefault(key, [])
                        for domain in domains:
                            if domain and domain not in bucket:
                                bucket.append(domain)
                    # Wrap phone events so the UI can demultiplex when
                    # ``fields="both"``: the e-mail and phone runs would
                    # otherwise both emit ``{"type": "phase"}`` and the
                    # consumer can't tell which one is reporting.
                    def push_phone(event: dict[str, Any]) -> None:
                        push({**event, "channel": "phone"})

                    phone_updates = _run_internal_phone_enrichment(
                        leads=selected,
                        company_domains=phone_domains,
                        on_event=push_phone,
                        cancel_check=cancel_event.is_set,
                        phone_sources=payload.phone_sources,
                    )
                    phone_counters = store.apply_internal_phone_enrichment_updates(
                        table_id, phone_updates
                    )
                refreshed = store.get_table(table_id)
                fresh_leads = store.list_leads(table_id)
                push(
                    {
                        "type": "done",
                        "summary": {
                            "requested_leads": len(selected),
                            "enriched_leads": email_counters["enriched"],
                            "skipped_existing_email": email_counters[
                                "skipped_existing_email"
                            ],
                            "failed_missing_domain": email_counters[
                                "failed_missing_domain"
                            ],
                            "no_change": email_counters["no_change"],
                            "enriched_phone_leads": phone_counters["enriched"],
                            "skipped_existing_phone": phone_counters[
                                "skipped_existing_phone"
                            ],
                            "failed_no_phone_candidate": phone_counters[
                                "failed_no_candidate"
                            ],
                        },
                        "table": _table_to_payload(refreshed).model_dump(),
                        "leads": [lead.model_dump(mode="json") for lead in fresh_leads],
                    }
                )
            except Exception as exc:
                logger.exception("internal enrichment stream failed")
                push({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            finally:
                events_queue.put(None)  # sentinel

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

        def event_stream():
            # Heartbeat helps Electron / nginx-style proxies keep the
            # connection open during longer harvest phases.
            last_beat = time.monotonic()
            while True:
                try:
                    event = events_queue.get(timeout=10.0)
                except queue.Empty:
                    if time.monotonic() - last_beat > 9.0:
                        yield ": heartbeat\n\n"
                        last_beat = time.monotonic()
                    continue
                if event is None:
                    return
                last_beat = time.monotonic()
                yield f"data: {json.dumps(event)}\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    @app.post(
        "/lead-tables/merge",
        response_model=SavedLeadTableDetail,
        status_code=status.HTTP_201_CREATED,
    )
    def merge_lead_tables(payload: MergeLeadTablesRequest) -> SavedLeadTableDetail:
        store = get_saved_leads_store(app)
        try:
            table = store.merge_tables(
                name=payload.name,
                table_ids=payload.table_ids,
                keywords=payload.keywords,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        leads = store.list_leads(table.id)
        return SavedLeadTableDetail(table=_table_to_payload(table), leads=leads)

    @app.post(
        "/lead-tables/{table_id}/experimental-search",
        response_model=ExperimentalSearchResponse,
    )
    def experimental_search_endpoint(table_id: str) -> ExperimentalSearchResponse:
        store = get_saved_leads_store(app)
        try:
            table = store.get_table(table_id)
            existing = store.list_leads(table_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        settings = load_settings()
        engines = _resolve_free_engines(settings)

        request_blob = table.search_request or {}
        company_name = (
            str(request_blob.get("company_name") or "").strip() or table.name
        )
        company_domain = request_blob.get("company_domain") or None
        linkedin_url = request_blob.get("linkedin_url") or None
        keywords = list(table.keywords) or [
            str(t) for t in (request_blob.get("titles") or [])
        ]

        summary = run_experimental_search(
            engines=engines,
            company_name=company_name,
            company_domain=company_domain if isinstance(company_domain, str) else None,
            keywords=[kw for kw in keywords if kw],
            existing_leads=existing,
            linkedin_url=linkedin_url if isinstance(linkedin_url, str) else None,
        )
        if summary.new_leads:
            store.add_leads(table_id, summary.new_leads)
        return _experimental_response(summary)

    @app.delete("/lead-tables/{table_id}")
    def delete_lead_table(table_id: str) -> dict[str, bool]:
        store = get_saved_leads_store(app)
        try:
            store.delete_table(table_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True}

    @app.delete("/runs/{run_id}", response_model=RunStateResponse)
    def cancel_run(run_id: str) -> RunStateResponse:
        record = get_run_registry(app).get(run_id)
        if record is None:
            raise HTTPException(status_code=404, detail="run não encontrado")
        record.cancel_event.set()
        if record.status in {RunStatus.PENDING, RunStatus.RUNNING}:
            record.status = RunStatus.CANCELLED
            get_run_registry(app).update(record)
        return RunStateResponse(
            run_id=record.run_id,
            status=record.status,
            error=record.error,
            result=record.result,
        )

    return app


def _run_playwright_diagnostic(endpoint: str) -> PlaywrightDiagnosticResponse:
    """Stepwise self-test that reproduces the people_search startup path.

    The runtime cost is bounded (~5s worst case) because each step has its own
    timeout. We never navigate to LinkedIn here — the goal is to localize a
    Playwright/CDP setup failure, not to scrape anything. The result feeds the
    "Diagnosticar Playwright" button so the operator sees exactly which step
    times out on their machine.
    """
    import time

    steps: list[PlaywrightDiagnosticStep] = []
    overall_ok = True
    playwright_version: str | None = None
    log_path: str | None = None

    def record(name: str, ok: bool, elapsed_ms: int, detail: str | None) -> None:
        nonlocal overall_ok
        if not ok:
            overall_ok = False
        steps.append(
            PlaywrightDiagnosticStep(
                name=name, ok=ok, elapsed_ms=elapsed_ms, detail=detail
            )
        )

    try:
        from beautiful_linkedin.server.sidecar import _default_log_path

        log_path = str(_default_log_path())
    except Exception as exc:  # pragma: no cover - defensive
        log_path = f"erro ao resolver caminho do log: {exc}"

    # Step 1: CDP probe
    start = time.perf_counter()
    try:
        alive = probe_cdp_endpoint(endpoint, timeout=2.0)
        elapsed = int((time.perf_counter() - start) * 1000)
        record(
            "cdp_probe",
            alive,
            elapsed,
            f"{endpoint}/json/version respondeu" if alive
            else f"{endpoint}/json/version não respondeu — Chromium embutido pode estar fechado",
        )
        if not alive:
            return PlaywrightDiagnosticResponse(
                overall_ok=False,
                cdp_endpoint=endpoint,
                sidecar_version=VERSION,
                playwright_version=playwright_version,
                log_path=log_path,
                steps=steps,
            )
    except Exception as exc:
        elapsed = int((time.perf_counter() - start) * 1000)
        record("cdp_probe", False, elapsed, f"exceção: {exc}")
        return PlaywrightDiagnosticResponse(
            overall_ok=False,
            cdp_endpoint=endpoint,
            sidecar_version=VERSION,
            playwright_version=playwright_version,
            log_path=log_path,
            steps=steps,
        )

    # Step 2: import playwright
    start = time.perf_counter()
    try:
        import playwright  # noqa: F401
        from playwright.sync_api import sync_playwright
        try:
            playwright_version = getattr(playwright, "__version__", None) or "desconhecida"
        except Exception:
            playwright_version = "desconhecida"
        elapsed = int((time.perf_counter() - start) * 1000)
        record(
            "import_playwright",
            True,
            elapsed,
            f"playwright {playwright_version} importado",
        )
    except Exception as exc:
        elapsed = int((time.perf_counter() - start) * 1000)
        record(
            "import_playwright",
            False,
            elapsed,
            f"falha: {exc} (sidecar empacotado sem playwright?)",
        )
        return PlaywrightDiagnosticResponse(
            overall_ok=False,
            cdp_endpoint=endpoint,
            sidecar_version=VERSION,
            playwright_version=playwright_version,
            log_path=log_path,
            steps=steps,
        )

    # Step 3: sync_playwright().start() — spawns the bundled node driver
    runtime = None
    start = time.perf_counter()
    try:
        runtime = sync_playwright().start()
        elapsed = int((time.perf_counter() - start) * 1000)
        record(
            "playwright_start",
            True,
            elapsed,
            "driver Node.js inicializado",
        )
    except Exception as exc:
        elapsed = int((time.perf_counter() - start) * 1000)
        record(
            "playwright_start",
            False,
            elapsed,
            f"falha ao iniciar driver: {exc}",
        )
        return PlaywrightDiagnosticResponse(
            overall_ok=False,
            cdp_endpoint=endpoint,
            sidecar_version=VERSION,
            playwright_version=playwright_version,
            log_path=log_path,
            steps=steps,
        )

    browser = None
    try:
        # Step 4: connect_over_cdp
        start = time.perf_counter()
        try:
            browser = runtime.chromium.connect_over_cdp(endpoint, timeout=8000)
            elapsed = int((time.perf_counter() - start) * 1000)
            record(
                "connect_over_cdp",
                True,
                elapsed,
                f"conectado ao Chromium em {endpoint}",
            )
        except Exception as exc:
            elapsed = int((time.perf_counter() - start) * 1000)
            record(
                "connect_over_cdp",
                False,
                elapsed,
                f"falha: {exc}",
            )
            return PlaywrightDiagnosticResponse(
                overall_ok=False,
                cdp_endpoint=endpoint,
                sidecar_version=VERSION,
                playwright_version=playwright_version,
                log_path=log_path,
                steps=steps,
            )

        # Step 5: contexts/pages enumeration
        start = time.perf_counter()
        try:
            contexts = list(getattr(browser, "contexts", []) or [])
            total_pages = 0
            linkedin_pages = 0
            for ctx in contexts:
                for page in getattr(ctx, "pages", []) or []:
                    total_pages += 1
                    url = getattr(page, "url", "") or ""
                    if "linkedin.com" in url.lower():
                        linkedin_pages += 1
            elapsed = int((time.perf_counter() - start) * 1000)
            record(
                "list_pages",
                True,
                elapsed,
                f"{len(contexts)} contexto(s), {total_pages} página(s), "
                f"{linkedin_pages} no linkedin.com",
            )
        except Exception as exc:
            elapsed = int((time.perf_counter() - start) * 1000)
            record(
                "list_pages",
                False,
                elapsed,
                f"falha: {exc}",
            )
    finally:
        try:
            if browser is not None:
                browser.close()
        except Exception:  # pragma: no cover - best effort
            pass
        try:
            runtime.stop()
        except Exception:  # pragma: no cover - best effort
            pass

    return PlaywrightDiagnosticResponse(
        overall_ok=overall_ok,
        cdp_endpoint=endpoint,
        sidecar_version=VERSION,
        playwright_version=playwright_version,
        log_path=log_path,
        steps=steps,
    )


def _global_dedupe_keys_for_search(app: FastAPI) -> set[str]:
    """Identity keys of every already-saved lead, for cross-table dedup.

    Best-effort: a store hiccup must not block a search, so failures degrade
    to "no exclusion" instead of raising.
    """
    try:
        return get_saved_leads_store(app).global_dedupe_keys()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Falha ao carregar histórico para dedup global: %s", exc)
        return set()


def _execute(
    request: SearchRequest,
    *,
    exclude_lead_keys: set[str] | None = None,
    on_lead_found: Callable[[Lead], None] | None = None,
) -> ProspectingResult:
    company = CompanyInput(
        company_name=request.company_name,
        company_domain=request.company_domain,
        linkedin_url=request.linkedin_url,
        titles=request.titles,
    )
    options = apply_scrape_mode(
        scrape_mode=request.scrape_mode,
        lead_providers=request.lead_providers,
        official_sites=request.official_sites,
        web_query_limit=request.web_query_limit,
        parallelism=request.parallelism,
    )
    lead_filter = _coerce_filter(request.filters)
    settings = _settings_with_api_key_overrides(request.api_keys)
    if request.cards_per_cycle is not None:
        from dataclasses import replace as _dc_replace

        settings = _dc_replace(settings, linkedin_cards_per_cycle=request.cards_per_cycle)
    if request.cdp_endpoint is not None:
        from dataclasses import replace as _dc_replace  # noqa: F811 — same symbol, re-import guard

        settings = _dc_replace(
            settings,
            linkedin_cdp_endpoint=request.cdp_endpoint,
            linkedin_cdp_enabled=True,
        )

    # The provider timeout has to scale with how much work the people_search
    # provider is going to do — each "Exibir mais resultados" click takes a
    # few seconds with human-like pacing, and we may do dozens of them. The
    # 120s default chokes any non-trivial run.
    provider_timeout = request.provider_timeout_seconds
    if options.scrape_mode == "people_search":
        cards_per_cycle = (
            request.cards_per_cycle
            if request.cards_per_cycle is not None
            else settings.linkedin_cards_per_cycle
        )
        provider_timeout = _people_search_timeout(
            max_results=request.max_results,
            cards_per_cycle=cards_per_cycle,
            user_supplied=request.provider_timeout_seconds,
        )
    return run_prospecting(
        companies=[company],
        max_results=request.max_results,
        official_sites=options.official_sites,
        include_uncertain=request.include_uncertain,
        output_path=request.output_path,
        output_format=request.output_format,
        search_engines=request.search_engines,
        search_depth=request.search_depth,
        lead_providers=options.lead_providers,
        parallelism=options.parallelism,
        web_query_limit=options.web_query_limit,
        provider_timeout_seconds=provider_timeout,
        use_cache=request.use_cache,
        linkedin_cookie=request.linkedin_cookie,
        linkedin_cookie_browser=request.linkedin_cookie_browser,
        lead_filter=lead_filter,
        playwright_headless=request.playwright_headless,
        settings=settings,
        exclude_lead_keys=exclude_lead_keys,
        on_lead_found=on_lead_found,
    )


def _execute_in_background(app: FastAPI, record: RunRecord, request: SearchRequest) -> None:
    record.status = RunStatus.RUNNING
    get_run_registry(app).update(record)
    try:
        exclude_keys = _global_dedupe_keys_for_search(app)
        result = _execute(
            request,
            exclude_lead_keys=exclude_keys,
            on_lead_found=record.record_found_lead,
        )
        if record.cancel_event.is_set():
            record.status = RunStatus.CANCELLED
        else:
            record.status = RunStatus.COMPLETED
            record.result = SearchResponse(
                leads=result.leads,
                summary=result.summary,
                provider_diagnostics=result.provider_diagnostics,
            )
    except Exception as exc:
        record.status = RunStatus.FAILED
        record.error = f"{type(exc).__name__}: {exc}"
    finally:
        get_run_registry(app).update(record)


def _ensure_risky_mode_consent(request: SearchRequest) -> None:
    risky_providers = {"linkedin_playwright", "playwright", "browser", "navegador"}
    is_risky_mode = request.scrape_mode.strip().lower() == "browser"
    is_risky_provider = any(
        provider.strip().lower() in risky_providers for provider in request.lead_providers
    )
    if (is_risky_mode or is_risky_provider) and not request.accept_risk:
        raise HTTPException(
            status_code=412,
            detail=(
                "Modo ARRISCADO (Playwright logado) requer accept_risk=true. "
                "A conta do LinkedIn pode ser bloqueada."
            ),
        )


def _people_search_timeout(
    *,
    max_results: int,
    cards_per_cycle: int,
    user_supplied: float,
) -> float:
    """Return a sensible provider timeout for the people_search mode.

    Each "Exibir mais resultados" click is paced with randomized human-like
    delays (~5s typical, 8s upper bound) plus a wait for new cards to render.
    On top of that we have initial navigation, login probing and final
    extraction (~30s budget). A flat 120s ceiling cuts off any run targeting
    more than ~30 leads — this helper scales the ceiling to the actual work.
    """
    import math

    clicks = max(1, math.ceil(max(1, max_results) / max(1, cards_per_cycle)) + 1)
    base_overhead = 30.0
    per_click = 8.0
    derived = base_overhead + clicks * per_click
    # If the caller passed something noticeably larger than the default, trust
    # them. Otherwise raise the floor to the derived value, capped at 15 min.
    floor = max(derived, 120.0)
    upper_cap = 900.0
    if user_supplied > 120.0:
        return min(max(user_supplied, floor), upper_cap)
    return min(floor, upper_cap)


def _resolve_free_engines(settings: Settings) -> dict[str, SearchEngine]:
    """Return only free/configured engines usable for the experimental flow.

    Paid providers (Serper, Google CSE w/ key) are intentionally excluded —
    the action is meant to be opportunistic and cost-free.
    """
    engines: dict[str, SearchEngine] = {}
    if settings.searxng_base_url:
        engines["searxng"] = SearxngSearchEngine(base_url=settings.searxng_base_url)
    # DuckDuckGo HTML is free; ddg-search Python library wraps it.
    try:
        engines["duckduckgo"] = DuckDuckGoSearchEngine()
    except Exception:
        pass
    return engines


def _experimental_response(
    summary: ExperimentalSearchSummary,
) -> ExperimentalSearchResponse:
    return ExperimentalSearchResponse(
        new_leads=list(summary.new_leads),
        duplicates_skipped=summary.duplicates_skipped,
        candidates_total=summary.candidates_total,
        engines_used=list(summary.engines_used),
        note=summary.note,
    )


def _default_company_email_harvester() -> CompanyEmailHarvester:
    """Build the production harvester. Tests monkeypatch this helper to
    return a no-op harvester so they stay offline."""
    return CompanyEmailHarvester()


def _default_domain_discoverer() -> Any:
    """Production wiring for the free-source domain discoverer.

    Composes crt.sh (HTTP) + SPF/DMARC (DNS TXT) + ccTLD-variant
    generator, gated by the real MX resolver. Tests monkeypatch this
    helper to return ``None`` so discovery is a no-op offline.
    """
    from beautiful_linkedin.storage.domain_discovery import (
        CctldVariantGenerator,
        CrtShClient,
        DomainDiscoveryService,
        SpfDmarcDiscoverer,
    )

    import httpx

    def http_fetcher(url: str) -> tuple[int, str]:
        try:
            response = httpx.get(
                url,
                timeout=5.0,
                headers={"User-Agent": "beautiful-linkedin/discovery"},
                follow_redirects=True,
            )
            return response.status_code, response.text
        except Exception:
            return 0, ""

    txt_resolver = _default_txt_resolver()
    mx_resolver = _default_mx_resolver()

    return DomainDiscoveryService(
        crt_sh_client=CrtShClient(http_fetcher=http_fetcher),
        spf_dmarc=SpfDmarcDiscoverer(txt_resolver=txt_resolver),
        cctld_generator=CctldVariantGenerator(),
        mx_checker=mx_resolver,
    )


def _default_txt_resolver() -> Callable[[str], list[str]]:
    """Resolve TXT records using ``dnspython`` when available.

    Falls back to a no-op when dnspython isn't installed — SPF/DMARC
    discovery simply yields nothing in that case (other sources still
    work). Same lazy-import pattern as the MX resolver so tests can
    monkeypatch this helper.
    """
    try:
        import dns.resolver  # type: ignore[import-not-found]
    except Exception:
        return lambda _domain: []

    def _resolve(domain: str) -> list[str]:
        cleaned = (domain or "").strip().lower()
        if not cleaned:
            return []
        try:
            answers = dns.resolver.resolve(cleaned, "TXT", lifetime=3.0)
        except Exception:
            return []
        out: list[str] = []
        for answer in answers:
            # dnspython yields strings as TXT chunks; join into a single
            # record before returning.
            chunks = getattr(answer, "strings", None) or []
            joined = "".join(
                chunk.decode("utf-8", "ignore") if isinstance(chunk, bytes) else str(chunk)
                for chunk in chunks
            )
            if joined:
                out.append(joined)
        return out

    return _resolve


def _build_internal_orchestrator(
    *,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> InternalEnrichmentOrchestrator:
    """Build an orchestrator wired with the production harvester / DNS /
    SMTP defaults. Tests monkeypatch ``_default_*`` factories to stay
    offline, so this helper always re-resolves them lazily."""

    harvester = _default_company_email_harvester()

    def harvest(domain: str) -> list[str]:
        try:
            results = harvester.harvest(domain)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("harvester %s falhou: %s", domain, exc)
            return []
        return [r.email.strip().lower() for r in results if "@" in r.email]

    service = InternalLeadEnrichmentService(
        validator=EmailValidator(
            mx_resolver=_default_mx_resolver(),
            mailbox_verifier=_default_mailbox_verifier(),
        ),
    )
    return InternalEnrichmentOrchestrator(
        service=service,
        harvest_fn=harvest,
        discoverer=_default_domain_discoverer(),
        on_event=on_event,
        cancel_check=cancel_check,
    )


def _run_linkedin_profile_validation(
    *,
    leads: list[Lead],
    settings: Settings,
    max_leads: int,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> list[Any]:
    """Run profile validation with the same browser order as people_search.

    Preferred path is CDP-connected Chrome (same logged-in browser, no cookie
    injection). If CDP is offline, fall back to a dedicated Playwright session
    with ``li_at`` just like ``linkedin_people_search`` does.
    """
    options = PeopleSearchOptions(
        min_delay_seconds=2.0,
        max_delay_seconds=4.5,
        headless=True,
        cdp_endpoint=settings.linkedin_cdp_endpoint,
        cdp_enabled=settings.linkedin_cdp_enabled,
    )
    endpoint = settings.linkedin_cdp_endpoint
    fetcher_cm: Any | None = None
    if settings.linkedin_cdp_enabled and endpoint and probe_cdp_endpoint(endpoint):
        fetcher_cm = CdpLinkedInProfilePageFetcher(
            endpoint=endpoint,
            min_delay_seconds=options.min_delay_seconds,
            max_delay_seconds=options.max_delay_seconds,
        )
    else:
        fetcher_cm = _build_profile_validation_cookie_fetcher(settings)
    if fetcher_cm is None:
        raise RuntimeError(
            "Chrome CDP offline e nenhum li_at válido disponível para fallback. "
            "Abra o Chrome com --remote-debugging-port=9222 ou configure um li_at fresco."
        )

    with fetcher_cm as fetcher:
        return run_profile_validation_with_fetcher(
            leads=leads,
            page_fetcher=fetcher,
            max_leads=max_leads,
            on_event=on_event,
            cancel_check=cancel_check,
        )


def _build_profile_validation_cookie_fetcher(
    settings: Settings,
) -> PlaywrightLinkedInProfilePageFetcher | None:
    li_at = resolve_linkedin_li_at_cookie(
        settings.linkedin_li_at_cookie,
        browser=settings.linkedin_cookie_browser,
    ) or ""
    if not li_at:
        return None
    if not looks_like_valid_li_at(li_at):
        raise RuntimeError(
            "li_at recuperado não tem formato esperado. Cole um li_at fresco "
            "ou use o Chrome com --remote-debugging-port=9222."
        )
    return PlaywrightLinkedInProfilePageFetcher(
        li_at=li_at,
        headless=True,
        min_delay_seconds=2.0,
        max_delay_seconds=4.5,
    )


class _NoopPhoneHarvester:
    def harvest(self, domain: str | None) -> list[Any]:  # noqa: ARG002
        return []


def _default_phone_harvester() -> _NoopPhoneHarvester:
    """Production phone source for now: Telegram-only lookup.

    The site/SERP phone discovery code remains available for isolated
    tests and future controlled experiments, but the app's "Achar
    telefones" action should not scrape company sites or SERPs while
    the Telegram bot is the only trusted source.
    """
    return _NoopPhoneHarvester()


def _default_phone_validator() -> PhoneValidator:
    """Production validator. Pure-Python, offline; the only reason for
    a factory is symmetry with the other ``_default_*`` helpers and to
    let tests override the default region in non-BR scenarios."""
    return PhoneValidator()


def _default_phone_lookup_providers(settings: Settings) -> list[PhoneLookupProvider]:
    """Build the list of external phone lookup providers (Bucket B+).

    Clean public-source providers are wired by default — they have low
    noise and high signal-to-cost: Receita Federal CNPJ open data
    (company-line phones) and the PDF extractor (individual phones
    from public decks/papers/CVs). The Telegram bot integration is
    additive and only fires when the operator configured Telegram
    credentials in their environment.

    Tests monkeypatch this helper to return ``[]`` so the lookup phase
    becomes a no-op.
    """
    providers: list[PhoneLookupProvider] = []

    engines_map = _resolve_free_engines(settings)
    engines = list(engines_map.values()) if engines_map else []
    engine_labels = list(engines_map.keys()) if engines_map else []

    # Receita Federal CNPJ — institutional phone, free, no auth.
    # Uses the same free engines to resolve "company name → CNPJ"
    # when the lead row doesn't carry it directly.
    providers.append(
        ReceitaCnpjLookupProvider(search_engines=engines)
    )

    # PDF SERP extractor — individual phones from public PDFs.
    # Gated on having at least one engine available; otherwise the
    # provider has no way to discover candidate PDFs.
    if engines:
        try:
            providers.append(
                PdfPhoneExtractor(
                    engines=engines,
                    engine_labels=engine_labels,
                )
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("PdfPhoneExtractor falhou em build: %s", exc)

    # Optional: Telegram bot adapter, only when configured. Kept as
    # additive so the clean providers above always run.
    if settings.telegram_api_id and settings.telegram_api_hash:
        providers.append(
            TelegramBotPhoneLookupProvider(
                api_id=settings.telegram_api_id,
                api_hash=settings.telegram_api_hash,
                session_name=settings.telegram_session_name,
                bot_username=settings.telegram_phone_bot_username,
            )
        )
        # Optional: Telegram public group adapter (e.g. CONSULTASGRATIS4NV).
        # Only fires when the operator opted in by setting the group username
        # explicitly — posting in groups carries ban risk.
        if settings.telegram_group_username:
            providers.append(
                TelegramGroupPhoneLookupProvider(
                    api_id=settings.telegram_api_id,
                    api_hash=settings.telegram_api_hash,
                    session_name=settings.telegram_group_session_name,
                    group_username=settings.telegram_group_username,
                    capture_seconds=settings.telegram_group_capture_seconds,
                    throttle_seconds=settings.telegram_group_throttle_seconds,
                )
            )
            providers.append(
                TelegramVoidPhoneLookupProvider(
                    api_id=settings.telegram_api_id,
                    api_hash=settings.telegram_api_hash,
                    session_name=settings.telegram_group_session_name,
                    group_username=settings.telegram_group_username,
                    capture_seconds=settings.telegram_group_capture_seconds,
                    throttle_seconds=settings.telegram_group_throttle_seconds,
                )
            )
    return providers


def _default_whatsapp_checker() -> WhatsAppNumberChecker | None:
    """Disabled while the production phone flow is Telegram-only."""
    return None


def _default_hlr_probe() -> HlrProbeProvider:
    """Production HLR probe.

    Returns the offline NOOP probe by default — paid HLR is gated by
    future env wiring (``Settings.hlr_provider`` + API key) and is not
    enabled in this build. Tests monkeypatch this helper to confirm
    the noop path.
    """
    return default_hlr_probe()


# Short UI-facing aliases → underlying provider.name. Keeping a mapping
# means the front can send "telegram_group" without hard-coding the full
# internal label; new providers can be exposed the same way later.
_PHONE_SOURCE_ALIASES: dict[str, str | tuple[str, ...]] = {
    "telegram_group": (
        "telegram_group_consultasgratis",
        "void_phone_consultasgratis",
    ),
    "telegram_bot": "consultoria_gonzales_bot",
    "void_phone": "void_phone_consultasgratis",
    "receita_cnpj": "receita_cnpj",
    "pdf": "pdf_serp",
}


def _resolve_phone_source_names(sources: list[str] | None) -> set[str] | None:
    """Translate front-end aliases (``telegram_group``) into the set of
    ``provider.name`` values the orchestrator filters by. ``None`` means
    "keep every provider"."""
    if not sources:
        return None
    resolved: set[str] = set()
    for source in sources:
        key = source.strip().lower()
        if not key:
            continue
        mapped = _PHONE_SOURCE_ALIASES.get(key, key)
        if isinstance(mapped, tuple):
            resolved.update(mapped)
        else:
            resolved.add(mapped)
    return resolved or None


def _build_phone_orchestrator(
    *,
    settings: Settings | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    phone_sources: list[str] | None = None,
) -> InternalPhoneEnrichmentOrchestrator:
    """Build a phone orchestrator wired with the production harvester,
    validator, lookup providers, WhatsApp checker, and HLR probe.

    When ``phone_sources`` is set, the orchestrator runs ONLY the
    matching lookup providers and skips the site harvester — used by the
    "Buscar via Telegram" button in the UI to scope the run to one
    channel instead of the full pipeline.

    Tests monkeypatch the ``_default_phone_*`` factories so the call
    chain stays offline.
    """
    settings = settings or load_settings()
    validator = _default_phone_validator()
    wa_checker = _default_whatsapp_checker()
    hlr_probe = _default_hlr_probe()
    service = InternalPhoneEnrichmentService(
        validator=validator,
        wa_checker=wa_checker,
        hlr_probe=hlr_probe,
    )
    lookup_providers = _default_phone_lookup_providers(settings)
    allowed = _resolve_phone_source_names(phone_sources)
    if allowed is not None:
        lookup_providers = [p for p in lookup_providers if p.name in allowed]
        # Site harvester is a separate bucket from lookup providers; the
        # UI scope is "only these lookup channels", so harvest is muted.
        harvest_fn: Callable[..., list] = lambda *_args, **_kwargs: []
    else:
        harvester = _default_phone_harvester()
        harvest_fn = default_phone_harvest_fn(harvester)
    return InternalPhoneEnrichmentOrchestrator(
        service=service,
        harvest_fn=harvest_fn,
        lookup_providers=lookup_providers,
        on_event=on_event,
        cancel_check=cancel_check,
    )


def _run_internal_phone_enrichment(
    *,
    leads: list[Lead],
    company_domains: dict[str, list[str]] | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    settings: Settings | None = None,
    phone_sources: list[str] | None = None,
) -> list[tuple[Lead, PhoneEnrichmentUpdate]]:
    """Synchronous wrapper used by both the blocking POST and the SSE
    streamer. Returns ``[(lead, PhoneEnrichmentUpdate)]``."""

    orchestrator = _build_phone_orchestrator(
        settings=settings,
        on_event=on_event,
        cancel_check=cancel_check,
        phone_sources=phone_sources,
    )
    return orchestrator.run(leads, company_domains=company_domains)


def _telegram_inter_lead_pause(settings: Settings) -> None:
    """Sleep a uniform-random pause between two consecutive Telegram leads.

    Centralized in this module so tests can monkeypatch a single
    indirection (``server.app._telegram_inter_lead_pause = lambda s: None``)
    and skip the wait entirely without disabling the real driver path.
    """
    import random
    import time as _time

    lo = max(0.0, float(settings.telegram_inter_lead_min_seconds))
    hi = max(lo, float(settings.telegram_inter_lead_max_seconds))
    if hi <= 0:
        return
    _time.sleep(random.uniform(lo, hi) if hi > lo else lo)


def _default_telegram_consult_lookup(
    settings: Settings | None = None,
) -> TelegramConsultOrchestrator:
    """Production driver: a :class:`TelegramConsultOrchestrator` that
    wraps the Gon and Unix subclasses with the operator's CDP endpoint.
    Tests monkeypatch this helper to return a fake with a ``consult``
    method returning ``list[TelegramConsultResult]`` so the route stays
    offline.
    """
    settings = settings or load_settings()
    gon = GonzalesBotConsult(cdp_endpoint=settings.linkedin_cdp_endpoint)
    unix = UnixBotConsult(cdp_endpoint=settings.linkedin_cdp_endpoint)
    return TelegramConsultOrchestrator(gon=gon, unix=unix)


def _default_telegram_multi_experimental_lookup(
    settings: Settings | None = None,
) -> TelegramConsultOrchestrator:
    """Experimental driver: Finder + Gon + Unix as independent evidence.

    Kept separate from :func:`_default_telegram_consult_lookup` so the
    production Telegram flow remains unchanged. The orchestrator still
    serializes access to the single Telegram Web/CDP session; providers
    are independent from the comparison/storage perspective, not clicked
    concurrently in the same browser profile.
    """
    settings = settings or load_settings()
    finder = FindexNameConsult(cdp_endpoint=settings.linkedin_cdp_endpoint)
    gon = GonzalesBotConsult(cdp_endpoint=settings.linkedin_cdp_endpoint)
    unix = UnixBotConsult(cdp_endpoint=settings.linkedin_cdp_endpoint)
    return TelegramConsultOrchestrator(gon=gon, unix=unix, finder=finder)


def _default_telegram_telethon_multi_experimental_lookup(
    settings: Settings | None = None,
) -> TelethonTelegramConsultOrchestrator:
    """Experimental driver: Finder + Gon + Unix + Void via Telethon.

    The orchestrator instantiates all four name providers by default, so
    the "extrair CPF via Telegram" flow dispatches each one per lead
    (respecting per-provider cooldown). Void runs its SI-PNI base.

    Kept separate from the CDP multi-provider lookup so the operator can
    compare the native Telegram path without changing the existing
    browser automation flows.
    """
    settings = settings or load_settings()
    if not settings.telegram_api_id or not settings.telegram_api_hash:
        raise RuntimeError(
            "Telegram Telethon nao configurado: defina "
            "BEAUTIFUL_LINKEDIN_TELEGRAM_API_ID e "
            "BEAUTIFUL_LINKEDIN_TELEGRAM_API_HASH."
        )
    return TelethonTelegramConsultOrchestrator(
        api_id=settings.telegram_api_id,
        api_hash=settings.telegram_api_hash,
        session_name=settings.telegram_session_name,
    )


def _default_telethon_gonzales_cpf_consult(
    settings: Settings | None = None,
) -> TelethonGonzalesCpfConsult:
    """Production driver for the Telethon-backed Gonzales /cpf + SISREG flow.

    The same throttle is shared with the /nome orchestrator so the
    pipeline's name→cpf hand-off respects the global Telethon spacing
    across phases, not just within a phase.
    """
    settings = settings or load_settings()
    if not settings.telegram_api_id or not settings.telegram_api_hash:
        raise RuntimeError(
            "Telegram Telethon nao configurado: defina "
            "BEAUTIFUL_LINKEDIN_TELEGRAM_API_ID e "
            "BEAUTIFUL_LINKEDIN_TELEGRAM_API_HASH."
        )
    return TelethonGonzalesCpfConsult(
        api_id=settings.telegram_api_id,
        api_hash=settings.telegram_api_hash,
        session_name=settings.telegram_session_name,
    )


def _default_telethon_serasa_cpf_consult(
    settings: Settings | None = None,
) -> TelethonSerasaCpfConsult:
    """Production driver for SERASA — the preferred Telethon ``/cpf`` flow.

    Sends ``/cpf`` to the ``@puxada2026`` group, follows the VER RESULTADO
    deep link into ``@OraculoPuxadaBot``, and harvests phones from the
    bot's inline answer. Shares the global Telethon throttle/session so the
    name→cpf hand-off keeps respecting the spacing across phases.
    """
    settings = settings or load_settings()
    if not settings.telegram_api_id or not settings.telegram_api_hash:
        raise RuntimeError(
            "Telegram Telethon nao configurado: defina "
            "BEAUTIFUL_LINKEDIN_TELEGRAM_API_ID e "
            "BEAUTIFUL_LINKEDIN_TELEGRAM_API_HASH."
        )
    return TelethonSerasaCpfConsult(
        api_id=settings.telegram_api_id,
        api_hash=settings.telegram_api_hash,
        session_name=settings.telegram_session_name,
    )


def _default_findex_cpf_consult(
    settings: Settings | None = None,
) -> FindexCpfConsult:
    """Production driver for the experimental Finder ``/cpf`` evidence row."""
    settings = settings or load_settings()
    return FindexCpfConsult(cdp_endpoint=settings.linkedin_cdp_endpoint)


def _lead_ref(lead: Lead) -> str:
    return _pipeline_lead_ref_for(lead)


def _refresh_linkedin_signals_for_telegram(
    *,
    selected: list[Lead],
    selected_refs: list[str],
    table_id: str,
    store: Any,
    settings: Settings,
) -> list[Lead]:
    # Inject the module-level names so monkeypatch on this module's
    # ``_run_linkedin_profile_validation`` / ``probe_cdp_endpoint`` keeps
    # working from tests — the pipeline helper resolves the callables it
    # receives, not the import-time references.
    return _pipeline_refresh_linkedin_signals(
        selected=selected,
        selected_refs=selected_refs,
        table_id=table_id,
        store=store,
        settings=settings,
        validation_runner=_run_linkedin_profile_validation,
        cdp_probe=probe_cdp_endpoint,
        select_leads=_select_leads,
    )


def _run_telegram_consult(
    *,
    selected: list[Lead],
    table_id: str,
    store: Any,
    lookup: Any,
) -> list[TelegramConsultPayload]:
    rows = _pipeline_run_telegram_consult(
        selected=selected,
        table_id=table_id,
        store=store,
        lookup=lookup,
    )
    return [_consult_to_payload(row) for row in rows]


def _run_experimental_finder_cpf_followup(
    *,
    selected: list[Lead],
    table_id: str,
    store: Any,
    finder_cpf: Any,
    max_cpfs_per_lead: int = 3,
) -> list[TelegramConsultPayload]:
    """Persist Finder ``/cpf`` evidence for strong name-stage matches.

    The experimental button is meant to compare evidence and then fetch
    phone payloads. Every CPF extracted from Finder itself is a follow-up
    target; CPFs seen only in other providers still need consensus or a
    high matcher score. We rank consensus/high-score CPFs first and cap
    the count per lead so one homonym-heavy name does not burn the bot
    quota.
    """
    out: list[TelegramConsultPayload] = []
    for lead in selected:
        ref = _lead_ref(lead)
        name = (lead.person_name or "").strip()
        if not ref or not name:
            continue
        rows = store.list_telegram_consults_for_lead(
            table_id, ref, query_type="name"
        )
        for cpf in _experimental_finder_cpf_targets(rows)[:max_cpfs_per_lead]:
            result = finder_cpf.consult(cpf)
            extraction = parse_telegram_text(result.raw_text, provider=result.provider)
            saved = store.save_telegram_consult(
                table_id=table_id,
                lead_ref=ref,
                provider=result.provider,
                lead_name=name,
                query=result.query,
                raw_text=result.raw_text,
                source_url=result.source_url,
                downloaded_at=result.downloaded_at,
                error=result.error,
                extracted_nome=extraction.primary_nome,
                extracted_cpf=extraction.primary_cpf,
                extracted_birth_date=extraction.primary_birth_date,
                extracted_address=extraction.primary_address,
                extracted_candidates=[
                    candidate.to_dict() for candidate in extraction.candidates
                ],
                run_id=getattr(result, "run_id", None),
                query_type="cpf",
                query_value=cpf,
            )
            out.append(_consult_to_payload(saved))
    return out


def _experimental_finder_cpf_targets(rows: list[Any]) -> list[str]:
    by_cpf: dict[str, dict[str, Any]] = {}
    for row in rows:
        provider = str(getattr(row, "provider", "") or "")
        primary_cpf = str(getattr(row, "extracted_cpf", "") or "").strip()
        if primary_cpf:
            entry = by_cpf.setdefault(
                primary_cpf, {"providers": set(), "best_score": 0}
            )
            entry["providers"].add(provider)
        for candidate in getattr(row, "extracted_candidates", None) or []:
            if not isinstance(candidate, dict):
                continue
            cpf = str(candidate.get("cpf") or "").strip()
            if not cpf:
                continue
            entry = by_cpf.setdefault(
                cpf, {"providers": set(), "best_score": 0}
            )
            entry["providers"].add(provider)
            try:
                score = int(candidate.get("match_score") or 0)
            except (TypeError, ValueError):
                score = 0
            entry["best_score"] = max(entry["best_score"], score)

    ranked = [
        (cpf, len(data["providers"]), int(data["best_score"]))
        for cpf, data in by_cpf.items()
        if (
            "finder" in data["providers"]
            or len(data["providers"]) >= 2
            or int(data["best_score"]) >= 65
        )
    ]
    ranked.sort(key=lambda item: (item[1], item[2]), reverse=True)
    return [cpf for cpf, _count, _score in ranked]


def _parse_and_rank(
    result: TelegramConsultResult, lead: Lead
) -> tuple[TelegramExtraction, list[dict[str, Any]], MatchScore | None]:
    return _pipeline_parse_and_rank(result, lead)


def _linkedin_experience_years(lead: Lead) -> list[int]:
    from beautiful_linkedin.storage.telegram_pipeline import linkedin_experience_years

    return linkedin_experience_years(lead)


def _consult_to_payload(
    consult: Any, *, lead: Lead | None = None
) -> TelegramConsultPayload:
    extracted_nome = consult.extracted_nome
    extracted_cpf = consult.extracted_cpf
    extracted_birth_date = consult.extracted_birth_date
    extracted_address = consult.extracted_address
    extracted_candidates = consult.extracted_candidates or []
    match_score = consult.match_score
    match_details = consult.match_details or {}

    if (
        consult.raw_text
        and not any(
            [
                extracted_nome,
                extracted_cpf,
                extracted_birth_date,
                extracted_address,
                extracted_candidates,
            ]
        )
    ):
        result = TelegramConsultResult(
            provider=consult.provider,
            lead_name=consult.lead_name,
            query=consult.query,
            raw_text=consult.raw_text,
            source_url=consult.source_url,
            downloaded_at=consult.downloaded_at,
            error=consult.error,
        )
        if lead is not None:
            extraction, ranked_candidates, top_score = _parse_and_rank(result, lead)
            extracted_candidates = ranked_candidates
            match_score = top_score.score if top_score else match_score
            match_details = top_score.breakdown if top_score else match_details
        else:
            extraction = parse_telegram_text(consult.raw_text, provider=consult.provider)
            extracted_candidates = [
                candidate.to_dict() for candidate in extraction.candidates
            ]
        extracted_nome = extraction.primary_nome
        extracted_cpf = extraction.primary_cpf
        extracted_birth_date = extraction.primary_birth_date
        extracted_address = extraction.primary_address

    return TelegramConsultPayload(
        id=consult.id,
        table_id=consult.table_id,
        lead_ref=consult.lead_ref,
        provider=consult.provider,
        lead_name=consult.lead_name,
        query=consult.query,
        raw_text=consult.raw_text,
        source_url=consult.source_url,
        downloaded_at=consult.downloaded_at,
        error=consult.error,
        extracted_nome=extracted_nome,
        extracted_cpf=extracted_cpf,
        extracted_birth_date=extracted_birth_date,
        extracted_address=extracted_address,
        extracted_candidates=extracted_candidates,
        match_score=match_score,
        match_details=match_details,
        created_at=consult.created_at,
        run_id=getattr(consult, "run_id", None),
        query_type=getattr(consult, "query_type", "name") or "name",
        query_value=getattr(consult, "query_value", None),
        blocked_reason=getattr(consult, "blocked_reason", None),
    )


def _default_gonzales_cpf_consult(
    settings: Settings | None = None,
) -> GonzalesCpfConsult:
    """Production driver: a :class:`GonzalesCpfConsult` bound to the
    operator's Chrome CDP endpoint. Tests monkeypatch this helper to
    return a fake whose ``consult(cpf)`` is deterministic and offline.
    """
    settings = settings or load_settings()
    return GonzalesCpfConsult(
        cdp_endpoint=settings.linkedin_cdp_endpoint,
        post_send_min_seconds=settings.telegram_post_send_min_seconds,
        post_send_max_seconds=settings.telegram_post_send_max_seconds,
        gon_abort_timeout_seconds=settings.telegram_gon_abort_timeout_seconds,
    )


def _default_gonzales_consult(
    settings: Settings | None = None,
) -> GonzalesBotConsult:
    """Production driver: a :class:`GonzalesBotConsult` for the name
    stage. Tests monkeypatch this helper to return a deterministic
    offline fake. Kept separate from
    :func:`_default_telegram_consult_lookup` (which bundles Gon + Unix)
    so the unified phone flow can pay only for Gon — Unix is skipped to
    cut Playwright time per lead.
    """
    settings = settings or load_settings()
    return GonzalesBotConsult(
        cdp_endpoint=settings.linkedin_cdp_endpoint,
        post_send_min_seconds=settings.telegram_post_send_min_seconds,
        post_send_max_seconds=settings.telegram_post_send_max_seconds,
        gon_abort_timeout_seconds=settings.telegram_gon_abort_timeout_seconds,
    )


def _default_findex_email_consult(
    settings: Settings | None = None,
) -> FindexEmailConsult:
    """Production driver for the Findex e-mail -> phone fallback.

    Tests monkeypatch this helper to keep endpoint tests offline.
    """
    settings = settings or load_settings()
    return FindexEmailConsult(
        cdp_endpoint=settings.linkedin_cdp_endpoint,
        post_send_min_seconds=settings.telegram_post_send_min_seconds,
        post_send_max_seconds=settings.telegram_post_send_max_seconds,
        gon_abort_timeout_seconds=settings.telegram_gon_abort_timeout_seconds,
    )


def _telegram_phone_candidate_to_update(
    candidate: TelegramPhoneCandidate,
) -> PhoneEnrichmentUpdate:
    """Project a pipeline candidate onto the existing phone enrichment
    update shape so :meth:`SavedLeadsStore.apply_internal_phone_enrichment_updates`
    handles the verification trail uniformly.

    The phone is normalized to ``+55<digits>`` when missing the country
    code — the pipeline harvests Brazilian numbers and the validator
    downstream expects an E.164-ish prefix. ``confidence`` is forwarded
    untouched from the matcher.
    """
    digits = candidate.phone_digits or ""
    e164 = digits if digits.startswith("55") else f"55{digits}"
    source_kind = (
        "telegram_consult_email"
        if candidate.provenance.get("score_source") == "findex_email_fallback"
        else "telegram_consult_cpf"
    )
    return PhoneEnrichmentUpdate(
        phone=f"+{e164}" if e164 else None,
        national=candidate.phone_raw,
        confidence=candidate.confidence,
        source=f"{source_kind}:{candidate.source_provider}",
        source_url=candidate.provenance.get("raw_source_url"),
    )


@dataclass(frozen=True)
class _TelegramContactEmailUpdate:
    email: str
    source: str
    confidence: int
    email_type: str = "personal"
    email_validation_status: str = "unknown"


def _apply_telegram_contact_details(
    *,
    store: Any,
    table_id: str,
    lead: Lead,
    candidates: list[TelegramPhoneCandidate],
) -> None:
    """Persist the contact details a CPF/SISREG report carries alongside
    the phone: personal e-mails and the residential address.

    Both ride on ``candidate.provenance`` (``contact_emails`` /
    ``contact_address``). E-mails are merged into the lead's contact trail;
    the address is written once (the store declines to overwrite an
    existing one). Candidates arrive best-score-first, so the first
    non-empty address wins.
    """
    updates: list[tuple[Lead, _TelegramContactEmailUpdate]] = []
    seen: set[str] = set()
    address: str | None = None
    for candidate in candidates:
        if address is None:
            candidate_address = candidate.provenance.get("contact_address")
            if isinstance(candidate_address, str) and candidate_address.strip():
                address = candidate_address.strip()
        emails = candidate.provenance.get("contact_emails") or []
        if not isinstance(emails, list):
            continue
        source_kind = (
            "telegram_consult_email"
            if candidate.provenance.get("score_source") == "findex_email_fallback"
            else "telegram_consult_cpf"
        )
        source = f"{source_kind}:{candidate.source_provider}"
        for value in emails:
            email = str(value or "").strip().lower()
            if not email or "@" not in email or email in seen:
                continue
            seen.add(email)
            updates.append(
                (
                    lead,
                    _TelegramContactEmailUpdate(
                        email=email,
                        source=source,
                        confidence=candidate.confidence,
                    ),
                )
            )
    if updates:
        store.apply_contact_email_updates(table_id, updates)
    if address:
        store.apply_contact_address_updates(table_id, [(lead, address)])


def _telegram_phone_candidate_to_payload(
    candidate: TelegramPhoneCandidate,
) -> TelegramFollowupPhoneCandidate:
    return TelegramFollowupPhoneCandidate(
        phone_raw=candidate.phone_raw,
        phone_digits=candidate.phone_digits,
        cpf=candidate.cpf,
        confidence=candidate.confidence,
        source_provider=candidate.source_provider,
        nome=candidate.nome,
        provenance=dict(candidate.provenance),
    )


def _ranked_candidate_to_payload(
    candidate: TelegramParsedCandidate, *, eligible: bool
) -> TelegramPhoneRankedCandidatePayload:
    return TelegramPhoneRankedCandidatePayload(
        cpf=candidate.cpf,
        nome=candidate.nome,
        data_nascimento=candidate.data_nascimento,
        endereco=candidate.endereco,
        match_score=candidate.match_score,
        signals_used=list(candidate.signals_used),
        breakdown=dict(candidate.breakdown),
        eligible=eligible,
    )


def _ranked_candidates_payload(
    stage: TelegramNameStageResult,
) -> list[TelegramPhoneRankedCandidatePayload]:
    eligible_cpfs = {c.cpf for c in stage.eligible_candidates}
    return [
        _ranked_candidate_to_payload(c, eligible=c.cpf in eligible_cpfs)
        for c in stage.all_candidates
        if _pipeline_cpf_candidate_allowed_for_review(c)
    ]


def _build_extract_cpfs_response_from_persisted(
    app: FastAPI,
    record: "TelegramPhoneRunRecord",
    table_id: str,
    target_ref: str,
) -> TelegramPhoneExtractCpfsResponse:
    """Devolve o estado de awaiting_cpf_confirmation já persistido sem
    rerodar ``/nome``. Usado quando o cliente chama ``/extract-cpfs``
    duas vezes em sequência para o mesmo lead (refresh do modal, retry
    de rede, etc.)."""
    store = get_saved_leads_store(app)
    persisted = _pipeline_collect_name_stage_candidates(
        store=store, table_id=table_id, lead_ref=target_ref
    )
    eligible = _pipeline_select_followup_candidates(persisted)
    eligible_cpfs = [c.cpf for c in eligible]
    eligible_set = set(eligible_cpfs)
    candidates_payload = [
        _ranked_candidate_to_payload(c, eligible=c.cpf in eligible_set)
        for c in persisted
        if _pipeline_cpf_candidate_allowed_for_review(c)
    ]
    consult = latest_name_stage_consult(
        store=store, table_id=table_id, lead_ref=target_ref
    )
    return TelegramPhoneExtractCpfsResponse(
        run_id=record.run_id,
        status="awaiting_cpf_confirmation",
        next_index=record.next_index,
        total_leads=len(record.lead_refs),
        lead_ref=target_ref,
        lead_name=consult.lead_name if consult is not None else None,
        blocked_reason=None,
        eligible_cpfs=eligible_cpfs,
        candidates=candidates_payload,
        name_consult=(
            _consult_to_payload(consult) if consult is not None else None
        ),
    )


def _process_one_telegram_phone_lead(
    *,
    lead: Lead,
    table_id: str,
    store: SavedLeadsStore,
    name_consult_fn: Callable[[str], Any],
    cpf_consult_fn: Callable[[str], Any],
    email_consult_fn: Callable[[str], Any] | None,
    target_titles: list[str] | None,
    run_id: str | None = None,
) -> tuple[TelegramPhoneLeadResult, dict[str, int]]:
    """Executa o fluxo /nome → /cpf → telefone para UM lead e devolve o
    payload + os contadores (``leads_with_phone``, ``leads_blocked``,
    ``phones_persisted``, ``skipped_existing_phone``).

    Compartilhado entre o endpoint legado em batch
    (``POST /lead-tables/{id}/telegram-phone``) e o endpoint resumível
    (``POST /lead-tables/{id}/telegram-phone/next``), que processa um
    lead por vez com confirmação humana entre cada um — esse split é
    o que protege os bots Telegram de ban por volume.
    """
    counters = {
        "leads_with_phone": 0,
        "leads_blocked": 0,
        "phones_persisted": 0,
        "skipped_existing_phone": 0,
    }
    flow: TelegramPhoneFlowResult = _pipeline_run_extract_phone_via_cpf(
        lead=lead,
        table_id=table_id,
        store=store,
        name_consult_fn=name_consult_fn,
        cpf_consult_fn=cpf_consult_fn,
        email_consult_fn=email_consult_fn,
        target_titles=target_titles,
        run_id=run_id,
    )
    if flow.blocked_reason:
        counters["leads_blocked"] += 1

    had_new_phone = False
    if flow.phone_candidates:
        ordered = sorted(
            flow.phone_candidates,
            key=lambda c: c.confidence,
            reverse=True,
        )
        primary_update = _telegram_phone_candidate_to_update(ordered[0])
        primary_counters = store.apply_internal_phone_enrichment_updates(
            table_id, [(lead, primary_update)]
        )
        if primary_counters.get("enriched", 0) > 0:
            counters["phones_persisted"] += primary_counters["enriched"]
            had_new_phone = True
        counters["skipped_existing_phone"] += primary_counters.get(
            "skipped_existing_phone", 0
        )
        for extra in ordered[1:]:
            extras_counter = store.apply_internal_phone_enrichment_updates(
                table_id,
                [(lead, _telegram_phone_candidate_to_update(extra))],
            )
            counters["phones_persisted"] += extras_counter.get("enriched", 0)
            counters["skipped_existing_phone"] += extras_counter.get(
                "skipped_existing_phone", 0
            )
        _apply_telegram_contact_details(
            store=store,
            table_id=table_id,
            lead=lead,
            candidates=ordered,
        )
    if had_new_phone:
        counters["leads_with_phone"] += 1

    stages_payload = [
        TelegramPhoneStageEvent(
            stage=event.stage,
            timestamp=event.timestamp,
            detail=dict(event.detail),
        )
        for event in (flow.stages or [])
    ]
    payload = TelegramPhoneLeadResult(
        lead_ref=flow.lead_ref,
        lead_name=flow.lead_name,
        blocked_reason=flow.blocked_reason,
        candidates=[
            _telegram_phone_candidate_to_payload(c)
            for c in flow.phone_candidates
        ],
        name_consult=(
            _consult_to_payload(flow.name_consult)
            if flow.name_consult is not None
            else None
        ),
        cpf_consult=(
            _consult_to_payload(flow.cpf_consult)
            if flow.cpf_consult is not None
            else None
        ),
        stages=stages_payload,
        last_stage=stages_payload[-1].stage if stages_payload else None,
    )
    return payload, counters


def _run_internal_enrichment(
    *,
    leads: list[Lead],
    existing_company_emails: list[tuple[str, str]],
    company_domains: dict[str, list[str]] | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> list[tuple[Lead, Any]]:
    """Synchronous wrapper used by both the blocking POST and the SSE
    streamer. Returns ``[(lead, EnrichmentUpdate)]``."""

    orchestrator = _build_internal_orchestrator(
        on_event=on_event, cancel_check=cancel_check
    )
    return orchestrator.run(
        leads,
        existing_company_emails=existing_company_emails,
        company_domains=company_domains,
    )


def _apply_internal_domain_fallback(
    leads: list[Lead], company_domain: str | None
) -> list[Lead]:
    """Return copies with ``company_domain`` filled where the lead lacks it.

    Saved People Search tables often come from LinkedIn URLs only. Internal
    e-mail inference cannot start without the company's real mail domain, so
    the UI/API can supply a table-level domain for this run.
    """

    domain = _clean_internal_company_domain(company_domain)
    if not domain:
        return leads
    return [
        lead
        if (lead.company_domain or "").strip()
        else lead.model_copy(update={"company_domain": domain})
        for lead in leads
    ]


def _clean_internal_company_domain(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    raw = value.strip().lower()
    if not raw:
        return None
    if "://" in raw:
        from urllib.parse import urlparse

        raw = urlparse(raw).netloc
    raw = raw.split("/", 1)[0].split("?", 1)[0].strip()
    if raw.startswith("www."):
        raw = raw[4:]
    if not raw or "linkedin.com" in raw or "." not in raw:
        return None
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789.-")
    if any(char not in allowed for char in raw):
        return None
    return raw.strip(".") or None


def _default_mailbox_verifier() -> Any:
    """Build the free SMTP mailbox verifier used by internal enrichment."""
    return SmtpMailboxVerifier(mx_hosts_resolver=_default_mx_hosts_resolver())


def _default_mx_hosts_resolver() -> Callable[[str], list[str]]:
    """Resolve MX hosts ordered by priority for SMTP recipient probing."""

    try:
        import dns.resolver  # type: ignore[import-not-found]
    except Exception:

        def _fallback(domain: str) -> list[str]:
            cleaned = (domain or "").strip().lower()
            return [cleaned] if cleaned else []

        return _fallback

    def _mx_hosts(domain: str) -> list[str]:
        cleaned = (domain or "").strip().lower()
        if not cleaned:
            return []
        try:
            answers = dns.resolver.resolve(cleaned, "MX", lifetime=3.0)
        except Exception:
            return []
        records: list[tuple[int, str]] = []
        for answer in answers:
            host = str(getattr(answer, "exchange", "")).rstrip(".")
            if not host:
                continue
            preference = int(getattr(answer, "preference", 0))
            records.append((preference, host))
        return [host for _, host in sorted(records)]

    return _mx_hosts


def _default_mx_resolver() -> Any:
    """Build a Callable[[str], bool] that checks whether a domain has MX
    records. Uses ``dnspython`` when available, otherwise falls back to
    ``socket.gethostbyname`` which only proves the domain resolves (a
    weaker but still useful signal). The function-of-function shape is
    intentional so tests can monkeypatch this module-level helper to
    return a deterministic resolver."""

    try:
        import dns.resolver  # type: ignore[import-not-found]
    except Exception:
        import socket

        def _socket_resolver(domain: str) -> bool:
            try:
                socket.gethostbyname(domain)
            except Exception:
                return False
            return True

        return _socket_resolver

    def _mx_resolver(domain: str) -> bool:
        try:
            answers = dns.resolver.resolve(domain, "MX", lifetime=3.0)
        except Exception:
            return False
        return any(answers)

    return _mx_resolver


def run_saved_lead_enrichment(
    *,
    store: SavedLeadsStore,
    table_id: str,
    selected_leads: list[Lead],
    options: EnrichmentOptions,
    providers: list[Any],
) -> EnrichmentRunSummary:
    errors: list[str] = []
    used: list[str] = []
    provider_logs: list[ProviderRunLog] = []
    enriched_lead_refs: set[str] = set()
    total_updated = 0
    for provider in providers:
        name = str(getattr(provider, "name", provider.__class__.__name__))
        estimate = estimate_enrichment_cost(
            selected_leads,
            EnrichmentOptions(
                fields=options.fields,
                providers=[name],
                credit_costs_brl=options.credit_costs_brl,
                apollo_webhook_url=options.apollo_webhook_url,
            ),
        )
        provider_estimate = estimate.provider_estimates[0] if estimate.provider_estimates else None
        try:
            provider_updates = provider.enrich(selected_leads, options)
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
            provider_logs.append(
                ProviderRunLog(
                    provider=name,
                    requested_leads=len(selected_leads),
                    matched_leads=0,
                    updated_leads=0,
                    estimated_credits=provider_estimate.estimated_credits
                    if provider_estimate
                    else 0,
                    estimated_brl=provider_estimate.estimated_brl
                    if provider_estimate
                    else 0.0,
                    status="error",
                    message=f"{_provider_label(name)} falhou: {type(exc).__name__}.",
                )
            )
            continue
        # Surface HTTP errors the provider swallowed (e.g. Apollo 403 plan-gated).
        provider_errors = getattr(provider, "errors", None) or []
        for err in provider_errors:
            errors.append(f"{name}: {err}")

        matched_refs = {_lead_ref(update.lead) for update in provider_updates}
        matched_refs.discard("")
        no_data_leads = [
            lead
            for lead in selected_leads
            if _lead_ref(lead) not in matched_refs
            and _lead_ref(lead) not in enriched_lead_refs
        ]
        if no_data_leads:
            store.mark_api_enrichment_attempt(
                table_id,
                no_data_leads,
                provider=name,
            )
        provider_updated = 0
        if provider_updates:
            used.append(name)
            enriched_lead_refs.update(ref for ref in matched_refs if ref)
            provider_updated = store.apply_enrichment_updates(table_id, provider_updates)
            total_updated += provider_updated

        matched_count = len(matched_refs)
        status_value = "updated" if provider_updates else "no_data"
        provider_logs.append(
            ProviderRunLog(
                provider=name,
                requested_leads=len(selected_leads),
                matched_leads=matched_count,
                updated_leads=provider_updated,
                estimated_credits=provider_estimate.estimated_credits
                if provider_estimate
                else 0,
                estimated_brl=provider_estimate.estimated_brl
                if provider_estimate
                else 0.0,
                status=status_value,
                message=_provider_run_message(
                    name=name,
                    requested=len(selected_leads),
                    matched=matched_count,
                    options=options,
                ),
            )
        )
    return EnrichmentRunSummary(
        requested_leads=len(selected_leads),
        enriched_leads=len(enriched_lead_refs),
        updated_leads=total_updated,
        providers_used=used,
        errors=errors,
        provider_logs=provider_logs,
    )


def _lead_ref(lead: Lead) -> str:
    return lead.linkedin_url or lead.source_url or lead.person_name or ""


def _provider_label(name: str) -> str:
    labels = {
        "apollo": "Apollo",
        "lusha": "Lusha",
        "snovio": "Snov.io",
        "pdl": "PDL",
    }
    return labels.get(name, name)


def _provider_run_message(
    *,
    name: str,
    requested: int,
    matched: int,
    options: EnrichmentOptions,
) -> str:
    label = _provider_label(name)
    if matched > 0:
        return f"{label} consultou {requested} lead(s) e retornou dados para {matched}."
    wanted: list[str] = []
    if options.wants_email:
        wanted.append("e-mail")
    if options.wants_phone:
        wanted.append("telefone")
    target = " ou ".join(wanted) if wanted else "dados"
    return f"{label} consultou {requested} lead(s), mas não retornou {target}."


def _select_leads(leads: list[Lead], refs: list[str]) -> list[Lead]:
    wanted = {ref.strip() for ref in refs if ref and ref.strip()}
    selected: list[Lead] = []
    for lead in leads:
        candidates = {
            lead.linkedin_url or "",
            lead.source_url or "",
            lead.person_name or "",
        }
        if wanted.intersection(candidates):
            selected.append(lead)
    return selected


def _build_enrichment_providers(
    settings: Settings, options: EnrichmentOptions
) -> list[Any]:
    providers: list[Any] = []
    names = _ordered_enrichment_provider_names(options)
    if "lusha" in names and settings.lusha_api_key:
        providers.append(LushaEnrichmentProvider(settings.lusha_api_key))
    if "apollo" in names and settings.apollo_api_key:
        providers.append(ApolloEnrichmentProvider(settings.apollo_api_key))
    if (
        "snovio" in names
        and settings.snovio_client_id
        and settings.snovio_client_secret
    ):
        providers.append(
            SnovioEnrichmentProvider(
                client_id=settings.snovio_client_id,
                client_secret=settings.snovio_client_secret,
            )
        )
    if "pdl" in names and settings.people_data_labs_api_key:
        providers.append(PdlEnrichmentProvider(settings.people_data_labs_api_key))
    return sorted(
        providers,
        key=lambda provider: names.index(str(getattr(provider, "name", ""))),
    )


# Tabela canônica de R$/crédito por provider.
#
# As APIs públicas de Apollo/Lusha/Snov.io/PDL **não** expõem preço por
# crédito (o preço depende do plano contratado). Por isso o servidor é a
# fonte da verdade — a UI nunca edita esses valores; ela lê via
# ``GET /enrichment/pricing`` e exibe read-only. Operadores que tenham plano
# diferente podem sobrescrever via env var ``ENRICHMENT_COST_BRL_<PROVIDER>``
# (ex.: ``ENRICHMENT_COST_BRL_APOLLO=0.18``).
_DEFAULT_ENRICHMENT_COST_BRL: dict[str, float] = {
    "snovio": 0.15,
    "apollo": 0.30,
    "pdl": 0.50,
    "lusha": 3.00,
}


def _env_override_pricing() -> dict[str, float]:
    """Read ENRICHMENT_COST_BRL_<PROVIDER> env vars and return overrides.

    Invalid floats are silently ignored — the default stays in place rather
    than 500'ing the endpoint because of a bad env value.
    """
    import os

    overrides: dict[str, float] = {}
    for name in _DEFAULT_ENRICHMENT_COST_BRL:
        raw = os.environ.get(f"ENRICHMENT_COST_BRL_{name.upper()}")
        if raw is None or not raw.strip():
            continue
        try:
            value = float(raw.strip().replace(",", "."))
        except ValueError:
            continue
        if value < 0:
            continue
        overrides[name] = value
    return overrides


def get_enrichment_pricing() -> dict[str, float]:
    """Return the effective R$/credit table (defaults + env overrides)."""
    table = dict(_DEFAULT_ENRICHMENT_COST_BRL)
    table.update(_env_override_pricing())
    return table


def _ordered_enrichment_provider_names(options: EnrichmentOptions) -> list[str]:
    seen: set[str] = set()
    names: list[str] = []
    for raw in options.providers:
        name = raw.strip().lower()
        if name not in {"apollo", "lusha", "snovio", "pdl"}:
            continue
        if name == "snovio" and not options.wants_email:
            continue
        if name in seen:
            continue
        seen.add(name)
        names.append(name)
    return sorted(
        names,
        key=lambda name: (
            float(options.credit_costs_brl.get(name, _DEFAULT_ENRICHMENT_COST_BRL[name])),
            name,
        ),
    )


def _enrichment_estimate_payload(
    estimate: EnrichmentEstimate,
) -> EnrichmentEstimatePayload:
    return EnrichmentEstimatePayload(
        selected_leads=estimate.selected_leads,
        provider_estimates=[
            EnrichmentProviderEstimatePayload(
                provider=item.provider,
                selected_leads=item.selected_leads,
                estimated_credits=item.estimated_credits,
                estimated_brl=item.estimated_brl,
                fields=list(item.fields),
            )
            for item in estimate.provider_estimates
        ],
        total_estimated_credits=estimate.total_estimated_credits,
        total_estimated_brl=estimate.total_estimated_brl,
        warnings=list(estimate.warnings),
    )


def _enrichment_summary_payload(
    summary: EnrichmentRunSummary,
) -> EnrichmentSummaryPayload:
    return EnrichmentSummaryPayload(
        requested_leads=summary.requested_leads,
        enriched_leads=summary.enriched_leads,
        updated_leads=summary.updated_leads,
        providers_used=list(summary.providers_used),
        errors=list(summary.errors),
        provider_logs=[
            ProviderRunLogPayload(
                provider=item.provider,
                requested_leads=item.requested_leads,
                matched_leads=item.matched_leads,
                updated_leads=item.updated_leads,
                estimated_credits=item.estimated_credits,
                estimated_brl=item.estimated_brl,
                status=item.status,
                message=item.message,
            )
            for item in summary.provider_logs
        ],
    )


def _resolve_people_company_size(
    *,
    company_name: str,
    company_domain: str | None,
    linkedin_url: str | None,
    emit: Callable[[str, dict[str, Any]], None],
) -> dict[str, Any]:
    """Resolve company-size signals via a CDP-connected Chrome.

    Falls back to no signals when CDP isn't enabled / alive. Tests
    monkey-patch this function to inject scripted resolvers.
    """
    settings = load_settings()
    if not settings.linkedin_cdp_enabled:
        emit("cdp_disabled", {})
        return {}
    endpoint = settings.linkedin_cdp_endpoint
    if not probe_cdp_endpoint(endpoint):
        emit("cdp_offline", {"endpoint": endpoint})
        return {}

    page_factory = _build_cdp_page_factory(endpoint, emit)
    if page_factory is None:
        return {}
    return resolve_company_size_via_page(
        company_name=company_name,
        linkedin_url=linkedin_url,
        company_domain=company_domain,
        emit=emit,
        page_factory=page_factory,
    )


def _build_cdp_page_factory(
    endpoint: str, emit: Callable[[str, dict[str, Any]], None]
) -> Callable[[], Any] | None:
    """Return a thunk that opens a new page in the user's CDP Chrome.

    Returns ``None`` (after emitting a diagnostic event) when the
    connection can't be established — we never raise out of the probe.
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        emit("playwright_unavailable", {"message": str(exc)})
        return None

    try:
        runtime = sync_playwright().start()
        browser = runtime.chromium.connect_over_cdp(endpoint)
    except Exception as exc:
        emit("cdp_connect_failed", {"message": str(exc)})
        return None

    contexts = list(getattr(browser, "contexts", []) or [])
    if not contexts:
        emit("cdp_no_context", {})
        return None
    context = contexts[0]

    def factory() -> Any:
        return context.new_page()

    return factory


def _run_people_probe(
    app: FastAPI, record: "ProbeRecord", payload: PeopleSearchProbeRequest
) -> None:
    def append_event(event: str, data: dict[str, Any] | None = None) -> None:
        with record.lock:
            record.events.append({"event": event, "data": dict(data or {})})

    with record.lock:
        record.status = "running"

    signals: dict[str, Any] = {}
    try:
        try:
            signals = _resolve_people_company_size(
                company_name=payload.company_name,
                company_domain=payload.company_domain,
                linkedin_url=payload.linkedin_url,
                emit=append_event,
            ) or {}
        except Exception as exc:
            append_event("error", {"message": f"{type(exc).__name__}: {exc}"})

        classification = classify_company_size(
            employee_count=signals.get("employee_count"),
            employee_count_text=signals.get("employee_count_text"),
            visible_card_count=signals.get("visible_card_count"),
        )
        probe_response = _probe_response(classification)
        append_event(
            "classified",
            {
                "is_small": probe_response.is_small,
                "employee_count": probe_response.employee_count,
                "source": probe_response.source,
                "should_offer_general_search": probe_response.should_offer_general_search,
            },
        )
        with record.lock:
            record.classification = probe_response
            record.status = "completed"
    except Exception as exc:
        with record.lock:
            record.status = "failed"
            record.error = f"{type(exc).__name__}: {exc}"


def _probe_people_company(
    *,
    company_name: str,
    company_domain: str | None = None,
    linkedin_url: str | None = None,
) -> dict[str, Any]:
    """Resolve company-size signals from public sources.

    Default implementation returns no signals — real resolution requires
    network calls and is patched in tests. Hooking a real resolver here is
    the integration point for the people_search provider once it grows the
    capability.
    """
    return {}


def _probe_response(classification: CompanySizeClassification) -> PeopleSearchProbeResponse:
    should_offer = classification.is_small is True
    return PeopleSearchProbeResponse(
        is_small=classification.is_small,
        employee_count=classification.employee_count,
        source=classification.source,
        note=classification.note,
        should_offer_general_search=should_offer,
    )


def _maybe_build_queries(
    search_request: dict[str, Any], fallback_keywords: list[str]
) -> list[str]:
    company_name = search_request.get("company_name") if search_request else None
    titles = search_request.get("titles") if search_request else None
    if not titles:
        titles = fallback_keywords
    if not company_name or not titles:
        return []
    try:
        company = CompanyInput(
            company_name=str(company_name),
            company_domain=search_request.get("company_domain"),
            linkedin_url=search_request.get("linkedin_url"),
            titles=list(titles),
        )
    except Exception:
        return []
    depth = str(search_request.get("search_depth") or "standard")
    return build_balanced_queries_for_company(company, depth=depth)


def _coerce_filter(payload: FilterPayload | None) -> LeadFilter | None:
    if payload is None:
        return None
    return build_lead_filter(
        seniority=",".join(payload.seniority_in) if payload.seniority_in else None,
        functions=",".join(payload.functions_in) if payload.functions_in else None,
        locations=",".join(payload.locations_in) if payload.locations_in else None,
        exclude_titles=",".join(payload.exclude_titles) if payload.exclude_titles else None,
        min_confidence=payload.min_confidence_score,
        drop_unclassified=payload.drop_unclassified,
    )


def _settings_with_api_key_overrides(payload: ApiKeyOverrides | None) -> Settings:
    settings = load_settings()
    if payload is None:
        return settings

    updates: dict[str, str] = {}
    for key, value in payload.model_dump(exclude_none=True).items():
        if not isinstance(value, str):
            continue
        cleaned = value.strip()
        if cleaned:
            updates[key] = cleaned
    if not updates:
        return settings
    return replace(settings, **updates)


_API_KEY_FIELD_TO_ENV: dict[str, str] = {
    "brave_search_api_key": "BRAVE_SEARCH_API_KEY",
    "google_custom_search_api_key": "GOOGLE_CUSTOM_SEARCH_API_KEY",
    "google_custom_search_cx": "GOOGLE_CUSTOM_SEARCH_CX",
    "serper_api_key": "SERPER_API_KEY",
    "searxng_base_url": "SEARXNG_BASE_URL",
    "people_data_labs_api_key": "PEOPLE_DATA_LABS_API_KEY",
    "coresignal_api_key": "CORESIGNAL_API_KEY",
    "apollo_api_key": "APOLLO_API_KEY",
    "lusha_api_key": "LUSHA_API_KEY",
    "snovio_client_id": "SNOVIO_CLIENT_ID",
    "snovio_client_secret": "SNOVIO_CLIENT_SECRET",
    "apify_api_key": "APIFY_API_KEY",
    "linkedin_li_at_cookie": "LINKEDIN_LI_AT_COOKIE",
    "linkedin_cookie_browser": "LINKEDIN_COOKIE_BROWSER",
}


def _mask_secret(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        return ""
    if cleaned.startswith("http://") or cleaned.startswith("https://"):
        return cleaned  # URLs (SearxNG) aren't secret — show in full
    if len(cleaned) <= 6:
        return "*" * len(cleaned)
    return f"{cleaned[:3]}…{cleaned[-3:]} ({len(cleaned)} chars)"


def _build_diagnostics(payload: ApiKeyOverrides | None) -> DiagnosticsResponse:
    env_settings = load_settings()
    merged_settings = _settings_with_api_key_overrides(payload)

    ui_overrides: dict[str, str] = {}
    if payload is not None:
        for key, value in payload.model_dump(exclude_none=True).items():
            if isinstance(value, str) and value.strip():
                ui_overrides[key] = value.strip()

    settings_rows: list[ApiKeySource] = []
    for field, env_var in _API_KEY_FIELD_TO_ENV.items():
        env_value = getattr(env_settings, field, None)
        merged_value = getattr(merged_settings, field, None)
        configured = bool(merged_value and str(merged_value).strip())
        if field in ui_overrides:
            source = "ui_override"
        elif configured and env_value:
            source = "env"
        else:
            source = "missing"
        preview = _mask_secret(str(merged_value)) if configured and merged_value else None
        settings_rows.append(
            ApiKeySource(
                field=field,
                env_var=env_var,
                configured=configured,
                source=source,
                preview=preview,
            )
        )

    s = merged_settings
    lead_providers = [
        ProviderConfigStatus(
            name="pdl",
            configured=bool(s.people_data_labs_api_key),
            requires=["PEOPLE_DATA_LABS_API_KEY"],
        ),
        ProviderConfigStatus(
            name="apollo",
            configured=bool(s.apollo_api_key),
            requires=["APOLLO_API_KEY"],
        ),
        ProviderConfigStatus(
            name="coresignal",
            configured=bool(s.coresignal_api_key),
            requires=["CORESIGNAL_API_KEY"],
        ),
        ProviderConfigStatus(
            name="lusha",
            configured=bool(s.lusha_api_key),
            requires=["LUSHA_API_KEY"],
        ),
        ProviderConfigStatus(
            name="snovio",
            configured=bool(s.snovio_client_id and s.snovio_client_secret),
            requires=["SNOVIO_CLIENT_ID", "SNOVIO_CLIENT_SECRET"],
        ),
        ProviderConfigStatus(
            name="apify_linkedin",
            configured=bool(s.apify_api_key),
            requires=["APIFY_API_KEY"],
        ),
        ProviderConfigStatus(
            name="linkedin_cookie",
            configured=bool(s.linkedin_li_at_cookie),
            requires=["LINKEDIN_LI_AT_COOKIE"],
        ),
    ]
    search_engines = [
        ProviderConfigStatus(
            name="searxng",
            configured=bool(s.searxng_base_url),
            requires=["SEARXNG_BASE_URL"],
        ),
        ProviderConfigStatus(
            name="serper",
            configured=bool(s.serper_api_key),
            requires=["SERPER_API_KEY"],
        ),
        ProviderConfigStatus(
            name="brave",
            configured=bool(s.brave_search_api_key),
            requires=["BRAVE_SEARCH_API_KEY"],
        ),
        ProviderConfigStatus(
            name="google_cse",
            configured=bool(s.google_custom_search_api_key and s.google_custom_search_cx),
            requires=["GOOGLE_CUSTOM_SEARCH_API_KEY", "GOOGLE_CUSTOM_SEARCH_CX"],
        ),
    ]
    return DiagnosticsResponse(
        settings=settings_rows,
        lead_providers=lead_providers,
        search_engines=search_engines,
    )
