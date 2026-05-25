from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from beautiful_linkedin.models import Lead
from beautiful_linkedin.server.app import build_app
from beautiful_linkedin.storage.linkedin_profile_validation import (
    _extract_birthday,
    extract_profile_validation_snapshot,
)
from beautiful_linkedin.storage.saved_leads import SavedLeadsStore


def _lead(
    *,
    person: str = "Ana Silva",
    title: str = "Head of Marketing",
    company: str = "Nubank",
    linkedin_url: str = "https://www.linkedin.com/in/ana-silva/",
    email: str | None = "ana.silva@nubank.com.br",
    phone: str | None = None,
) -> Lead:
    return Lead(
        company_name=company,
        company_domain="nubank.com.br",
        person_name=person,
        title=title,
        linkedin_url=linkedin_url,
        email=email,
        phone=phone,
        source_url=linkedin_url,
        source_type="linkedin_people_search",
        snippet="",
        confidence_score=92,
    )


def test_linkedin_profile_extractor_reads_experience_and_contact_info() -> None:
    from beautiful_linkedin.storage.linkedin_profile_validation import (
        extract_profile_validation_snapshot,
    )

    profile_html = """
    <main>
      <section>
        <h2>Experiência</h2>
        <div>
          <span aria-hidden="true">Head of Growth</span>
          <span aria-hidden="true">Nubank</span>
          <span aria-hidden="true">mai de 2024 - o momento</span>
        </div>
      </section>
    </main>
    """
    contact_html = """
    <section>
      <h2>Informações de contato</h2>
      <a href="mailto:ana@gmail.com">ana@gmail.com</a>
      <a href="https://portfolio.example/ana">Portfolio</a>
      <span>+55 11 99999-0000</span>
    </section>
    """

    update = extract_profile_validation_snapshot(
        lead=_lead(),
        profile_html=profile_html,
        contact_html=contact_html,
        profile_url="https://www.linkedin.com/in/ana-silva/",
    )

    assert update.status == "validated"
    assert update.experience_title == "Head of Growth"
    assert update.experience_company == "Nubank"
    assert update.experience_start_year == 2024
    assert update.contact_email == "ana@gmail.com"
    assert update.contact_website == "https://portfolio.example/ana"
    assert update.contact_phone == "+55 11 99999-0000"


def test_linkedin_profile_extractor_ignores_skip_to_main_content_noise() -> None:
    from beautiful_linkedin.storage.linkedin_profile_validation import (
        extract_profile_validation_snapshot,
    )

    profile_html = """
    <main>
      <a href="#main">Pular para conteúdo principal</a>
      <section>
        <h2>Experiência</h2>
        <span aria-hidden="true">Pular para conteúdo principal</span>
        <span aria-hidden="true">Head of Growth</span>
        <span aria-hidden="true">Nubank</span>
        <span aria-hidden="true">mai de 2024 - o momento</span>
      </section>
    </main>
    """

    update = extract_profile_validation_snapshot(
        lead=_lead(),
        profile_html=profile_html,
        contact_html="",
        profile_url="https://www.linkedin.com/in/ana-silva/",
    )

    assert update.experience_title == "Head of Growth"
    assert update.experience_company == "Nubank"
    assert update.raw["experience_period"] == "mai de 2024 - o momento"


def test_linkedin_profile_extractor_skips_noise_in_positional_fallback() -> None:
    # When aria-hidden spans are absent the structured extractor returns nothing
    # and the textual fallback walks backwards from the year. Without the noise
    # filter the skip-link two lines above the year hijacked ``title``; this
    # exercises the fallback path with HTML that has no aria attributes.
    from beautiful_linkedin.storage.linkedin_profile_validation import (
        extract_profile_validation_snapshot,
    )

    profile_html = """
    <main>
      <p>Pular para conteúdo principal</p>
      <p>Experiência</p>
      <p>Head of Growth</p>
      <p>Nubank</p>
      <p>mai de 2024 - o momento</p>
    </main>
    """

    update = extract_profile_validation_snapshot(
        lead=_lead(),
        profile_html=profile_html,
        contact_html="",
        profile_url="https://www.linkedin.com/in/ana-silva/",
    )

    assert update.experience_title == "Head of Growth"
    assert update.experience_company == "Nubank"


