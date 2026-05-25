from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from beautiful_linkedin.storage.telegram_group_playwright_lookup import (
    TelegramConsultResult,
)
from beautiful_linkedin.storage.telegram_telethon_lookup import (
    TelegramActionThrottle,
    TelethonBotConsultBase,
    TelethonBotResponse,
    TelethonGonzalesNameConsult,
    TelethonTelegramConsultOrchestrator,
    TelethonVoidNameConsult,
    _compose_raw_text,
    _is_result_message,
    _looks_like_loading,
    _strip_html,
)


@dataclass
class _FakeButton:
    url: str | None = None
    text: str | None = None


@dataclass
class _FakeMessage:
    raw_text: str | None = None
    buttons: list[list[_FakeButton]] = field(default_factory=list)
    media: Any = None
    id: int = 0
    out: bool = False
    clicked: list[str] = field(default_factory=list)

    @property
    def message(self) -> str | None:
        return self.raw_text

    async def click(self, *, text: str) -> None:
        self.clicked.append(text)


class _FakeConversation:
    """Replays a queued list of bot messages via ``get_response``."""

    def __init__(self, messages: list[_FakeMessage]):
        self._messages = list(messages)
        self.sent: list[str] = []

    async def __aenter__(self) -> "_FakeConversation":
        return self

    async def __aexit__(self, *_: object) -> None:  # noqa: D401
        return None

    async def send_message(self, query: str) -> None:
        self.sent.append(query)

    async def get_response(self) -> _FakeMessage:
        if not self._messages:
            await asyncio.sleep(10)  # let the outer wait_for time out
            raise asyncio.TimeoutError
        return self._messages.pop(0)


class _FakeClient:
    def __init__(self, conversation: _FakeConversation):
        self._conversation = conversation
        self.downloaded: list[Any] = []
        self.deleted: list[list[int]] = []

    def conversation(self, _entity: Any, timeout: float) -> _FakeConversation:  # noqa: ARG002
        return self._conversation

    async def get_messages(self, _entity: Any, limit: int) -> list[_FakeMessage]:  # noqa: ARG002
        return []

    async def delete_messages(self, _entity: Any, ids: list[int], revoke: bool = True) -> None:  # noqa: ARG002
        self.deleted.append(ids)

    async def download_media(self, message: Any, file: str) -> str:  # noqa: ARG002
        self.downloaded.append(message)
        return f"{file}/media.bin"


class _FakeGonCleanupClient:
    def __init__(self) -> None:
        self.deleted: list[tuple[Any, list[int], bool]] = []

    async def get_messages(self, entity: Any, limit: int) -> list[_FakeMessage]:  # noqa: ARG002
        return [
            _FakeMessage(raw_text="old", id=1),
            _FakeMessage(raw_text="old result", id=2),
        ]

    async def delete_messages(self, entity: Any, ids: list[int], revoke: bool = True) -> None:
        self.deleted.append((entity, ids, revoke))


class _FakePollingClient:
    def __init__(self, snapshots: list[list[_FakeMessage]], *, download_text: str):
        self._snapshots = list(snapshots)
        self.sent: list[tuple[Any, str]] = []
        self.downloaded: list[Any] = []
        self._download_text = download_text

    async def send_message(self, entity: Any, query: str) -> None:
        self.sent.append((entity, query))

    async def get_messages(
        self, _entity: Any, limit: int, min_id: int | None = None
    ) -> list[_FakeMessage]:  # noqa: ARG002
        if not self._snapshots:
            return []
        return self._snapshots.pop(0)

    async def download_media(self, message: Any, file: str) -> str:  # noqa: ARG002
        from pathlib import Path

        self.downloaded.append(message)
        path = Path(file) / "NRC-MAURICIOCERRI.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self._download_text, encoding="utf-8")
        return str(path)


class _FakeDelayedDownloadPollingClient(_FakePollingClient):
    async def download_media(self, message: Any, file: str) -> str:  # noqa: ARG002
        from pathlib import Path

        self.downloaded.append(message)
        path = Path(file) / "NRC-MAURICIOCERRI.txt"
        path.parent.mkdir(parents=True, exist_ok=True)

        async def write_later() -> None:
            await asyncio.sleep(0.05)
            path.write_text(self._download_text, encoding="utf-8")

        asyncio.create_task(write_later())
        return str(path)


