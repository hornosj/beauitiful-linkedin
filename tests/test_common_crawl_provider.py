import json

import httpx

from beautiful_linkedin.models import CompanyInput
from beautiful_linkedin.providers.common_crawl import CommonCrawlProvider


def test_common_crawl_provider_skips_when_no_index_response():
    def handler(request):
        if "collinfo.json" in str(request.url):
            return httpx.Response(404, text="not found")
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    provider = CommonCrawlProvider(transport=httpx.MockTransport(handler))
    company = CompanyInput(
        company_name="Nubank",
        company_domain="nubank.com.br",
        linkedin_url=None,
        titles=["marketing"],
    )

    assert provider.find_leads(company, max_results=5, include_uncertain=False) == []


def test_common_crawl_provider_returns_lead_when_capture_matches_company():
    warc = _warc_with_html(
        "<html><head><title>Fulana Silva | Head of Marketing - Nubank | LinkedIn</title></head>"
        "<body>Fulana Silva trabalha na Nubank em marketing.</body></html>"
    )
    cdx = {
        "url": "https://br.linkedin.com/in/fulana-silva",
        "timestamp": "20250101000000",
        "digest": "abc",
        "offset": "0",
        "length": str(len(warc)),
        "filename": "crawl-data/CC-MAIN-2026-10/segments/file.warc.gz",
    }

    def handler(request):
        url = str(request.url)
        if "collinfo.json" in url:
            return httpx.Response(200, json=[{"id": "CC-MAIN-2026-10"}])
        if "CC-MAIN-2026-10-index" in url:
            return httpx.Response(200, text=json.dumps(cdx) + "\n")
        if "data.commoncrawl.org" in url:
            assert request.headers["Range"] == f"bytes=0-{len(warc) - 1}"
            return httpx.Response(206, content=warc)
        raise AssertionError(f"Unexpected request: {request.method} {url}")

    provider = CommonCrawlProvider(transport=httpx.MockTransport(handler))
    company = CompanyInput(
        company_name="Nubank",
        company_domain="nubank.com.br",
        linkedin_url=None,
        titles=["marketing"],
    )

    leads = provider.find_leads(company, max_results=5, include_uncertain=False)

    assert len(leads) == 1
    assert leads[0].person_name == "Fulana Silva"
    assert leads[0].title == "Head of Marketing"
    assert leads[0].source_type == "common_crawl"
    assert leads[0].linkedin_url == "https://br.linkedin.com/in/fulana-silva"


def test_common_crawl_provider_filters_out_capture_without_company_mention():
    warc = _warc_with_html(
        "<html><head><title>Fulana Silva | Head of Marketing - Outra Empresa | LinkedIn</title></head>"
        "<body>Fulana Silva trabalha em outra empresa.</body></html>"
    )
    cdx = {
        "url": "https://br.linkedin.com/in/fulana-silva",
        "timestamp": "20250101000000",
        "digest": "abc",
        "offset": "0",
        "length": str(len(warc)),
        "filename": "crawl-data/CC-MAIN-2026-10/segments/file.warc.gz",
    }

    def handler(request):
        url = str(request.url)
        if "collinfo.json" in url:
            return httpx.Response(200, json=[{"id": "CC-MAIN-2026-10"}])
        if "CC-MAIN-2026-10-index" in url:
            return httpx.Response(200, text=json.dumps(cdx) + "\n")
        if "data.commoncrawl.org" in url:
            return httpx.Response(206, content=warc)
        raise AssertionError(f"Unexpected request: {request.method} {url}")

    provider = CommonCrawlProvider(transport=httpx.MockTransport(handler))
    company = CompanyInput(
        company_name="Nubank",
        company_domain="nubank.com.br",
        linkedin_url=None,
        titles=["marketing"],
    )

    assert provider.find_leads(company, max_results=5, include_uncertain=False) == []


def _warc_with_html(html: str) -> bytes:
    return (
        "WARC/1.0\r\n"
        "WARC-Type: response\r\n"
        "\r\n"
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: text/html\r\n"
        "\r\n"
        f"{html}"
    ).encode("utf-8")
