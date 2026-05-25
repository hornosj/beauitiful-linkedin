"""Tests for the Gon failure detector.

The detector reads the latest bubble of the @CONSULTASGRATIS4NV chat
after we send ``/nome <Name>``. When the bot replies with a known
"can't help" message we want the Gon flow to fail FAST so the
orchestrator moves to Unix without waiting for the full navigation
timeout.

These tests pin the patterns so a future contributor who adds a new
variant doesn't accidentally regress the existing matches.
"""

from __future__ import annotations

from beautiful_linkedin.storage.telegram_group_playwright_lookup import (
    detect_gon_failure,
)


def test_detects_not_found_with_accent() -> None:
    result = detect_gon_failure("CPF não encontrado em nossas bases")
    assert result is not None
    assert result[0] == "gon_not_found"
    assert result[1] == "não encontrado"


def test_detects_not_found_without_accent() -> None:
    result = detect_gon_failure("nao encontrado, tente outra grafia")
    assert result is not None
    assert result[0] == "gon_not_found"


def test_detects_rate_limit_uso_excessivo() -> None:
    result = detect_gon_failure("Uso excessivo detectado, aguarde")
    assert result is not None
    assert result[0] == "gon_rate_limit"


def test_detects_rate_limit_limite_de_consultas() -> None:
    result = detect_gon_failure("Limite de consultas atingido para hoje")
    assert result is not None
    assert result[0] == "gon_rate_limit"


def test_detects_rate_limit_tente_novamente_em() -> None:
    result = detect_gon_failure("tente novamente em 10 minutos")
    assert result is not None
    assert result[0] == "gon_rate_limit"


def test_passes_through_normal_response() -> None:
    """A successful Gon reply must NOT trip the detector."""
    text = (
        "Resultado encontrado para João Silva.\n"
        "Clique em ver resultado completo para abrir."
    )
    assert detect_gon_failure(text) is None


def test_passes_through_empty_input() -> None:
    assert detect_gon_failure(None) is None
    assert detect_gon_failure("") is None
    assert detect_gon_failure("   \n  ") is None


def test_is_case_insensitive() -> None:
    assert detect_gon_failure("NÃO ENCONTRADO") is not None
    assert detect_gon_failure("Uso Excessivo") is not None


def test_does_not_match_generic_words() -> None:
    """Guard against over-broad patterns that would false-positive on
    normal replies. "erro" or "aguarde" alone must NOT abort the flow.
    """
    assert detect_gon_failure("Processando...") is None
    assert detect_gon_failure("Buscando informações") is None
    assert detect_gon_failure("Aguardando confirmação") is None
