"""TDD for the CompanyEmailHarvester.

The harvester fetches a small set of likely-public pages on the company's
own domain (homepage + /contato, /sobre, /team, /about, /contact) and
extracts e-mail addresses found in ``mailto:`` links or plain text. Only
addresses on the company's own domain are kept — third-party support
mails (``support@some-saas.com``) and personal accounts are filtered out.

External HTTP is injected via ``http_client(url) -> (status, html)`` so
tests stay offline and deterministic.
"""

from __future__ import annotations

from beautiful_linkedin.storage.company_email_harvester import (
    CompanyEmailHarvester,
    HarvestedEmail,
)


class FakeHttpClient:
    """Map ``url -> (status, html)``. Records every URL we were asked for."""

    def __init__(self, responses: dict[str, tuple[int, str]]) -> None:
        self.responses = responses
        self.requests: list[str] = []

    def __call__(self, url: str) -> tuple[int, str]:
        self.requests.append(url)
        return self.responses.get(url, (404, ""))


# ---- happy paths ----------------------------------------------------------


def test_harvester_extracts_mailto_emails_from_homepage() -> None:
    html = """
    <html><body>
      <a href="mailto:contato@empresa.com">Fale conosco</a>
      <p>Vendas: <a href="mailto:vendas@empresa.com">vendas@empresa.com</a></p>
    </body></html>
    """
    client = FakeHttpClient({"https://empresa.com/": (200, html)})
    harvester = CompanyEmailHarvester(http_client=client, paths=[])
    results = harvester.harvest("empresa.com")
    emails = {r.email for r in results}
    assert emails == {"contato@empresa.com", "vendas@empresa.com"}


def test_harvester_extracts_plain_text_emails() -> None:
    html = """
    <html><body>
      <p>Time: ana.silva@empresa.com e bruno.costa@empresa.com</p>
    </body></html>
    """
    client = FakeHttpClient({"https://empresa.com/": (200, html)})
    harvester = CompanyEmailHarvester(http_client=client, paths=[])
    emails = {r.email for r in harvester.harvest("empresa.com")}
    assert "ana.silva@empresa.com" in emails
    assert "bruno.costa@empresa.com" in emails


def test_harvester_visits_extra_paths() -> None:
    """The harvester should visit /contato, /sobre, /team, /about, /contact."""
    home = "<a href='mailto:home@empresa.com'>x</a>"
    contato = "<p>contato@empresa.com</p>"
    sobre = "<a href='mailto:carla@empresa.com'>carla</a>"
    client = FakeHttpClient(
        {
            "https://empresa.com/": (200, home),
            "https://empresa.com/contato": (200, contato),
            "https://empresa.com/sobre": (200, sobre),
            "https://empresa.com/team": (404, ""),
            "https://empresa.com/about": (404, ""),
            "https://empresa.com/contact": (404, ""),
        }
    )
    harvester = CompanyEmailHarvester(http_client=client)
    emails = {r.email for r in harvester.harvest("empresa.com")}
    assert emails == {"home@empresa.com", "contato@empresa.com", "carla@empresa.com"}
    assert "https://empresa.com/contato" in client.requests
    assert "https://empresa.com/sobre" in client.requests


# ---- filtering ------------------------------------------------------------


def test_harvester_drops_emails_from_other_domains() -> None:
    html = """
      <a href="mailto:vendas@empresa.com">ok</a>
      <a href="mailto:support@some-saas.com">saas</a>
      <p>ceo@gmail.com (pessoal)</p>
    """
    client = FakeHttpClient({"https://empresa.com/": (200, html)})
    harvester = CompanyEmailHarvester(http_client=client, paths=[])
    emails = {r.email for r in harvester.harvest("empresa.com")}
    assert emails == {"vendas@empresa.com"}


def test_harvester_treats_root_and_www_as_same_domain() -> None:
    """``empresa.com`` and ``www.empresa.com`` should both yield emails."""
    html = """
      <a href="mailto:ok@empresa.com">x</a>
      <a href="mailto:ok2@www.empresa.com">y</a>
    """
    client = FakeHttpClient({"https://empresa.com/": (200, html)})
    harvester = CompanyEmailHarvester(http_client=client, paths=[])
    emails = {r.email for r in harvester.harvest("empresa.com")}
    assert "ok@empresa.com" in emails
    assert "ok2@www.empresa.com" in emails


def test_harvester_dedupes_emails_seen_in_multiple_places() -> None:
    home = "<a href='mailto:contato@empresa.com'>a</a> contato@empresa.com"
    contato = "Email: contato@empresa.com"
    client = FakeHttpClient(
        {
            "https://empresa.com/": (200, home),
            "https://empresa.com/contato": (200, contato),
        }
    )
    harvester = CompanyEmailHarvester(http_client=client, paths=["contato"])
    results = harvester.harvest("empresa.com")
    emails = [r.email for r in results]
    assert emails.count("contato@empresa.com") == 1


# ---- failure modes --------------------------------------------------------


def test_harvester_returns_empty_when_domain_missing() -> None:
    client = FakeHttpClient({})
    assert CompanyEmailHarvester(http_client=client).harvest("") == []
    assert CompanyEmailHarvester(http_client=client).harvest(None) == []


def test_harvester_skips_non_200_responses() -> None:
    client = FakeHttpClient(
        {
            "https://empresa.com/": (500, "<a href='mailto:should@not.be.seen'>x</a>"),
            "https://empresa.com/contato": (200, "<a href='mailto:ok@empresa.com'>x</a>"),
        }
    )
    harvester = CompanyEmailHarvester(http_client=client, paths=["contato"])
    emails = {r.email for r in harvester.harvest("empresa.com")}
    assert emails == {"ok@empresa.com"}


def test_harvester_swallows_client_exceptions() -> None:
    """An exception fetching one page must not abort the whole harvest."""

    def flaky(url: str) -> tuple[int, str]:
        if "contato" in url:
            raise RuntimeError("boom")
        return (200, "<a href='mailto:ok@empresa.com'>x</a>")

    harvester = CompanyEmailHarvester(http_client=flaky, paths=["contato"])
    emails = {r.email for r in harvester.harvest("empresa.com")}
    assert emails == {"ok@empresa.com"}


def test_harvested_email_records_source_url() -> None:
    html = "<a href='mailto:ok@empresa.com'>x</a>"
    client = FakeHttpClient({"https://empresa.com/": (200, html)})
    results = CompanyEmailHarvester(http_client=client, paths=[]).harvest("empresa.com")
    assert len(results) == 1
    assert results[0] == HarvestedEmail(
        email="ok@empresa.com", source_url="https://empresa.com/"
    )