def test_telethon_gonzales_consult_sends_nome_command_and_returns_result() -> None:
    calls: list[tuple[str, str]] = []

    def requester(username: str, query: str) -> TelethonBotResponse:
        calls.append((username, query))
        return TelethonBotResponse(
            raw_text="Nome: Ana Silva\nCPF: 111.222.333-44",
            source_url="https://t.me/ConsultoriaGonzalesbot",
            downloaded_at="2026-05-22T10:00:00+00:00",
        )

    consult = TelethonGonzalesNameConsult(requester=requester)

    result = consult.consult("Ana Silva")

    assert calls == [("@ConsultoriaGonzalesbot", "/nome Ana Silva")]
    assert result == TelegramConsultResult(
        provider="gon",
        lead_name="Ana Silva",
        query="/nome Ana Silva",
        raw_text="Nome: Ana Silva\nCPF: 111.222.333-44",
        source_url="https://t.me/ConsultoriaGonzalesbot",
        downloaded_at="2026-05-22T10:00:00+00:00",
        error=None,
    )


def test_telethon_gonzales_clears_recent_private_chat_messages() -> None:
    client = _FakeGonCleanupClient()
    consult = TelethonGonzalesNameConsult()

    asyncio.run(consult._clear_private_chat_before_query(client, "gon-entity"))

    assert client.deleted == [("gon-entity", [1, 2], True)]


def test_telethon_consult_is_fail_soft_when_credentials_are_missing() -> None:
    result = TelethonGonzalesNameConsult(api_id=None, api_hash=None).consult("Ana Silva")

    assert result.provider == "gon"
    assert result.query == "/nome Ana Silva"
    assert result.raw_text is None
    assert result.error == "telegram_not_configured"


def test_telethon_failure_log_omits_provider_name_and_lead(caplog) -> None:
    def requester(username: str, query: str) -> TelethonBotResponse:  # noqa: ARG001
        raise RuntimeError("boom")

    consult = TelethonGonzalesNameConsult(requester=requester)

    with caplog.at_level("DEBUG", logger="beautiful_linkedin.storage.telegram_telethon_lookup"):
        result = consult.consult("Ana Silva")

    assert result.raw_text is None
    assert result.error == "RuntimeError:boom"
    assert "Telethon Telegram consult failed" in caplog.text
    assert "gon" not in caplog.text
    assert "Ana Silva" not in caplog.text


def test_strip_html_drops_scripts_styles_and_tags() -> None:
    html = """
    <html><head><style>.x { color: red; }</style></head>
    <body>
      <script>var x = 1;</script>
      <h1>Nome: Ana</h1>
      <p>CPF: <b>111.222.333-44</b></p>
      <br/>
      Telefone&nbsp;(11)&nbsp;99999-9999
    </body></html>
    """
    text = _strip_html(html)
    assert "Nome: Ana" in text
    assert "111.222.333-44" in text
    assert "Telefone" in text
    assert "var x" not in text
    assert "color: red" not in text


def test_looks_like_loading_matches_known_phrases() -> None:
    assert _looks_like_loading("Buscando informação, aguarde!")
    assert _looks_like_loading("Consultando...\nProcessando sua solicitação.")
    assert _looks_like_loading("Carregando dados")
    assert not _looks_like_loading("Nome: Ana Silva\nCPF: 111.222.333-44")
    assert not _looks_like_loading(None)


def test_is_result_message_prefers_button_or_media_over_text() -> None:
    loading = _FakeMessage(raw_text="Aguarde, processando...")
    assert not _is_result_message(loading)

    loading_with_button = _FakeMessage(
        raw_text="Aguarde, processando...",
        buttons=[[_FakeButton(url="https://example.com/r/123")]],
    )
    assert _is_result_message(loading_with_button)

    text_only_result = _FakeMessage(raw_text="Resultado: Ana Silva")
    assert _is_result_message(text_only_result)


def test_compose_raw_text_returns_only_scraped_page_when_available() -> None:
    # When the bot announces a clickable link AND we manage to scrape
    # the link's page, the persisted ``raw_text`` is ONLY the scraped
    # content. The chatty bot announcement ("✅ Consulta concluída… 👇
    # Clique no botão…") is discarded — it is boilerplate, not data,
    # and the URL itself stays on ``source_url``.
    composed = _compose_raw_text(
        bot_text="✅ Resultado disponível\n👇 Clique no botão",
        url="https://results.example.com/r/abc",
        page_text="Nome: Ana\nCPF: 111.222.333-44",
    )
    assert composed == "Nome: Ana\nCPF: 111.222.333-44"
    # No section headers, no bot text bleed-through.
    assert "Resultado disponível" not in composed
    assert "Clique no botão" not in composed
    assert "===" not in composed


