from beautiful_linkedin.models import Lead
from beautiful_linkedin.processing.function_taxonomy import JobFunction
from beautiful_linkedin.processing.lead_filter import (
    LeadFilter,
    apply_lead_filter,
)
from beautiful_linkedin.processing.seniority_taxonomy import Seniority


def make_lead(
    title: str,
    *,
    snippet: str = "",
    confidence: int = 70,
    source_type: str = "linkedin_cookie",
) -> Lead:
    return Lead(
        company_name="Nubank",
        person_name="Test Person",
        title=title,
        linkedin_url=f"https://www.linkedin.com/in/{abs(hash(title))}/",
        source_url=f"https://www.linkedin.com/in/{abs(hash(title))}/",
        source_type=source_type,
        snippet=snippet,
        matched_title="marketing",
        confidence_score=confidence,
    )


def test_empty_filter_returns_all_leads():
    leads = [make_lead("CEO"), make_lead("Junior Analyst")]
    outcome = apply_lead_filter(leads, LeadFilter())
    assert len(outcome.leads) == 2
    assert outcome.stats.kept == 2


def test_seniority_filter_keeps_only_matching_levels():
    leads = [
        make_lead("CEO at Nubank"),
        make_lead("VP Marketing"),
        make_lead("Marketing Manager"),
        make_lead("Junior Analyst"),
    ]
    outcome = apply_lead_filter(
        leads,
        LeadFilter(seniority_in=[Seniority.C_LEVEL, Seniority.VP]),
    )
    titles = [lead.title for lead in outcome.leads]
    assert titles == ["CEO at Nubank", "VP Marketing"]
    assert outcome.stats.dropped_total == 2


def test_function_filter_keeps_matching_areas():
    leads = [
        make_lead("Head of Marketing"),
        make_lead("Software Engineer"),
        make_lead("People Partner"),
    ]
    outcome = apply_lead_filter(
        leads,
        LeadFilter(functions_in=[JobFunction.MARKETING, JobFunction.HR]),
    )
    titles = sorted(lead.title for lead in outcome.leads)
    assert titles == ["Head of Marketing", "People Partner"]


def test_exclude_titles_drops_matches():
    leads = [
        make_lead("Marketing Manager"),
        make_lead("Recruiter at Nubank"),
        make_lead("Sales Director"),
    ]
    outcome = apply_lead_filter(
        leads,
        LeadFilter(exclude_titles=["recruiter", "headhunter"]),
    )
    titles = [lead.title for lead in outcome.leads]
    assert "Recruiter at Nubank" not in titles
    assert len(titles) == 2


def test_min_confidence_drops_low_score_leads():
    leads = [
        make_lead("CEO", confidence=90),
        make_lead("Mystery Title", confidence=40),
    ]
    outcome = apply_lead_filter(leads, LeadFilter(min_confidence_score=60))
    assert len(outcome.leads) == 1
    assert outcome.leads[0].confidence_score == 90


def test_drop_unclassified_removes_unknown_seniority():
    leads = [
        make_lead("CEO"),
        make_lead("Profissional liberal"),
    ]
    outcome = apply_lead_filter(
        leads,
        LeadFilter(seniority_in=[Seniority.C_LEVEL], drop_unclassified=True),
    )
    assert len(outcome.leads) == 1
    assert outcome.leads[0].title == "CEO"


def test_unclassified_kept_by_default_when_filter_active():
    leads = [
        make_lead("CEO"),
        make_lead("Profissional liberal"),
    ]
    outcome = apply_lead_filter(
        leads,
        LeadFilter(seniority_in=[Seniority.C_LEVEL]),
    )
    titles = sorted(lead.title for lead in outcome.leads)
    assert titles == ["CEO", "Profissional liberal"]


def test_locations_filter_uses_snippet():
    leads = [
        make_lead("Marketing Manager", snippet="Sao Paulo, Brasil"),
        make_lead("Marketing Manager", snippet="Lisboa, Portugal"),
    ]
    outcome = apply_lead_filter(
        leads,
        LeadFilter(locations_in=["brasil"]),
    )
    snippets = [lead.snippet for lead in outcome.leads]
    assert snippets == ["Sao Paulo, Brasil"]


def test_filter_stats_track_drop_reasons():
    leads = [
        make_lead("Junior Analyst", confidence=30),
        make_lead("Recruiter at Nubank", confidence=80),
    ]
    outcome = apply_lead_filter(
        leads,
        LeadFilter(
            min_confidence_score=50,
            exclude_titles=["recruiter"],
        ),
    )
    assert outcome.stats.kept == 0
    assert outcome.stats.dropped_by_reason.get("min_confidence_score") == 1
    assert outcome.stats.dropped_by_reason.get("exclude_titles") == 1