def test_linkedin_profile_extractor_ignores_skip_noise_without_period() -> None:
    from beautiful_linkedin.storage.linkedin_profile_validation import (
        extract_profile_validation_snapshot,
    )

    profile_html = """
    <main>
      <section>
        <h2>Experiência</h2>
        <span aria-hidden="true">Pular para conteúdo principal</span>
        <span aria-hidden="true">Head of Growth</span>
        <span aria-hidden="true">Nubank</span>
      </section>
    </main>
    """

    update = extract_profile_validation_snapshot(
        lead=_lead(),
        profile_html=profile_html,
        contact_html="",
        profile_url="https://www.linkedin.com/in/ana-silva/",
    )

    assert update.experience_title == "Head of Growth"
    assert update.experience_company == "Nubank"


def test_linkedin_profile_extractor_reads_location_and_education_signals() -> None:
    from beautiful_linkedin.storage.linkedin_profile_validation import (
        extract_profile_validation_snapshot,
    )

    profile_html = """
    <main>
      <section>
        <span aria-hidden="true">Ana Silva</span>
        <span aria-hidden="true">São Paulo, São Paulo, Brasil</span>
      </section>
      <section>
        <h2>Formação acadêmica</h2>
        <span aria-hidden="true">Universidade de São Paulo</span>
        <span aria-hidden="true">Bacharelado, Administração</span>
        <span aria-hidden="true">2003 - 2007</span>
      </section>
    </main>
    """

    update = extract_profile_validation_snapshot(
        lead=_lead(),
        profile_html=profile_html,
        contact_html="",
        profile_url="https://www.linkedin.com/in/ana-silva/",
    )

    assert update.status == "validated"
    assert update.location == "São Paulo, São Paulo, Brasil"
    assert update.education == [
        {
            "institution": "Universidade de São Paulo",
            "degree": "Bacharelado, Administração",
            "end_year": 2007,
            "period": "2003 - 2007",
        }
    ]


def test_linkedin_profile_extractor_reads_junior_experience_period() -> None:
    from beautiful_linkedin.storage.linkedin_profile_validation import (
        extract_profile_validation_snapshot,
    )

    profile_html = """
    <main>
      <section>
        <h2>Experiência</h2>
        <div>
          <span aria-hidden="true">Analista de Marketing Junior</span>
          <span aria-hidden="true">Acme</span>
          <span aria-hidden="true">jan de 2022 - dez de 2023</span>
        </div>
      </section>
    </main>
    """

    update = extract_profile_validation_snapshot(
        lead=_lead(),
        profile_html=profile_html,
        contact_html="",
        profile_url="https://www.linkedin.com/in/ana-silva/",
    )

    assert update.experience_title == "Analista de Marketing Junior"
    assert update.experience_company == "Acme"
    assert update.experience_start_year == 2022
    assert update.experience_end_year == 2023


def test_linkedin_profile_extractor_reads_contact_info_modal_site_redirect() -> None:
    from beautiful_linkedin.storage.linkedin_profile_validation import (
        extract_profile_validation_snapshot,
    )

    contact_html = """
    <section>
      <h2>Informações de contato</h2>
      <a href="https://www.linkedin.com/in/lucianodiisouza/">linkedin.com/in/lucianodiisouza</a>
      <a href="https://www.linkedin.com/safety/go?url=https%3A%2F%2Foprimo.dev%2F&trk=flagship3_profile_self_view_contact-info">
        oprimo.dev/ (Blog)
      </a>
    </section>
    """

    update = extract_profile_validation_snapshot(
        lead=_lead(),
        profile_html="",
        contact_html=contact_html,
        profile_url="https://www.linkedin.com/in/lucianodiisouza/",
    )

    assert update.status == "validated"
    assert update.contact_website == "https://oprimo.dev/"


def test_profile_validation_persists_linkedin_contact_without_overwriting_existing_data(
    tmp_path: Path,
) -> None:
    from beautiful_linkedin.storage.linkedin_profile_validation import (
        LinkedInProfileValidationUpdate,
    )

    store = SavedLeadsStore(tmp_path / "saved.sqlite")
    table = store.create_table(name="Nubank")
    store.add_leads(table.id, [_lead(email="ana@nubank.com.br", phone="+551140028922")])
    saved = store.list_leads(table.id)[0]

    counters = store.apply_linkedin_profile_validation_updates(
        table.id,
        [
            (
                saved,
                LinkedInProfileValidationUpdate(
                    status="validated",
                    experience_title="Head of Growth",
                    experience_company="Nubank Brasil",
                    experience_start_year=2024,
                    contact_email="ana@gmail.com",
                    contact_website="https://portfolio.example/ana",
                    contact_phone="+55 11 99999-0000",
                    raw={"profile_url": saved.linkedin_url},
                ),
            )
        ],
    )

    assert counters["validated"] == 1
    enriched = store.list_leads(table.id)[0]
    assert enriched.title == "Head of Growth"
    assert enriched.company_name == "Nubank"
    assert enriched.email == "ana@nubank.com.br"
    assert enriched.phone == "+551140028922"
    assert enriched.linkedin_experience_title == "Head of Growth"
    assert enriched.linkedin_experience_company == "Nubank Brasil"
    assert enriched.linkedin_experience_start_year == 2024
    assert enriched.linkedin_contact_email == "ana@gmail.com"
    assert enriched.linkedin_contact_website == "https://portfolio.example/ana"
    assert enriched.linkedin_contact_phone == "+55 11 99999-0000"
    assert len(enriched.email_alternatives) == 1
    assert enriched.email_alternatives[0]["email"] == "ana@gmail.com"
    assert enriched.email_alternatives[0]["source"] == "linkedin_contact"
    assert enriched.phone_alternatives[0]["phone"] == "+55 11 99999-0000"
    assert enriched.phone_alternatives[0]["source"] == "linkedin_contact"


