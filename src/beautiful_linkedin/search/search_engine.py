from __future__ import annotations

from abc import ABC, abstractmethod

from beautiful_linkedin.models import SearchResult


class SearchEngine(ABC):
    @abstractmethod
    def search(self, query: str, max_results: int) -> list[SearchResult]:
        """Run a public search query and return normalized results."""
