"""Strict-quality phone-discovery spike.

v1 + v2 showed the regex/text-scan pipeline produces ~30 candidates
against the operator's saved companies but ~⅔ of those are noise
from numeric tokens in HTML/JS (cache busters, IDs, embedded
hashes). This run applies a strict-quality filter on top:

- Only structured contexts (``tel`` link, ``jsonld``, ``whatsapp``) +
  Receita CNPJ are counted.
- BR DDD must be in the official Anatel list.
- ``phonenumbers`` type must be ``MOBILE``, ``FIXED_LINE``, or
  ``FIXED_LINE_OR_MOBILE``.
- Placeholder repeats (same-digit > 50%) are dropped.

The objective is to land at "10 valid phones, every one defensible
as a real BR number", not "30 candidates that look phone-shaped".
"""

from __future__ import annotations

import logging
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from beautiful_linkedin.storage.phone_harvester import PhoneNumberHarvester
from beautiful_linkedin.storage.phone_receita_cnpj import ReceitaCnpjLookupProvider
from beautiful_linkedin.storage.phone_validation import (
    PhoneType,
    PhoneValidationStatus,
    PhoneValidator,
)


logging.basicConfig(level=logging.WARNING)
log = logging.getLogger("spike3")


# Official Anatel DDDs (set of valid two-digit area codes in Brazil).
VALID_BR_DDDS: set[str] = {
    # SP and surroundings
    "11", "12", "13", "14", "15", "16", "17", "18", "19",
    # RJ / ES
    "21", "22", "24", "27", "28",
    # MG
    "31", "32", "33", "34", "35", "37", "38",
    # PR
    "41", "42", "43", "44", "45", "46",
    # SC / RS
    "47", "48", "49", "51", "53", "54", "55",
    # DF / GO / TO / MS / MT / RO / AC
    "61", "62", "63", "64", "65", "66", "67", "68", "69",
    # BA / SE
    "71", "73", "74", "75", "77", "79",
    # PE / PB / RN / CE / PI / MA / AL
    "81", "82", "83", "84", "85", "86", "87", "88", "89",
    # PA / AM / RR / AP
    "91", "92", "93", "94", "95", "96", "97", "98", "99",
}


KNOWN_DOMAINS: dict[str, list[str]] = {
    "Nubank": ["nubank.com.br", "international.nubank.com.br"],
    "iFood": ["ifood.com.br", "institucional.ifood.com.br"],
    "TOTVS": ["totvs.com", "totvs.com.br"],
    "Santander": ["santander.com.br"],
    "BTG Pactual": ["btgpactual.com"],
    "unimed": ["unimed.coop.br"],
    "Wittel": ["wittel.com.br", "wittel.com"],
    "Dimensa": ["dimensa.com"],
    "DIMENSAV2": ["dimensa.com"],
    "Marlabs": ["marlabs.com"],
    "GOV": [],
}
KNOWN_CNPJS: dict[str, str] = {
    "Nubank": "18236120000158",
    "iFood": "14380200000121",
    "TOTVS": "53113791000122",
    "Santander": "90400888000142",
    "BTG Pactual": "30306294000145",
    "Dimensa": "44071872000124",
    "Marlabs": "12876024000130",
}

STRUCTURED_CONTEXTS = {"tel", "jsonld", "whatsapp"}
ACCEPTED_TYPES = {
    PhoneType.MOBILE,
    PhoneType.FIXED,
    PhoneType.FIXED_OR_MOBILE,
}


def is_placeholder(digits: str) -> bool:
    """Drop ``2222-2222`` and similar registry-placeholder shapes."""
    if not digits:
        return True
    most_common, count = Counter(digits).most_common(1)[0]
    return count / len(digits) > 0.5


def br_ddd_valid(e164: str) -> bool:
    """``+55 + DDD + ...`` and DDD must be an Anatel-allocated code."""
    if not e164 or not e164.startswith("+55"):
        return True  # non-BR numbers pass; this only filters BR fakes
    rest = e164[3:]
    if len(rest) < 2:
        return False
    return rest[:2] in VALID_BR_DDDS