def test_profile_validation_cleans_persisted_skip_title_noise(
    tmp_path: Path,
) -> None:
    from beautiful_linkedin.storage.linkedin_profile_validation import (
        LinkedInProfileValidationUpdate,
    )

    store = SavedLeadsStore(tmp_path / "saved.sqlite")
    table = store.create_table(name="Nubank")
    polluted = _lead(title="Pular para conteúdo principal", email=None)
    polluted.linkedin_experience_title = "Pular para conteúdo principal"
    store.add_leads(table.id, [polluted])
    saved = store.list_leads(table.id)[0]

    store.apply_linkedin_profile_validation_updates(
        table.id,
        [
            (
                saved,
                LinkedInProfileValidationUpdate(
                    status="validated",
                    contact_email="ana.linkedin@gmail.com",
                    raw={"profile_url": saved.linkedin_url},
                ),
            )
        ],
    )

    enriched = store.list_leads(table.id)[0]
    assert enriched.title != "Pular para conteúdo principal"
    assert enriched.linkedin_experience_title != "Pular para conteúdo principal"
    assert enriched.linkedin_contact_email == "ana.linkedin@gmail.com"


def test_cdp_fetcher_clicks_contact_info_modal_before_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import beautiful_linkedin.storage.linkedin_profile_validation as module
    from beautiful_linkedin.storage.linkedin_profile_validation import (
        CdpLinkedInProfilePageFetcher,
    )

    class FakePage:
        def __init__(self) -> None:
            self.url = ""
            self.clicked_contact = False
            self.visited: list[str] = []

        def set_default_timeout(self, _timeout: int) -> None:
            return None

        def goto(self, url: str, wait_until: str = "domcontentloaded") -> None:
            self.url = url
            self.visited.append(url)

        def content(self) -> str:
            if self.clicked_contact:
                return "<section><h2>Informações de contato</h2><a href='mailto:ana@gmail.com'>ana@gmail.com</a></section>"
            if self.url.endswith("/details/experience/"):
                return "<section><h2>Experiência</h2><span aria-hidden='true'>Head of Growth</span></section>"
            return ""

        def get_by_text(self, text: str, exact: bool = True) -> "FakeLocator":
            assert text == "Informações de contato"
            assert exact is False
            return FakeLocator(self)

        def close(self) -> None:
            return None

    class FakeLocator:
        def __init__(self, page: FakePage) -> None:
            self._page = page

        @property
        def first(self) -> "FakeLocator":
            return self

        def click(self, timeout: int = 5000) -> None:
            self._page.clicked_contact = True

    class FakeContext:
        def __init__(self) -> None:
            self.page = FakePage()

        def new_page(self) -> FakePage:
            return self.page

    monkeypatch.setattr(module, "_settle_like_human", lambda *args, **kwargs: None)
    fetcher = CdpLinkedInProfilePageFetcher.__new__(CdpLinkedInProfilePageFetcher)
    fetcher._context = FakeContext()
    fetcher._navigation_timeout_ms = 30_000
    fetcher._min_delay_seconds = 0
    fetcher._max_delay_seconds = 0

    snapshot = fetcher.fetch("https://www.linkedin.com/in/ana-silva/")

    assert "mailto:ana@gmail.com" in snapshot.contact_html
    assert "https://www.linkedin.com/in/ana-silva/" in fetcher._context.page.visited
    assert not any("/overlay/contact-info/" in url for url in fetcher._context.page.visited)


