"""Unit tests for the SISREG-III flow in :class:`GonzalesCpfConsult`.

Cobre os helpers que executam a parte custosa do fluxo /cpf:

1. Esperar o botão SISREG-III aparecer numa nova mensagem do Gonzales.
2. Esperar a SEGUNDA nova mensagem (post-SISREG) cujo texto contém
   'consulta' + o CPF consultado.
3. Achar e clicar o "ver resultado completo" DENTRO desse bubble.

Os testes mockam ``page``/``locator`` em vez de instanciar Playwright,
mantendo a suíte offline e instantânea.
"""

from __future__ import annotations

from typing import Any

import pytest

from beautiful_linkedin.storage.telegram_group_playwright_lookup import (
    FindexCpfConsult,
    GON_RESULT_BUTTON_SELECTOR,
    GON_SISREG_BUTTON_SELECTOR,
    GON_DELETE_BUTTON_SELECTOR,
    FindexEmailConsult,
    FindexNameConsult,
    GonzalesBotConsult,
    GonzalesCpfConsult,
    UnixBotConsult,
)


# ---- mock plumbing ---------------------------------------------------------


class _LocatorList:
    """Mock minimal de ``page.locator(selector)`` que devolve um
    container com ``.count()``, ``.last``, ``.first``, ``.nth(i)``,
    ``.inner_text()`` e ``.filter()`` o quanto for usado pelos helpers
    testados."""

    def __init__(self, items: list[dict[str, Any]]):
        self._items = items

    def count(self) -> int:
        return len(self._items)

    @property
    def last(self) -> "_BubbleHandle":
        return _BubbleHandle(self._items[-1])

    @property
    def first(self) -> "_BubbleHandle":
        return _BubbleHandle(self._items[0])

    def nth(self, index: int) -> "_BubbleHandle":
        return _BubbleHandle(self._items[index])


class _BubbleHandle:
    def __init__(self, payload: dict[str, Any]):
        self._payload = payload

    def inner_text(self, timeout: int = 400) -> str:  # noqa: ARG002
        return self._payload.get("text", "")

    def locator(self, selector: str) -> _LocatorList:
        return _LocatorList(self._payload.get("locators", {}).get(selector, []))

    def click(self) -> None:
        self._payload["clicks"] = self._payload.get("clicks", 0) + 1

    def scroll_into_view_if_needed(self) -> None:
        pass


class _PageMock:
    """``page.locator(selector)`` chave -> lista de itens. ``stepper``
    permite que o teste mude o estado entre polls (simulando o bot
    escrevendo novas mensagens)."""

    def __init__(self, snapshots: list[dict[str, list[dict[str, Any]]]]):
        self._snapshots = snapshots
        self._index = 0

    def advance(self) -> None:
        if self._index < len(self._snapshots) - 1:
            self._index += 1

    def locator(self, selector: str) -> _LocatorList:
        state = self._snapshots[self._index]
        items = state.get(selector, [])
        return _LocatorList(items)


# ---- post-send wait default (contrato anti-loading) -----------------------


def test_all_telegram_drivers_post_send_wait_default_to_sixteen_seconds() -> None:
    """Todos os bots (Gonzales /nome, Gonzales /cpf, Unix, Findex)
    mostram bubbles transitórios ("Consultando..."/"Aguarde...") logo
    após o Enter. Sair direto pro poll dá flake — 16s fixos dão tempo
    do loading sumir antes da automação inspecionar o estado. O default
    vive no ``TelegramGroupConsultBase`` para que todo driver herde."""
    assert GonzalesBotConsult()._post_send_wait_seconds == 16.0
    assert GonzalesCpfConsult()._post_send_wait_seconds == 16.0
    assert FindexEmailConsult()._post_send_wait_seconds == 16.0
    assert FindexNameConsult()._post_send_wait_seconds == 16.0
    assert FindexCpfConsult()._post_send_wait_seconds == 16.0
    assert UnixBotConsult()._post_send_wait_seconds == 16.0


