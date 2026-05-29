from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class CompanyInput(BaseModel):
    company_name: str = Field(min_length=1)
    company_domain: Optional[str] = None
    linkedin_url: Optional[str] = None
    # An empty ``titles`` list signals "general search" mode — the
    # people_search provider then fetches /company/<slug>/people/ without
    # any ?keywords= filter and accepts every visible card. Other providers
    # treat empty titles as "no keyword constraint".
    titles: list[str] = Field(default_factory=list)

    @field_validator("company_name")
    @classmethod
    def clean_company_name(cls, value: str) -> str:
        cleaned = " ".join(value.strip().split())
        if not cleaned:
            raise ValueError("company_name is required")
        return cleaned

    @field_validator("company_domain", "linkedin_url", mode="before")
    @classmethod
    def blank_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("titles", mode="before")
    @classmethod
    def parse_titles(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        if value is None:
            return []
        return value

    @field_validator("titles")
    @classmethod
    def clean_titles(cls, value: list[str]) -> list[str]:
        return [" ".join(title.strip().split()) for title in value if title.strip()]


class SearchResult(BaseModel):
    title: str = ""
    url: str = Field(min_length=1)
    snippet: str = ""
    source_type: str = "search"


class Lead(BaseModel):
    company_name: str
    company_domain: Optional[str] = None
    person_name: Optional[str] = None
    title: Optional[str] = None
    linkedin_url: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    source_url: str
    source_type: str
    snippet: str = ""
    matched_title: Optional[str] = None
    validation_status: Literal["valid", "maybe_incorrect"] = "valid"
    validation_note: Optional[str] = None
    confidence_score: int = Field(ge=0, le=100)
    previously_consulted_at: Optional[str] = None
    consultation_note: Optional[str] = None
    # Enrichment metadata. All fields are optional and additive — they
    # describe how (and how confidently) the email/phone was populated.
    # Defaults preserve the legacy shape of leads that were never enriched.
    enrichment_source: Optional[str] = None  # "internal" | "lusha" | "apollo" | ...
    enrichment_status: Optional[str] = None  # not_enriched|estimated|enriched|partial|failed
    enrichment_confidence: Optional[int] = Field(default=None, ge=0, le=100)
    email_type: Optional[str] = None  # work | personal | unknown
    email_validation_status: Optional[str] = None  # valid | probable | risky | unknown
    enriched_at: Optional[str] = None  # ISO timestamp
    # Cross-provider verification trail.
    #
    # ``email_verified_by`` lists every source whose result matched the
    # primary ``email`` exactly. When length >= 2 the UI shows a "verified"
    # badge — two independent layers (e.g. internal pattern inference and
    # Apollo) landed on the same address, which is a strong signal.
    #
    # ``email_alternatives`` collects e-mails that *other* sources returned
    # but disagreed with the primary. We don't overwrite the primary, but
    # we keep these so the user can see "Apollo says ana@acme.com.br
    # instead". Each entry is a dict with ``email``, ``source``,
    # ``confidence`` (0-100 or null), and ``found_at`` (ISO timestamp).
    email_verified_by: list[str] = Field(default_factory=list)
    email_alternatives: list[dict[str, Any]] = Field(default_factory=list)
    # Phone enrichment metadata — same shape as the e-mail trail so the
    # UI can mirror its "verified by 2 sources" badge and "alternatives"
    # expander for phone numbers. ``phone`` itself stays as the primary
    # E.164-ish value; these columns describe how confidently it was
    # found and which sources agreed.
    phone_type: Optional[str] = None  # mobile | fixed | voip | toll_free | ...
    phone_country: Optional[str] = None  # ISO-3166 alpha-2
    phone_carrier: Optional[str] = None
    phone_region: Optional[str] = None
    phone_validation_status: Optional[str] = None  # valid | probable | risky | invalid
    phone_confidence: Optional[int] = Field(default=None, ge=0, le=100)
    phone_source: Optional[str] = None  # harvester | apollo | lusha | usersbox | ...
    phone_source_url: Optional[str] = None
    phone_verified_by: list[str] = Field(default_factory=list)
    phone_alternatives: list[dict[str, Any]] = Field(default_factory=list)
    # LinkedIn profile validation metadata. These fields are populated by the
    # opt-in profile validation flow that opens the lead's LinkedIn profile,
    # reads the current Experience entry, and extracts self-published Contact
    # Info. They intentionally do not overwrite ``title``, ``company_name``,
    # ``email`` or ``phone`` so API/internal/searcher data stays auditable.
    linkedin_profile_validation_status: Optional[str] = None
    linkedin_experience_title: Optional[str] = None
    linkedin_experience_company: Optional[str] = None
    linkedin_experience_start_year: Optional[int] = None
    linkedin_experience_end_year: Optional[int] = None
    linkedin_experience_checked_at: Optional[str] = None
    linkedin_contact_email: Optional[str] = None
    linkedin_contact_website: Optional[str] = None
    linkedin_contact_phone: Optional[str] = None
    # LinkedIn profile signals used by the Telegram-consult matcher to
    # rank CPF candidates against the actual person. Both default None
    # — when missing the matcher falls back to whatever signals exist
    # (e.g. ``snippet`` for a coarse location guess).
    linkedin_location: Optional[str] = None
    # ``linkedin_education`` is a list of {institution, degree, start_year,
    # end_year, field} dicts. Year fields are integers when known.
    linkedin_education: list[dict[str, Any]] = Field(default_factory=list)
    # ``linkedin_birthday`` is the day/month string the LinkedIn profile
    # exposes under "Dados pessoais" (visible only to first-degree
    # connections). Format is ``DD/MM`` — year is intentionally absent
    # because LinkedIn does not publish it. The Telegram-consult matcher
    # uses this as a top-weight signal when present: an exact DD/MM match
    # against a CPF candidate's ``data_nascimento`` is a near-decisive
    # disambiguator between homonyms.
    linkedin_birthday: Optional[str] = None
    # Residential address recovered from a Telegram CPF (SISREG-III)
    # consult. Informational only — it never affects confidence or
    # enrichment status, and is never overwritten once set. Populated by
    # the Telegram phone stage alongside ``phone``.
    endereco: Optional[str] = None


class ProspectingSummary(BaseModel):
    total_companies_processed: int = 0
    total_raw_leads: int = 0
    total_deduplicated_leads: int = 0
    total_previously_consulted_leads: int = 0
    total_maybe_incorrect_leads: int = 0
    output_file: str
    top_sources: dict[str, int] = Field(default_factory=dict)


class ProviderDiagnostic(BaseModel):
    provider: str
    company_name: Optional[str] = None
    raw_records: int = 0
    leads_returned: int = 0
    dropped_company_evidence: int = 0
    dropped_title_filter: int = 0
    last_error: Optional[str] = None
    notes: list[str] = Field(default_factory=list)


class ProspectingResult(BaseModel):
    leads: list[Lead]
    summary: ProspectingSummary
    provider_diagnostics: list[ProviderDiagnostic] = Field(default_factory=list)
