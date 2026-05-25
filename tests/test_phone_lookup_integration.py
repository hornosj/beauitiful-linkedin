"""Integration test for the full Bucket A + Bucket B + WhatsApp path.

Exercises the orchestrator end-to-end with stubs for every external
dependency so the test runs in milliseconds:

- A fake harvester returns one fixed-line phone (lower score baseline).
- A fake lookup provider returns a mobile from "SERP" with name
  proximity (higher base + proximity boost).
- A fake WhatsApp checker says ACTIVE for the mobile (extra boost).

Expectation: the mobile wins, source is the SERP, and confidence
lands in the 90+ range — exactly the "individual lead's mobile,
externally verified" outcome the Fatia 2 was built for.
"""

from __future__ import annotations

from beautiful_linkedin.models import Lead
from beautiful_linkedin.storage.internal_phone_enrichment import (
    InternalPhoneEnrichmentOrchestrator,
    InternalPhoneEnrichmentService,
)
from beautiful_linkedin.storage.phone_harvester import HarvestedPhone
from beautiful_linkedin.storage.phone_lookup import (
    LookupQuery,
    PhoneCandidate,
    PhoneLookupProvider,
)
from beautiful_linkedin.storage.phone_validation import PhoneValidator
from beautiful_linkedin.storage.whatsapp_checker import (
    WhatsAppCheck,
    WhatsAppStatus,
)


def _lead() -> Lead:
    return Lead(
        company_name="Empresa",
        company_domain="empresa.com",
        person_name="Ana Silva",
        title="Marketing Manager",
        linkedin_url="https://www.linkedin.com/in/ana/",
        source_url="https://www.linkedin.com/in/ana/",
        source_type="linkedin_people_search",
        snippet="",
        confidence_score=80,
    )


class _StubLookupProvider:
    name = "serp"

    def __init__(self, by_name: dict[str, list[PhoneCandidate]]) -> None:
        self._by_name = by_name
        self.errors: list[str] = []

    def lookup(self, query: LookupQuery) -> list[PhoneCandidate]:
        return list(self._by_name.get(query.full_name, []))


class _StubWaChecker:
    def __init__(self, by_e164: dict[str, WhatsAppStatus]) -> None:
        self._by_e164 = by_e164

    def check(self, e164: str) -> WhatsAppCheck:
        return WhatsAppCheck(
            status=self._by_e164.get(e164, WhatsAppStatus.UNKNOWN)
        )


def test_serp_mobile_wins_over_site_fixed_with_whatsapp_active() -> None:
    site_phones = {
        "empresa.com": [
            HarvestedPhone(
                raw="(11) 3030-4040",
                digits="1130304040",
                source_url="https://empresa.com/contato",
                context="tel",
            )
        ]
    }
    serp_candidates = {
        "Ana Silva": [
            PhoneCandidate(
                raw="(11) 99999-9999",
                source="serp",
                source_url="https://example.com/ana",
                context="serp_name_proximity",
                extra={"engine": "searxng"},
            )
        ]
    }

    def harvest(domain: str):
        return site_phones.get(domain, [])

    wa_checker = _StubWaChecker(
        {"+5511999999999": WhatsAppStatus.ACTIVE}
    )
    service = InternalPhoneEnrichmentService(
        validator=PhoneValidator(),
        wa_checker=wa_checker,
    )
    orchestrator = InternalPhoneEnrichmentOrchestrator(
        service=service,
        harvest_fn=harvest,
        lookup_providers=[_StubLookupProvider(serp_candidates)],
    )

    events: list[dict] = []
    orchestrator_with_events = InternalPhoneEnrichmentOrchestrator(
        service=service,
        harvest_fn=harvest,
        lookup_providers=[_StubLookupProvider(serp_candidates)],
        on_event=events.append,
    )
    results = orchestrator_with_events.run([_lead()])

    assert len(results) == 1
    _, update = results[0]
    assert update.phone == "+5511999999999"
    assert update.phone_type == "mobile"
    assert update.source == "serp"
    assert update.whatsapp_status == "active"
    assert update.confidence >= 90, (
        f"expected high-confidence individual mobile, got {update.confidence}"
    )

    types = [e["type"] for e in events]
    # Every phase must show up so the UI can render the progress bar.
    assert "phase" in types
    assert any(e.get("phase") == "lookup" for e in events if e["type"] == "phase")


def test_serp_inactive_whatsapp_demotes_candidate_but_still_picks_best() -> None:
    """If SERP gave us a number but wa.me says no account, we still
    persist it (no overwrite of primary, but better than nothing) —
    the score just doesn't get the +20 active boost."""
    serp_candidates = {
        "Ana Silva": [
            PhoneCandidate(
                raw="(11) 99999-9999",
                source="serp",
                source_url="https://example.com/ana",
                context="serp_name_proximity",
                extra={},
            )
        ]
    }
    wa_checker = _StubWaChecker(
        {"+5511999999999": WhatsAppStatus.INACTIVE}
    )
    service = InternalPhoneEnrichmentService(
        validator=PhoneValidator(),
        wa_checker=wa_checker,
    )
    orchestrator = InternalPhoneEnrichmentOrchestrator(
        service=service,
        harvest_fn=lambda _domain: [],
        lookup_providers=[_StubLookupProvider(serp_candidates)],
    )
    _, update = orchestrator.run([_lead()])[0]
    assert update.phone == "+5511999999999"
    assert update.whatsapp_status == "inactive"
    # Active case lands at clamp(120) = 100; inactive subtracts 10
    # from the unclamped sum (50 + 35 + 15 = 100 → −10 → 90). The
    # point is the inactive case has to be **strictly less** than
    # what an active probe would produce so the score function
    # actually changes ranking when WhatsApp talks.
    assert 0 < update.confidence < 100


def test_no_lookup_providers_falls_back_to_site_only_behavior() -> None:
    """Backwards compat: pipeline still works with zero lookup providers
    (Fatia 1 behavior). Picks the site phone, no SERP, no WhatsApp."""
    site_phones = {
        "empresa.com": [
            HarvestedPhone(
                raw="(11) 99999-9999",
                digits="11999999999",
                source_url="https://empresa.com/contato",
                context="tel",
            )
        ]
    }
    service = InternalPhoneEnrichmentService(validator=PhoneValidator())
    orchestrator = InternalPhoneEnrichmentOrchestrator(
        service=service,
        harvest_fn=lambda d: site_phones.get(d, []),
        lookup_providers=[],
    )
    _, update = orchestrator.run([_lead()])[0]
    assert update.phone == "+5511999999999"
    assert update.source == "internal"
    assert update.whatsapp_status is None
