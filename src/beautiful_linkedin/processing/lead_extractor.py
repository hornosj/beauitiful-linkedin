from __future__ import annotations

import re
from urllib.parse import unquote, urlparse

from rapidfuzz import fuzz

from beautiful_linkedin.models import CompanyInput, Lead, SearchResult
from beautiful_linkedin.processing.normalizer import (
    contains_linkedin_profile_url,
    normalize_text,
)
from beautiful_linkedin.processing.scorer import score_lead
from beautiful_linkedin.processing.title_aliases import matches_title_alias

ROLE_KEYWORDS = [
    "CEO",
    "Founder",
    "Co-Founder",
    "Diretor",
    "Diretora",
    "Director",
    "Head",
    "Manager",
    "Coordenador",
    "Coordenadora",
    "Marketing",
    "Growth",
    "Sales",
    "Comercial",
    "RH",
    "People",
    "Talent",
    "Data",
    "Dados",
    "Legal",
    "Jurídico",
    "Juridico",
    "Design",
    "Designer",
    "Operations",
    "Operações",
    "Operacoes",
    "Finance",
    "Financeiro",
    "Customer Success",
    "Partnerships",
    "Produto",
    "Product",
    "Analyst",
    "Analista",
    "Engineer",
    "Engenheiro",
    "Engenheira",
    "Developer",
    "Desenvolvedor",
    "Desenvolvedora",
    "Software",
    "Technology",
    "Tecnologia",
    "Sistemas",
]

CLEAR_SEPARATORS = [" - ", " | ", " – ", " — "]

TITLE_STRUCTURE_KEYWORDS = [
    "ceo",
    "founder",
    "co-founder",
    "cofounder",
    "diretor",
    "diretora",
    "director",
    "head",
    "manager",
    "gerente",
    "coordenador",
    "coordenadora",
    "coordinator",
    "superintendente",
    "superintendent",
    "analyst",
    "analista",
    "engineer",
    "engenheiro",
    "engenheira",
    "developer",
    "desenvolvedor",
    "desenvolvedora",
    "lead",
    "leader",
    "especialista",
    "specialist",
    "consultor",
    "consultora",
    "executive",
    "executivo",
    "executiva",
    "representante",
    "sdr",
    "bdr",
    "vp",
    "vice president",
    "operations",
    "business development",
    "designer",
    "scientist",
    "counsel",
    "lawyer",
    "advogado",
    "advogada",
    "architect",
    "partner",
    "consultant",
    "officer",
    "president",
]

AMBIGUOUS_STANDALONE_ROLE_TERMS = {
    "marketing",
    "growth",
    "sales",
    "comercial",
    "people",
    "talent",
    "product",
    "produto",
    "data",
    "dados",
    "legal",
    "juridico",
    "jurídico",
    "design",
    "software",
    "technology",
    "tecnologia",
    "operations",
    "operacoes",
    "operações",
    "finance",
    "financeiro",
}

MAYBE_INCORRECT_NOTE_TEMPLATE = (
    "Talvez incorreto: '{matched_title}' apareceu fora de um contexto confiável de cargo."
)


def match_target_title(text: str | None, target_titles: list[str]) -> str | None:
    normalized_text = normalize_text(text)
    if not normalized_text:
        return None

    for title in target_titles:
        normalized_title = normalize_text(title)
        if not normalized_title:
            continue
        if matches_title_alias(normalized_text, title):
            return title
        if normalized_title in normalized_text or normalized_text in normalized_title:
            return title

    for title in target_titles:
        normalized_title = normalize_text(title)
        if normalized_title and fuzz.partial_ratio(normalized_title, normalized_text) >= 85:
            return title

    return None


def has_role_hint(text: str | None) -> bool:
    normalized_text = normalize_text(text)
    return any(normalize_text(keyword) in normalized_text for keyword in ROLE_KEYWORDS)