def test_gonzales_cpf_action_waits_default_to_sixteen_seconds() -> None:
    driver = GonzalesCpfConsult()

    assert driver._sisreg_post_click_wait_seconds == 16.0
    assert driver._sisreg_button_timeout_seconds == 16.0
    assert driver._consulta_bubble_timeout_seconds == 16.0


def test_gonzales_post_send_wait_can_be_overridden() -> None:
    """O default herda pra subclasses, mas testes/operadores podem
    sobrescrever. A base clampa o mínimo em 0.5s."""
    assert GonzalesCpfConsult(post_send_wait_seconds=2.0)._post_send_wait_seconds == 2.0


def test_findex_name_and_cpf_queries_use_expected_commands() -> None:
    assert FindexNameConsult()._build_query("Ana Silva") == "/nome Ana Silva"
    assert FindexCpfConsult()._build_query("111.222.333-44") == "/cpf 111.222.333-44"


def test_gonzales_name_result_button_is_scoped_to_matching_query_bubble() -> None:
    """Quando existem duas respostas com botão no chat, o /nome deve
    clicar no bubble que ecoa a query recém-enviada, não no botão global
    ``last`` que pode vir ordenado diferente pelo Telegram Web."""
    mauricio_button = {"text": "Ver resultado completo", "id": "mauricio"}
    isabela_button = {"text": "Ver resultado completo", "id": "isabela"}
    page = _PageMock(
        snapshots=[
            {
                "div.bubble": [
                    {
                        "text": "/nome Mauricio Cerri\nConsulta concluída.",
                        "locators": {GON_RESULT_BUTTON_SELECTOR: [mauricio_button]},
                    },
                    {
                        "text": "/nome Isabela Neves de Souza\nConsulta concluída.",
                        "locators": {GON_RESULT_BUTTON_SELECTOR: [isabela_button]},
                    },
                ],
                # Ordem global propositalmente errada para reproduzir o bug:
                # se o código usar locator(selector).last, clicaria Mauricio.
                GON_RESULT_BUTTON_SELECTOR: [isabela_button, mauricio_button],
            }
        ]
    )

    driver = GonzalesBotConsult()
    button = driver._result_button_for_query(
        page, GON_RESULT_BUTTON_SELECTOR, "/nome Isabela Neves de Souza"
    )
    button.click()

    assert isabela_button.get("clicks") == 1
    assert mauricio_button.get("clicks") is None


def test_gonzales_cleanup_clicks_visible_apagar_buttons_before_query() -> None:
    buttons = [{"text": "Apagar"}, {"text": "Apagar"}]
    page = _PageMock(snapshots=[{GON_DELETE_BUTTON_SELECTOR: buttons}])

    GonzalesBotConsult()._clear_gonzales_private_chat(page)

    assert [button.get("clicks") for button in buttons] == [1, 1]


# ---- _extract_cpf_from_query (pure) ----------------------------------------


def test_extract_cpf_from_query_strips_mask() -> None:
    driver = GonzalesCpfConsult()
    assert driver._extract_cpf_from_query("/cpf 111.222.333-44") == "11122233344"
    assert driver._extract_cpf_from_query("/cpf 11122233344") == "11122233344"
    assert driver._extract_cpf_from_query("/cpf ") == ""


# ---- _wait_for_sisreg_button -----------------------------------------------


def test_wait_for_sisreg_button_returns_when_new_button_appears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """O botão SISREG-III só aparece no 2º poll — o helper deve esperar
    sem estourar timeout e retornar normalmente."""
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    page = _PageMock(
        snapshots=[
            # poll 1: nenhum botão novo, nenhum bubble extra
            {
                GON_SISREG_BUTTON_SELECTOR: [],
                "div.bubble": [{"text": "/cpf 11122233344"}],
            },
            # poll 2: bot devolveu mensagem com o SISREG-III
            {
                GON_SISREG_BUTTON_SELECTOR: [{"text": "SISREG-III"}],
                "div.bubble": [
                    {"text": "/cpf 11122233344"},
                    {"text": "Resultado preliminar\nSISREG-III"},
                ],
            },
        ]
    )

    real_sleep_calls: list[float] = []

    def fake_sleep(seconds: float) -> None:
        real_sleep_calls.append(seconds)
        page.advance()

    monkeypatch.setattr(
        "beautiful_linkedin.storage.telegram_group_playwright_lookup.time.sleep",
        fake_sleep,
    )

    driver = GonzalesCpfConsult(sisreg_button_timeout_seconds=5.0)
    driver._wait_for_sisreg_button(
        page, initial_button_count=0, initial_bubble_count=1
    )

    # Deve ter avançado pelo menos uma vez.
    assert real_sleep_calls, "helper deve ter dormido entre polls"


