"""Offline tests for PhoneNumberHarvester.

The harvester is the entry point of the free phone-discovery pipeline,
so it has to extract numbers from every encoding we expect to see on
company pages: ``tel:`` anchors, ``wa.me`` links, JSON-LD ``telephone``
fields, and raw prose. We feed it canned HTML through a fake HTTP client
to keep the suite zero-network.
"""

from __future__ import annotations

from beautiful_linkedin.storage.phone_harvester import (
    PhoneNumberHarvester,
    iter_jsonld_phones,
)


def _client(pages: dict[str, str]):
    def fetch(url: str) -> tuple[int, str]:
        return (200, pages[url]) if url in pages else (404, "")

    return fetch


def test_harvester_extracts_phone_from_tel_anchor() -> None:
    pages = {
        "https://empresa.com/": (
            '<html><body><a href="tel:+551130303030">Fale conosco</a></body></html>'
        ),
    }
    harvester = PhoneNumberHarvester(http_client=_client(pages), paths=[])
    results = harvester.harvest("empresa.com")
    assert len(results) == 1
    found = results[0]
    assert found.context == "tel"
    assert found.digits == "551130303030"
    assert found.source_url == "https://empresa.com/"


def test_harvester_extracts_whatsapp_link_in_any_form() -> None:
    pages = {
        "https://empresa.com/": (
            '<html><body>'
            '<a href="https://wa.me/5511999998888">WhatsApp</a>'
            '<a href="https://api.whatsapp.com/send?phone=5511777776666&text=Oi">SAC</a>'
            '</body></html>'
        ),
    }
    harvester = PhoneNumberHarvester(http_client=_client(pages), paths=[])
    digits = sorted(r.digits for r in harvester.harvest("empresa.com"))
    assert "5511999998888" in digits
    assert "5511777776666" in digits
    assert all(r.context == "whatsapp" for r in harvester.harvest("empresa.com"))


def test_harvester_extracts_jsonld_telephone() -> None:
    pages = {
        "https://empresa.com/": (
            '<html><head>'
            '<script type="application/ld+json">'
            '{"@type":"Organization","telephone":"+55 11 4002-8922"}'
            '</script></head><body></body></html>'
        ),
    }
    harvester = PhoneNumberHarvester(http_client=_client(pages), paths=[])
    results = harvester.harvest("empresa.com")
    assert any(r.context == "jsonld" for r in results)
    assert any(r.digits == "551140028922" for r in results)


def test_harvester_extracts_phone_from_raw_text() -> None:
    pages = {
        "https://empresa.com/": (
            "<html><body><p>Telefone: (11) 99999-9999</p></body></html>"
        ),
    }
    harvester = PhoneNumberHarvester(http_client=_client(pages), paths=[])
    results = harvester.harvest("empresa.com")
    assert len(results) == 1
    assert results[0].context == "text"
    assert results[0].digits == "11999999999"


def test_harvester_skips_call_center_prefixes_and_repetitive_digits() -> None:
    pages = {
        "https://empresa.com/": (
            "<html><body>"
            "<p>SAC: 0800 123 4567</p>"
            "<p>Apoio: 4004-2020</p>"
            "<p>Placeholder: (00) 00000-0000</p>"
            "<p>Comercial: (11) 3030-4040</p>"
            "</body></html>"
        ),
    }
    harvester = PhoneNumberHarvester(http_client=_client(pages), paths=[])
    digits = {r.digits for r in harvester.harvest("empresa.com")}
    assert digits == {"1130304040"}


def test_harvester_dedupes_same_number_across_paths() -> None:
    pages = {
        "https://empresa.com/": (
            '<a href="tel:+551130303030">Tel</a>'
        ),
        "https://empresa.com/contato": (
            '<p>Ligue: +55 11 3030-3030</p>'
        ),
    }
    harvester = PhoneNumberHarvester(http_client=_client(pages), paths=["contato"])
    results = harvester.harvest("empresa.com")
    # Same number on two pages: keep the first encounter only.
    assert [r.digits for r in results] == ["551130303030"]
    assert results[0].source_url == "https://empresa.com/"


def test_harvester_handles_http_errors_and_empty_pages() -> None:
    def fetch(url: str) -> tuple[int, str]:
        if url.endswith("/"):
            return (500, "internal server error")
        return (200, "")

    harvester = PhoneNumberHarvester(http_client=fetch, paths=["contato"])
    assert harvester.harvest("empresa.com") == []


def test_harvester_returns_empty_for_blank_domain() -> None:
    harvester = PhoneNumberHarvester(http_client=_client({}))
    assert harvester.harvest(None) == []
    assert harvester.harvest("") == []
    assert harvester.harvest("   ") == []


def test_iter_jsonld_phones_walks_nested_payloads() -> None:
    html = (
        '<script type="application/ld+json">'
        '{"@type":"Organization","contactPoint":{"telephone":"+551130303030"}}'
        '</script>'
    )
    assert iter_jsonld_phones(html) == ["+551130303030"]


def test_harvester_strips_scripts_before_text_regex() -> None:
    """Cache-buster integers inside JS would otherwise look like phones.

    A 10-digit timestamp in a ``<script>`` block (cache buster, GA event
    id) used to leak through the permissive text regex and pollute the
    candidate list. The harvester must skip script/style content for the
    text path while still letting JSON-LD ``telephone`` fields through.
    """
    pages = {
        "https://empresa.com/": (
            '<html><body>'
            '<script>const buster = 1730000000000;</script>'
            '<p>Comercial: (11) 3030-4040</p>'
            '</body></html>'
        ),
    }
    harvester = PhoneNumberHarvester(http_client=_client(pages), paths=[])
    digits = {r.digits for r in harvester.harvest("empresa.com")}
    assert digits == {"1130304040"}
