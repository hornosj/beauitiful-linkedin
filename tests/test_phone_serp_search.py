"""Offline tests for SerpPhoneSearchProvider (Bucket B).

A fake :class:`SearchEngine` returns canned :class:`SearchResult`
lists per query string. The provider should:

- Build queries from templates substituting name + company,
- Pull phone-shaped substrings out of every snippet,
- Tag candidates as ``serp_name_proximity`` when the lead's name
  appears within ~60 chars of the matched phone,
- Fail soft when an engine raises (record in ``errors``, return [] for
  that engine).
"""

from __future__ import annotations

from beautiful_linkedin.models import SearchResult
from beautiful_linkedin.search.search_engine import SearchEngine
from beautiful_linkedin.storage.phone_lookup import LookupQuery
from beautiful_linkedin.storage.phone_serp_search import SerpPhoneSearchProvider


class _FakeEngine(SearchEngine):
    def __init__(
        self,
        results_by_query: dict[str, list[SearchResult]],
        *,
        raise_for: set[str] | None = None,
    ) -> None:
        self._results = results_by_query
        self._raise_for = raise_for or set()
        self.calls: list[str] = []

    def search(self, query: str, max_results: int) -> list[SearchResult]:  # noqa: ARG002
        self.calls.append(query)
        if query in self._raise_for:
            raise RuntimeError("engine boom")
        return self._results.get(query, [])


def test_provider_extracts_phone_from_snippet_with_name_proximity() -> None:
    engine = _FakeEngine(
        {
            '"Ana Silva" "Acme" (telefone OR celular OR whatsapp)': [
                SearchResult(
                    title="Ana Silva - Acme",
                    url="https://example.com/ana",
                    snippet="Ana Silva trabalha na Acme. Telefone (11) 99999-9999",
                ),
            ]
        }
    )
    provider = SerpPhoneSearchProvider(engines=[engine], engine_labels=["fake"])

    candidates = provider.lookup(
        LookupQuery(full_name="Ana Silva", company_name="Acme")
    )
    assert len(candidates) == 1
    cand = candidates[0]
    assert cand.source == "serp"
    assert cand.context == "serp_name_proximity"
    assert "1199999" in "".join(ch for ch in cand.raw if ch.isdigit())
    assert cand.source_url == "https://example.com/ana"
    assert cand.extra["engine"] == "fake"


def test_provider_marks_far_match_without_name_proximity() -> None:
    """A snippet containing the number far from the name should NOT
    get the proximity boost — the score function relies on this to
    avoid associating an unrelated number with the lead."""
    long_intro = " ".join(["irrelevante"] * 80)
    engine = _FakeEngine(
        {
            '"Ana Silva" "Acme" (telefone OR celular OR whatsapp)': [
                SearchResult(
                    title="Acme corporate",
                    url="https://example.com/about",
                    snippet=(
                        f"Ana Silva trabalha aqui. {long_intro} Comercial: "
                        "(11) 3030-4040"
                    ),
                ),
            ]
        }
    )
    provider = SerpPhoneSearchProvider(engines=[engine], engine_labels=["fake"])
    cand = provider.lookup(
        LookupQuery(full_name="Ana Silva", company_name="Acme")
    )[0]
    assert cand.context == "serp"


def test_provider_dedupes_same_number_across_queries() -> None:
    same_result = [
        SearchResult(
            title="Ana Silva",
            url="https://example.com/x",
            snippet="Ana Silva — (11) 99999-9999",
        )
    ]
    engine = _FakeEngine(
        {
            '"Ana Silva" "Acme" (telefone OR celular OR whatsapp)': same_result,
            '"Ana Silva" "Acme" contato': same_result,
            '"Ana Silva" "Acme" "+55"': same_result,
            '"Ana Silva" Acme (mobile OR phone)': same_result,
        }
    )
    provider = SerpPhoneSearchProvider(engines=[engine], engine_labels=["fake"])
    candidates = provider.lookup(
        LookupQuery(full_name="Ana Silva", company_name="Acme")
    )
    assert len(candidates) == 1


def test_provider_records_engine_failure_in_errors() -> None:
    engine = _FakeEngine(
        {},
        raise_for={'"Ana Silva" "Acme" (telefone OR celular OR whatsapp)'},
    )
    provider = SerpPhoneSearchProvider(engines=[engine], engine_labels=["broken"])
    candidates = provider.lookup(
        LookupQuery(full_name="Ana Silva", company_name="Acme")
    )
    assert candidates == []
    assert any("broken" in e for e in provider.errors)


def test_provider_returns_empty_when_query_is_incomplete() -> None:
    engine = _FakeEngine({})
    provider = SerpPhoneSearchProvider(engines=[engine])
    assert provider.lookup(LookupQuery(full_name="", company_name="Acme")) == []
    assert provider.lookup(LookupQuery(full_name="Ana", company_name="")) == []
    assert engine.calls == []