def test_wait_for_sisreg_button_aborts_on_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Se o Gonzales devolve 'uso excessivo' antes do SISREG-III, o
    helper deve abortar com erro tagged para o orquestrador trocar para
    Unix."""
    monkeypatch.setattr(
        "beautiful_linkedin.storage.telegram_group_playwright_lookup.time.sleep",
        lambda _: None,
    )

    page = _PageMock(
        snapshots=[
            {
                GON_SISREG_BUTTON_SELECTOR: [],
                "div.bubble": [
                    {"text": "/cpf 11122233344"},
                    {"text": "Uso excessivo. Tente novamente em 30 minutos."},
                ],
            }
        ]
    )

    driver = GonzalesCpfConsult(sisreg_button_timeout_seconds=2.0)
    with pytest.raises(RuntimeError, match="gon_rate_limit"):
        driver._wait_for_sisreg_button(
            page, initial_button_count=0, initial_bubble_count=1
        )


def test_wait_for_sisreg_button_raises_timeout_when_button_never_arrives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "beautiful_linkedin.storage.telegram_group_playwright_lookup.time.sleep",
        lambda _: None,
    )

    page = _PageMock(
        snapshots=[
            {
                GON_SISREG_BUTTON_SELECTOR: [],
                "div.bubble": [{"text": "/cpf 11122233344"}],
            }
        ]
    )

    driver = GonzalesCpfConsult(sisreg_button_timeout_seconds=0.0)
    with pytest.raises(RuntimeError, match="sisreg_timeout"):
        driver._wait_for_sisreg_button(
            page, initial_button_count=0, initial_bubble_count=1
        )


# ---- _wait_for_post_sisreg_result_button -----------------------------------


def test_wait_for_post_sisreg_result_button_returns_when_count_grows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Após o clique no SISREG-III + sleep fixo, o helper polla até a
    contagem de "ver resultado completo" crescer e retorna sem erro.
    Não tenta casar texto/CPF do bubble — funciona mesmo quando o bot
    masca o CPF na resposta SISREG."""
    page = _PageMock(
        snapshots=[
            # Estado inicial pós-clique: ainda só o bubble de loading,
            # botão "ver resultado completo" não apareceu.
            {
                GON_RESULT_BUTTON_SELECTOR: [],
                "div.bubble": [
                    {"text": "/cpf 11122233344"},
                    {"text": "menu SISREG-III"},
                    {"text": "Consultando..."},
                ],
            },
            # Bot terminou de renderizar: 1 botão novo apareceu.
            {
                GON_RESULT_BUTTON_SELECTOR: [{"text": "ver resultado completo"}],
                "div.bubble": [
                    {"text": "/cpf 11122233344"},
                    {"text": "menu SISREG-III"},
                    {"text": "Consulta SISREG concluída\nver resultado completo"},
                ],
            },
        ]
    )

    def fake_sleep(_seconds: float) -> None:
        page.advance()

    monkeypatch.setattr(
        "beautiful_linkedin.storage.telegram_group_playwright_lookup.time.sleep",
        fake_sleep,
    )

    driver = GonzalesCpfConsult(consulta_bubble_timeout_seconds=5.0)
    # Deve retornar sem exceção.
    driver._wait_for_post_sisreg_result_button(
        page, initial_button_count=0, initial_bubble_count=3
    )