def extract_leads_from_search_results(
    company: CompanyInput,
    results: list[SearchResult],
    include_uncertain: bool = False,
) -> list[Lead]:
    leads: list[Lead] = []

    for result in results:
        if not contains_linkedin_profile_url(result.url):
            continue

        person_name, parsed_title = parse_person_and_title(result.title, company.titles)
        if not person_name:
            snippet_name, snippet_title = parse_person_and_title(
                result.snippet,
                company.titles,
                allow_single_name=False,
            )
            person_name = snippet_name
            parsed_title = parsed_title or snippet_title
        if not person_name:
            person_name = _extract_name_from_profile_url(result.url)

        title_evidence = _title_evidence_text(
            result.title,
            result.snippet,
            parsed_title,
            company.titles,
        )
        matched_title = match_target_title(title_evidence, company.titles)
        raw_matched_title = match_target_title(
            _raw_search_text(result.title, result.snippet),
            company.titles,
        )
        validation_status = "valid"
        validation_note = None
        if raw_matched_title and not matched_title:
            validation_status = "maybe_incorrect"
            validation_note = MAYBE_INCORRECT_NOTE_TEMPLATE.format(
                matched_title=raw_matched_title
            )

        if (
            not include_uncertain
            and not matched_title
            and validation_status != "maybe_incorrect"
        ):
            continue

        lead = Lead(
            company_name=company.company_name,
            company_domain=company.company_domain,
            person_name=person_name,
            title=parsed_title,
            linkedin_url=result.url,
            source_url=result.url,
            source_type=result.source_type,
            snippet=result.snippet,
            matched_title=matched_title,
            validation_status=validation_status,
            validation_note=validation_note,
            confidence_score=30,
        )
        leads.append(lead.model_copy(update={"confidence_score": score_lead(lead)}))

    return leads


def extract_leads_from_company_site_text(
    company: CompanyInput,
    source_url: str,
    text: str,
    include_uncertain: bool = False,
) -> list[Lead]:
    leads: list[Lead] = []
    seen_snippets: set[str] = set()

    for snippet in _role_snippets(text):
        normalized_snippet = normalize_text(snippet)
        if normalized_snippet in seen_snippets:
            continue
        seen_snippets.add(normalized_snippet)

        matched_title = match_target_title(snippet, company.titles)
        person_name, parsed_title = parse_person_and_title(snippet, company.titles)

        if not _has_person_with_role(person_name, parsed_title):
            continue

        title = parsed_title or matched_title or _first_role_keyword(snippet)

        lead = Lead(
            company_name=company.company_name,
            company_domain=company.company_domain,
            person_name=person_name,
            title=title,
            linkedin_url=None,
            source_url=source_url,
            source_type="company_site",
            snippet=snippet,
            matched_title=matched_title,
            confidence_score=30,
        )
        leads.append(lead.model_copy(update={"confidence_score": score_lead(lead)}))

    return leads


def parse_person_and_title(
    text: str | None,
    target_titles: list[str],
    allow_single_name: bool = True,
) -> tuple[str | None, str | None]:
    if not text:
        return None, None

    cleaned = _strip_search_noise(text)
    view_profile_name = _parse_view_profile_name(cleaned)
    if view_profile_name:
        return view_profile_name, None

    parts = _split_clear_parts(cleaned)
    if not parts:
        return None, None
    if len(parts) == 1 and not allow_single_name:
        return None, None

    person_name = _clean_person_candidate(parts[0])
    title = None

    if len(parts) >= 2:
        title = _clean_title_candidate(parts[1], target_titles)
        if person_name is None and _looks_like_company_part(parts[1]):
            title = _clean_title_candidate(parts[0], target_titles)

    return person_name, title


def _title_evidence_text(
    result_title: str | None,
    result_snippet: str | None,
    parsed_title: str | None,
    target_titles: list[str],
) -> str:
    candidates: list[str] = []
    if parsed_title:
        candidates.append(parsed_title)

    for text in [result_title, result_snippet]:
        cleaned = _strip_search_noise(text or "")
        for part in _split_clear_parts(cleaned):
            title = _clean_title_candidate(part, target_titles)
            if title:
                candidates.append(title)

    return " ".join(_dedupe_preserving_order(candidates))


def _raw_search_text(result_title: str | None, result_snippet: str | None) -> str:
    return " ".join(
        _strip_search_noise(text)
        for text in [result_title or "", result_snippet or ""]
        if text
    )


def _split_clear_parts(text: str) -> list[str]:
    normalized = text
    for separator in CLEAR_SEPARATORS[1:]:
        normalized = normalized.replace(separator, " - ")
    return [part.strip(" -|") for part in normalized.split(" - ") if part.strip(" -|")]


