"""LLM-based fallback extractor for the LinkedIn People tab HTML.

When the deterministic heuristics in ``linkedin_playwright`` fail to recover
``full_name`` / ``headline`` / ``location`` from a profile card (LinkedIn
shipped a fresh class-name shuffle, the markup is layered behind shadow DOM,
etc.), this module re-extracts the profiles from the raw HTML using
``crawl4ai`` plus an LLM via ``LLMExtractionStrategy``.

The fallback is opt-in. It runs only when:

- the heuristic returned zero records, or
- a configurable share of the heuristic records lack name **or** headline,

and an LLM API token is configured. If ``crawl4ai`` is not installed, or if
the LLM call raises, the function logs and returns an empty list — callers
fall back to whatever the heuristic produced.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from beautiful_linkedin.providers.linkedin_playwright import (
    PlaywrightProfileRecord,
    _canonical_profile_url,
)

logger = logging.getLogger(__name__)


CrawlerFactory = Callable[[], Any]


_LLM_INSTRUCTION = (
    "You are looking at the rendered HTML of the LinkedIn 'People' tab of a "
    "company page. Extract every visible employee profile card. For each card "
    "return: full_name (the person's name), headline (their current job title "
    "or summary, exactly as shown), location (city/region, when shown), and "
    "profile_url (absolute https://www.linkedin.com/in/<handle>/ URL). "
    "Skip ads, suggested companies, and 'see more' rows. Return a JSON array."
)

_PROFILE_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "full_name": {"type": ["string", "null"]},
            "headline": {"type": ["string", "null"]},
            "location": {"type": ["string", "null"]},
            "profile_url": {"type": "string"},
        },
        "required": ["profile_url"],
    },
}


@dataclass(frozen=True)
class LLMExtractionConfig:
    model: str = "openai/gpt-4o-mini"
    api_token: str | None = None
    timeout_seconds: float = 45.0
    incomplete_ratio_threshold: float = 0.5


def should_run_llm_fallback(
    records: list[PlaywrightProfileRecord],
    *,
    threshold: float = 0.5,
) -> bool:
    """Decide whether the heuristic output looks weak enough to retry with LLM."""
    if not records:
        return True
    incomplete = sum(1 for r in records if not (r.full_name and r.headline))
    return (incomplete / len(records)) >= threshold


def extract_records_from_html(
    html: str,
    *,
    config: LLMExtractionConfig,
    crawler_factory: CrawlerFactory | None = None,
) -> list[PlaywrightProfileRecord]:
    """Re-extract profile records from the rendered People-tab HTML using an LLM.

    Returns an empty list on any failure (missing dependency, missing token,
    LLM error, parse error). The caller decides whether to merge or fall back.
    """
    if not html or not html.strip():
        return []
    if not config.api_token:
        logger.info(
            "LLM extractor: API token ausente para %s. Pulando fallback.", config.model
        )
        return []

    factory = crawler_factory or _default_factory(config)
    if factory is None:
        return []

    try:
        raw = _run_async(_extract_async(factory, html, config))
    except Exception as exc:
        logger.warning("LLM extractor falhou: %s", exc)
        return []

    return _parse_records(raw)


async def _extract_async(
    factory: CrawlerFactory,
    html: str,
    config: LLMExtractionConfig,
) -> str:
    crawler = factory()
    async with crawler:
        coro = crawler.arun(url=f"raw:{html}")
        result = await asyncio.wait_for(coro, timeout=config.timeout_seconds)
    return getattr(result, "extracted_content", "") or ""


def _default_factory(config: LLMExtractionConfig) -> CrawlerFactory | None:
    try:
        from crawl4ai import (  # type: ignore[import-not-found]
            AsyncWebCrawler,
            BrowserConfig,
            CrawlerRunConfig,
            LLMConfig,
        )
        from crawl4ai.extraction_strategy import (  # type: ignore[import-not-found]
            LLMExtractionStrategy,
        )
    except Exception as exc:
        logger.warning(
            "crawl4ai indisponível para fallback LLM: %s. "
            "Instale com `pip install beautiful-linkedin[crawl4ai]`.",
            exc,
        )
        return None

    strategy = LLMExtractionStrategy(
        llm_config=LLMConfig(provider=config.model, api_token=config.api_token),
        schema=_PROFILE_SCHEMA,
        extraction_type="schema",
        instruction=_LLM_INSTRUCTION,
        input_format="html",
        apply_chunking=False,
    )
    run_config = CrawlerRunConfig(extraction_strategy=strategy)

    class _BoundCrawler:
        def __init__(self) -> None:
            self._crawler = AsyncWebCrawler(config=BrowserConfig(headless=True))

        async def __aenter__(self) -> "_BoundCrawler":
            await self._crawler.__aenter__()
            return self

        async def __aexit__(self, exc_type, exc, tb) -> Any:
            return await self._crawler.__aexit__(exc_type, exc, tb)

        async def arun(self, url: str) -> Any:
            return await self._crawler.arun(url=url, config=run_config)

    return _BoundCrawler


def _parse_records(raw: str) -> list[PlaywrightProfileRecord]:
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("LLM extractor: JSON inválido. %s", exc)
        return []

    items: list[Any]
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = payload.get("items") or payload.get("profiles") or []
    else:
        items = []

    out: list[PlaywrightProfileRecord] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        url = _clean_str(item.get("profile_url"))
        if not url or "/in/" not in url:
            continue
        canonical = _canonical_profile_url(url)
        if canonical in seen:
            continue
        seen.add(canonical)
        out.append(
            PlaywrightProfileRecord(
                full_name=_clean_str(item.get("full_name")),
                headline=_clean_str(item.get("headline")),
                location=_clean_str(item.get("location")),
                profile_url=canonical,
            )
        )
    return out


def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _run_async(coro: Awaitable[Any]) -> Any:
    try:
        return asyncio.run(coro)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()


def merge_records(
    primary: list[PlaywrightProfileRecord],
    secondary: list[PlaywrightProfileRecord],
) -> list[PlaywrightProfileRecord]:
    """Merge LLM records into the heuristic output, filling missing fields.

    Records are matched by canonical profile URL. New URLs from the secondary
    source are appended at the end so order from the heuristic (which reflects
    page order) is preserved.
    """
    merged: list[PlaywrightProfileRecord] = []
    by_url: dict[str, int] = {}
    for record in primary:
        by_url[record.profile_url] = len(merged)
        merged.append(record)

    for extra in secondary:
        idx = by_url.get(extra.profile_url)
        if idx is None:
            by_url[extra.profile_url] = len(merged)
            merged.append(extra)
            continue
        current = merged[idx]
        merged[idx] = PlaywrightProfileRecord(
            full_name=current.full_name or extra.full_name,
            headline=current.headline or extra.headline,
            location=current.location or extra.location,
            profile_url=current.profile_url,
        )
    return merged