def test_wait_for_post_sisreg_result_button_aborts_on_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "beautiful_linkedin.storage.telegram_group_playwright_lookup.time.sleep",
        lambda _: None,
    )
    page = _PageMock(
        snapshots=[
            {
                GON_RESULT_BUTTON_SELECTOR: [],
                "div.bubble": [
                    {"text": "/cpf 11122233344"},
                    {"text": "uso excessivo — tente mais tarde"},
                ],
            }
        ]
    )
    driver = GonzalesCpfConsult(consulta_bubble_timeout_seconds=2.0)
    with pytest.raises(RuntimeError, match="gon_rate_limit"):
        driver._wait_for_post_sisreg_result_button(
            page, initial_button_count=0, initial_bubble_count=1
        )


def test_wait_for_post_sisreg_result_button_ignores_loading_bubble_for_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """'Consultando...' não deve disparar nenhum detector de falha; o
    helper deve continuar pollando até o botão real aparecer."""
    page = _PageMock(
        snapshots=[
            {
                GON_RESULT_BUTTON_SELECTOR: [],
                "div.bubble": [
                    {"text": "/cpf 11122233344"},
                    {"text": "Consultando..."},
                ],
            },
            {
                GON_RESULT_BUTTON_SELECTOR: [{"text": "ver resultado completo"}],
                "div.bubble": [
                    {"text": "/cpf 11122233344"},
                    {"text": "Resultado pronto\nver resultado completo"},
                ],
            },
        ]
    )

    def fake_sleep(_seconds: float) -> None:
        page.advance()

    monkeypatch.setattr(
        "beautiful_linkedin.storage.telegram_group_playwright_lookup.time.sleep",
        fake_sleep,
    )

    driver = GonzalesCpfConsult(consulta_bubble_timeout_seconds=5.0)
    driver._wait_for_post_sisreg_result_button(
        page, initial_button_count=0, initial_bubble_count=2
    )


def test_wait_for_post_sisreg_result_button_timeouts_with_specific_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "beautiful_linkedin.storage.telegram_group_playwright_lookup.time.sleep",
        lambda _: None,
    )
    page = _PageMock(
        snapshots=[
            {
                GON_RESULT_BUTTON_SELECTOR: [],
                "div.bubble": [{"text": "/cpf 11122233344"}],
            }
        ]
    )
    driver = GonzalesCpfConsult(consulta_bubble_timeout_seconds=0.0)
    with pytest.raises(RuntimeError, match="post_sisreg_timeout"):
        driver._wait_for_post_sisreg_result_button(
            page, initial_button_count=0, initial_bubble_count=1
        )


# ---- _find_latest_consulta_bubble_with_cpf ---------------------------------


def test_find_latest_consulta_bubble_picks_bubble_with_matching_cpf() -> None:
    """Há vários bubbles; só o último com 'consulta' + CPF do alvo deve
    ser devolvido."""
    page = _PageMock(
        snapshots=[
            {
                "div.bubble": [
                    {"text": "/cpf 11122233344"},
                    {"text": "consulta concluída\nCPF: 999.999.999-99"},
                    {"text": "consulta concluída\nCPF: 111.222.333-44"},
                ]
            }
        ]
    )
    driver = GonzalesCpfConsult()
    bubble = driver._find_latest_consulta_bubble_with_cpf(
        page, cpf="11122233344"
    )
    assert bubble is not None
    assert "111.222.333-44" in bubble.inner_text()


def test_find_latest_consulta_bubble_ignores_old_cpf_bubble() -> None:
    """Bubble com CPF de outra consulta não deve casar."""
    page = _PageMock(
        snapshots=[
            {
                "div.bubble": [
                    {"text": "consulta concluída\nCPF: 999.999.999-99"},
                ]
            }
        ]
    )
    driver = GonzalesCpfConsult()
    bubble = driver._find_latest_consulta_bubble_with_cpf(
        page, cpf="11122233344"
    )
    assert bubble is None


def test_find_latest_consulta_bubble_matches_unmasked_cpf() -> None:
    """O bot às vezes serializa o CPF sem máscara — comparação só de
    dígitos deve casar."""
    page = _PageMock(
        snapshots=[
            {
                "div.bubble": [
                    {"text": "consulta concluída CPF 11122233344"},
                ]
            }
        ]
    )
    driver = GonzalesCpfConsult()
    bubble = driver._find_latest_consulta_bubble_with_cpf(
        page, cpf="111.222.333-44"
    )
    assert bubble is not None


