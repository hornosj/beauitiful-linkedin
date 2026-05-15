from __future__ import annotations

import logging
import math
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
from dataclasses import dataclass
from dataclasses import replace
from inspect import signature
from pathlib import Path

from rich.console import Console

from beautiful_linkedin.cache.lead_history import LeadConsultationHistory
from beautiful_linkedin.cache.sqlite_cache import SqliteJsonCache
from beautiful_linkedin.config import Settings, load_settings
from beautiful_linkedin.export.csv_exporter import export_csv
from beautiful_linkedin.export.xlsx_exporter import export_xlsx
from beautiful_linkedin.models import (
    CompanyInput,
    Lead,
    ProspectingResult,
    ProspectingSummary,
    ProviderDiagnostic,
)
from beautiful_linkedin.processing.deduplicator import deduplicate_leads, lead_dedupe_key
from beautiful_linkedin.processing.lead_filter import LeadFilter, apply_lead_filter
from beautiful_linkedin.scraping.company_site_scraper import CompanySiteScraper
from beautiful_linkedin.providers.factory import build_lead_providers
from beautiful_linkedin.providers.lead_provider import LeadProvider
from beautiful_linkedin.providers.linkedin_playwright import (
    LinkedInPlaywrightProvider,
    PlaywrightCollectorOptions,
)
from beautiful_linkedin.search.factory import build_search_engine
from beautiful_linkedin.search.search_engine import SearchEngine
from beautiful_linkedin.ui.progress import create_progress
from beautiful_linkedin.ui.tables import source_counts

logger = logging.getLogger(__name__)
MAX_API_ROUNDS = 5


def run_prospecting(
    companies: list[CompanyInput],
    max_results: int,
    official_sites: bool,
    include_uncertain: bool,
    output_path: str | Path,
    output_format: str | None = None,
    console: Console | None = None,
    search_engine: SearchEngine | None = None,
    search_engines: list[str] | None = None,
    search_depth: str = "standard",
    lead_providers: list[str] | None = None,
    parallelism: int = 6,
    use_cache: bool = True,
    web_query_limit: int | None = None,
    provider_timeout_seconds: float | None = None,
    linkedin_cookie: str | None = None,
    linkedin_cookie_browser: str | None = None,
    lead_filter: LeadFilter | None = None,
    playwright_headless: bool | None = None,
    settings: Settings | None = None,
    track_lead_history: bool = True,
) -> ProspectingResult:
    console = console or Console()
    settings = settings or load_settings()
    if linkedin_cookie is not None or linkedin_cookie_browser is not None:
        settings = replace(
            settings,
            linkedin_li_at_cookie=linkedin_cookie or settings.linkedin_li_at_cookie,
            linkedin_cookie_browser=linkedin_cookie_browser or settings.linkedin_cookie_browser,
        )
    selected_providers = lead_providers or ["auto"]
    needs_web_search = _needs_web_search_provider(selected_providers)
    engine = (
        search_engine
        if search_engine is not None
        else build_search_engine(settings, search_engines)
        if needs_web_search
        else None
    )
    cache = (
        SqliteJsonCache(settings.cache_path, ttl_seconds=settings.cache_ttl_seconds)
        if use_cache
        else None
    )
    providers = build_lead_providers(
        settings=settings,
        search_engine=engine,
        provider_names=lead_providers,
        search_parallelism=max(1, min(parallelism, 4)),
        web_query_limit=web_query_limit or settings.web_query_limit,
        cache=cache,
    )
    if playwright_headless is not None:
        for provider in providers:
            if isinstance(provider, LinkedInPlaywrightProvider):
                current = provider.options
                provider.options = PlaywrightCollectorOptions(
                    headless=playwright_headless,
                    nav_timeout_ms=current.nav_timeout_ms,
                    max_scrolls=current.max_scrolls,
                    min_delay_seconds=current.min_delay_seconds,
                    max_delay_seconds=current.max_delay_seconds,
                    user_data_dir=current.user_data_dir,
                    extra_browser_args=list(current.extra_browser_args),
                )
    scraper = CompanySiteScraper(
        timeout_seconds=settings.company_site_timeout_seconds,
        max_pages=settings.company_site_max_pages,
    )

    raw_leads: list[Lead] = []
    diagnostics: list[ProviderDiagnostic] = []
    total_provider_tasks = len(companies) * (
        _provider_task_budget(providers) + (1 if official_sites else 0)
    )
    provider_tasks_done = 0

    with create_progress() as progress:
        companies_task = progress.add_task("empresas processadas", total=len(companies))
        providers_task = progress.add_task(
            "buscas em fontes concluídas",
            total=max(total_provider_tasks, 1),
        )
        raw_task = progress.add_task("leads brutos encontrados: 0", total=None)
        dedupe_task = progress.add_task("leads deduplicados: 0", total=None)

        for company in companies:
            try:
                def on_provider_batch(leads: list[Lead]) -> None:
                    nonlocal provider_tasks_done
                    raw_leads.extend(leads)
                    provider_tasks_done += 1
                    progress.update(providers_task, completed=provider_tasks_done)
                    progress.update(raw_task, description=f"leads brutos encontrados: {len(raw_leads)}")
                    progress.update(
                        dedupe_task,
                        description=f"leads deduplicados: {len(deduplicate_leads(raw_leads))}",
                    )

                _run_company_providers(
                    company=company,
                    providers=providers,
                    scraper=scraper,
                    max_results=max_results,
                    include_uncertain=include_uncertain,
                    search_depth=search_depth,
                    official_sites=official_sites,
                    parallelism=parallelism,
                    provider_timeout_seconds=provider_timeout_seconds
                    or settings.provider_timeout_seconds,
                    on_batch=on_provider_batch,
                    diagnostics_sink=diagnostics,
                )

            except Exception as exc:
                logger.warning("Empresa falhou, mas a execução vai continuar: %s. %s", company.company_name, exc)
            finally:
                progress.advance(companies_task)
        progress.update(providers_task, completed=max(total_provider_tasks, provider_tasks_done))

    deduped_leads = deduplicate_leads(raw_leads)
    filter_outcome = apply_lead_filter(deduped_leads, lead_filter)
    final_leads = filter_outcome.leads
    if track_lead_history:
        final_leads = LeadConsultationHistory(settings.cache_path).annotate_and_record(
            final_leads
        )
    previously_consulted_count = sum(
        1 for lead in final_leads if lead.consultation_note
    )
    maybe_incorrect_count = sum(
        1 for lead in final_leads if lead.validation_status == "maybe_incorrect"
    )
    if lead_filter is not None and not lead_filter.is_empty():
        logger.info(
            "Filtros pós-coleta aplicados: %d mantidos, %d descartados (motivos=%s)",
            filter_outcome.stats.kept,
            filter_outcome.stats.dropped_total,
            filter_outcome.stats.dropped_by_reason,
        )
    output_file = export_leads(final_leads, output_path, output_format)
    top_sources = source_counts(final_leads)
    for source_type in (_provider_source_type(provider) for provider in providers):
        top_sources.setdefault(source_type, 0)
    if official_sites:
        top_sources.setdefault("company_site", 0)

    summary = ProspectingSummary(
        total_companies_processed=len(companies),
        total_raw_leads=len(raw_leads),
        total_deduplicated_leads=len(final_leads),
        total_previously_consulted_leads=previously_consulted_count,
        total_maybe_incorrect_leads=maybe_incorrect_count,
        output_file=str(output_file),
        top_sources=top_sources,
    )
    return ProspectingResult(
        leads=final_leads,
        summary=summary,
        provider_diagnostics=diagnostics,
    )


