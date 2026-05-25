"""Score Telegram-consult candidates against a saved LinkedIn lead.

A single consult often returns multiple CPF candidates for the same
name (homonyms). To pick the most likely actual person we compare each
candidate's structured fields against signals from the LinkedIn lead
and produce a 0-100 score plus a per-signal breakdown.

Signal weights:
- Location  (40 pts) — Brazilian state/city from the candidate's
  address vs ``lead.linkedin_location`` (and a fallback to
  ``lead.snippet`` when the location field isn't filled yet).
- Age       (60 pts) — Brazilian undergrad starts around 18 and ends
  around 22. Given a candidate's ``data_nascimento`` and the lead's
  ``linkedin_education`` entries (graduation year), we estimate an
  expected birth year and reward proximity.

Both signals degrade gracefully when their inputs are missing — the
score reflects only the comparisons we could actually make, plus a
``signals_used`` list so the UI can show "Comparou: localização" or
"Sem sinais comparáveis" honestly.

The scoring is deliberately conservative: when we can't compare a
field, we DON'T pretend we did. A candidate with one missing signal
caps at 60 (if only age compared) or 40 (if only location), giving
the operator a visible "needs more data" signal.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


logger = logging.getLogger(__name__)


# Brazilian state abbreviations + their full names. Used to match
# state across canonical and verbose forms ("SP" vs "São Paulo").
_STATE_BY_UF: dict[str, str] = {
    "AC": "acre",
    "AL": "alagoas",
    "AP": "amapa",
    "AM": "amazonas",
    "BA": "bahia",
    "CE": "ceara",
    "DF": "distrito federal",
    "ES": "espirito santo",
    "GO": "goias",
    "MA": "maranhao",
    "MT": "mato grosso",
    "MS": "mato grosso do sul",
    "MG": "minas gerais",
    "PA": "para",
    "PB": "paraiba",
    "PR": "parana",
    "PE": "pernambuco",
    "PI": "piaui",
    "RJ": "rio de janeiro",
    "RN": "rio grande do norte",
    "RS": "rio grande do sul",
    "RO": "rondonia",
    "RR": "roraima",
    "SC": "santa catarina",
    "SP": "sao paulo",
    "SE": "sergipe",
    "TO": "tocantins",
}
_UF_BY_NAME: dict[str, str] = {name: uf for uf, name in _STATE_BY_UF.items()}


_NAME_WEIGHT = 20
_LOCATION_WEIGHT = 30
_AGE_CAREER_WEIGHT = 45
_DATA_QUALITY_WEIGHT = 5
# Birthday is the strongest individual signal we have: an exact DD/MM
# match between a Telegram CPF candidate's ``data_nascimento`` and the
# lead's ``linkedin_birthday`` is a near-decisive disambiguator between
# homonyms. When this signal is positive we suppress the indirect age
# signals (career_age / education_age) so the two paths do not
# double-count the same underlying age inference. The total still caps
# at 100 — see ``capped_total`` at the bottom of ``score_candidate``.
_BIRTHDAY_WEIGHT = 60


@dataclass
class MatchScore:
    """Outcome of one candidate-vs-lead comparison."""

    score: int  # 0-100
    breakdown: dict[str, Any] = field(default_factory=dict)
    signals_used: list[str] = field(default_factory=list)

    @property
    def rejected(self) -> bool:
        return bool(self.breakdown.get("rejected"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "breakdown": self.breakdown,
            "signals_used": list(self.signals_used),
        }


def enforce_name_gate(
    lead_name: str | None, candidate_name: str | None
) -> tuple[bool, str, dict[str, Any]]:
    """Hard gate: should we even consider this CPF candidate for the lead?

    Returns ``(passed, reason, detail)``. When ``passed`` is False the
    matcher MUST score the candidate at 0 and mark it ``rejected`` so
    downstream selection skips it — homônimos do CPF alheio nunca devem
    consumir a quota do /cpf.

    Regras (conservadoras de propósito):

    - ``candidate_name`` ausente → reject ``name_missing``. Sem nome
      extraído não há como confirmar que é o lead certo.
    - Primeiro token do lead deve aparecer EM ALGUMA posição do
      candidato → senão reject ``first_name_mismatch``.
    - Último token do lead (sobrenome principal) deve aparecer EM ALGUMA
      posição do candidato → senão reject ``last_name_mismatch``.
    - Quando o lead tem só 1 token, exigimos match exato desse token.

    Stopwords pt-BR (``de``, ``da``, ``dos``...) já são filtradas por
    :func:`_name_tokens`, então "Maria da Silva" e "Maria Silva" são
    equivalentes para a regra.
    """
    if not candidate_name or not str(candidate_name).strip():
        return False, "name_missing", {
            "lead_name": lead_name,
            "candidate_name": candidate_name,
        }

    lead_tokens = _name_tokens(lead_name or "")
    candidate_tokens = _name_tokens(candidate_name)
    if not lead_tokens:
        return False, "lead_name_unparseable", {
            "lead_name": lead_name,
            "candidate_name": candidate_name,
        }
    if not candidate_tokens:
        return False, "candidate_name_unparseable", {
            "lead_name": lead_name,
            "candidate_name": candidate_name,
        }

    candidate_set = set(candidate_tokens)
    first_token = lead_tokens[0]
    last_token = lead_tokens[-1]
    detail = {
        "lead_tokens": lead_tokens,
        "candidate_tokens": candidate_tokens,
        "first_token": first_token,
        "last_token": last_token,
    }

    if first_token not in candidate_set:
        return False, "first_name_mismatch", detail
    if last_token != first_token and last_token not in candidate_set:
        return False, "last_name_mismatch", detail
    return True, "ok", detail


def score_candidate(
    candidate: Any,  # TelegramCandidate (avoid circular import in type hint)
    *,
    lead_name: str | None = None,
    linkedin_location: str | None = None,
    linkedin_education: list[dict[str, Any]] | None = None,
    linkedin_experience_title: str | None = None,
    linkedin_experience_years: list[int] | None = None,
    linkedin_birthday: str | None = None,
    snippet_fallback: str | None = None,
) -> MatchScore:
    """Rank one CPF candidate against the lead's LinkedIn signals.

    ``snippet_fallback`` is used as a coarse location source when the
    lead doesn't carry a proper ``linkedin_location`` yet — search
    snippets frequently mention the city/state.
    """
    breakdown: dict[str, Any] = {}
    signals_used: list[str] = []
    penalties: list[dict[str, Any]] = []
    total = 0

    candidate_nome = getattr(candidate, "nome", None)
    if lead_name and candidate_nome:
        passed, gate_reason, gate_detail = enforce_name_gate(lead_name, candidate_nome)
        if not passed:
            return MatchScore(
                score=0,
                breakdown={
                    "name_gate": {
                        "passed": False,
                        "reason": gate_reason,
                        **gate_detail,
                    },
                    "rejected": True,
                    "rejection_reason": gate_reason,
                    "confidence_label": "rejeitado",
                },
                signals_used=["name_gate"],
            )
        breakdown["name_gate"] = {"passed": True, "reason": gate_reason, **gate_detail}
        sub_score, sub_detail = _score_name_match(lead_name, candidate_nome)
        earned = int(sub_score * _NAME_WEIGHT / 100)
        breakdown["name"] = {
            "score": sub_score,
            "weight": _NAME_WEIGHT,
            "earned": earned,
            **sub_detail,
        }
        signals_used.append("name")
        total += earned
    else:
        breakdown["name"] = {"score": None, "reason": "missing_input"}
        breakdown["name_gate"] = {
            "passed": None,
            "reason": "missing_input",
            "lead_name": lead_name,
            "candidate_name": candidate_nome,
        }

    location_text = linkedin_location or snippet_fallback or ""
    if candidate.endereco and location_text:
        sub_score, sub_detail = _score_location(candidate.endereco, location_text)
        breakdown["location"] = {
            "score": sub_score,
            "weight": _LOCATION_WEIGHT,
            "earned": int(sub_score * _LOCATION_WEIGHT / 100),
            **sub_detail,
        }
        signals_used.append("location")
        total += int(sub_score * _LOCATION_WEIGHT / 100)
    else:
        breakdown["location"] = {"score": None, "reason": "missing_input"}

    birthday_outcome: str | None = None
    if linkedin_birthday and candidate.data_nascimento:
        birthday_sub, birthday_detail = _score_birthday(
            linkedin_birthday, candidate.data_nascimento
        )
        earned = int(birthday_sub * _BIRTHDAY_WEIGHT / 100)
        breakdown["birthday"] = {
            "score": birthday_sub,
            "weight": _BIRTHDAY_WEIGHT,
            "earned": earned,
            **birthday_detail,
        }
        signals_used.append("birthday")
        total += earned
        birthday_outcome = birthday_detail.get("reason")
        # Mismatched DD/MM between LinkedIn and the CPF candidate is a
        # strong "different person" signal — same name, different
        # birthday. Cap the total so even with a perfect name+location
        # match the candidate stays clearly below the follow-up
        # threshold.
        if birthday_outcome == "day_month_mismatch":
            penalties.append(
                {
                    "code": "birthday_day_month_mismatch",
                    "cap": 25,
                    "reason": "linkedin_birthday_differs_from_candidate_data_nascimento",
                }
            )
    elif linkedin_birthday:
        breakdown["birthday"] = {
            "score": None,
            "reason": "candidate_missing_birth_date",
        }
    elif candidate.data_nascimento:
        breakdown["birthday"] = {"score": None, "reason": "missing_linkedin_birthday"}
    else:
        breakdown["birthday"] = {"score": None, "reason": "missing_input"}

    # When the birthday matched exactly, the candidate's data_nascimento
    # is already maxed-out as an age anchor — running career_age /
    # education_age on top would double-count the same signal and push
    # noise into ``signals_used``. Skip them and short-circuit ``age``.
    suppress_indirect_age = birthday_outcome == "day_month_exact"

    age_score: int | None = None
    if candidate.data_nascimento and not suppress_indirect_age:
        career_score, career_detail = _score_career_age(
            candidate.data_nascimento,
            linkedin_experience_title=linkedin_experience_title,
            linkedin_experience_years=linkedin_experience_years or [],
        )
        if career_score is not None:
            breakdown["career_age"] = career_detail
            signals_used.append("career_age")
            age_score = career_score
            if career_detail.get("implausible") is True:
                penalties.append(
                    {
                        "code": "career_age_implausible",
                        "cap": 20,
                        "reason": "candidate_age_does_not_match_linkedin_career_stage",
                    }
                )
        else:
            breakdown["career_age"] = career_detail

        education_score: int | None = None
        if linkedin_education:
            education_score, education_detail = _score_age(
                candidate.data_nascimento, linkedin_education
            )
            breakdown["education_age"] = education_detail
            if education_score is not None:
                signals_used.append("education_age")
                age_score = max(age_score or 0, education_score)
        else:
            breakdown["education_age"] = {"score": None, "reason": "missing_input"}

        if age_score is not None:
            earned = int(age_score * _AGE_CAREER_WEIGHT / 100)
            breakdown["age"] = {
                "score": age_score,
                "weight": _AGE_CAREER_WEIGHT,
                "earned": earned,
                "source": (
                    "career_age"
                    if career_score is not None and career_score >= (education_score or -1)
                    else "education_age"
                ),
            }
            total += earned
        else:
            breakdown["age"] = {"score": None, "reason": "missing_linkedin_age_anchor"}
    elif suppress_indirect_age:
        breakdown["age"] = {"score": None, "reason": "covered_by_birthday"}
        breakdown["career_age"] = {"score": None, "reason": "covered_by_birthday"}
        breakdown["education_age"] = {"score": None, "reason": "covered_by_birthday"}
    else:
        breakdown["age"] = {"score": None, "reason": "missing_input"}
        breakdown["career_age"] = {"score": None, "reason": "missing_birth_date"}
        breakdown["education_age"] = {"score": None, "reason": "missing_birth_date"}

    core_total = total
    quality_score, quality_detail = _score_data_quality(candidate)
    quality_earned = int(quality_score * _DATA_QUALITY_WEIGHT / 100) if core_total > 0 else 0
    breakdown["data_quality"] = {
        "score": quality_score,
        "weight": _DATA_QUALITY_WEIGHT,
        "earned": quality_earned,
        **quality_detail,
    }
    if quality_earned > 0:
        signals_used.append("data_quality")
    total += quality_earned

    capped_total = max(0, min(100, total))
    for penalty in penalties:
        capped_total = min(capped_total, int(penalty["cap"]))
    breakdown["penalties"] = penalties
    breakdown["confidence_label"] = _confidence_label(capped_total, penalties)

    return MatchScore(
        score=capped_total,
        breakdown=breakdown,
        signals_used=signals_used,
    )


# ---- name -----------------------------------------------------------------


def _score_name_match(lead_name: str, candidate_name: str) -> tuple[int, dict[str, Any]]:
    lead_tokens = _name_tokens(lead_name)
    candidate_tokens = _name_tokens(candidate_name)
    if not lead_tokens or not candidate_tokens:
        return 0, {
            "reason": "name_unparseable",
            "lead_name": lead_name,
            "candidate_name": candidate_name,
        }

    lead_set = set(lead_tokens)
    candidate_set = set(candidate_tokens)
    overlap = lead_set & candidate_set
    first_match = lead_tokens[0] == candidate_tokens[0]
    last_match = lead_tokens[-1] == candidate_tokens[-1]

    if lead_tokens == candidate_tokens:
        sub = 100
        reason = "exact"
    elif lead_set <= candidate_set or candidate_set <= lead_set:
        sub = 95
        reason = "contained"
    elif first_match and last_match:
        sub = 85
        reason = "first_last"
    elif first_match and len(overlap) >= 2:
        sub = 70
        reason = "first_plus_overlap"
    elif len(overlap) >= 2:
        sub = 50
        reason = "partial_overlap"
    elif first_match:
        sub = 25
        reason = "first_name_only"
    else:
        sub = 0
        reason = "mismatch"

    return sub, {
        "reason": reason,
        "lead_tokens": lead_tokens,
        "candidate_tokens": candidate_tokens,
        "overlap": sorted(overlap),
    }


def _name_tokens(value: str | None) -> list[str]:
    normalized = _strip_accents(value or "").lower()
    tokens = re.findall(r"[a-z0-9]+", normalized)
    return [token for token in tokens if token not in _NAME_STOPWORDS]


_NAME_STOPWORDS = {"de", "da", "das", "do", "dos", "e"}


# ---- location -------------------------------------------------------------


def _score_location(address: str, linkedin_location: str) -> tuple[int, dict[str, Any]]:
    """Compare Brazilian addresses to LinkedIn location strings.

    Returns ``(0..100, detail)``. Detail explains which level matched
    (state and/or city) for transparency in the UI.
    """
    addr_states, addr_cities = _extract_states_and_cities(address)
    li_states, li_cities = _extract_states_and_cities(linkedin_location)

    if not addr_states and not addr_cities:
        return 0, {"reason": "address_unparseable", "address": address}
    if not li_states and not li_cities:
        return 0, {
            "reason": "linkedin_location_unparseable",
            "linkedin_location": linkedin_location,
        }

    state_match = bool(addr_states & li_states)
    city_match = bool(addr_cities & li_cities)

    # Full match (city + state, or any match if one side carries only
    # a state) → 100. State-only → 60. City-only → 70 (cities are more
    # specific but we lose state context). Mismatch → 0.
    if city_match and (state_match or not li_states):
        sub = 100
    elif state_match and not addr_cities and not li_cities:
        sub = 100  # both sides carry only state → exact state match
    elif state_match and city_match:
        sub = 100
    elif state_match:
        sub = 60
    elif city_match:
        sub = 70
    else:
        sub = 0

    return sub, {
        "address_states": sorted(addr_states),
        "address_cities": sorted(addr_cities),
        "linkedin_states": sorted(li_states),
        "linkedin_cities": sorted(li_cities),
        "state_match": state_match,
        "city_match": city_match,
    }


def _extract_states_and_cities(text: str) -> tuple[set[str], set[str]]:
    """Return ``({uf}, {city_normalized})`` from any free-form text.

    Recognized formats:
    - ``...Cidade/UF``, ``...Cidade-UF``
    - ``Cidade, UF`` or ``UF, Cidade``
    - Standalone state name (``São Paulo``, ``SP``) and full city
      names from a small list of major capitals. We deliberately do
      NOT ship a full IBGE city list — overmatch on common names
      (``Vitória``, ``Salvador``) hurts precision; the operator's
      LinkedIn location usually carries a state/capital combo we can
      anchor on.
    """
    cleaned = _strip_accents(text).lower()
    states: set[str] = set()
    cities: set[str] = set()

    # 1) Look for "city/uf" or "city - uf" patterns.
    for m in re.finditer(
        r"([a-z][a-z\s]{2,})[\s\-/,]+([a-z]{2})\b",
        cleaned,
    ):
        uf = m.group(2).upper()
        if uf in _STATE_BY_UF:
            states.add(uf)
            city = m.group(1).strip()
            if 2 < len(city) <= 40:
                cities.add(city)

    # 2) Standalone UF tokens.
    for m in re.finditer(r"\b([a-z]{2})\b", cleaned):
        uf = m.group(1).upper()
        if uf in _STATE_BY_UF:
            states.add(uf)

    # 3) Full state names (longer patterns first to win over substrings).
    for state_name, uf in sorted(_UF_BY_NAME.items(), key=lambda x: -len(x[0])):
        if state_name in cleaned:
            states.add(uf)

    # 4) Brazilian capital cities — small, hand-picked list.
    for capital in _BRAZILIAN_CAPITALS:
        if capital in cleaned:
            cities.add(capital)

    return states, cities


_BRAZILIAN_CAPITALS = {
    "sao paulo",
    "rio de janeiro",
    "belo horizonte",
    "salvador",
    "fortaleza",
    "brasilia",
    "curitiba",
    "porto alegre",
    "recife",
    "manaus",
    "belem",
    "goiania",
    "natal",
    "florianopolis",
    "vitoria",
    "campo grande",
    "cuiaba",
    "joao pessoa",
    "teresina",
    "aracaju",
    "maceio",
    "boa vista",
    "macapa",
    "palmas",
    "porto velho",
    "sao luis",
    "rio branco",
}


def _strip_accents(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in nfkd if not unicodedata.combining(c))


# ---- age -----------------------------------------------------------------


# Heuristic: Brazilian undergrads typically enroll at 18 and graduate
# around 22. We accept a generous tolerance because the data is noisy
# (master/PhD throw the year off, working professionals re-enroll
# later, etc.).
_UNDERGRAD_DURATION_YEARS = 4
_EXPECTED_GRAD_AGE = 22
_AGE_TOLERANCE_YEARS = 5


def _score_birthday(
    linkedin_birthday: str, candidate_birth_date: str
) -> tuple[int, dict[str, Any]]:
    """Compare LinkedIn's DD/MM birthday against a CPF candidate's birth date.

    LinkedIn exposes day/month only (no year) under "Dados pessoais".
    The Telegram CPF results include a full ``data_nascimento`` (usually
    ``DD/MM/YYYY``). We compare on (day, month). Outcomes:

    - Exact day+month → 100. Combined with the lead's name gate this is
      a near-decisive identity confirmation between homonyms.
    - Off-by-one day (rendering differences, e.g. ``28/02`` vs ``29/02``
      in leap-year fixtures) → 70. Common enough in data cleanup that a
      hard 0 here would over-reject real matches.
    - Same month, different day → 30. Some signal but weak.
    - Different month → 0 and marked ``day_month_mismatch`` so the
      caller can cap the total — same name + different birthday is a
      strong "not the same person" signal.

    Inputs are tolerant about format: anything that looks like
    ``D/M`` or ``D-M`` or ``DD/MM/YYYY`` is accepted on both sides.
    """
    lead_day, lead_month = _parse_day_month(linkedin_birthday)
    cand_day, cand_month = _parse_day_month(candidate_birth_date)
    detail: dict[str, Any] = {
        "linkedin_birthday": linkedin_birthday,
        "candidate_birth_date": candidate_birth_date,
        "lead_day_month": (lead_day, lead_month),
        "candidate_day_month": (cand_day, cand_month),
    }
    if lead_day is None or lead_month is None:
        detail["reason"] = "linkedin_birthday_unparseable"
        return 0, detail
    if cand_day is None or cand_month is None:
        detail["reason"] = "candidate_birth_date_unparseable"
        return 0, detail
    if lead_day == cand_day and lead_month == cand_month:
        detail["reason"] = "day_month_exact"
        return 100, detail
    if lead_month == cand_month and abs(lead_day - cand_day) == 1:
        detail["reason"] = "day_off_by_one"
        return 70, detail
    if lead_month == cand_month:
        detail["reason"] = "month_only_match"
        return 30, detail
    detail["reason"] = "day_month_mismatch"
    return 0, detail


_BIRTHDAY_PARTS_RE = re.compile(r"(\d{1,2})\s*[/\-\.]\s*(\d{1,2})")


def _parse_day_month(value: str | None) -> tuple[int | None, int | None]:
    if not value:
        return None, None
    match = _BIRTHDAY_PARTS_RE.search(value)
    if not match:
        return None, None
    try:
        day = int(match.group(1))
        month = int(match.group(2))
    except ValueError:
        return None, None
    if not (1 <= day <= 31 and 1 <= month <= 12):
        return None, None
    return day, month


def _score_career_age(
    birth_date: str,
    *,
    linkedin_experience_title: str | None,
    linkedin_experience_years: list[int],
) -> tuple[int | None, dict[str, Any]]:
    title = (linkedin_experience_title or "").strip()
    years = [year for year in linkedin_experience_years if 1900 < int(year) < 2100]
    if not title or not years:
        return None, {"score": None, "reason": "missing_input"}
    birth_year = _birth_year(birth_date)
    if birth_year is None:
        return None, {"score": None, "reason": "birth_date_unparseable"}

    stage, min_age, max_age = _career_stage_age_range(title)
    anchor_year = min(years)
    age_at_anchor = anchor_year - birth_year
    sub_score, implausible = _score_age_against_range(
        age_at_anchor, min_age=min_age, max_age=max_age
    )
    return sub_score, {
        "score": sub_score,
        "career_stage": stage,
        "linkedin_experience_title": title,
        "linkedin_experience_year_anchor": anchor_year,
        "candidate_birth_year": birth_year,
        "age_at_anchor_year": age_at_anchor,
        "plausible_min_age": min_age,
        "plausible_max_age": max_age,
        "implausible": implausible,
    }


def _career_stage_age_range(title: str) -> tuple[str, int, int]:
    normalized = _strip_accents(title).lower()
    if re.search(r"\b(estagio|estagiario|trainee|intern|aprendiz)\b", normalized):
        return "intern", 17, 30
    if re.search(r"\b(junior|jr|assistente|auxiliar)\b", normalized):
        return "junior", 20, 34
    if re.search(r"\b(senior|sr|especialista|principal|staff)\b", normalized):
        return "senior", 26, 58
    if re.search(
        r"\b(head|gerente|manager|director|diretor|coordenador|coordinator|lead|lider)\b",
        normalized,
    ):
        return "executive", 27, 65
    if re.search(r"\b(pleno|analista|consultor|consultant|designer|developer)\b", normalized):
        return "mid", 23, 45
    return "professional", 21, 65


def _score_age_against_range(age: int, *, min_age: int, max_age: int) -> tuple[int, bool]:
    if min_age <= age <= max_age:
        return 100, False
    if age < min_age:
        delta = min_age - age
    else:
        delta = age - max_age
    score = max(0, int(round(100 - delta * 12)))
    return score, delta >= 12


def _birth_year(birth_date: str) -> int | None:
    try:
        _d, _m, y = (int(p) for p in birth_date.split("/"))
    except (ValueError, AttributeError):
        return None
    if 1900 <= y <= 2100:
        return y
    return None


def _score_age(
    birth_date: str, linkedin_education: list[dict[str, Any]]
) -> tuple[int, dict[str, Any]]:
    """Score the candidate's birth date against the lead's education
    history. Picks the EARLIEST grad year as the anchor — that's the
    undergrad in the common case and gives the most reliable age
    estimate. Returns ``(0..100, detail)``.
    """
    try:
        d, m, y = (int(p) for p in birth_date.split("/"))
    except (ValueError, AttributeError):
        return 0, {"reason": "birth_date_unparseable", "birth_date": birth_date}
    candidate_birth_year = y

    grad_years = _collect_grad_years(linkedin_education)
    if not grad_years:
        return 0, {"reason": "no_education_years"}

    # Use the earliest grad year as the anchor (likely undergrad).
    anchor_grad_year = min(grad_years)
    expected_birth_year = anchor_grad_year - _EXPECTED_GRAD_AGE
    delta = abs(candidate_birth_year - expected_birth_year)

    # Linear decay across the tolerance window: exact match → 100,
    # ±tolerance → 0. Beyond tolerance is still 0 (no negative scores).
    if delta >= _AGE_TOLERANCE_YEARS:
        sub_score = 0
    else:
        sub_score = int(round((1 - delta / _AGE_TOLERANCE_YEARS) * 100))

    today = datetime.now().year
    estimated_age = today - candidate_birth_year
    return sub_score, {
        "score": sub_score,
        "candidate_birth_year": candidate_birth_year,
        "candidate_estimated_age": estimated_age,
        "linkedin_grad_year_anchor": anchor_grad_year,
        "expected_birth_year": expected_birth_year,
        "delta_years": delta,
        "tolerance_years": _AGE_TOLERANCE_YEARS,
    }


def _score_data_quality(candidate: Any) -> tuple[int, dict[str, Any]]:
    fields = {
        "cpf": bool(getattr(candidate, "cpf", None)),
        "nome": bool(getattr(candidate, "nome", None)),
        "data_nascimento": bool(getattr(candidate, "data_nascimento", None)),
        "endereco": bool(getattr(candidate, "endereco", None)),
    }
    present = [name for name, ok in fields.items() if ok]
    score = int(round(len(present) / len(fields) * 100))
    return score, {"present_fields": present}


def _confidence_label(score: int, penalties: list[dict[str, Any]]) -> str:
    if penalties:
        return "baixa"
    if score >= 85:
        return "alta"
    if score >= 65:
        return "media"
    if score >= 40:
        return "fraca"
    return "baixa"


def _collect_grad_years(education: list[dict[str, Any]]) -> list[int]:
    """Pull every plausible end-year / start-year out of education
    entries. Accept ints, ``"2010"`` strings, or compound year ranges
    like ``"2008-2012"``."""
    years: list[int] = []
    for entry in education or []:
        explicit_end_years: list[int] = []
        for key in ("end_year", "year_end", "graduated_year"):
            value = entry.get(key)
            if value is None:
                continue
            year = _coerce_year(value)
            if year is not None:
                explicit_end_years.append(year)
        if explicit_end_years:
            years.extend(explicit_end_years)
            continue

        # Some serializations store "2008 - 2012" in a single text field.
        # Treat the rightmost year as the graduation/end year; using the
        # start year would make the age heuristic systematically too old.
        period_years: list[int] = []
        for key in ("period", "dates", "years"):
            text = entry.get(key)
            if isinstance(text, str):
                for m in re.finditer(r"\b(19|20)\d{2}\b", text):
                    period_years.append(int(m.group(0)))
        if period_years:
            years.append(max(period_years))
            continue

        for key in ("start_year", "year_start"):
            value = entry.get(key)
            if value is None:
                continue
            year = _coerce_year(value)
            if year is not None:
                years.append(year)
    return [y for y in years if 1900 < y < 2100]


def _coerce_year(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        m = re.search(r"\b(19|20)\d{2}\b", value)
        if m:
            return int(m.group(0))
    return None