def test_linkedin_profile_validation_endpoint_updates_selected_leads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from beautiful_linkedin.storage.linkedin_profile_validation import (
        LinkedInProfileValidationUpdate,
    )
    import beautiful_linkedin.server.app as app_module

    app = build_app(saved_leads_path=str(tmp_path / "saved.sqlite"))
    client = TestClient(app)
    create = client.post(
        "/lead-tables",
        json={"name": "Nubank", "leads": [_lead().model_dump(mode="json")]},
    )
    table_id = create.json()["table"]["id"]

    def fake_run(
        *,
        leads: list[Lead],
        settings: Any,
        max_leads: int,
        on_event: Any = None,
        cancel_check: Any = None,
    ) -> list[tuple[Lead, LinkedInProfileValidationUpdate]]:
        assert max_leads == 40
        assert len(leads) == 1
        return [
            (
                leads[0],
                LinkedInProfileValidationUpdate(
                    status="validated",
                    experience_title="Head of Growth",
                    experience_company="Nubank",
                    contact_email="ana@gmail.com",
                    contact_website="https://portfolio.example/ana",
                ),
            )
        ]

    monkeypatch.setattr(app_module, "_run_linkedin_profile_validation", fake_run)

    response = client.post(
        f"/lead-tables/{table_id}/linkedin-profile-validate",
        json={"lead_refs": ["https://www.linkedin.com/in/ana-silva/"]},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["summary"]["requested_leads"] == 1
    assert body["summary"]["validated_leads"] == 1
    lead = body["leads"][0]
    assert lead["linkedin_experience_title"] == "Head of Growth"
    assert lead["linkedin_contact_email"] == "ana@gmail.com"


def test_profile_validation_falls_back_to_playwright_cookie_when_cdp_is_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import beautiful_linkedin.server.app as app_module
    from beautiful_linkedin.config import Settings
    from beautiful_linkedin.storage.linkedin_profile_validation import (
        ProfileHtmlSnapshot,
    )

    class _FallbackFetcher:
        def __enter__(self) -> "_FallbackFetcher":
            return self

        def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
            return None

        def fetch(self, profile_url: str) -> ProfileHtmlSnapshot:
            return ProfileHtmlSnapshot(
                profile_url=profile_url,
                profile_html=(
                    "<section><h2>Experiência</h2>"
                    "<span aria-hidden='true'>Head of Growth</span>"
                    "<span aria-hidden='true'>Nubank</span>"
                    "<span aria-hidden='true'>o momento</span></section>"
                ),
                contact_html="<a href='mailto:ana@gmail.com'>ana@gmail.com</a>",
            )

    monkeypatch.setattr(app_module, "probe_cdp_endpoint", lambda _endpoint: False)
    monkeypatch.setattr(
        app_module,
        "_build_profile_validation_cookie_fetcher",
        lambda settings: _FallbackFetcher(),
    )

    results = app_module._run_linkedin_profile_validation(
        leads=[_lead()],
        settings=Settings(linkedin_li_at_cookie="li_at=fake"),
        max_leads=40,
    )

    assert len(results) == 1
    _, update = results[0]
    assert update.status == "validated"
    assert update.experience_title == "Head of Growth"
    assert update.contact_email == "ana@gmail.com"


def test_linkedin_profile_extractor_reads_multi_role_experience_structured() -> None:
    from beautiful_linkedin.storage.linkedin_profile_validation import (
        extract_profile_validation_snapshot,
    )

    profile_html = """
    <section id="experience">
      <h2>Experiência</h2>
      <ul>
        <li>
          <div>
            <span>TOTVS</span>
            <span>Tempo integral · 2 a 3 m</span>
            <span>Belo Horizonte, Minas Gerais, Brasil · Híbrido</span>
          </div>
          <ul>
            <li>
              <div>
                <span>Analista I de suporte de RH</span>
                <span>out de 2025 - o momento · 8 meses</span>
              </div>
            </li>
            <li>
              <div>
                <span>Técnico de suporte de RH</span>
                <span>mar de 2024 - out de 2025 · 1 ano 8 meses</span>
              </div>
            </li>
          </ul>
        </li>
      </ul>
    </section>
    """

    update = extract_profile_validation_snapshot(
        lead=_lead(),
        profile_html=profile_html,
        contact_html="",
        profile_url="https://www.linkedin.com/in/ana-silva/",
    )

    assert update.status == "validated"
    assert update.experience_title == "Analista I de suporte de RH"
    assert update.experience_company == "TOTVS"
    assert update.experience_start_year == 2025
    assert update.experience_end_year is None


def test_linkedin_profile_extractor_reads_single_role_experience_structured() -> None:
    from beautiful_linkedin.storage.linkedin_profile_validation import (
        extract_profile_validation_snapshot,
    )

    profile_html = """
    <section id="experience">
      <h2>Experiência</h2>
      <ul>
        <li>
          <div>
            <span>Head of Growth</span>
            <span>Nubank</span>
            <span>mai de 2024 - o momento</span>
          </div>
        </li>
      </ul>
    </section>
    """

    update = extract_profile_validation_snapshot(
        lead=_lead(),
        profile_html=profile_html,
        contact_html="",
        profile_url="https://www.linkedin.com/in/ana-silva/",
    )

    assert update.status == "validated"
    assert update.experience_title == "Head of Growth"
    assert update.experience_company == "Nubank"
    assert update.experience_start_year == 2024
    assert update.experience_end_year is None


def test_linkedin_profile_extractor_reads_div_layout_single_role() -> None:
    from beautiful_linkedin.storage.linkedin_profile_validation import (
        extract_profile_validation_snapshot,
    )

    profile_html = """
    <section id="experience">
      <h2>Experiência</h2>
      <div>
        <div componentkey="entity-collection-item-1234">
          <span>Assistente administrativo</span>
          <span>Marlabs Brasil · Tempo integral</span>
          <span>fev de 2025 - o momento · 1 ano 4 meses</span>
        </div>
      </div>
    </section>
    """

    update = extract_profile_validation_snapshot(
        lead=_lead(),
        profile_html=profile_html,
        contact_html="",
        profile_url="https://www.linkedin.com/in/ana-silva/",
    )

    assert update.status == "validated"
    assert update.experience_title == "Assistente administrativo"
    assert update.experience_company == "Marlabs Brasil · Tempo integral"
    assert update.experience_start_year == 2025
    assert update.experience_end_year is None


def test_linkedin_profile_extractor_reads_div_layout_multi_role() -> None:
    from beautiful_linkedin.storage.linkedin_profile_validation import (
        extract_profile_validation_snapshot,
    )

    profile_html = """
    <section id="experience">
      <h2>Experiência</h2>
      <div>
        <div componentkey="entity-collection-item-5678">
          <div>
            <span>Marlabs Brasil</span>
            <span>Tempo integral · 1 ano 4 meses</span>
          </div>
          <ul>
            <li>
              <div>
                <span>Analista sênior</span>
                <span>fev de 2025 - o momento · 4 meses</span>
              </div>
            </li>
            <li>
              <div>
                <span>Analista pleno</span>
                <span>jan de 2024 - fev de 2025 · 1 ano 1 mês</span>
              </div>
            </li>
          </ul>
        </div>
      </div>
    </section>
    """

    update = extract_profile_validation_snapshot(
        lead=_lead(),
        profile_html=profile_html,
        contact_html="",
        profile_url="https://www.linkedin.com/in/ana-silva/",
    )

    assert update.status == "validated"
    assert update.experience_title == "Analista sênior"
    assert update.experience_company == "Marlabs Brasil"
    assert update.experience_start_year == 2025
    assert update.experience_end_year is None


def test_linkedin_profile_extractor_returns_none_when_experience_section_missing() -> None:
    from beautiful_linkedin.storage.linkedin_profile_validation import (
        extract_profile_validation_snapshot,
    )

    profile_html = """
    <main>
      <h1>Leticia Arielle</h1>
      <p>444.669 seguidores</p>
      <p>444.669 seguidores</p>
      <div>São Paulo, São Paulo, Brasil</div>
    </main>
    """

    update = extract_profile_validation_snapshot(
        lead=_lead(title="RH at TOTVS"),
        profile_html=profile_html,
        contact_html="",
        profile_url="https://www.linkedin.com/in/leticia-arielle/",
    )

    assert update.experience_title is None
    assert update.experience_company is None


def test_extract_birthday_pt_long_form() -> None:
    html = """
    <section>
      <h2>Dados pessoais</h2>
      <h3>Aniversário</h3>
      <p>27 de dezembro</p>
    </section>
    """
    assert _extract_birthday(html) == "27/12"


def test_extract_birthday_slash_form() -> None:
    html = """
    <section>
      <span>Birthday</span>
      <span>03/05</span>
    </section>
    """
    assert _extract_birthday(html) == "03/05"


def test_extract_birthday_returns_none_when_absent() -> None:
    html = "<html><body><p>Sem dados pessoais aqui.</p></body></html>"
    assert _extract_birthday(html) is None


def test_snapshot_populates_birthday_field() -> None:
    contact_html = """
    <div>
      <h3>Aniversário</h3>
      <p>15 de março</p>
    </div>
    """
    update = extract_profile_validation_snapshot(
        lead=_lead(),
        profile_html="",
        contact_html=contact_html,
        profile_url="https://www.linkedin.com/in/ana-silva/",
    )
    assert update.birthday == "15/03"
    assert update.status == "validated"