def _run_company_providers(
    company: CompanyInput,
    providers: list[LeadProvider],
    scraper: CompanySiteScraper,
    max_results: int,
    include_uncertain: bool,
    search_depth: str,
    official_sites: bool,
    parallelism: int,
    provider_timeout_seconds: float = 35.0,
    on_batch: Callable[[list[Lead]], None] | None = None,
    diagnostics_sink: list[ProviderDiagnostic] | None = None,
) -> list[list[Lead]]:
    results: list[list[Lead]] = []
    accepted_leads: list[Lead] = []
    api_providers = [provider for provider in providers if _is_structured_api_provider(provider)]
    fallback_providers = [
        provider for provider in providers if not _is_structured_api_provider(provider)
    ]

    if api_providers:
        per_provider_limit = max(1, math.ceil(max_results / len(api_providers)))
        for round_index in range(MAX_API_ROUNDS):
            offset = round_index * per_provider_limit
            round_batches = _run_provider_jobs(
                jobs=[
                    _ProviderJob(provider=provider, max_results=per_provider_limit, offset=offset)
                    for provider in api_providers
                ],
                company=company,
                include_uncertain=include_uncertain,
                search_depth=search_depth,
                parallelism=parallelism,
                provider_timeout_seconds=provider_timeout_seconds,
            )
            if diagnostics_sink is not None:
                for batch in round_batches:
                    if batch.diagnostic is not None:
                        diagnostics_sink.append(batch.diagnostic)
            remaining = max_results - len(accepted_leads)
            new_batches = _extract_balanced_new_unique_batches(
                accepted_leads,
                round_batches,
                limit=remaining,
            )
            round_new_count = sum(len(batch.leads) for batch in new_batches)
            for batch in new_batches:
                accepted_leads.extend(batch.leads)
                results.append(batch.leads)
                if on_batch:
                    on_batch(batch.leads)
            if len(accepted_leads) >= max_results or round_new_count == 0:
                break

    if fallback_providers and len(accepted_leads) < max_results:
        fallback_jobs = [
            _ProviderJob(provider=provider, max_results=max_results, offset=0)
            for provider in fallback_providers
        ]
        fallback_batches = _run_provider_jobs(
            jobs=fallback_jobs,
            company=company,
            include_uncertain=include_uncertain,
            search_depth=search_depth,
            parallelism=parallelism,
            provider_timeout_seconds=provider_timeout_seconds,
        )
        if diagnostics_sink is not None:
            for batch in fallback_batches:
                if batch.diagnostic is not None:
                    diagnostics_sink.append(batch.diagnostic)
        new_batches = _extract_balanced_new_unique_batches(
            accepted_leads,
            fallback_batches,
            limit=max_results - len(accepted_leads),
        )
        for batch in new_batches:
            accepted_leads.extend(batch.leads)
            results.append(batch.leads)
            if on_batch:
                on_batch(batch.leads)

    if official_sites and scraper is not None and len(accepted_leads) < max_results:
        site_batches = _run_callable_jobs(
            jobs=[lambda: scraper.scrape_company(company, include_uncertain)],
            parallelism=1,
            provider_timeout_seconds=provider_timeout_seconds,
        )
        for batch in site_batches:
            new_batch, _duplicates = _extract_new_unique(
                accepted_leads,
                batch,
                limit=max_results - len(accepted_leads),
            )
            accepted_leads.extend(new_batch)
            results.append(new_batch)
            if on_batch:
                on_batch(new_batch)

    return results


