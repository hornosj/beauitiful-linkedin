"""Direct site-harvest spike — bypass CNPJ resolution flakiness.

The first spike showed Receita CNPJ resolution stalls when the SERP
engine du jour rate-limits. For the operator's saved leads (11 well-
known BR companies), the actual company website is known a priori,
so we shortcut: harvest the site directly via the existing
``PhoneNumberHarvester``, validate with ``PhoneValidator``, and count
unique E.164 phones discovered.

This is the fastest path to validate that the pipeline produces
real phones against the operator's data. Per-lead lookup (Bucket B)
is added on top if SERP engines respond.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from beautiful_linkedin.storage.phone_harvester import PhoneNumberHarvester
from beautiful_linkedin.storage.phone_receita_cnpj import ReceitaCnpjLookupProvider
from beautiful_linkedin.storage.phone_lookup import LookupQuery
from beautiful_linkedin.storage.phone_validation import (
    PhoneValidationStatus,
    PhoneValidator,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("spike2")


# Known domains for the 11 companies in the operator's saved data.
# Maintained here only for the spike script so we can validate the
# full pipeline against real targets without depending on SERP
# resolvers. Production code resolves dynamically.
KNOWN_DOMAINS: dict[str, list[str]] = {
    "Nubank": ["nubank.com.br", "international.nubank.com.br"],
    "iFood": ["ifood.com.br", "institucional.ifood.com.br"],
    "TOTVS": ["totvs.com", "totvs.com.br"],
    "Santander": ["santander.com.br", "www.santander.com.br"],
    "BTG Pactual": ["btgpactual.com", "www.btgpactual.com"],
    "unimed": ["unimed.coop.br", "www.unimed.coop.br"],
    "Wittel": ["wittel.com.br", "wittel.com"],
    "Dimensa": ["dimensa.com", "dimensa.com.br"],
    "DIMENSAV2": ["dimensa.com", "dimensa.com.br"],
    "Marlabs": ["marlabs.com", "marlabs.com.br"],
    "GOV": [],  # too generic to guess
}

# Pre-resolved CNPJs for the same companies. Used to short-circuit
# the SERP→CNPJ search when known; production code is expected to
# resolve via the public search path.
KNOWN_CNPJS: dict[str, str] = {
    "Nubank": "18236120000158",
    "iFood": "14380200000121",
    "TOTVS": "53113791000122",
    "Santander": "90400888000142",
    "BTG Pactual": "30306294000145",
    "Dimensa": "44071872000124",
    "Marlabs": "12876024000130",
}


def load_companies(db_path: Path) -> dict[str, int]:
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT company_name, COUNT(*) FROM saved_leads GROUP BY company_name"
    ).fetchall()
    conn.close()
    return {r[0]: r[1] for r in rows}


def main() -> int:
    db_path = Path("data/saved_leads.sqlite")
    if not db_path.exists():
        log.error("DB not found: %s", db_path)
        return 2

    companies = load_companies(db_path)
    log.info("Saved-data companies: %d", len(companies))

    validator = PhoneValidator()
    harvester = PhoneNumberHarvester()
    receita = ReceitaCnpjLookupProvider(search_engines=[])

    valid_phones: dict[str, dict[str, str]] = {}
    by_company: dict[str, list[str]] = defaultdict(list)

    for company, lead_count in companies.items():
        log.info("=== %s (%d leads) ===", company, lead_count)

        # --- Receita CNPJ via known mapping (short-circuit SERP) ---
        cnpj = KNOWN_CNPJS.get(company)
        if cnpj:
            t0 = time.monotonic()
            data = receita._fetch_cnpj_data(cnpj)  # noqa: SLF001
            log.info("  receita CNPJ %s: %.1fs", cnpj, time.monotonic() - t0)
            if data:
                for fld in ("ddd_telefone_1", "ddd_telefone_2"):
                    raw = (data.get(fld) or "").strip()
                    if not raw:
                        continue
                    v = validator.validate(raw)
                    log.info(
                        "    %-30s  %s  %s",
                        v.e164 or raw,
                        v.status.value if v.status else "?",
                        v.type.value if v.type else "?",
                    )
                    if v.status in {
                        PhoneValidationStatus.VALID,
                        PhoneValidationStatus.PROBABLE,
                    }:
                        key = v.e164 or raw
                        if key not in valid_phones:
                            valid_phones[key] = {
                                "source": f"receita_cnpj:{cnpj}",
                                "type": v.type.value if v.type else "?",
                                "company": company,
                                "carrier": v.carrier or "",
                            }
                            by_company[company].append(key)

        # --- Site harvest against known domains ---
        for domain in KNOWN_DOMAINS.get(company, []):
            t0 = time.monotonic()
            try:
                phones = harvester.harvest(domain)
            except Exception as exc:
                log.warning("  site harvest %s failed: %s", domain, exc)
                continue
            log.info(
                "  site %-30s: %d candidates in %.1fs",
                domain,
                len(phones),
                time.monotonic() - t0,
            )
            for p in phones[:8]:
                v = validator.validate(p.raw)
                tag = "  "
                if v.status in {
                    PhoneValidationStatus.VALID,
                    PhoneValidationStatus.PROBABLE,
                }:
                    key = v.e164 or p.raw
                    if key not in valid_phones:
                        tag = "+ "
                        valid_phones[key] = {
                            "source": f"site:{domain}({p.context})",
                            "type": v.type.value if v.type else "?",
                            "company": company,
                            "carrier": v.carrier or "",
                        }
                        by_company[company].append(key)
                log.info(
                    "  %s  %-30s  %s  %s  ctx=%s",
                    tag,
                    v.e164 or p.raw,
                    v.status.value if v.status else "?",
                    v.type.value if v.type else "?",
                    p.context,
                )

    print()
    print("=" * 72)
    print(f"Total distinct VALID/PROBABLE phones: {len(valid_phones)}")
    print("=" * 72)
    for phone, meta in valid_phones.items():
        print(
            f"  {phone:20s}  type={meta['type']:6s}  carrier={meta.get('carrier', ''):20s}  "
            f"company={meta['company']}  source={meta['source']}"
        )

    print()
    print("Breakdown by company:")
    for company, phones in sorted(by_company.items(), key=lambda kv: -len(kv[1])):
        print(f"  {company:20s}  {len(phones)} phones")

    return 0 if len(valid_phones) >= 10 else 1


if __name__ == "__main__":
    sys.exit(main())
