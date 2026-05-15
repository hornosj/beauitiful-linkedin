from beautiful_linkedin.processing.role_taxonomy import get_terms_for_area, is_custom_area

def test_marketing_growth_returns_expected_terms():
    terms = get_terms_for_area("Marketing / Growth")
    assert "marketing" in terms
    assert "growth" in terms

def test_custom_area_returns_itself():
    terms = get_terms_for_area("Qualquer Outra Coisa")
    assert terms == ["Qualquer Outra Coisa"]
    
def test_is_custom_area():
    assert is_custom_area("Customizada")
    assert not is_custom_area("Marketing / Growth")
