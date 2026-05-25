"""In-process phone enrichment — no paid APIs.

Buckets A + B of the proposal in ``docs/phone-discovery-architecture.md``.
For each lead:

1. **Harvest** phones from the company's own website
   (:class:`PhoneNumberHarvester`) — Bucket A.
2. **Lookup** phones via every configured
   :class:`PhoneLookupProvider` (SERP search, future paid bots,
   etc.) — Bucket B / C plug-ins.
3. **Validate + boost** each candidate via :class:`PhoneValidator`
   (phonenumbers offline) plus optional :class:`WhatsAppNumberChecker`
   and :class:`HlrProbeProvider`.
4. **Score** by source provenance + number type + active-presence
   evidence. The best candidate wins, with the full discard trail
   recorded.

Same cross-layer contract as the e-mail pipeline:

- The primary ``lead.phone`` is never overwritten.
- Cross-provider trail is delegated to ``_merge_phone_verification`` in
  :mod:`saved_leads`, so internal + paid + future trails collapse into
  the same "verified by" badge.
- External I/O is injected via factories (``harvest_fn``,
  ``lookup_providers``, ``wa_checker``, ``hlr_probe``) so the orchestrator
  has no transitive network imports — tests stay fully offline.
"""

from __future__ import annotations

import logging
import unicodedata
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from beautiful_linkedin.models import Lead
from beautiful_linkedin.storage.phone_harvester import (
    HarvestedPhone,
    PhoneNumberHarvester,
)
from beautiful_linkedin.storage.phone_hlr import (
    HlrProbeProvider,
    HlrResult,
    HlrStatus,
    NoopHlrProbe,
)
from beautiful_linkedin.storage.phone_lookup import (
    LookupQuery,
    PhoneCandidate,
    PhoneLookupProvider,
)
from beautiful_linkedin.storage.phone_validation import (
    PhoneType,
    PhoneValidationResult,
    PhoneValidationStatus,
    PhoneValidator,
)
from beautiful_linkedin.storage.whatsapp_checker import (
    WhatsAppCheck,
    WhatsAppNumberChecker,
    WhatsAppStatus,
)


logger = logging.getLogger(__name__)


# Score weights. All sum to <= 100 after clamping; the calibration aims
# to land an individual lead's mobile from a SERP snippet that mentions
# their name, validated by WhatsApp, at the top of the [0, 100] range
# — that's the strongest evidence the offline path can produce.
_CONTEXT_BOOST: dict[str, int] = {
    # Highest individual signal: lead self-published via LinkedIn's
    # Contact Info section (consent-based).
    "linkedin_self_published": 40,
    # PDF surfaces where the lead's name appears on the same page
    # as the phone — usually a deck/proposal/cv they wrote.
    "pdf_name_proximity": 38,
    # Bucket B (lead-specific external lookups)
    "serp_name_proximity": 35,
    "serp": 15,
    "pdf": 10,                  # phone found in a PDF but name was elsewhere
    "lookup": 20,
    "telegram_bot_name_match": 35,
    # Receita Federal — institutional phone, high confidence as a
    # company line but not individual.
    "receita_cnpj": 22,
    # Bucket A (company site) — strongest contexts first
    "tel": 25,
    "whatsapp": 25,
    "jsonld": 20,
    "text": 0,
}
_TYPE_BOOST: dict[PhoneType, int] = {
    PhoneType.MOBILE: 15,
    PhoneType.FIXED_OR_MOBILE: 10,
    PhoneType.VOIP: 5,
    PhoneType.FIXED: 5,
    PhoneType.UNKNOWN: 0,
    PhoneType.PAGER: -10,
    PhoneType.TOLL_FREE: -50,
}
# Active-presence boosts. WhatsApp ACTIVE is grounded in a network
# fetch so it's worth more than HLR REACHABLE which depends on the
# carrier honoring the probe — but neither is required.
_WHATSAPP_ACTIVE_BOOST = 20
_HLR_REACHABLE_BOOST = 15
_HLR_ABSENT_PENALTY = -40  # number not assigned at all → drop hard


