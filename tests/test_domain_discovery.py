"""Tests for the free domain-discovery sources.

Every external I/O point (HTTP, DNS, MX) is injected, so these run
fully offline. The fake clients return deterministic fixtures based on
real-world shapes (crt.sh JSON, SPF/DMARC TXT records, etc.).
"""

from __future__ import annotations

import json

from beautiful_linkedin.storage.domain_discovery import (
    CctldVariantGenerator,
    CrtShClient,
    DomainDiscoveryService,
    SpfDmarcDiscoverer,
    _extract_core,
    _normalize_hostname,
)


# ---- helpers --------------------------------------------------------------


def _stub_fetcher(status: int, body: str):
    def fetch(_url: str) -> tuple[int, str]:
        return status, body

    return fetch


# ---- _normalize_hostname --------------------------------------------------


def test_normalize_strips_wildcards_and_lowercases() -> None:
    assert _normalize_hostname("*.Acme.COM") == "acme.com"
    assert _normalize_hostname(" acme.io ") == "acme.io"


def test_normalize_rejects_non_domain_tokens() -> None:
    assert _normalize_hostname("not-a-domain") == ""
    assert _normalize_hostname("foo@bar.com") == ""
    assert _normalize_hostname("") == ""


def test_normalize_strips_port() -> None:
    assert _normalize_hostname("acme.com:443") == "acme.com"


# ---- _extract_core (ccTLD-aware) ------------------------------------------


def test_extract_core_handles_single_label_tld() -> None:
    assert _extract_core("acme.com") == "acme"
    assert _extract_core("acme.io") == "acme"


def test_extract_core_handles_compound_tlds() -> None:
    assert _extract_core("acme.com.br") == "acme"
    assert _extract_core("acme.co.uk") == "acme"


def test_extract_core_preserves_subdomains() -> None:
    # Subdomains are intentionally kept — they often double as brand
    # names ("app.acme.io" → "app.acme" → variants like "app.acme.com").
    assert _extract_core("app.acme.io") == "app.acme"


# ---- CrtShClient ----------------------------------------------------------


def test_crt_sh_parses_san_list_from_name_value() -> None:
    body = json.dumps(
        [
            {"name_value": "acme.com\n*.acme.com\nwww.acme.com"},
            {"name_value": "acme.io"},
            {"name_value": "mail.acme.com.br"},
        ]
    )
    client = CrtShClient(http_fetcher=_stub_fetcher(200, body))
    domains = client.query("acme.com")
    assert "acme.com" in domains
    assert "acme.io" in domains
    assert "mail.acme.com.br" in domains
    # Wildcard prefix collapsed to its parent — never returned as a SAN.
    assert "*.acme.com" not in domains


def test_crt_sh_returns_empty_on_http_error() -> None:
    client = CrtShClient(http_fetcher=_stub_fetcher(503, ""))
    assert client.query("acme.com") == []


def test_crt_sh_returns_empty_on_bad_json() -> None:
    client = CrtShClient(http_fetcher=_stub_fetcher(200, "not-json"))
    assert client.query("acme.com") == []


def test_crt_sh_handles_fetcher_exception_silently() -> None:
    def boom(_url: str) -> tuple[int, str]:
        raise RuntimeError("network down")

    client = CrtShClient(http_fetcher=boom)
    # Discovery is best-effort; one source dying must never propagate.
    assert client.query("acme.com") == []


# ---- SpfDmarcDiscoverer ---------------------------------------------------


def test_spf_dmarc_extracts_includes_and_redirects() -> None:
    txt = {
        "acme.com": [
            "v=spf1 include:_spf.google.com include:mail.acme.io ~all",
            "google-site-verification=xyz",  # noise, ignored
        ],
        "_dmarc.acme.com": [
            "v=DMARC1; p=reject; rua=mailto:reports@dmarc.acme.com.br",
        ],
    }

    def resolver(name: str) -> list[str]:
        return txt.get(name, [])

    out = SpfDmarcDiscoverer(txt_resolver=resolver).discover("acme.com")
    assert "_spf.google.com" in out
    assert "mail.acme.io" in out
    assert "dmarc.acme.com.br" in out
    # Seed itself is never echoed back.
    assert "acme.com" not in out


