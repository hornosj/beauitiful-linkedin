"""Tests for the regex-based Telegram-consult parser.

The parser must:
- Split records correctly so each CPF carries its OWN Nome/Nascimento/
  Endereço, not the previous block's data.
- Canonicalize CPFs (formatted ``111.222.333-44`` and digits-only
  ``11122233344``).
- Accept date format variations (DD/MM/YYYY).
- Return an empty extraction without raising when given empty or
  garbage input.
"""

from __future__ import annotations

from beautiful_linkedin.storage.telegram_consult_parser import (
    parse_telegram_text,
)


def test_extracts_single_candidate() -> None:
    text = (
        "Nome Completo: Ana Maria Silva\n"
        "CPF: 123.456.789-09\n"
        "Data de Nascimento: 15/03/1985\n"
        "Endereço: Rua das Flores, 123, São Paulo/SP"
    )
    e = parse_telegram_text(text, provider="gon")
    assert len(e.candidates) == 1
    c = e.candidates[0]
    assert c.cpf == "123.456.789-09"
    assert c.nome == "Ana Maria Silva"
    assert c.data_nascimento == "15/03/1985"
    assert "Rua das Flores" in (c.endereco or "")
    assert e.primary_cpf == "123.456.789-09"


def test_separates_two_records_correctly() -> None:
    """Two records in the same text must carry their own data — the
    bug we fixed is the second CPF inheriting the first record's name.
    """
    text = (
        "Nome Completo: Ana Maria Silva Costa\n"
        "CPF: 111.444.777-35\n"
        "Data de Nascimento: 15/03/1985\n"
        "Endereço: Rua das Flores, 123, São Paulo/SP\n"
        "\n"
        "Nome: Ana Silva Costa\n"
        "CPF: 222.555.888-46\n"
        "Nascimento: 20/07/1990\n"
        "Logradouro: Av. Paulista, 1000, Bela Vista, São Paulo/SP"
    )
    e = parse_telegram_text(text, provider="gon")
    assert len(e.candidates) == 2
    assert e.candidates[0].cpf == "111.444.777-35"
    assert e.candidates[0].nome == "Ana Maria Silva Costa"
    assert e.candidates[0].data_nascimento == "15/03/1985"
    assert e.candidates[1].cpf == "222.555.888-46"
    assert e.candidates[1].nome == "Ana Silva Costa"
    assert e.candidates[1].data_nascimento == "20/07/1990"


def test_accepts_digits_only_cpf() -> None:
    text = "Nome: João\nCPF 12345678909\nNascimento: 01/01/1990"
    e = parse_telegram_text(text, provider="unix")
    assert len(e.candidates) == 1
    assert e.candidates[0].cpf == "123.456.789-09"


def test_rejects_repeated_digit_cpf() -> None:
    """11 identical digits is not a real CPF; the parser must drop it
    to avoid polluting the candidate list with sentinel values."""
    text = "Nome: Foo\nCPF: 111.111.111-11"
    e = parse_telegram_text(text, provider="gon")
    assert e.candidates == []


def test_returns_empty_extraction_for_empty_input() -> None:
    e = parse_telegram_text("", provider="gon")
    assert e.candidates == []
    assert e.primary_cpf is None

    e2 = parse_telegram_text(None, provider="gon")
    assert e2.candidates == []


def test_keeps_primary_nome_when_no_cpf_present() -> None:
    """Garbage-but-labeled input still produces a primary nome so the
    operator sees what the parser DID find."""
    text = "Nome: João da Silva\nNascimento: 01/02/1990"
    e = parse_telegram_text(text, provider="gon")
    assert e.candidates == []
    assert e.primary_nome == "João da Silva"
    assert e.primary_birth_date == "01/02/1990"


def test_handles_nbsp_and_extra_whitespace() -> None:
    """Telegram pages sometimes ship non-breaking spaces inside labels;
    the normalizer must collapse them so the regex still matches."""
    text = "Nome: Ana Silva\nCPF: 123.456.789-09"
    e = parse_telegram_text(text, provider="gon")
    assert len(e.candidates) == 1
    assert e.candidates[0].nome == "Ana Silva"


def test_invalid_date_is_dropped() -> None:
    text = "Nome: Ana\nCPF: 123.456.789-09\nNascimento: 99/99/9999"
    e = parse_telegram_text(text, provider="gon")
    assert e.candidates[0].data_nascimento is None


def test_extracts_unix_dot_leader_records() -> None:
    text = (
        "══════════════════════════════════════════════════════\n"
        "MK | UNIX — RESULTADO DA CONSULTA NOME\n"
        "── 1. PESSOAS ENCONTRADAS ──────────────────────────────\n"
        "▸ PESSOA 1\n"
        "NOME....................: RODRIGO BIBIANO\n"
        "CPF.....................: 301.244.328-24\n"
        "DATA DE NASCIMENTO......: 04/07/1981\n"
        "SEXO....................: DESCONHECIDO\n"
        "NOME DA MÃE.............: ANA LUISA DE JESUS BIBIANO\n"
        "ENDEREÇO COMPLETO.......: EDMUNDO FRANCO DE CAMPOS, Nº 32, Bairro: JD NOVO II, MOGI-GUACU, SP, CEP: 13847-000\n"
        "\n"
        "▸ PESSOA 2\n"
        "NOME....................: RODRIGO BIBIANO BISPO\n"
        "CPF.....................: 166.135.966-39\n"
        "DATA DE NASCIMENTO......: 04/07/1994\n"
        "NOME DA MÃE.............: EDNEUZA BIBIANO BISPO\n"
        "ENDEREÇO COMPLETO.......: SAO LUCAS, Nº 21, Bairro: CABULA, SALVADOR, BA, CEP: 41150-000\n"
    )

    e = parse_telegram_text(text, provider="unix")

    assert len(e.candidates) == 2
    assert e.candidates[0].cpf == "301.244.328-24"
    assert e.candidates[0].nome == "RODRIGO BIBIANO"
    assert e.candidates[0].data_nascimento == "04/07/1981"
    assert "MOGI-GUACU" in (e.candidates[0].endereco or "")
    assert e.candidates[1].cpf == "166.135.966-39"
    assert e.candidates[1].nome == "RODRIGO BIBIANO BISPO"
    assert e.candidates[1].data_nascimento == "04/07/1994"
    assert "SALVADOR" in (e.candidates[1].endereco or "")
    assert "MÃE" not in (e.candidates[0].nome or "")


def test_extracts_void_receita_name_cpf_and_iso_birth_date() -> None:
    text = (
        "🔎 𝗖𝗢𝗡𝗦𝗨𝗟𝗧𝗔 𝗡𝗢𝗠𝗘 RECEITA 🕵🏻‍♂️\n\n"
        "「📄」 RESULTADOS (148):\n\n"
        "RESULTADO (1):\n\n"
        "「👤」 𝗗𝗔𝗗𝗢𝗦 𝗖𝗔𝗗𝗔𝗦𝗧𝗥𝗔𝗜𝗦\n\n"
        "- NOME: VINICIUS YAN SOUSA MELO\n"
        "- CPF: 63173598300\n"
        "- NASC: 2005-12-01\n"
    )

    e = parse_telegram_text(text, provider="void")

    assert len(e.candidates) == 1
    assert e.candidates[0].nome == "VINICIUS YAN SOUSA MELO"
    assert e.candidates[0].cpf == "631.735.983-00"
    assert e.candidates[0].data_nascimento == "01/12/2005"
    assert e.primary_nome == "VINICIUS YAN SOUSA MELO"
    assert e.primary_birth_date == "01/12/2005"
