"""Bucket B: discover individual phone numbers from public SERPs.

Site harvesting (Bucket A) gives us the company's institutional phone.
For an individual lead's mobile we need a different signal: search
engines index personal pages, deck PDFs, event sign-ups, conference
attendee lists, podcast guest pages, GitHub READMEs — places where a
lead's own number frequently leaks. The provider:

1. Builds a small set of phone-oriented queries per lead
   (``"{nome} {empresa} celular"`` / ``"{nome} {empresa} whatsapp"``
   / ``site:linkedin.com {nome} telefone``…).
2. Fans them out across one or more :class:`SearchEngine` instances.
3. Pulls phone-shaped substrings out of every result's ``title``,
   ``snippet`` and ``url`` using the same regexes the website harvester
   uses (so behavior is consistent across buckets).
4. Returns :class:`PhoneCandidate` objects tagged with the search
   engine name + the SERP URL where the snippet came from.

Concurrency: queries inside one lookup are issued sequentially because
each ``SearchEngine.search()`` already does its own internal fan-out
and a single lead's queries are few (~3). Across leads the orchestrator
runs an outer thread pool, so the total budget is bounded.

Fail-soft: every engine that raises lands in ``self.errors``; the
lookup keeps going with the remaining engines. An empty result is the
worst-case return; the orchestrator never sees an exception.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from beautiful_linkedin.models import SearchResult
from beautiful_linkedin.search.search_engine import SearchEngine
from beautiful_linkedin.storage.phone_harvester import (
    _PHONE_REGEX,
    _digits_only,
    _is_plausible_phone,
)
from beautiful_linkedin.storage.phone_lookup import (
    LookupQuery,
    PhoneCandidate,
)


logger = logging.getLogger(__name__)


# Per-engine result cap. Snippets are short — going past 10 results per
# query rarely buys us more phones, but it does multiply rate-limit risk
# on free SERP providers.
_DEFAULT_RESULTS_PER_QUERY = 8


# Query templates. Each ``{name}`` and ``{company}`` is substituted in
# the order below. The set is intentionally broad — phone numbers leak
# in many places that don't overlap (a deck on Speakerdeck, a bio on
# about.me, a link tree on Linktree, a contact line on a personal
# GitHub page). One short list keeps the SERP rate low while still
# covering the most common sources of individual contact info.
_DEFAULT_QUERY_TEMPLATES: tuple[str, ...] = (
    # General: name + company + a phone-ish word
    '"{name}" "{company}" (telefone OR celular OR whatsapp)',
    '"{name}" "{company}" contato',
    '"{name}" "{company}" "+55"',
    '"{name}" {company} (mobile OR phone)',
    # Personal pages where users publish their own contact
    '"{name}" "{company}" site:linktr.ee',
    '"{name}" site:about.me',
    '"{name}" "{company}" site:linkedin.com/in',
    '"{name}" site:github.io contato',
    # Conference / deck pages — palestrantes leave direct contact
    '"{name}" site:speakerdeck.com',
    '"{name}" site:slideshare.net',
    # Lattes / academic profiles (no phone in public view but the
    # institutional page often has it, and lattes.cnpq.br entries
    # surface alongside currículo-style PDFs)
    '"{name}" "{company}" curriculo',
    # WhatsApp deep-link mining — these often surface direct mobiles
    '"{name}" "{company}" "wa.me"',
    '"{name}" "{company}" "api.whatsapp.com/send"',
)

# A lead's full name typed exactly into quotes makes the SERP much more
# specific, which dramatically cuts false positives ("Ana Silva 11
# 99999-9999" is much less likely to be a coincidental match than
# "Ana 11 99999-9999"). We don't substitute the email/linkedin into
# the query — they bias results toward profile pages without the phone.


@dataclass(frozen=True)
class _SnippetMatch:
    raw: str
    digits: str
    source_url: str
    engine: str
    has_name_proximity: bool


class SerpPhoneSearchProvider:
    """Bucket B provider: ``name+company → list[PhoneCandidate]`` via SERPs."""

    name = "serp"

    def __init__(
        self,
        *,
        engines: list[SearchEngine],
        max_results_per_query: int = _DEFAULT_RESULTS_PER_QUERY,
        query_templates: tuple[str, ...] | None = None,
        engine_labels: list[str] | None = None,
    ) -> None:
        if not engines:
            raise ValueError("SerpPhoneSearchProvider requires at least one engine")
        self._engines = engines
        self._engine_labels = engine_labels or [
            type(e).__name__ for e in engines
        ]
        self._templates = tuple(query_templates or _DEFAULT_QUERY_TEMPLATES)
        self._max_results = max(1, max_results_per_query)
        self.errors: list[str] = []

    def lookup(self, query: LookupQuery) -> list[PhoneCandidate]:
        full_name = (query.full_name or "").strip()
        company = (query.company_name or "").strip()
        if not full_name or not company:
            return []

        formatted_queries = [
            template.format(name=full_name, company=company)
            for template in self._templates
        ]
        # Dedup queries that collapse to the same string after
        # formatting (defensive: future templates may overlap).
        formatted_queries = list(dict.fromkeys(formatted_queries))

        seen_digits: set[str] = set()
        out: list[PhoneCandidate] = []
        for q in formatted_queries:
            for engine, label in zip(self._engines, self._engine_labels):
                results = self._search_safe(engine, label, q)
                for snippet in self._snippets_from(results, label):
                    for match in self._phones_in(snippet, full_name):
                        if match.digits in seen_digits:
                            continue
                        seen_digits.add(match.digits)
                        out.append(
                            PhoneCandidate(
                                raw=match.raw,
                                source=self.name,
                                source_url=match.source_url,
                                context=(
                                    "serp_name_proximity"
                                    if match.has_name_proximity
                                    else "serp"
                                ),
                                extra={"engine": match.engine, "query": q},
                            )
                        )
        return out

    def _search_safe(
        self, engine: SearchEngine, label: str, q: str
    ) -> list[SearchResult]:
        try:
            return engine.search(q, max_results=self._max_results) or []
        except Exception as exc:
            self.errors.append(f"{label}:{type(exc).__name__}:{exc}")
            logger.debug("SERP engine %s falhou em '%s': %s", label, q, exc)
            return []

    def _snippets_from(
        self, results: list[SearchResult], engine_label: str
    ) -> list[tuple[str, str, str]]:
        """Flatten SERP results into ``(text_blob, source_url, engine)``."""
        out: list[tuple[str, str, str]] = []
        for r in results:
            blob = " ".join(
                part for part in (r.title or "", r.snippet or "", r.url or "") if part
            )
            if blob.strip():
                out.append((blob, r.url or "", engine_label))
        return out

    def _phones_in(
        self,
        snippet: tuple[str, str, str],
        full_name: str,
    ) -> list[_SnippetMatch]:
        text, source_url, engine_label = snippet
        found: list[_SnippetMatch] = []
        name_lower = full_name.lower()
        text_lower = text.lower()
        for match in _PHONE_REGEX.finditer(text):
            raw = match.group(1).strip()
            digits = _digits_only(raw)
            if not _is_plausible_phone(digits):
                continue
            # Tie-breaker: did the full name appear within ~60 chars of
            # the matched phone? Snippets are short, so 60 chars
            # corresponds roughly to "in the same sentence". When true,
            # the score function will treat the candidate as much
            # stronger evidence that this number belongs to the lead.
            span_start = max(0, match.start() - 60)
            span_end = min(len(text), match.end() + 60)
            window = text_lower[span_start:span_end]
            has_proximity = name_lower in window or _name_tokens_in_window(
                name_lower, window
            )
            found.append(
                _SnippetMatch(
                    raw=raw,
                    digits=digits,
                    source_url=source_url,
                    engine=engine_label,
                    has_name_proximity=has_proximity,
                )
            )
        return found


def _name_tokens_in_window(name_lower: str, window: str) -> bool:
    """First + last token of the name both appear in the window.

    Catches "Ana ... Silva ... 11 99999-9999" cases where the snippet
    paraphrases the name without the exact full string.
    """
    tokens = [t for t in name_lower.split() if t]
    if len(tokens) < 2:
        return False
    first, last = tokens[0], tokens[-1]
    return first in window and last in window