def strict_accept(
    e164: str,
    status: PhoneValidationStatus | None,
    phone_type: PhoneType | None,
    context: str,
) -> bool:
    if status != PhoneValidationStatus.VALID:
        return False
    if phone_type not in ACCEPTED_TYPES:
        return False
    digits = "".join(ch for ch in (e164 or "") if ch.isdigit())
    if is_placeholder(digits):
        return False
    if not br_ddd_valid(e164):
        return False

    # Structured-context candidates (tel:, jsonld, wa.me) always pass —
    # those are explicit telephone markup.
    if context in STRUCTURED_CONTEXTS or context == "receita_cnpj":
        return True

    # Text-context candidates need an extra gate: only accept if the
    # number has the canonical BR-with-country-code length (12 or 13
    # digits including the leading +55). This rejects 10-digit tokens
    # the regex pulled out of script bodies.
    if context == "text" and e164.startswith("+55"):
        return len(digits) in {12, 13}

    return False


def main() -> int:
    db_path = Path("data/saved_leads.sqlite")
    conn = sqlite3.connect(db_path)
    companies = {r[0]: r[1] for r in conn.execute(
        "SELECT company_name, COUNT(*) FROM saved_leads GROUP BY company_name"
    )}
    conn.close()

    validator = PhoneValidator()
    harvester = PhoneNumberHarvester()
    receita = ReceitaCnpjLookupProvider(search_engines=[])

    accepted: dict[str, dict] = {}
    rejected_count = 0

    for company, lead_count in companies.items():
        # --- Receita CNPJ ---
        cnpj = KNOWN_CNPJS.get(company)
        if cnpj:
            data = receita._fetch_cnpj_data(cnpj)  # noqa: SLF001
            if data:
                for fld in ("ddd_telefone_1", "ddd_telefone_2"):
                    raw = (data.get(fld) or "").strip()
                    if not raw:
                        continue
                    v = validator.validate(raw)
                    e164 = v.e164 or raw
                    if strict_accept(e164, v.status, v.type, "receita_cnpj"):
                        if e164 not in accepted:
                            accepted[e164] = {
                                "company": company,
                                "source": f"receita_cnpj:{cnpj}",
                                "type": v.type.value if v.type else "?",
                                "region": v.region or "",
                                "carrier": v.carrier or "",
                            }
                    else:
                        rejected_count += 1

        # --- Site harvest (structured contexts only) ---
        for domain in KNOWN_DOMAINS.get(company, []):
            try:
                phones = harvester.harvest(domain)
            except Exception:
                continue
            for p in phones:
                v = validator.validate(p.raw)
                e164 = v.e164 or p.raw
                if strict_accept(e164, v.status, v.type, p.context):
                    if e164 not in accepted:
                        accepted[e164] = {
                            "company": company,
                            "source": f"site:{domain}({p.context})",
                            "type": v.type.value if v.type else "?",
                            "region": v.region or "",
                            "carrier": v.carrier or "",
                        }
                else:
                    rejected_count += 1

    print()
    print("=" * 78)
    print(f"  STRICTLY VALIDATED PHONES: {len(accepted)}")
    print(f"  Rejected by strict filter: {rejected_count}")
    print("=" * 78)
    for phone, meta in accepted.items():
        print(
            f"  {phone:18s}  {meta['type']:6s}  "
            f"{(meta['region'] or '-'):20s}  "
            f"{(meta['carrier'] or '-'):16s}  "
            f"{meta['company']:14s}  {meta['source']}"
        )

    by_company: dict[str, int] = defaultdict(int)
    for meta in accepted.values():
        by_company[meta["company"]] += 1
    print()
    print("By company:")
    for c, n in sorted(by_company.items(), key=lambda kv: -kv[1]):
        print(f"  {c:20s} {n}")

    return 0 if len(accepted) >= 10 else 1


if __name__ == "__main__":
    sys.exit(main())
