from beautiful_linkedin.models import Lead
from beautiful_linkedin.processing.scorer import score_lead


def test_lead_score_calculation_with_linkedin_and_matched_title():
    lead = Lead(
        company_name="Nubank",
        company_domain="nubank.com.br",
        person_name="Fulano Silva",
        title="Head of Marketing",
        linkedin_url="https://www.linkedin.com/in/fulano",
        source_url="https://www.linkedin.com/in/fulano",
        source_type="search",
        snippet="Fulano Silva - Head of Marketing - Nubank | LinkedIn",
        matched_title="marketing",
        confidence_score=0,
    )

    assert score_lead(lead) == 100


def test_lead_score_is_capped_at_100():
    lead = Lead(
        company_name="Nubank",
        company_domain="nubank.com.br",
        person_name="Fulano Silva",
        title="CEO and Head of Marketing",
        linkedin_url="https://www.linkedin.com/in/fulano",
        source_url="https://nubank.com.br/team",
        source_type="company_site",
        snippet="Fulano Silva - CEO and Head of Marketing - Nubank",
        matched_title="marketing",
        confidence_score=0,
    )

    assert score_lead(lead) == 100


def test_maybe_incorrect_lead_score_is_penalized():
    lead = Lead(
        company_name="XP Inc",
        company_domain="xpi.com.br",
        person_name="Joao Sales",
        title=None,
        linkedin_url="https://www.linkedin.com/in/joao-sales",
        source_url="https://www.linkedin.com/in/joao-sales",
        source_type="search",
        snippet="Joao Sales - XP Inc",
        matched_title=None,
        confidence_score=0,
        validation_status="maybe_incorrect",
        validation_note="Talvez incorreto: termo de cargo apareceu fora de um contexto confiavel.",
    )

    assert score_lead(lead) == 55
