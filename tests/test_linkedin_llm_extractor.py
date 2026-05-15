from __future__ import annotations

import json
from types import SimpleNamespace

from beautiful_linkedin.providers.linkedin_llm_extractor import (
    LLMExtractionConfig,
    extract_records_from_html,
    merge_records,
    should_run_llm_fallback,
)
from beautiful_linkedin.providers.linkedin_playwright import PlaywrightProfileRecord


def _record(profile_url: str, *, name: str | None = None, headline: str | None = None,
            location: str | None = None) -> PlaywrightProfileRecord:
    return PlaywrightProfileRecord(
        full_name=name,
        headline=headline,
        location=location,
        profile_url=profile_url,
    )


def test_should_run_fallback_when_records_empty():
    assert should_run_llm_fallback([]) is True


def test_should_skip_fallback_when_records_complete():
    records = [
        _record("https://www.linkedin.com/in/a/", name="A", headline="Eng"),
        _record("https://www.linkedin.com/in/b/", name="B", headline="PM"),
    ]
    assert should_run_llm_fallback(records) is False


def test_should_run_fallback_when_majority_incomplete():
    records = [
        _record("https://www.linkedin.com/in/a/"),
        _record("https://www.linkedin.com/in/b/"),
        _record("https://www.linkedin.com/in/c/", name="C", headline="Eng"),
    ]
    assert should_run_llm_fallback(records) is True


def test_extract_records_returns_empty_without_token():
    config = LLMExtractionConfig(api_token=None)

    result = extract_records_from_html("<html></html>", config=config)

    assert result == []


def test_extract_records_parses_llm_json_into_profile_records():
    payload = json.dumps(
        [
            {
                "full_name": "Ana Silva",
                "headline": "Head of Marketing at Nubank",
                "location": "Sao Paulo, BR",
                "profile_url": "https://www.linkedin.com/in/ana-silva?ref=foo",
            },
            {
                "full_name": "Bob Sem URL",
                "profile_url": "not-a-linkedin-url",  # should be dropped
            },
            {
                "full_name": "Ana Silva (dup)",
                "headline": "duplicate",
                "profile_url": "https://www.linkedin.com/in/ana-silva/",
            },
        ]
    )

    captured: dict[str, object] = {}

    class FakeCrawler:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def arun(self, url: str):
            captured["url"] = url
            return SimpleNamespace(extracted_content=payload)

    config = LLMExtractionConfig(api_token="sk-test", model="openai/gpt-4o-mini")

    records = extract_records_from_html(
        "<html><body>People</body></html>",
        config=config,
        crawler_factory=lambda: FakeCrawler(),
    )

    assert isinstance(captured["url"], str)
    assert captured["url"].startswith("raw:")
    assert "<html>" in captured["url"]

    assert len(records) == 1
    record = records[0]
    assert record.full_name == "Ana Silva"
    assert record.headline == "Head of Marketing at Nubank"
    assert record.location == "Sao Paulo, BR"
    # canonicalized URL — query string stripped, trailing slash added
    assert record.profile_url == "https://www.linkedin.com/in/ana-silva/"


def test_extract_records_handles_invalid_json():
    class FakeCrawler:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def arun(self, url: str):
            return SimpleNamespace(extracted_content="not really json")

    config = LLMExtractionConfig(api_token="sk-test")
    records = extract_records_from_html(
        "<html></html>", config=config, crawler_factory=lambda: FakeCrawler()
    )
    assert records == []


def test_maybe_apply_llm_fallback_skips_when_records_complete(monkeypatch):
    from beautiful_linkedin.providers import linkedin_playwright

    def boom(*args, **kwargs):
        raise AssertionError("LLM should not be invoked when records are complete")

    monkeypatch.setattr(
        "beautiful_linkedin.providers.linkedin_llm_extractor.extract_records_from_html",
        boom,
    )

    page = SimpleNamespace(content=lambda: "<html></html>")
    title_records = [
        _record("https://www.linkedin.com/in/a/", name="A", headline="Eng"),
        _record("https://www.linkedin.com/in/b/", name="B", headline="PM"),
    ]
    config = LLMExtractionConfig(api_token="sk-test")

    result = linkedin_playwright._maybe_apply_llm_fallback(
        page=page, title_records=title_records, llm_config=config
    )
    assert result is None


def test_maybe_apply_llm_fallback_invokes_extractor_when_records_weak(monkeypatch):
    from beautiful_linkedin.providers import linkedin_playwright

    captured: dict[str, object] = {}

    def fake_extract(html, *, config):
        captured["html"] = html
        captured["config"] = config
        return [
            _record(
                "https://www.linkedin.com/in/a/",
                name="A From LLM",
                headline="Eng From LLM",
            ),
            _record(
                "https://www.linkedin.com/in/c/",
                name="C New",
                headline="Designer",
            ),
        ]

    monkeypatch.setattr(
        "beautiful_linkedin.providers.linkedin_llm_extractor.extract_records_from_html",
        fake_extract,
    )

    page = SimpleNamespace(content=lambda: "<html>People page</html>")
    title_records = [_record("https://www.linkedin.com/in/a/")]
    config = LLMExtractionConfig(api_token="sk-test")

    merged = linkedin_playwright._maybe_apply_llm_fallback(
        page=page, title_records=title_records, llm_config=config
    )

    assert captured["html"] == "<html>People page</html>"
    assert captured["config"] is config
    assert merged is not None
    urls = [r.profile_url for r in merged]
    assert urls == [
        "https://www.linkedin.com/in/a/",
        "https://www.linkedin.com/in/c/",
    ]
    assert merged[0].full_name == "A From LLM"


def test_merge_records_fills_missing_fields_from_secondary():
    primary = [
        _record("https://www.linkedin.com/in/a/", name=None, headline=None),
        _record(
            "https://www.linkedin.com/in/b/", name="B Already", headline="PM"
        ),
    ]
    secondary = [
        _record(
            "https://www.linkedin.com/in/a/",
            name="A From LLM",
            headline="Eng From LLM",
            location="SP",
        ),
        _record("https://www.linkedin.com/in/b/", name="B Override", headline="x"),
        _record("https://www.linkedin.com/in/c/", name="C New", headline="Designer"),
    ]

    merged = merge_records(primary, secondary)

    assert [r.profile_url for r in merged] == [
        "https://www.linkedin.com/in/a/",
        "https://www.linkedin.com/in/b/",
        "https://www.linkedin.com/in/c/",
    ]
    # A: filled from LLM
    assert merged[0].full_name == "A From LLM"
    assert merged[0].headline == "Eng From LLM"
    assert merged[0].location == "SP"
    # B: kept primary values, LLM did not override
    assert merged[1].full_name == "B Already"
    assert merged[1].headline == "PM"
    # C: new from LLM
    assert merged[2].full_name == "C New"