@dataclass
class PhoneEnrichmentUpdate:
    """Outcome of trying to enrich a single lead with a phone."""

    phone: str | None = None
    national: str | None = None
    phone_type: str | None = None
    country: str | None = None
    carrier: str | None = None
    region: str | None = None
    validation_status: str | None = None
    confidence: int = 0
    source: str = "internal"
    source_url: str | None = None
    skipped_existing_phone: bool = False
    failure_reason: str | None = None
    discarded_candidates: list[str] = field(default_factory=list)
    whatsapp_status: str | None = None
    hlr_status: str | None = None


@dataclass(frozen=True)
class _RawCandidate:
    """Unified shape that the scoring layer consumes.

    Both harvesting (Bucket A) and lookup providers (Bucket B+) project
    onto this so the score function has one input format.
    """

    raw: str
    source: str               # "internal" (site harvester) | "serp" | ...
    source_url: str | None
    context: str              # see _CONTEXT_BOOST keys
    extra: dict[str, str]


@dataclass(frozen=True)
class _ScoredCandidate:
    e164: str
    national: str
    score: int
    validation: PhoneValidationResult
    raw_candidate: _RawCandidate
    whatsapp: WhatsAppCheck | None
    hlr: HlrResult | None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class InternalPhoneEnrichmentService:
    """End-to-end phone enrichment: validate → score → pick best.

    Receives raw candidates pre-collected by the orchestrator (so the
    expensive I/O is shared across leads of the same company) and
    runs them through the offline validator plus the optional
    WhatsApp / HLR probes.
    """

    def __init__(
        self,
        *,
        validator: PhoneValidator | None = None,
        wa_checker: WhatsAppNumberChecker | None = None,
        hlr_probe: HlrProbeProvider | None = None,
    ) -> None:
        self._validator = validator or PhoneValidator()
        self._wa_checker = wa_checker
        self._hlr_probe = hlr_probe or NoopHlrProbe()

    def enrich_lead(
        self,
        lead: Lead,
        *,
        harvested_phones: list[HarvestedPhone] | None = None,
        lookup_candidates: list[PhoneCandidate] | None = None,
    ) -> PhoneEnrichmentUpdate:
        if lead.phone and lead.phone.strip():
            return PhoneEnrichmentUpdate(
                skipped_existing_phone=True,
                failure_reason="existing_phone",
            )

        raw_candidates = list(_iter_raw_candidates(harvested_phones, lookup_candidates))
        if not raw_candidates:
            return PhoneEnrichmentUpdate(failure_reason="no_candidate")

        scored: list[_ScoredCandidate] = []
        discarded: list[str] = []
        seen_e164: set[str] = set()

        for cand in raw_candidates:
            validation = self._validator.validate(cand.raw)
            if validation.status in {
                PhoneValidationStatus.INVALID,
                PhoneValidationStatus.RISKY,
            }:
                discarded.append(cand.raw)
                continue
            e164 = validation.e164 or cand.raw
            if e164 in seen_e164:
                continue
            seen_e164.add(e164)

            wa_result = self._check_whatsapp(e164)
            hlr_result = self._check_hlr(e164)
            if hlr_result and hlr_result.status == HlrStatus.ABSENT:
                # Carrier says this number was never assigned. No matter
                # how good the source was, this is a dead end.
                discarded.append(cand.raw)
                continue

            score = _score(cand, validation, wa_result, hlr_result)
            scored.append(
                _ScoredCandidate(
                    e164=e164,
                    national=validation.national or cand.raw,
                    score=score,
                    validation=validation,
                    raw_candidate=cand,
                    whatsapp=wa_result,
                    hlr=hlr_result,
                )
            )

        if not scored:
            return PhoneEnrichmentUpdate(
                failure_reason="no_candidate",
                discarded_candidates=discarded,
            )

        scored.sort(key=lambda c: c.score, reverse=True)
        best = scored[0]
        return PhoneEnrichmentUpdate(
            phone=best.e164,
            national=best.national,
            phone_type=best.validation.type.value,
            country=best.validation.country,
            carrier=best.validation.carrier,
            region=best.validation.region,
            validation_status=best.validation.status.value,
            confidence=best.score,
            source=best.raw_candidate.source,
            source_url=best.raw_candidate.source_url,
            discarded_candidates=discarded,
            whatsapp_status=best.whatsapp.status.value if best.whatsapp else None,
            hlr_status=best.hlr.status.value if best.hlr else None,
        )

    def _check_whatsapp(self, e164: str) -> WhatsAppCheck | None:
        if self._wa_checker is None:
            return None
        try:
            return self._wa_checker.check(e164)
        except Exception as exc:
            logger.debug("wa.me probe %s falhou: %s", e164, exc)
            return None

    def _check_hlr(self, e164: str) -> HlrResult | None:
        if self._hlr_probe is None:
            return None
        try:
            return self._hlr_probe.probe(e164)
        except Exception as exc:
            logger.debug("hlr probe %s falhou: %s", e164, exc)
            return None


