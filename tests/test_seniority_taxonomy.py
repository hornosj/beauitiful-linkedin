from beautiful_linkedin.processing.seniority_taxonomy import (
    Seniority,
    classify_seniority,
    levels_at_or_above,
)


def test_c_level_titles_pt_and_en():
    assert classify_seniority("CEO at Nubank") == Seniority.C_LEVEL
    assert classify_seniority("Chief Marketing Officer") == Seniority.C_LEVEL
    assert classify_seniority("Founder & Presidente") == Seniority.C_LEVEL
    assert classify_seniority("Co-fundador da empresa") == Seniority.C_LEVEL


def test_vp_takes_precedence_over_lower_levels():
    assert (
        classify_seniority("VP of Engineering and former Senior Manager")
        == Seniority.VP
    )


def test_director_pt_and_en():
    assert classify_seniority("Diretor de Marketing") == Seniority.DIRECTOR
    assert classify_seniority("Director of Sales") == Seniority.DIRECTOR


def test_head_matches_in_titles():
    assert classify_seniority("Head of People at Stone") == Seniority.HEAD


def test_manager_pt_and_en():
    assert classify_seniority("Engineering Manager") == Seniority.MANAGER
    assert classify_seniority("Gerente de Vendas") == Seniority.MANAGER
    assert classify_seniority("Coordenadora de Marketing") == Seniority.MANAGER


def test_senior_pleno_junior_intern():
    assert classify_seniority("Senior Software Engineer") == Seniority.SENIOR
    assert classify_seniority("Analista Pleno de RH") == Seniority.MID
    assert classify_seniority("Analista Junior de Dados") == Seniority.JUNIOR
    assert classify_seniority("Estagiário de Marketing") == Seniority.INTERN


def test_returns_none_when_no_match():
    assert classify_seniority(None) is None
    assert classify_seniority("") is None
    assert classify_seniority("Profissional liberal") is None


def test_matches_tokens_glued_to_punctuation():
    # Previously "vp," / "cmo," would not match the bare "vp"/"cmo" alias
    # because of the trailing comma. Word-bounded matching fixes it.
    assert classify_seniority("VP, Marketing") == Seniority.VP
    assert classify_seniority("CMO, Head of Growth") == Seniority.C_LEVEL
    assert classify_seniority("Sr. Product Designer") == Seniority.SENIOR


def test_levels_at_or_above_is_descending_and_inclusive():
    assert levels_at_or_above(Seniority.DIRECTOR) == [
        Seniority.C_LEVEL,
        Seniority.VP,
        Seniority.DIRECTOR,
    ]
    assert levels_at_or_above(Seniority.C_LEVEL) == [Seniority.C_LEVEL]
