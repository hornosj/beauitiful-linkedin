"""End-to-end smoke test of the clean phone-discovery pipeline.

Runs the new ReceitaCnpjLookupProvider + the site harvester +
PhoneValidator against the leads already in
``data/saved_leads.sqlite``. Reports per-lead outcomes and a total
count of distinct, validated E.164 phones discovered.

Usage:

    python scripts/spike_phone_discovery.py --sample 50

The script never writes back to the database. It exists to validate
that the production pipeline reaches the goal of "at least 10 valid
phones" against the operator's real saved leads, without going
through the FastAPI sidecar.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

# Make ``src`` importable when running this script directly.
HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from beautiful_linkedin.config import load_settings
from beautiful_linkedin.models import Lead
from beautiful_linkedin.search.duckduckgo_search import DuckDuckGoSearchEngine
from beautiful_linkedin.search.searxng_search import SearxngSearchEngine
from beautiful_linkedin.storage.phone_harvester import PhoneNumberHarvester
from beautiful_linkedin.storage.phone_lookup import LookupQuery
from beautiful_linkedin.storage.phone_receita_cnpj import ReceitaCnpjLookupProvider
from beautiful_linkedin.storage.phone_validation import (
    PhoneValidationStatus,
    PhoneValidator,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("spike")


def load_leads(db_path: Path) -> list[Lead]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT person_name, company_name, company_domain, linkedin_url,
               email, phone, source_url, source_type, confidence_score
        FROM saved_leads
        """
    ).fetchall()
    conn.close()

    leads: list[Lead] = []
    for r in rows:
        # ``company_domain`` is polluted with LinkedIn URLs in the
        # current saved data; strip those so the harvester doesn't try
        # to scrape linkedin.com (which we wouldn't want anyway).
        domain = (r["company_domain"] or "").strip()
        if "linkedin.com" in domain.lower():
            domain = ""
        leads.append(
            Lead(
                company_name=r["company_name"] or "",
                company_domain=domain or None,
                person_name=r["person_name"],
                title=None,
                linkedin_url=r["linkedin_url"],
                email=r["email"],
                phone=r["phone"],
                source_url=r["source_url"] or r["linkedin_url"] or "",
                source_type=r["source_type"] or "linkedin_people_search",
                snippet="",
                confidence_score=int(r["confidence_score"] or 0),
            )
        )
    return leads


def build_engines() -> list[Any]:
    settings = load_settings()
    engines: list[Any] = []
    if settings.searxng_base_url:
        engines.append(SearxngSearchEngine(base_url=settings.searxng_base_url))
        log.info("SearxNG available at %s", settings.searxng_base_url)
    try:
        engines.append(DuckDuckGoSearchEngine())
        log.info("DuckDuckGo engine available")
    except Exception as exc:
        log.warning("DuckDuckGo unavailable: %s", exc)
    return engines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default="data/saved_leads.sqlite",
        help="path to the saved leads sqlite file",
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=0,
        help="cap to this many leads (0 = all)",
    )
    parser.add_argument(
        "--companies-only",
        action="store_true",
        help=(
            "skip per-lead lookup; resolve CNPJ once per unique company "
            "and report the company-line phones found"
        ),
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        log.error("DB not found: %s", db_path)
        return 2

    leads = load_leads(db_path)
    if args.sample > 0:
        leads = leads[: args.sample]
    log.info("Loaded %d leads", len(leads))

    engines = build_engines()
    if not engines:
        log.error("No search engines available — cannot resolve CNPJs")
        return 3

    receita = ReceitaCnpjLookupProvider(search_engines=engines)
    validator = PhoneValidator()
    harvester = PhoneNumberHarvester()

    # Group leads by company so Receita and harvester only fire once
    # per unique target.
    companies = defaultdict(list)
    for lead in leads:
        companies[lead.company_name].append(lead)
    log.info("Unique companies: %d", len(companies))

    valid_phones: dict[str, dict[str, Any]] = {}

    for company, company_leads in companies.items():
        log.info("=== %s (%d leads) ===", company, len(company_leads))
        sample_lead = company_leads[0]

        # --- Receita CNPJ -------------------------------------------------
        t0 = time.monotonic()
        try:
            receita_candidates = receita.lookup(
                LookupQuery(
                    full_name=sample_lead.person_name or "",
                    company_name=company,
                    company_domain=sample_lead.company_domain,
                    linkedin_url=sample_lead.linkedin_url,
                    email=sample_lead.email,
                )
            )
        except Exception as exc:
            log.error("Receita lookup failed for %s: %s", company, exc)
            receita_candidates = []
        log.info(
            "  receita: %d candidates in %.1fs",
            len(receita_candidates),
            time.monotonic() - t0,
        )
        for cand in receita_candidates:
            v = validator.validate(cand.raw)
            status = v.status.value if v.status else "unknown"
            log.info(
                "    %-30s  %s  %s  (cnpj=%s)",
                v.e164 or cand.raw,
                status,
                v.type.value if v.type else "?",
                cand.extra.get("cnpj", "-"),
            )
            if v.status in {PhoneValidationStatus.VALID, PhoneValidationStatus.PROBABLE}:
                key = v.e164 or cand.raw
                if key not in valid_phones:
                    valid_phones[key] = {
                        "company": company,
                        "source": "receita_cnpj",
                        "type": v.type.value if v.type else None,
                        "carrier": v.carrier,
                    }

        if args.companies_only:
            continue

        # --- Site harvester -----------------------------------------------
        # When Receita gave us a CNPJ, BrasilAPI's response also has the
        # company's email — extract its domain and harvest from that.
        domain = None
        for cand in receita_candidates:
            email = cand.extra.get("razao_social", "")
            # We didn't grab the email from BrasilAPI in extras; refetch.
            break
        # Fallback: try a domain that's a slug of the company name.
        if not domain:
            slug = (company.lower().replace(" ", "")
                                    .replace(".", "")
                                    .replace("/", ""))
            for tld in (".com.br", ".com", ".io"):
                candidate = f"{slug}{tld}"
                t0 = time.monotonic()
                phones = harvester.harvest(candidate)
                if phones:
                    domain = candidate
                    log.info(
                        "  site %-22s: %d phones in %.1fs",
                        candidate,
                        len(phones),
                        time.monotonic() - t0,
                    )
                    for p in phones[:5]:
                        v = validator.validate(p.raw)
                        if v.status in {
                            PhoneValidationStatus.VALID,
                            PhoneValidationStatus.PROBABLE,
                        }:
                            key = v.e164 or p.raw
                            if key not in valid_phones:
                                valid_phones[key] = {
                                    "company": company,
                                    "source": f"site:{candidate}",
                                    "type": v.type.value if v.type else None,
                                    "carrier": v.carrier,
                                }
                            log.info(
                                "    %-30s  %s  %s",
                                v.e164 or p.raw,
                                v.status.value,
                                v.type.value if v.type else "?",
                            )
                    break

    log.info("==========================================")
    log.info("Total distinct valid phones discovered: %d", len(valid_phones))
    for phone, meta in list(valid_phones.items())[:30]:
        log.info(
            "  %-20s  via %-30s  type=%s  carrier=%s  company=%s",
            phone,
            meta["source"],
            meta.get("type") or "?",
            meta.get("carrier") or "?",
            meta["company"],
        )
    if receita.errors:
        log.warning("Receita errors (sample): %s", receita.errors[:3])
    return 0 if len(valid_phones) >= 10 else 1


if __name__ == "__main__":
    sys.exit(main())