def _strip_search_noise(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    cleaned = re.sub(r"\s*\|\s*LinkedIn\s*$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*-\s*LinkedIn\s*$", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def _parse_view_profile_name(text: str) -> str | None:
    patterns = [
        r"^(?:ver|veja)\s+o\s+perfil\s+de\s+(?P<name>.+?)(?:\s+no\s+linkedin|$)",
        r"^view\s+(?P<name>.+?)'?s\s+profile(?:\s+on\s+linkedin|$)",
        r"^(?P<name>.+?)\s+no\s+linkedin:",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return _clean_person_candidate(match.group("name"))
    return None


def _clean_person_candidate(value: str) -> str | None:
    candidate = value.strip()
    normalized = normalize_text(candidate)
    if not candidate or len(candidate) > 80:
        return None
    if "linkedin" in normalized or normalized.startswith("ver o perfil"):
        return None
    if _has_title_structure(candidate):
        return None
    words = [word for word in candidate.split() if word.strip()]
    if len(words) < 2 or len(words) > 6:
        return None
    if any("http" in word.lower() for word in words):
        return None
    return candidate


def _clean_title_candidate(value: str, target_titles: list[str]) -> str | None:
    candidate = value.strip()
    if not candidate:
        return None

    lowered = candidate.lower()
    for separator in [" at ", " em "]:
        index = lowered.find(separator)
        if index > 0:
            candidate = candidate[:index].strip()
            break

    if _looks_like_person_name(candidate) and not _has_title_structure(candidate):
        return None
    if _is_ambiguous_standalone_title(candidate):
        return None
    if has_role_hint(candidate) or match_target_title(candidate, target_titles):
        return candidate
    return None


def _has_title_structure(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    padded = f" {normalized} "
    return any(f" {keyword} " in padded for keyword in TITLE_STRUCTURE_KEYWORDS)


def _is_ambiguous_standalone_title(text: str | None) -> bool:
    normalized = normalize_text(text)
    return (
        bool(normalized)
        and len(normalized.split()) == 1
        and normalized in AMBIGUOUS_STANDALONE_ROLE_TERMS
    )


def _looks_like_person_name(value: str) -> bool:
    candidate = value.strip()
    if not candidate or len(candidate) > 80:
        return False
    if _has_title_structure(candidate):
        return False
    words = [word for word in candidate.split() if word.strip()]
    if len(words) < 2 or len(words) > 4:
        return False
    if sum(1 for word in words if _is_role_name_token(word)) >= 2:
        return False
    return all(_looks_name_token(word) for word in words)


def _looks_name_token(word: str) -> bool:
    cleaned = re.sub(r"[^A-Za-zÀ-ÿ'-]", "", word).strip("-'")
    if len(cleaned) <= 1:
        return False
    return True


def _is_role_name_token(word: str) -> bool:
    normalized_word = normalize_text(re.sub(r"[^A-Za-zÀ-ÿ]", "", word))
    if not normalized_word:
        return False
    role_tokens = {
        token
        for keyword in ROLE_KEYWORDS
        for token in normalize_text(keyword).split()
        if token
    }
    return normalized_word in role_tokens


def _looks_like_company_part(value: str) -> bool:
    normalized = normalize_text(value)
    return "banco" in normalized or "bank" in normalized or len(normalized.split()) <= 4


def _role_snippets(text: str) -> list[str]:
    chunks = re.split(r"[\n\r.;]+", text)
    snippets: list[str] = []
    for chunk in chunks:
        cleaned = re.sub(r"\s+", " ", chunk).strip()
        if not cleaned or len(cleaned) < 4:
            continue
        if has_role_hint(cleaned):
            snippets.append(cleaned[:320])
        if len(snippets) >= 30:
            break
    return snippets


def _first_role_keyword(text: str) -> str | None:
    normalized_text = normalize_text(text)
    for keyword in ROLE_KEYWORDS:
        if normalize_text(keyword) in normalized_text:
            return keyword
    return None


def _has_person_with_role(person_name: str | None, title: str | None) -> bool:
    return bool(person_name and title and has_role_hint(title))


def _dedupe_preserving_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        normalized = normalize_text(value)
        if not normalized or normalized in seen:
            continue
        deduped.append(value)
        seen.add(normalized)
    return deduped


def _extract_name_from_profile_url(url: str) -> str | None:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2 or parts[0] != "in":
        return None

    slug = unquote(parts[1]).strip().strip("/")
    if "-" not in slug:
        return None

    tokens: list[str] = []
    for token in slug.split("-"):
        if any(character.isdigit() for character in token):
            break
        cleaned = re.sub(r"[^A-Za-zÀ-ÿ]", "", token).strip()
        if len(cleaned) <= 1:
            continue
        tokens.append(cleaned)
        if len(tokens) == 4:
            break

    if len(tokens) < 2:
        return None

    return " ".join(token.capitalize() for token in tokens)
