from beautiful_linkedin.export.csv_exporter import leads_to_dataframe
from beautiful_linkedin.models import Lead


def test_leads_dataframe_includes_validation_columns():
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
        confidence_score=55,
        validation_status="maybe_incorrect",
        validation_note="Talvez incorreto: termo de cargo apareceu fora de um contexto confiavel.",
    )

    dataframe = leads_to_dataframe([lead])

    assert dataframe.loc[0, "validation_status"] == "maybe_incorrect"
    assert "Talvez incorreto" in dataframe.loc[0, "validation_note"]