def test_spf_dmarc_ignores_non_policy_txt_records() -> None:
    def resolver(name: str) -> list[str]:
        if name == "acme.com":
            return ["google-site-verification=abc", "facebook-domain-verification=def"]
        return []

    out = SpfDmarcDiscoverer(txt_resolver=resolver).discover("acme.com")
    assert out == []


# ---- CctldVariantGenerator ------------------------------------------------


def test_cctld_generator_emits_variants_excluding_seed() -> None:
    gen = CctldVariantGenerator(tlds=("com", "com.br", "io", "co.uk"))
    variants = gen.variants("acme.com")
    assert "acme.com.br" in variants
    assert "acme.io" in variants
    assert "acme.co.uk" in variants
    # The seed itself is not re-emitted.
    assert "acme.com" not in variants


def test_cctld_generator_works_from_compound_seed() -> None:
    gen = CctldVariantGenerator(tlds=("com", "io"))
    variants = gen.variants("acme.com.br")
    assert "acme.com" in variants
    assert "acme.io" in variants


# ---- DomainDiscoveryService -----------------------------------------------


def test_discovery_service_merges_sources_and_gates_by_mx() -> None:
    """The service should combine results from all enabled sources and
    only keep domains whose MX resolves — that's the difference between
    "discovered N candidates" and "discovered N real mail domains"."""

    class FakeCrt:
        def query(self, seed: str) -> list[str]:
            return ["acme.io", "marketing.acme.io", "dead.acme.io"]

    class FakeSpf:
        def discover(self, seed: str) -> list[str]:
            return ["mail.acme.com.br"]

    cctld = CctldVariantGenerator(tlds=("co",))

    mx_calls: list[str] = []

    def mx_checker(domain: str) -> bool:
        mx_calls.append(domain)
        return domain != "dead.acme.io"  # all others have MX

    service = DomainDiscoveryService(
        crt_sh_client=FakeCrt(),
        spf_dmarc=FakeSpf(),
        cctld_generator=cctld,
        mx_checker=mx_checker,
    )
    result = service.discover("acme.com")

    assert "acme.io" in result.domains
    assert "marketing.acme.io" in result.domains
    assert "mail.acme.com.br" in result.domains
    assert "acme.co" in result.domains
    assert "dead.acme.io" not in result.domains

    # Source breakdown is preserved so the UI can show provenance.
    assert "acme.io" in result.sources["crt_sh"]
    assert "mail.acme.com.br" in result.sources["spf_dmarc"]
    assert "acme.co" in result.sources["cctld"]

    # MX checker called at most once per unique candidate (cache hit on
    # the seed avoids that one entirely).
    assert mx_calls.count("acme.io") == 1


def test_discovery_service_skips_seed_in_results() -> None:
    class EchoCrt:
        def query(self, seed: str) -> list[str]:
            return [seed, "sister.com"]

    service = DomainDiscoveryService(
        crt_sh_client=EchoCrt(),
        mx_checker=lambda _d: True,
    )
    result = service.discover("acme.com")
    assert "acme.com" not in result.domains
    assert "sister.com" in result.domains


def test_discovery_service_tolerates_failing_source() -> None:
    class BrokenCrt:
        def query(self, seed: str) -> list[str]:
            raise RuntimeError("upstream is down")

    class WorkingSpf:
        def discover(self, seed: str) -> list[str]:
            return ["sister.io"]

    service = DomainDiscoveryService(
        crt_sh_client=BrokenCrt(),
        spf_dmarc=WorkingSpf(),
        mx_checker=lambda _d: True,
    )
    result = service.discover("acme.com")
    # The working source still contributed despite the broken one.
    assert "sister.io" in result.domains
