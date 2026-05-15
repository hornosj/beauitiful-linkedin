from __future__ import annotations

from datetime import datetime, timezone

from beautiful_linkedin.cache.lead_history import LeadConsultationHistory
from beautiful_linkedin.models import Lead


def _lead(**overrides):
    data = {
        "company_name": "XP Inc",
        "company_domain": "xpi.com.br",
        "person_name": "Ana Silva",
        "title": "Head of Marketing",
        "linkedin_url": "https://www.linkedin.com/in/ana-silva/",
        "source_url": "https://www.linkedin.com/in/ana-silva/",
        "source_type": "api_apollo",
        "snippet": "Head of Marketing | XP Inc",
        "matched_title": "marketing",
        "confidence_score": 92,
    }
    data.update(overrides)
    return Lead(**data)


def test_history_marks_repeated_leads_without_dropping_them(tmp_path):
    history = LeadConsultationHistory(tmp_path / "cache.sqlite")
    lead = _lead()

    first_run = history.annotate_and_record(
        [lead],
        now=datetime(2026, 5, 9, 10, 30, tzinfo=timezone.utc),
    )
    second_run = history.annotate_and_record(
        [lead],
        now=datetime(2026, 5, 10, 11, 45, tzinfo=timezone.utc),
    )

    assert len(first_run) == 1
    assert first_run[0].consultation_note is None
    assert len(second_run) == 1
    assert second_run[0].previously_consulted_at == "2026-05-09T10:30:00+00:00"
    assert second_run[0].consultation_note == "Já consultado antes em 09/05/2026 10:30"
