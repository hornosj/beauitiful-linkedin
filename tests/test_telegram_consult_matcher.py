"""Tests for the candidate-vs-LinkedIn matcher.

Scoring contract pinned:
- Location-only match in same city/state → 40 (the location weight).
- Age-only match within tolerance → up to 60 (the age weight).
- Both signals matching → up to 100.
- Missing input on one side caps the total at the other signal's weight.
- The breakdown records which signals were used so the UI can show
  "Comparou: localização + idade" honestly.
"""

from __future__ import annotations

from beautiful_linkedin.storage.telegram_consult_matcher import (
    enforce_name_gate,
    score_candidate,
)
from beautiful_linkedin.storage.telegram_consult_parser import TelegramCandidate


def _candidate(**overrides):
    base = TelegramCandidate(
        cpf="111.222.333-44",
        nome="Ana Silva",
        data_nascimento="15/03/1985",
        endereco="Rua X, Vila Mariana, São Paulo/SP",
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def test_full_match_caps_at_100() -> None:
    score = score_candidate(
        _candidate(),
        lead_name="Ana Silva",
        linkedin_location="São Paulo, Brazil",
        linkedin_education=[{"institution": "USP", "end_year": 2007}],
    )
    assert score.score == 100
    assert "location" in score.signals_used
    assert "education_age" in score.signals_used


def test_mismatch_on_both_signals_scores_zero() -> None:
    score = score_candidate(
        _candidate(
            data_nascimento="10/05/1965",  # ~57 years old at 2026
            endereco="Rua Y, Belém/PA",
        ),
        linkedin_location="São Paulo, Brazil",
        linkedin_education=[{"institution": "USP", "end_year": 2007}],
    )
    assert score.score == 0


def test_only_location_matches() -> None:
    score = score_candidate(
        _candidate(data_nascimento=None),
        linkedin_location="São Paulo, Brazil",
        linkedin_education=[{"institution": "USP", "end_year": 2007}],
    )
    assert score.score == 33  # location weight + structured data quality
    assert score.signals_used == ["location", "data_quality"]


def test_only_age_matches() -> None:
    score = score_candidate(
        _candidate(endereco=None),
        linkedin_location=None,
        linkedin_education=[{"institution": "USP", "end_year": 2007}],
    )
    assert score.score == 48  # age weight + structured data quality
    assert "education_age" in score.signals_used


def test_no_signals_available_scores_zero() -> None:
    score = score_candidate(
        _candidate(endereco=None, data_nascimento=None),
        linkedin_location=None,
        linkedin_education=None,
    )
    assert score.score == 0
    assert score.signals_used == []


def test_state_only_match_is_partial() -> None:
    score = score_candidate(
        _candidate(endereco="Rua Z, Campinas/SP"),
        linkedin_location="São Paulo, Brazil",
        linkedin_education=None,
    )
    # 'sao paulo' is a city in the LinkedIn input; candidate's city is
    # 'campinas' (not in our capital list) but BOTH are SP. So state
    # matches; city doesn't. State-only → 60% of the 30-pt location
    # weight = 18, plus 5 points of complete candidate data quality.
    assert score.score == 23


def test_age_uses_earliest_grad_year_as_anchor() -> None:
    """When the lead carries multiple education entries, the matcher
    anchors on the earliest grad year (likely undergrad). That keeps
    PhD entries from skewing the estimated age."""
    score = score_candidate(
        _candidate(data_nascimento="15/03/1985", endereco=None),
        linkedin_location=None,
        linkedin_education=[
            {"institution": "USP", "end_year": 2007},
            {"institution": "MIT", "end_year": 2015},
        ],
    )
    # Anchor on 2007 (USP undergrad), expected birth ~1985 → exact.
    assert score.score == 48


def test_snippet_used_as_location_fallback() -> None:
    """When ``linkedin_location`` is missing the matcher must still
    extract a coarse location from the snippet text."""
    score = score_candidate(
        _candidate(data_nascimento=None),
        linkedin_location=None,
        linkedin_education=None,
        snippet_fallback="Senior Designer at Foo · São Paulo, Brasil",
    )
    assert "location" in score.signals_used
    assert score.score == 33


def test_score_breakdown_reports_signals_used() -> None:
    score = score_candidate(
        _candidate(),
        lead_name="Ana Silva",
        linkedin_location="São Paulo, Brazil",
        linkedin_education=[{"institution": "USP", "end_year": 2007}],
    )
    assert score.breakdown["location"]["score"] == 100
    assert score.breakdown["education_age"]["score"] == 100
    assert score.breakdown["location"]["state_match"] is True


def test_recent_junior_experience_caps_implausibly_old_candidate() -> None:
    """If LinkedIn says the person is junior in 2022, a 1947 birth date
    should not win just because address/name match.
    """
    score = score_candidate(
        _candidate(data_nascimento="10/05/1947"),
        lead_name="Ana Silva",
        linkedin_location="São Paulo, Brazil",
        linkedin_experience_title="Analista de Marketing Junior",
        linkedin_experience_years=[2022],
    )

    assert score.score <= 20
    assert "career_age" in score.signals_used
    assert score.breakdown["career_age"]["career_stage"] == "junior"
    assert score.breakdown["career_age"]["age_at_anchor_year"] == 75
    assert score.breakdown["penalties"][0]["code"] == "career_age_implausible"


def test_recent_junior_experience_rewards_plausible_age() -> None:
    score = score_candidate(
        _candidate(data_nascimento="15/03/1998"),
        lead_name="Ana Silva",
        linkedin_location="São Paulo, Brazil",
        linkedin_experience_title="Analista de Marketing Junior",
        linkedin_experience_years=[2022],
    )

    assert score.score >= 85
    assert score.breakdown["career_age"]["score"] >= 90


def test_name_mismatch_hard_rejects_even_with_good_age_and_location() -> None:
    """Hard gate: candidato com primeiro nome divergente é rejeitado
    independentemente do quão bem age/location combinem.

    Antes da regra-gate o matcher devolvia score capped em 35; agora ele
    devolve 0 + ``rejected=True`` para o ``select_followup_candidates``
    poder descartar com certeza, sem confiar em threshold de score.
    """
    score = score_candidate(
        _candidate(nome="Maria Souza", data_nascimento="15/03/1998"),
        lead_name="Ana Silva",
        linkedin_location="São Paulo, Brazil",
        linkedin_experience_title="Analista de Marketing Junior",
        linkedin_experience_years=[2022],
    )

    assert score.score == 0
    assert score.rejected is True
    assert score.breakdown["name_gate"]["passed"] is False
    assert score.breakdown["name_gate"]["reason"] == "first_name_mismatch"
    assert score.signals_used == ["name_gate"]


def test_senior_current_role_allows_older_plausible_candidate() -> None:
    score = score_candidate(
        _candidate(data_nascimento="20/08/1982"),
        lead_name="Ana Silva",
        linkedin_location="São Paulo, Brazil",
        linkedin_experience_title="Head of Marketing",
        linkedin_experience_years=[2022],
    )

    assert score.score >= 80
    assert score.breakdown["career_age"]["career_stage"] == "executive"


# ---- enforce_name_gate ---------------------------------------------------


def test_name_gate_rejects_completely_different_person() -> None:
    """Cenário do bug Totvs: lead 'Nadia Ramos', candidato extraído
    'Guilherme Ferreira'. Não deve nem chegar ao /cpf."""
    passed, reason, detail = enforce_name_gate("Nadia Ramos", "Guilherme Ferreira")
    assert passed is False
    assert reason == "first_name_mismatch"
    assert detail["first_token"] == "nadia"


def test_name_gate_accepts_middle_name_inserted() -> None:
    """'Nadia Ramos' vs 'Nadia R Ramos' (middle initial) deve passar."""
    passed, reason, _ = enforce_name_gate("Nadia Ramos", "Nadia R Ramos")
    assert passed is True
    assert reason == "ok"


def test_name_gate_accepts_lead_is_subset_of_candidate() -> None:
    """'Nadia Ramos' vs 'Nadia Ramos Silva' deve passar — sobrenome
    composto no resultado do bot ainda contém o do lead."""
    passed, reason, _ = enforce_name_gate("Nadia Ramos", "Nadia Ramos Silva")
    assert passed is True
    assert reason == "ok"


def test_name_gate_accepts_candidate_is_subset_of_lead() -> None:
    """'Nadia Ramos Silva' vs 'Nadia Ramos' deve passar — sobrenome
    abreviado pelo bot ainda valida porque primeiro e último do lead
    ('nadia' e 'silva') aparecem... espera, 'silva' não aparece em 'Nadia
    Ramos'. Esse caso DEVE reprovar para evitar falso positivo."""
    passed, reason, _ = enforce_name_gate("Nadia Ramos Silva", "Nadia Ramos")
    assert passed is False
    assert reason == "last_name_mismatch"


def test_name_gate_rejects_missing_candidate_name() -> None:
    passed, reason, _ = enforce_name_gate("Nadia Ramos", None)
    assert passed is False
    assert reason == "name_missing"


def test_name_gate_handles_single_token_lead() -> None:
    """Lead com 1 token só → exige que esse token apareça no candidato."""
    assert enforce_name_gate("Ramos", "Nadia Ramos")[0] is True
    assert enforce_name_gate("Ramos", "Ana Silva")[0] is False


def test_name_gate_ignores_stopwords_da_de() -> None:
    """'Maria da Silva' vs 'Maria Silva' são equivalentes — stopwords
    pt-BR são filtradas antes da comparação."""
    passed, reason, _ = enforce_name_gate("Maria da Silva", "Maria Silva")
    assert passed is True
    assert reason == "ok"


def test_name_gate_case_and_accent_insensitive() -> None:
    """Acentos e caixa não devem mudar o resultado."""
    passed, _, _ = enforce_name_gate("João Antônio", "joao antonio")
    assert passed is True