@dataclass(frozen=True)
class _ProviderJob:
    provider: LeadProvider
    max_results: int
    offset: int


@dataclass(frozen=True)
class _ProviderBatch:
    provider_name: str
    leads: list[Lead]
    diagnostic: ProviderDiagnostic | None = None


def _run_provider_jobs(
    jobs: list[_ProviderJob],
    company: CompanyInput,
    include_uncertain: bool,
    search_depth: str,
    parallelism: int,
    provider_timeout_seconds: float,
) -> list[_ProviderBatch]:
    if not jobs:
        return []

    results: list[_ProviderBatch | None] = [None] * len(jobs)
    executor = ThreadPoolExecutor(max_workers=max(1, min(parallelism, len(jobs))))
    futures = {
        executor.submit(
            _call_provider,
            job.provider,
            company,
            job.max_results,
            include_uncertain,
            search_depth,
            job.offset,
        ): (index, job)
        for index, job in enumerate(jobs)
    }

    try:
        for future in as_completed(futures, timeout=provider_timeout_seconds):
            index, job = futures[future]
            try:
                leads = future.result(timeout=0)
            except Exception as exc:
                logger.warning("Provider %s falhou, mas a execução vai continuar. %s", job.provider.name, exc)
                leads = []
            results[index] = _ProviderBatch(
                job.provider.name, leads, getattr(job.provider, "last_diagnostic", None)
            )
    except TimeoutError:
        logger.warning(
            "Timeout de provider após %.1fs. Continuando com as fontes concluídas.",
            provider_timeout_seconds,
        )
        for future, (index, job) in futures.items():
            if not future.done():
                future.cancel()
                timeout_diag = ProviderDiagnostic(
                    provider=job.provider.name,
                    company_name=company.company_name,
                    last_error=f"timeout após {provider_timeout_seconds:.1f}s",
                )
                results[index] = _ProviderBatch(job.provider.name, [], timeout_diag)
                continue
            if not future.cancelled():
                try:
                    leads = future.result(timeout=0)
                except Exception as exc:
                    logger.warning("Provider %s falhou, mas a execução vai continuar. %s", job.provider.name, exc)
                    leads = []
                results[index] = _ProviderBatch(
                    job.provider.name, leads, getattr(job.provider, "last_diagnostic", None)
                )
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    return [
        batch if batch is not None else _ProviderBatch(jobs[index].provider.name, [])
        for index, batch in enumerate(results)
    ]


def _run_callable_jobs(
    jobs: list[Callable[[], list[Lead]]],
    parallelism: int,
    provider_timeout_seconds: float,
) -> list[list[Lead]]:
    if not jobs:
        return []

    results: list[list[Lead]] = []
    executor = ThreadPoolExecutor(max_workers=max(1, min(parallelism, len(jobs))))
    futures = [executor.submit(job) for job in jobs]

    try:
        for future in as_completed(futures, timeout=provider_timeout_seconds):
            try:
                batch = future.result(timeout=0)
            except Exception as exc:
                logger.warning("Provider falhou, mas a execução vai continuar. %s", exc)
                batch = []
            results.append(batch)
    except TimeoutError:
        logger.warning(
            "Timeout de provider após %.1fs. Continuando com as fontes concluídas.",
            provider_timeout_seconds,
        )
        for future in futures:
            if not future.done():
                future.cancel()
                batch = []
                results.append(batch)
                continue
            if not future.cancelled():
                try:
                    batch = future.result(timeout=0)
                except Exception as exc:
                    logger.warning("Provider falhou, mas a execução vai continuar. %s", exc)
                    batch = []
                results.append(batch)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    return results


