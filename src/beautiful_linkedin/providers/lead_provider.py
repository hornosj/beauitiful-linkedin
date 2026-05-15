from __future__ import annotations

from abc import ABC, abstractmethod

from beautiful_linkedin.models import CompanyInput, Lead, ProviderDiagnostic


class LeadProvider(ABC):
    name: str
    last_diagnostic: ProviderDiagnostic | None = None

    @abstractmethod
    def find_leads(
        self,
        company: CompanyInput,
        max_results: int,
        include_uncertain: bool,
        search_depth: str = "standard",
        offset: int = 0,
    ) -> list[Lead]:
        """Return lead candidates for one company."""

    def _new_diagnostic(self, company: CompanyInput) -> ProviderDiagnostic:
        diagnostic = ProviderDiagnostic(
            provider=self.name,
            company_name=company.company_name,
        )
        self.last_diagnostic = diagnostic
        return diagnostic
