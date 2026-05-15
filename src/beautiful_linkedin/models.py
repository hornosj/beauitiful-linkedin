from __future__ import annotations

from typing import Literal, Optional

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