def _call_provider(
    provider: LeadProvider,
    company: CompanyInput,
    max_results: int,
    include_uncertain: bool,
    search_depth: str,
    offset: int,
) -> list[Lead]:
    parameters = signature(provider.find_leads).parameters
    if "offset" in parameters:
        return provider.find_leads(
            company,
            max_results,
            include_uncertain,
            search_depth,
            offset=offset,
        )
    return provider.find_leads(company, max_results, include_uncertain, search_depth)


def _extract_new_unique(
    accepted_leads: list[Lead],
    candidate_leads: list[Lead],
    limit: int | None = None,
) -> tuple[list[Lead], int]:
    seen_keys = {
        key
        for key in (lead_dedupe_key(lead) for lead in accepted_leads)
        if key is not None
    }
    new_leads: list[Lead] = []
    duplicates = 0
    for lead in candidate_leads:
        if limit is not None and len(new_leads) >= limit:
            break
        key = lead_dedupe_key(lead)
        if key is not None and key in seen_keys:
            duplicates += 1
            continue
        new_leads.append(lead)
        if key is not None:
            seen_keys.add(key)
    return new_leads, duplicates


def _extract_balanced_new_unique_batches(
    accepted_leads: list[Lead],
    provider_batches: list[_ProviderBatch],
    limit: int,
) -> list[_ProviderBatch]:
    if limit <= 0:
        return []

    seen_keys = {
        key
        for key in (lead_dedupe_key(lead) for lead in accepted_leads)
        if key is not None
    }
    provider_queues: list[tuple[str, list[Lead]]] = []
    for provider_batch in provider_batches:
        queue: list[Lead] = []
        for lead in provider_batch.leads:
            key = lead_dedupe_key(lead)
            if key is not None and key in seen_keys:
                continue
            queue.append(lead)
            if key is not None:
                seen_keys.add(key)
        provider_queues.append((provider_batch.provider_name, queue))

    balanced: dict[str, list[Lead]] = {
        provider_name: [] for provider_name, _queue in provider_queues
    }
    accepted_count = 0
    while accepted_count < limit and any(queue for _provider_name, queue in provider_queues):
        for provider_name, queue in provider_queues:
            if accepted_count >= limit:
                break
            if not queue:
                continue
            balanced[provider_name].append(queue.pop(0))
            accepted_count += 1

    return [
        _ProviderBatch(provider_name, leads)
        for provider_name, leads in balanced.items()
        if leads
    ]


def _is_structured_api_provider(provider: LeadProvider) -> bool:
    return provider.name in {
        "pdl",
        "coresignal",
        "apollo",
        "lusha",
        "apify_linkedin",
        "linkedin_cookie",
        "linkedin_sales_navigator",
        "linkedin_playwright",
    }


def _provider_task_budget(providers: list[LeadProvider]) -> int:
    api_count = sum(1 for provider in providers if _is_structured_api_provider(provider))
    fallback_count = len(providers) - api_count
    return api_count * MAX_API_ROUNDS + fallback_count


def _provider_source_type(provider: LeadProvider) -> str:
    source_by_provider = {
        "pdl": "api_pdl",
        "coresignal": "api_coresignal",
        "apollo": "api_apollo",
        "lusha": "api_lusha",
        "apify_linkedin": "api_apify_linkedin",
        "linkedin_cookie": "linkedin_cookie",
        "linkedin_sales_navigator": "linkedin_sales_navigator",
        "linkedin_playwright": "linkedin_playwright",
        "web_search": "web_search",
        "public_directories": "public_dir",
        "common_crawl": "common_crawl",
    }
    return source_by_provider.get(provider.name, provider.name)


def _needs_web_search_provider(provider_names: list[str]) -> bool:
    normalized = {name.strip().lower() for name in provider_names if name.strip()}
    return bool(
        {"auto", "web", "search", "public_search"}.intersection(normalized)
    )


def export_leads(
    leads: list[Lead],
    output_path: str | Path,
    output_format: str | None = None,
) -> Path:
    path = Path(output_path)
    selected_format = (output_format or path.suffix.lstrip(".") or "csv").lower()

    if selected_format == "xlsx":
        if path.suffix.lower() != ".xlsx":
            path = path.with_suffix(".xlsx")
        return export_xlsx(leads, path)

    if path.suffix.lower() != ".csv":
        path = path.with_suffix(".csv")
    return export_csv(leads, path)