def test_find_latest_consulta_bubble_requires_consulta_keyword() -> None:
    """Bubble com CPF mas sem a palavra 'consulta' (ex.: o /cpf inicial
    do usuário) não deve ser confundido com a resposta SISREG."""
    page = _PageMock(
        snapshots=[
            {
                "div.bubble": [
                    {"text": "/cpf 111.222.333-44"},
                ]
            }
        ]
    )
    driver = GonzalesCpfConsult()
    bubble = driver._find_latest_consulta_bubble_with_cpf(
        page, cpf="11122233344"
    )
    assert bubble is None


def test_find_latest_consulta_bubble_skips_consultando_loading_bubble() -> None:
    """'Consultando...' contém a substring 'consulta' mas é o loading
    transitório do Gonzales — nunca pode casar."""
    page = _PageMock(
        snapshots=[
            {
                "div.bubble": [
                    {"text": "Consultando..."},
                ]
            }
        ]
    )
    driver = GonzalesCpfConsult()
    bubble = driver._find_latest_consulta_bubble_with_cpf(
        page, cpf="11122233344"
    )
    assert bubble is None


def test_find_latest_consulta_bubble_skips_consultando_even_with_cpf() -> None:
    """Defensa: mesmo se o bot embutir o CPF dentro do bubble de loading,
    o filtro de loading deve preceder e descartar."""
    page = _PageMock(
        snapshots=[
            {
                "div.bubble": [
                    {"text": "Consultando... 111.222.333-44"},
                ]
            }
        ]
    )
    driver = GonzalesCpfConsult()
    bubble = driver._find_latest_consulta_bubble_with_cpf(
        page, cpf="11122233344"
    )
    assert bubble is None


def test_find_latest_consulta_bubble_respects_min_index() -> None:
    """O bubble pre-SISREG já tem 'consulta concluída' + CPF, mas o
    matcher só pode considerar bubbles APÓS o snapshot que tiramos
    imediatamente antes de clicar no SISREG-III. Caso contrário,
    clicaríamos no "ver resultado completo" da resposta antiga, sem o
    telefone."""
    page = _PageMock(
        snapshots=[
            {
                "div.bubble": [
                    # 0: bubble pre-SISREG (tem 'consulta' + CPF)
                    {"text": "consulta concluída\nCPF: 111.222.333-44"},
                    # 1: /cpf do usuário (sem 'consulta')
                    {"text": "/cpf 111.222.333-44"},
                    # 2: SISREG-III foi clicado aqui — snapshot = 2
                    # 3: loading transitório
                    {"text": "Consultando..."},
                    # 4: resposta real pós-SISREG
                    {"text": "consulta concluída\nCPF: 111.222.333-44\nTelefone disponível"},
                ]
            }
        ]
    )
    driver = GonzalesCpfConsult()
    bubble = driver._find_latest_consulta_bubble_with_cpf(
        page, cpf="11122233344", min_index=2
    )
    assert bubble is not None
    # Deve ter pegado o ÍNDICE 4 (texto com "Telefone disponível"), não
    # o índice 0 (pre-SISREG).
    assert "Telefone disponível" in bubble.inner_text()


def test_find_latest_consulta_bubble_returns_none_when_only_loading_after_snapshot() -> None:
    """Antes da resposta real chegar (só "Consultando..." pós-SISREG),
    o matcher deve devolver None — sinaliza para o caller continuar
    pollando."""
    page = _PageMock(
        snapshots=[
            {
                "div.bubble": [
                    {"text": "consulta concluída\nCPF: 111.222.333-44"},  # pre-SISREG
                    {"text": "/cpf 111.222.333-44"},
                    {"text": "Consultando..."},
                ]
            }
        ]
    )
    driver = GonzalesCpfConsult()
    bubble = driver._find_latest_consulta_bubble_with_cpf(
        page, cpf="11122233344", min_index=2
    )
    assert bubble is None