def _iter_raw_candidates(
    harvested: list[HarvestedPhone] | None,
    lookups: list[PhoneCandidate] | None,
):
    """Project both source shapes onto :class:`_RawCandidate`.

    Order matters: harvested goes first because the validator's seen
    set short-circuits later duplicates with the same E.164. We want
    the harvested entry to win the source tag for a number that
    coincidentally appears in both the site and a SERP — though score
    will recompute on context regardless.
    """
    for h in harvested or []:
        yield _RawCandidate(
            raw=h.raw,
            source="internal",
            source_url=h.source_url,
            context=h.context,
            extra={},
        )
    for c in lookups or []:
        yield _RawCandidate(
            raw=c.raw,
            source=c.source,
            source_url=c.source_url,
            context=c.context or "lookup",
            extra=dict(c.extra),
        )


def _score(
    candidate: _RawCandidate,
    validation: PhoneValidationResult,
    wa: WhatsAppCheck | None,
    hlr: HlrResult | None,
) -> int:
    base = 50 if validation.status == PhoneValidationStatus.VALID else 35
    base += _CONTEXT_BOOST.get(candidate.context, 0)
    base += _TYPE_BOOST.get(validation.type, 0)
    if wa is not None:
        if wa.status == WhatsAppStatus.ACTIVE:
            base += _WHATSAPP_ACTIVE_BOOST
        elif wa.status == WhatsAppStatus.INACTIVE:
            # WhatsApp says "no such account". For mobile numbers in BR
            # this is a meaningful negative signal — most working
            # mobiles have WhatsApp. We discount but don't drop.
            base -= 10
    if hlr is not None:
        if hlr.status == HlrStatus.REACHABLE:
            base += _HLR_REACHABLE_BOOST
        elif hlr.status == HlrStatus.ABSENT:
            base += _HLR_ABSENT_PENALTY  # in practice we've already dropped these
    return max(0, min(100, base))


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


HarvestFn = Callable[[str], list[HarvestedPhone]]
ProgressFn = Callable[[dict[str, Any]], None]


def _normalize_company_key(name: str | None) -> str:
    """Stable lookup key. Duplicated from internal_enrichment.py — see
    note in that module about avoiding circular imports across the
    storage submodule boundary."""
    if not name:
        return ""
    stripped = unicodedata.normalize("NFD", name)
    ascii_text = "".join(ch for ch in stripped if not unicodedata.combining(ch))
    cleaned = re.sub(r"[^A-Za-z0-9 ]+", " ", ascii_text).lower()
    tokens = [t for t in cleaned.split() if t]
    while tokens and tokens[-1] in _COMPANY_SUFFIX_NOISE:
        tokens.pop()
    return " ".join(tokens)


_COMPANY_SUFFIX_NOISE: frozenset[str] = frozenset(
    {
        "inc", "incorporated", "ltd", "limited", "ltda", "llc", "sa",
        "s", "co", "corp", "corporation", "company", "gmbh", "bv", "oy", "ag",
    }
)