def test_compose_raw_text_falls_back_to_link_marker_when_no_page() -> None:
    no_page = _compose_raw_text(
        bot_text="✅ Resultado disponível",
        url="https://results.example.com/r/abc",
        page_text=None,
    )
    # Without a scraped page we keep a minimal marker so the row is not
    # empty — but the chatty bot text still does not leak through.
    assert no_page == "Link: https://results.example.com/r/abc"
    assert "Resultado disponível" not in no_page


def test_compose_raw_text_keeps_bot_text_when_no_url_to_follow() -> None:
    # Some bot variants return inline data without a clickable link.
    # In that case the bot text IS the evidence — keep it.
    composed = _compose_raw_text(
        bot_text="Nome: Ana Silva\nCPF: 111.222.333-44",
        url=None,
        page_text=None,
    )
    assert composed == "Nome: Ana Silva\nCPF: 111.222.333-44"


def test_compose_raw_text_returns_none_when_nothing_useful() -> None:
    assert _compose_raw_text(None, None, None) is None


def test_compose_raw_text_renders_fetch_diagnostic_clean() -> None:
    from beautiful_linkedin.storage.telegram_telethon_lookup import (
        _FETCH_DIAGNOSTIC_PREFIX,
        _format_fetch_failure,
    )

    diagnostic = _format_fetch_failure(["cdp_connect_failed:ConnectionError"])
    composed = _compose_raw_text(
        bot_text="✅ Consulta concluída\n👇 Clique no botão",
        url="https://example.com/r/x",
        page_text=diagnostic,
    )
    assert composed is not None
    # The diagnostic text is surfaced directly — no sentinel prefix, no
    # ``=== ... ===`` wrapping, and no bot-text bleed-through.
    assert "Não consegui abrir o link" in composed
    assert "cdp_connect_failed" in composed
    assert _FETCH_DIAGNOSTIC_PREFIX not in composed
    assert "===" not in composed
    assert "Consulta concluída" not in composed


def test_response_from_message_skips_loading_and_scrapes_button_url() -> None:
    loading = _FakeMessage(raw_text="Buscando informação, aguarde!")
    real = _FakeMessage(
        raw_text="✅ Resultado pronto",
        buttons=[[_FakeButton(url="https://results.example.com/r/abc")]],
    )
    conv = _FakeConversation([loading, real])
    client = _FakeClient(conv)
    fetched: list[str] = []

    def fake_fetcher(url: str) -> str:
        fetched.append(url)
        return "Nome: Ana Silva\nCPF: 111.222.333-44"

    consult = TelethonBotConsultBase(url_fetcher=fake_fetcher)

    async def run() -> Any:
        message = await consult._await_result_message(client, conv, entity=object())
        return await consult._response_from_message(client, message)

    response = asyncio.run(run())

    assert fetched == ["https://results.example.com/r/abc"]
    assert response.source_url == "https://results.example.com/r/abc"
    # ``raw_text`` is the scraped page ONLY — the bot's chatty
    # announcement is discarded as boilerplate.
    assert response.raw_text == "Nome: Ana Silva\nCPF: 111.222.333-44"


def test_response_from_message_falls_back_when_fetch_returns_nothing() -> None:
    real = _FakeMessage(
        raw_text="✅ Resultado",
        buttons=[[_FakeButton(url="https://example.com/r/x")]],
    )
    conv = _FakeConversation([real])
    client = _FakeClient(conv)

    def fake_fetcher(url: str) -> str | None:  # noqa: ARG001
        return None

    consult = TelethonBotConsultBase(url_fetcher=fake_fetcher)

    async def run() -> Any:
        message = await consult._await_result_message(client, conv, entity=object())
        return await consult._response_from_message(client, message)

    response = asyncio.run(run())

    assert response.source_url == "https://example.com/r/x"
    # When the scrape fails completely we keep a minimal "Link: …" marker
    # so the row still carries the URL the operator can open manually,
    # but the bot's chatty announcement is NOT replayed into raw_text.
    assert response.raw_text == "Link: https://example.com/r/x"
    assert "✅ Resultado" not in (response.raw_text or "")


