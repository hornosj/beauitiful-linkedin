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
from beautiful_linkedin.config import Settings, load_settings
from beautiful_linkedin.cookie_resolver import diagnose_li_at_cookie
from beautiful_linkedin.providers.linkedin_people_search import (
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
from beautiful_linkedin.storage.internal_enrichment import (
    CompanyDomainResolver,
    EmailValidator,
    InternalEnrichmentOrchestrator,
    InternalLeadEnrichmentService,
    SmtpMailboxVerifier,
    collect_company_domains,
)
from beautiful_linkedin.storage.saved_leads import (
    ImportColumnError,
    SavedLeadsStore,
    SOURCE_TYPE_SEARCH,
)

VERSION = "0.1.0"

logger = logging.getLogger(__name__)


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


class StartRunResponse(BaseModel):
    run_id: str
    status: RunStatus = RunStatus.PENDING


class RunStateResponse(BaseModel):
    run_id: str
    status: RunStatus
    error: str | None = None
    result: SearchResponse | None = None


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

    @model_validator(mode="after")
    def _validate(self) -> "InternalEnrichRequest":
        if self.fields != "email":
            raise ValueError(
                "Versão atual do enriquecimento interno só suporta fields='email'."
            )
        if self.company_domain is not None:
            cleaned = _clean_internal_company_domain(self.company_domain)
            if not cleaned:
                raise ValueError(
                    "company_domain deve ser um domínio corporativo válido (ex.: empresa.com.br)."
                )
            self.company_domain = cleaned
        return self


class InternalEnrichSummary(BaseModel):
    requested_leads: int = 0
    enriched_leads: int = 0
    skipped_existing_email: int = 0
    failed_missing_domain: int = 0
    no_change: int = 0


class InternalEnrichResponse(BaseModel):
    status: str
    summary: InternalEnrichSummary
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
        result = _execute(request)
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
        return RunStateResponse(
            run_id=record.run_id,
            status=record.status,
            error=record.error,
            result=record.result,
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

        updates = _run_internal_enrichment(
            leads=selected,
            existing_company_emails=existing_company_emails,
            company_domains=company_domains,
        )

        counters = store.apply_internal_enrichment_updates(table_id, updates)
        summary = InternalEnrichSummary(
            requested_leads=len(selected),
            enriched_leads=counters["enriched"],
            skipped_existing_email=counters["skipped_existing_email"],
            failed_missing_domain=counters["failed_missing_domain"],
            no_change=counters["no_change"],
        )
        refreshed = store.get_table(table_id)
        leads = store.list_leads(table_id)
        return InternalEnrichResponse(
            status="completed",
            summary=summary,
            table=_table_to_payload(refreshed),
            leads=leads,
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
                    }
                )
                updates = _run_internal_enrichment(
                    leads=selected,
                    existing_company_emails=existing_company_emails,
                    company_domains=company_domains,
                    on_event=push,
                    cancel_check=cancel_event.is_set,
                )
                counters = store.apply_internal_enrichment_updates(
                    table_id, updates
                )
                refreshed = store.get_table(table_id)
                fresh_leads = store.list_leads(table_id)
                push(
                    {
                        "type": "done",
                        "summary": {
                            "requested_leads": len(selected),
                            "enriched_leads": counters["enriched"],
                            "skipped_existing_email": counters[
                                "skipped_existing_email"
                            ],
                            "failed_missing_domain": counters[
                                "failed_missing_domain"
                            ],
                            "no_change": counters["no_change"],
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


def _execute(request: SearchRequest) -> ProspectingResult:
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
    )


def _execute_in_background(app: FastAPI, record: RunRecord, request: SearchRequest) -> None:
    record.status = RunStatus.RUNNING
    get_run_registry(app).update(record)
    try:
        result = _execute(request)
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