class InternalPhoneEnrichmentOrchestrator:
    """Parallel harvest + lookup + validate, with progress events.

    Three phases:

    1. **harvesting** — fetch each unique company site once across the
       input leads. Bounded thread pool. Fail-soft per domain.
    2. **lookup** — for every lead, ask each configured
       :class:`PhoneLookupProvider` for candidates. Bounded thread
       pool, separate semaphore so SERP rate-limit doesn't dominate
       the harvest budget.
    3. **validating** — score every candidate (Bucket A + Bucket B)
       per lead, run optional WhatsApp / HLR probes, pick the best.

    ``on_event`` payload shapes:

    - ``{"type": "phase", "phase": "harvesting" | "lookup" | "validating" | "completed" | "cancelled"}``
    - ``{"type": "domain", "domain": ..., "harvested": N}``
    - ``{"type": "lookup", "lead_ref": ..., "provider": ..., "candidates": N}``
    - ``{"type": "lead", "lead_ref": ..., "status": ..., "phone": ..., "confidence": N, "whatsapp_status": ..., "hlr_status": ...}``
    - ``{"type": "progress", "completed": N, "total": M}``
    """

    def __init__(
        self,
        *,
        service: InternalPhoneEnrichmentService,
        harvest_fn: HarvestFn,
        lookup_providers: list[PhoneLookupProvider] | None = None,
        domain_concurrency: int = 6,
        lead_concurrency: int = 6,
        lookup_concurrency: int = 4,
        on_event: ProgressFn | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> None:
        self._service = service
        self._harvest_fn = harvest_fn
        self._lookup_providers = list(lookup_providers or [])
        self._domain_concurrency = max(1, domain_concurrency)
        self._lead_concurrency = max(1, lead_concurrency)
        self._lookup_concurrency = max(1, lookup_concurrency)
        self._on_event = on_event or (lambda event: None)
        self._cancel_check = cancel_check or (lambda: False)

    def run(
        self,
        leads: list[Lead],
        *,
        company_domains: dict[str, list[str]] | None = None,
    ) -> list[tuple[Lead, PhoneEnrichmentUpdate]]:
        if not leads:
            self._emit({"type": "phase", "phase": "completed"})
            return []

        company_domains = dict(company_domains or {})

        domains_per_lead: list[list[str]] = [
            _ranked_domains_for_lead(lead, company_domains) for lead in leads
        ]
        unique_domains: list[str] = sorted(
            {domain for domains in domains_per_lead for domain in domains}
        )

        # ---- Phase 1: harvest -------------------------------------------------
        self._emit({"type": "phase", "phase": "harvesting"})
        harvested_by_domain: dict[str, list[HarvestedPhone]] = {}
        with ThreadPoolExecutor(max_workers=self._domain_concurrency) as pool:
            future_to_domain = {
                pool.submit(self._harvest_safe, domain): domain
                for domain in unique_domains
            }
            for future in as_completed(future_to_domain):
                if self._cancel_check():
                    for pending in future_to_domain:
                        pending.cancel()
                    self._emit({"type": "phase", "phase": "cancelled"})
                    return []
                domain = future_to_domain[future]
                phones = future.result()
                harvested_by_domain[domain] = phones
                self._emit(
                    {
                        "type": "domain",
                        "domain": domain,
                        "harvested": len(phones),
                    }
                )

        # ---- Phase 2: lookup (per-lead external sources) ---------------------
        lookup_by_lead: dict[int, list[PhoneCandidate]] = {}
        if self._lookup_providers and not self._cancel_check():
            self._emit({"type": "phase", "phase": "lookup"})
            with ThreadPoolExecutor(max_workers=self._lookup_concurrency) as pool:
                future_to_idx = {
                    pool.submit(self._lookup_safe, lead): idx
                    for idx, lead in enumerate(leads)
                }
                for future in as_completed(future_to_idx):
                    if self._cancel_check():
                        for pending in future_to_idx:
                            pending.cancel()
                        self._emit({"type": "phase", "phase": "cancelled"})
                        return []
                    idx = future_to_idx[future]
                    candidates = future.result()
                    lookup_by_lead[idx] = candidates
                    self._emit(
                        {
                            "type": "lookup",
                            "lead_ref": leads[idx].linkedin_url
                            or leads[idx].source_url,
                            "candidates": len(candidates),
                            "providers": [
                                p.name for p in self._lookup_providers
                            ],
                        }
                    )

        # ---- Phase 3: validate/score per lead ---------------------------------
        self._emit({"type": "phase", "phase": "validating"})
        total = len(leads)
        completed = 0
        results: list[tuple[Lead, PhoneEnrichmentUpdate]] = []
        with ThreadPoolExecutor(max_workers=self._lead_concurrency) as pool:
            future_to_lead = {
                pool.submit(
                    self._enrich_one,
                    lead,
                    domains_per_lead[idx],
                    harvested_by_domain,
                    lookup_by_lead.get(idx, []),
                ): lead
                for idx, lead in enumerate(leads)
            }
            for future in as_completed(future_to_lead):
                if self._cancel_check():
                    for pending in future_to_lead:
                        pending.cancel()
                    self._emit({"type": "phase", "phase": "cancelled"})
                    break
                lead = future_to_lead[future]
                update = future.result()
                results.append((lead, update))
                completed += 1
                self._emit(
                    {
                        "type": "lead",
                        "lead_ref": lead.linkedin_url or lead.source_url,
                        "person_name": lead.person_name,
                        "company_name": lead.company_name,
                        "status": _summarize_update(update),
                        "phone": update.phone,
                        "confidence": update.confidence,
                        "source": update.source,
                        "whatsapp_status": update.whatsapp_status,
                        "hlr_status": update.hlr_status,
                    }
                )
                self._emit(
                    {"type": "progress", "completed": completed, "total": total}
                )

        self._emit({"type": "phase", "phase": "completed"})
        return results

    def _harvest_safe(self, domain: str) -> list[HarvestedPhone]:
        try:
            return self._harvest_fn(domain)
        except Exception as exc:
            logger.debug("phone harvest %s falhou: %s", domain, exc)
            return []

    def _lookup_safe(self, lead: Lead) -> list[PhoneCandidate]:
        if not self._lookup_providers:
            return []
        query = LookupQuery(
            full_name=lead.person_name or "",
            company_name=lead.company_name,
            company_domain=lead.company_domain,
            linkedin_url=lead.linkedin_url,
            email=lead.email,
        )
        out: list[PhoneCandidate] = []
        for provider in self._lookup_providers:
            try:
                out.extend(provider.lookup(query))
            except Exception as exc:
                logger.debug(
                    "lookup %s falhou para %s: %s",
                    provider.name,
                    lead.person_name,
                    exc,
                )
        return out

    def _enrich_one(
        self,
        lead: Lead,
        ranked_domains: list[str],
        harvested_by_domain: dict[str, list[HarvestedPhone]],
        lookup_candidates: list[PhoneCandidate],
    ) -> PhoneEnrichmentUpdate:
        merged: list[HarvestedPhone] = []
        seen_digits: set[str] = set()
        for domain in ranked_domains:
            for candidate in harvested_by_domain.get(domain, []):
                if candidate.digits in seen_digits:
                    continue
                seen_digits.add(candidate.digits)
                merged.append(candidate)
        return self._service.enrich_lead(
            lead,
            harvested_phones=merged,
            lookup_candidates=lookup_candidates,
        )

    def _emit(self, event: dict[str, Any]) -> None:
        try:
            self._on_event(event)
        except Exception:
            pass


def _ranked_domains_for_lead(
    lead: Lead, company_domains: dict[str, list[str]]
) -> list[str]:
    own = (getattr(lead, "company_domain", None) or "").strip().lower()
    key = _normalize_company_key(getattr(lead, "company_name", None))
    company_list = company_domains.get(key, []) if key else []

    out: list[str] = []
    seen: set[str] = set()
    if own:
        out.append(own)
        seen.add(own)
    for domain in company_list:
        if domain and domain not in seen:
            out.append(domain)
            seen.add(domain)
    return out


def _summarize_update(update: PhoneEnrichmentUpdate) -> str:
    if update.skipped_existing_phone:
        return "skipped_existing_phone"
    if update.failure_reason:
        return f"failed_{update.failure_reason}"
    if update.phone:
        return "enriched"
    return "no_change"


def collect_company_domains_from_leads(leads: list[Lead]) -> dict[str, list[str]]:
    """Build ``company_key -> [domains]`` ranked by lead count."""
    votes: dict[str, Counter[str]] = {}
    for lead in leads:
        key = _normalize_company_key(getattr(lead, "company_name", None))
        if not key:
            continue
        bucket = votes.setdefault(key, Counter())
        col = (getattr(lead, "company_domain", None) or "").strip().lower()
        if col:
            bucket[col] += 1
    return {
        key: [domain for domain, _ in bucket.most_common()]
        for key, bucket in votes.items()
        if bucket
    }


def default_harvest_fn(
    harvester: PhoneNumberHarvester | None = None,
) -> HarvestFn:
    """Wrap :class:`PhoneNumberHarvester` as the orchestrator's
    ``harvest_fn``. Exposed for the server's DI and for tests."""
    real = harvester or PhoneNumberHarvester()

    def _harvest(domain: str) -> list[HarvestedPhone]:
        try:
            return real.harvest(domain)
        except Exception as exc:
            logger.debug("phone harvest %s falhou: %s", domain, exc)
            return []

    return _harvest