def test_await_result_message_times_out_on_loading_only() -> None:
    loading = _FakeMessage(raw_text="Aguarde...")
    conv = _FakeConversation([loading])
    client = _FakeClient(conv)
    consult = TelethonBotConsultBase(
        url_fetcher=lambda _u: None,
        result_wait_seconds=0.5,
        result_poll_interval=0.1,
    )

    async def run() -> Any:
        return await consult._await_result_message(client, conv, entity=object())

    # Loading-shaped messages do not satisfy the result contract; the
    # consult must raise so the orchestrator records the timeout cleanly
    # instead of persisting "Aguarde..." as the lead's evidence.
    with pytest.raises(RuntimeError, match="telethon_result_timeout"):
        asyncio.run(run())


def test_await_result_message_raises_when_no_messages_arrive() -> None:
    conv = _FakeConversation([])
    client = _FakeClient(conv)
    consult = TelethonBotConsultBase(
        url_fetcher=lambda _u: None,
        result_wait_seconds=0.3,
        result_poll_interval=0.1,
    )

    async def run() -> Any:
        return await consult._await_result_message(client, conv, entity=object())

    with pytest.raises(RuntimeError, match="telethon_result_timeout"):
        asyncio.run(run())


def test_telethon_orchestrator_runs_finder_gon_unix_void_in_order() -> None:
    seen: list[tuple[str, str]] = []

    def requester(username: str, query: str) -> str:
        seen.append((username, query))
        return f"Resposta de {username}"

    orchestrator = TelethonTelegramConsultOrchestrator(requester=requester)

    results = orchestrator.consult("Ana Silva")

    assert orchestrator.provider_names == ("finder", "gon", "unix", "void")
    assert [result.provider for result in results] == ["finder", "gon", "unix", "void"]
    assert seen == [
        ("@FdxGP_bot", "/nome Ana Silva"),
        ("@ConsultoriaGonzalesbot", "/nome Ana Silva"),
        ("@UnixGruposRobot", "/nome Ana Silva"),
        ("@CONSULTASGRATIS4NV", "/nome Ana Silva"),
    ]


def test_telethon_void_consult_sends_nome_to_group_clicks_receita_and_downloads_txt(
    tmp_path,
) -> None:
    menu = _FakeMessage(
        raw_text="🔎 SELECIONE UMA BASE",
        id=10,
        buttons=[
            [_FakeButton(text="NACIONAL"), _FakeButton(text="SI-PNI")],
            [_FakeButton(text="RECEITA"), _FakeButton(text="VOID")],
        ],
    )
    txt = _FakeMessage(
        raw_text="NRC-MAURICIOCERRI.txt",
        id=11,
        media=object(),
    )
    client = _FakePollingClient(
        snapshots=[[menu], [txt]],
        download_text="- MODULO NOME RECEITA : Mauricio Cerri\nCPF: 111.222.333-44",
    )
    consult = TelethonVoidNameConsult(
        artifact_dir=tmp_path,
        result_poll_interval=0.01,
        throttle=TelegramActionThrottle(min_spacing_seconds=0),
    )

    async def run() -> TelethonBotResponse:
        return await consult._run_void_flow(
            client,
            send_entity="@CONSULTASGRATIS4NV",
            read_entity="@CONSULTASGRATIS4NV",
            query="/nome Mauricio Cerri",
            baseline_id=0,
        )

    response = asyncio.run(run())

    assert client.sent == [("@CONSULTASGRATIS4NV", "/nome Mauricio Cerri")]
    assert menu.clicked == ["RECEITA"]
    assert client.downloaded == [txt]
    assert response.raw_text is not None
    assert "=== telegram_bot_message ===" in response.raw_text
    assert "NRC-MAURICIOCERRI.txt" in response.raw_text
    assert "=== telegram_artifact:NRC-MAURICIOCERRI.txt ===" in response.raw_text
    assert "- MODULO NOME RECEITA : Mauricio Cerri" in response.raw_text
    assert response.source_url == "https://web.telegram.org/k/#@CONSULTASGRATIS4NV"


