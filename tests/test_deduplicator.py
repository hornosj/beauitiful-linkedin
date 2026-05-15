from beautiful_linkedin.models import Lead
from beautiful_linkedin.processing.deduplicator import deduplicate_leads


def _lead(**overrides):
    data = {
        "company_name": "Nubank",
        "company_domain": "nubank.com.br",
        "person_name": "Fulano Silva",
        "title": "Head of Marketing",
        "linkedin_url": "https://www.linkedin.com/in/fulano/",
        "source_url": "https://www.linkedin.com/in/fulano/",
        "source_type": "search",
        "snippet": "Fulano Silva - Head of Marketing - Nubank | LinkedIn",
        "matched_title": "marketing",
        "confidence_score": 80,
    }
    data.update(overrides)
    return Lead(**data)


def test_deduplicator_removes_duplicates_by_linkedin_url():
    low = _lead(confidence_score=70)
    high = _lead(
        linkedin_url="https://www.linkedin.com/in/fulano?trk=public_profile",
        source_url="https://search.example/result",
        confidence_score=95,
    )

    deduped = deduplicate_leads([low, high])

    assert len(deduped) == 1
    assert deduped[0].confidence_score == 95


def test_deduplicator_does_not_merge_null_person_names_incorrectly():
    first = _lead(
        person_name=None,
        linkedin_url=None,
        source_url="https://nubank.com.br/team#marketing",
        confidence_score=60,
    )
    second = _lead(
        person_name=None,
        linkedin_url=None,
        source_url="https://nubank.com.br/team#sales",
        confidence_score=65,
    )

    deduped = deduplicate_leads([first, second])

    assert len(deduped) == 2

def test_deduplicator_removes_duplicates_by_email():
    first = _lead(
        linkedin_url=None,
        email="fulano@nubank.com.br",
        source_url="https://source.example/1",
        confidence_score=75,
    )
    second = _lead(
        linkedin_url=None,
        email="fulano@nubank.com.br",
        source_url="https://source.example/2",
        confidence_score=90,
    )

    deduped = deduplicate_leads([first, second])

    assert len(deduped) == 1
    assert deduped[0].confidence_score == 90