def test_telethon_void_waits_for_txt_file_before_persisting_raw_text(tmp_path) -> None:
    menu = _FakeMessage(
        raw_text="🔎 SELECIONE UMA BASE",
        id=10,
        buttons=[[_FakeButton(text="RECEITA")]],
    )
    txt = _FakeMessage(
        raw_text="NRC-MAURICIOCERRI.txt",
        id=11,
        media=object(),
    )
    client = _FakeDelayedDownloadPollingClient(
        snapshots=[[menu], [txt]],
        download_text="- MODULO NOME RECEITA : Mauricio Cerri\nCPF: 111.222.333-44",
    )
    consult = TelethonVoidNameConsult(
        artifact_dir=tmp_path,
        result_poll_interval=0.01,
        throttle=TelegramActionThrottle(min_spacing_seconds=0),
    )

    async def run() -> TelethonBotResponse:
        return await consult._run_void_flow(
            client,
            send_entity="@CONSULTASGRATIS4NV",
            read_entity="@CONSULTASGRATIS4NV",
            query="/nome Mauricio Cerri",
            baseline_id=0,
        )

    response = asyncio.run(run())

    assert response.raw_text is not None
    assert "=== telegram_artifact:NRC-MAURICIOCERRI.txt ===" in response.raw_text
    assert "- MODULO NOME RECEITA : Mauricio Cerri" in response.raw_text
    assert "CPF: 111.222.333-44" in response.raw_text


# ---------------------------------------------------------------------------
# Access remediation: bot /start + group join (#5)
# ---------------------------------------------------------------------------


# Names must match the real Telethon classes — the classifier keys on
# ``type(exc).__name__``, not the imported symbol.
class ChatWriteForbiddenError(Exception):
    """Stand-in matching the Telethon class name our classifier keys on."""


class YouBlockedUserError(Exception):
    pass


class _AccessFakeClient:
    """Minimal client recording sends and replaying scripted failures."""

    def __init__(self, *, history: list[Any] | None = None, fail_with: type[Exception] | None = None):
        self._history = list(history or [])
        self._fail_with = fail_with
        self.sent: list[Any] = []

    async def get_messages(self, _entity: Any, limit: int = 1, **_: Any) -> list[Any]:  # noqa: ARG002
        return list(self._history)

    async def send_message(self, _entity: Any, text: str) -> None:
        if self._fail_with is not None:
            raise self._fail_with("nope")
        self.sent.append(text)


def test_classify_access_error_maps_known_telethon_errors():
    from beautiful_linkedin.storage.telegram_telethon_lookup import _classify_access_error

    assert _classify_access_error(ChatWriteForbiddenError()) == "telethon_not_in_group"
    assert _classify_access_error(YouBlockedUserError()) == "telethon_blocked_bot"
    assert _classify_access_error(ValueError("x")) is None


def test_ensure_bot_started_presses_start_when_no_history():
    from beautiful_linkedin.storage.telegram_telethon_lookup import (
        _ensure_bot_conversation_started,
    )

    client = _AccessFakeClient(history=[])
    throttle = TelegramActionThrottle(min_spacing_seconds=0)
    asyncio.run(_ensure_bot_conversation_started(client, "@bot", throttle))
    assert client.sent == ["/start"]


def test_ensure_bot_started_noop_when_history_exists():
    from beautiful_linkedin.storage.telegram_telethon_lookup import (
        _ensure_bot_conversation_started,
    )

    client = _AccessFakeClient(history=[object()])
    throttle = TelegramActionThrottle(min_spacing_seconds=0)
    asyncio.run(_ensure_bot_conversation_started(client, "@bot", throttle))
    assert client.sent == []


def test_guarded_send_raises_stable_code_on_write_forbidden():
    from beautiful_linkedin.storage.telegram_telethon_lookup import _guarded_send_message

    client = _AccessFakeClient(history=[object()], fail_with=ChatWriteForbiddenError)
    throttle = TelegramActionThrottle(min_spacing_seconds=0)

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(
            _guarded_send_message(client, "@grupo", "/nome X", throttle, is_group=True)
        )
    assert str(excinfo.value) == "telethon_not_in_group"


def test_guarded_send_succeeds_normally():
    from beautiful_linkedin.storage.telegram_telethon_lookup import _guarded_send_message

    client = _AccessFakeClient(history=[object()])
    throttle = TelegramActionThrottle(min_spacing_seconds=0)
    asyncio.run(_guarded_send_message(client, "@bot", "/nome X", throttle, is_group=False))
    assert client.sent == ["/nome X"]
