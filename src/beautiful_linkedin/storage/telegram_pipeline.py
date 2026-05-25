"""In-process pipeline for Telegram-group enrichment of saved leads.

This module owns the orchestration that used to live inline in
``server/app.py`` for the ``/lead-tables/{id}/telegram-consult`` route.
The goal is a durable, EDA-shaped flow without introducing a broker or
external workers — everything is plain Python classes wired around the
existing SQLite store.

Phase 1 of the refactor is intentionally a no-op: the public functions
preserve the previous behavior so the existing endpoint tests stay
green. Later phases add the explicit gate, provider cooldown state, and
phone follow-up on top of the same primitives without touching the
surface area exposed to ``server/app.py``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Literal, Protocol

from beautiful_linkedin.models import Lead
from beautiful_linkedin.processing.title_validator import (
    ValidationReason,
    validate_lead_titles,
)
from beautiful_linkedin.storage.phone_harvester import (
    _PHONE_REGEX,
    _digits_only,
    _is_plausible_phone,
)
from beautiful_linkedin.storage.telegram_consult_matcher import (
    MatchScore,
    enforce_name_gate,
    score_candidate,
)
from beautiful_linkedin.storage.telegram_consult_parser import (
    TelegramCandidate,  # noqa: F401 (re-exported for callers)
    TelegramExtraction,
    parse_telegram_text,
)
from beautiful_linkedin.storage.telegram_group_playwright_lookup import (
    TelegramConsultResult,
)


logger = logging.getLogger(__name__)


def _log_telegram_stage(
    stage: str,
    *,
    run_id: str | None = None,
    table_id: str | None = None,
    lead_ref: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Emit one sidecar-visible, grep-friendly Telegram flow log line.

    Keep sensitive payloads out of the message: CPF/e-mail/phone values
    should appear as counts or source labels, not raw identifiers.
    """
    safe_detail = {
        key: value
        for key, value in (detail or {}).items()
        if value is not None
    }
    detail_text = " ".join(
        f"{key}={value}" for key, value in sorted(safe_detail.items())
    )
    logger.info(
        "telegram_phone_stage stage=%s run_id=%s table_id=%s lead_ref=%s%s%s",
        stage,
        run_id or "-",
        table_id or "-",
        lead_ref or "-",
        " " if detail_text else "",
        detail_text,
    )


# Threshold above which a Telegram CPF candidate is considered solid
# enough to trigger an automatic follow-up query (e.g. /cpf for the
# bot's phone lookup). Calibrated against the matcher's ``confidence_label``
# bands — 65 sits in the lower half of the ``media`` band so the operator
# gets follow-ups on confident matches while clearly weak ones do not
# burn Telegram quota.
TELEGRAM_FOLLOWUP_MIN_SCORE: int = 65

# Maximum number of CPF candidates we follow up on per (lead, run). The
# matcher already ranks by score; this cap protects the Telegram daily
# limits from a single lead with many homonyms.
TELEGRAM_FOLLOWUP_MAX_CANDIDATES: int = 3

# CPFs com idade acima deste limite não devem ir para revisão humana nem
# para follow-up automático. Datas ausentes ou inválidas continuam sendo
# revisáveis porque não há sinal confiável para remover o candidato.
TELEGRAM_CPF_REVIEW_MAX_AGE: int = 75

# Findex e-mail fallback does not have a CPF match score to inherit. Keep
# it conservative and explicit in provenance instead of pretending it has
# the same confidence semantics as the CPF path.
TELEGRAM_EMAIL_FALLBACK_CONFIDENCE: int = 60


# Minutes a provider waits before being consulted again after the bot
# reports a rate-limit signal. Chosen to be conservative — the bots
# typically clear after ~15 minutes, but doubling that absorbs noise in
# the detection layer.
TELEGRAM_RATE_LIMIT_COOLDOWN_MINUTES: int = 30


# Substrings that, when present in ``TelegramConsultResult.error``,
# indicate the bot is rate-limiting us and the provider should enter
# cooldown. Kept conservative to avoid mass cooldowns from generic
# transport errors (timeouts, navigation failures).
_RATE_LIMIT_ERROR_TOKENS = (
    "gon_rate_limit",
    "unix_rate_limit",
    "rate_limit",
    "uso excessivo",
    "limite excedido",
    "muitas requisic",
)


# ---------------------------------------------------------------------------
# Pipeline dataclasses. Stage events live in SQLite (Phase 3), but the
# in-memory representation is what every workflow step exchanges so the
# tests can drive the flow without touching the DB.
# ---------------------------------------------------------------------------


TelegramQueryType = Literal["name", "cpf", "phone", "email"]


TelegramProviderCapability = Literal["name", "cpf", "email", "phone"]


@dataclass(frozen=True)
class TelegramRawRecord:
    """One raw response from a Telegram provider, ready to persist.

    The pipeline never treats ``raw_text`` as truth — it is the input to
    the parser, and any structured fact derived from it is recorded as a
    :class:`TelegramParsedCandidate` with its own provenance.
    """

    run_id: str
    lead_ref: str
    provider: str
    query_type: TelegramQueryType
    query_value: str
    raw_text: str | None = None
    source_url: str | None = None
    downloaded_at: str | None = None
    error: str | None = None
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass(frozen=True)
class TelegramParsedCandidate:
    """A candidate extracted from a raw record, with its score attached.

    The ``match_score`` comes directly from
    :func:`beautiful_linkedin.storage.telegram_consult_matcher.score_candidate`
    — the pipeline never recomputes or invents new scoring. ``signals_used``
    and ``breakdown`` are forwarded verbatim so the UI and downstream
    phone follow-up can audit how the number was derived.
    """

    cpf: str
    nome: str | None
    data_nascimento: str | None
    endereco: str | None
    match_score: int
    signals_used: list[str]
    breakdown: dict[str, Any]
    source_provider: str
    run_id: str


@dataclass
class TelegramProviderState:
    """In-memory view of one provider's runtime state.

    ``cooldown_until`` is the wall-clock instant past which it is safe to
    consult the provider again. ``last_error`` carries the most recent
    failure code (e.g. ``gon_rate_limit``) so the UI can explain why a
    consult was skipped. Daily caps are deliberately not modelled —
    callers asked for cooldown-only because the bots' own rate-limit
    messages are the source of truth.
    """

    provider: str
    cooldown_until: str | None = None
    last_error: str | None = None
    updated_at: str | None = None


@dataclass
class TelegramPipelineResult:
    """Aggregated outcome for one lead in one run.

    Lifetime: a single ``run`` call returns one of these per selected
    lead. The persisted ``TelegramConsult`` rows in SQLite are still the
    source of truth for the UI; this dataclass is the in-memory shape
    that workflow steps pass between themselves.
    """

    run_id: str
    lead_ref: str
    raw_records: list[TelegramRawRecord] = field(default_factory=list)
    candidates: list[TelegramParsedCandidate] = field(default_factory=list)
    blocked_reason: str | None = None
    status: str = "pending"


class TelegramProvider(Protocol):
    """Minimal protocol every Telegram driver must satisfy.

    Each driver advertises which query types it can handle so the
    workflow can route the right query (``/nome``, ``/cpf``, ...) to a
    provider that actually supports it without hard-coding driver
    names. Gon and Unix today advertise ``("name",)``; the phone
    follow-up adapter added in Phase 7 advertises ``("cpf",)``.
    """

    provider: str
    capabilities: tuple[TelegramProviderCapability, ...]

    def consult(
        self, *, query_type: TelegramQueryType, value: str
    ) -> TelegramConsultResult: ...


# ---------------------------------------------------------------------------
# Gates. These run BEFORE we dispatch a sensitive Telegram query (CPF or
# phone follow-up) so leads whose LinkedIn cargo no longer matches the
# table's target titles don't burn the operator's daily quota.
# ---------------------------------------------------------------------------


def evaluate_title_gate(
    lead: Lead, *, target_titles: list[str] | None
) -> str | None:
    """Return ``None`` when the lead's current LinkedIn cargo matches the
    table's target titles, or a string explaining why it is blocked.

    Behavior:

    - ``target_titles`` empty / ``None`` → gate is disabled, returns
      ``None``. Tables that were built from a "busca geral" search never
      ran any title filter to begin with, so applying one at the
      Telegram stage would be surprising.
    - Uses the most-recent title available — ``lead.linkedin_experience_title``
      (populated by the LinkedIn profile refresh) when present, else
      ``lead.title`` (the one the saved row already had).
    - Delegates to :func:`validate_lead_titles` with ``strict=True`` so
      both the alias map and the word-boundary regex match the same gate
      used by the people-search loop. Consistency matters here: a lead
      that the search loop accepted as "marketing" must not be rejected
      by this gate for the same title.
    """
    if not target_titles:
        return None
    fresh_title = _best_linkedin_title_for_gate(lead)
    if not fresh_title:
        return "linkedin_titulo_ausente"
    fresh = lead.model_copy(update={"title": fresh_title})
    outcome = validate_lead_titles([fresh], target_titles, strict=True)
    if not outcome.invalid:
        return None
    reason = outcome.invalid[0].reason
    if reason == ValidationReason.MISSING_TITLE:
        return "linkedin_titulo_ausente"
    return "linkedin_cargo_divergente"


def evaluate_linkedin_signals_gate(lead: Lead) -> str | None:
    """Return ``None`` when the lead has at least one LinkedIn anchor the
    matcher can score against, or a string explaining why it is blocked.

    Sem âncora de localização nem de idade, ``score_candidate`` cai para
    "só nome + data_quality" — homônimos passam o ``min_score`` sem
    nenhum sinal real do LinkedIn. Em vez de queimar quota do /cpf em
    matching cego, paramos o lead aqui com um marker explícito que o
    usuário enxerga na UI ("sem sinais LinkedIn — não consultou") e
    pode resolver populando o lead manualmente.

    Aceita qualquer um dos sinais usados pelo matcher:

    - ``linkedin_location`` (entrada de localização);
    - ``linkedin_education`` não vazio (âncora de idade por graduação);
    - ``linkedin_experience_title`` + ``linkedin_experience_start_year``
      (âncora de idade por estágio de carreira). Os dois juntos são
      necessários porque o ``_score_career_age`` exige título E ano.
    """
    if (lead.linkedin_location or "").strip():
        return None
    if lead.linkedin_education:
        return None
    has_career_anchor = (
        bool(_best_linkedin_title_for_gate(lead))
        and bool(linkedin_experience_years(lead))
    )
    if has_career_anchor:
        return None
    return "missing_linkedin_signals"


def _best_linkedin_title_for_gate(lead: Lead) -> str:
    """Prefer LinkedIn's fresh title unless it is page-navigation noise."""
    for value in (lead.linkedin_experience_title, lead.title):
        title = (value or "").strip()
        if title and not _is_linkedin_navigation_title(title):
            return title
    return ""


def _is_linkedin_navigation_title(value: str) -> bool:
    normalized = _normalize_ascii_words(value)
    return (
        normalized == "skip to main content"
        or (
            normalized.startswith("pular para conte")
            and normalized.endswith("principal")
        )
    )


def _normalize_ascii_words(value: str) -> str:
    import unicodedata

    normalized = unicodedata.normalize("NFD", value or "")
    ascii_text = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    import re

    return re.sub(r"[^a-z0-9]+", " ", ascii_text.lower()).strip()


def select_followup_candidates(
    candidates: list[TelegramParsedCandidate],
    *,
    min_score: int = TELEGRAM_FOLLOWUP_MIN_SCORE,
    max_candidates: int = TELEGRAM_FOLLOWUP_MAX_CANDIDATES,
) -> list[TelegramParsedCandidate]:
    """Pick the candidates that should drive an automatic CPF/phone
    follow-up. Stable order (matcher score desc, then first CPF wins) so
    re-running the workflow on the same persisted candidates yields the
    same shortlist — the workflow's idempotency depends on it.

    Defensa em profundidade contra falso positivo de nome: além de
    exigir ``match_score >= min_score``, descartamos qualquer candidato
    cujo ``breakdown.rejected`` esteja True. O gate determinístico em
    :func:`telegram_consult_matcher.enforce_name_gate` já força score=0
    nesse caso, mas filtrar explicitamente nos protege caso outro
    caminho (legacy data, plug-in, replay) injete score alto com flag de
    rejeição.
    """
    eligible: list[TelegramParsedCandidate] = []
    for candidate in candidates:
        if not cpf_candidate_allowed_for_review(candidate):
            continue
        if candidate.match_score < min_score:
            continue
        if bool((candidate.breakdown or {}).get("rejected")):
            continue
        eligible.append(candidate)
    eligible.sort(key=lambda c: (-c.match_score, c.cpf))
    return eligible[:max_candidates]


def select_persisted_name_compatible_candidates(
    candidates: list[TelegramParsedCandidate],
    *,
    max_candidates: int = TELEGRAM_FOLLOWUP_MAX_CANDIDATES,
) -> list[TelegramParsedCandidate]:
    """For already-persisted CPF evidence, name agreement is enough to
    open the human CPF-selection screen.

    The operator explicitly asked that saved CPF candidates that match
    the lead name skip straight to selection, even when LinkedIn signals
    are missing or the older score is below the automatic follow-up
    threshold. Mismatched names are pruned before this function runs.
    """
    reviewable = [c for c in candidates if cpf_candidate_allowed_for_review(c)]
    ordered = sorted(reviewable, key=lambda c: (-c.match_score, c.cpf))
    return ordered[:max_candidates]


def cpf_candidate_allowed_for_review(candidate: TelegramParsedCandidate) -> bool:
    age = cpf_candidate_age_years(candidate.data_nascimento)
    return age is None or age <= TELEGRAM_CPF_REVIEW_MAX_AGE


def cpf_candidate_age_years(
    data_nascimento: str | None, *, today: date | None = None
) -> int | None:
    if not data_nascimento:
        return None
    raw = data_nascimento.strip()
    if not raw:
        return None
    parsed: date | None = None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(raw, fmt).date()
            break
        except ValueError:
            continue
    if parsed is None:
        return None
    today = today or date.today()
    age = today.year - parsed.year
    if (today.month, today.day) < (parsed.month, parsed.day):
        age -= 1
    if age < 0 or age > 130:
        return None
    return age


def parsed_candidate_from_dict(
    payload: dict[str, Any], *, source_provider: str, run_id: str
) -> TelegramParsedCandidate:
    """Convert the legacy candidate dict (as persisted in
    ``extracted_candidates_json``) into a :class:`TelegramParsedCandidate`.

    Keeps the new pipeline layer ergonomic without breaking the existing
    storage shape. Used by Phase 7 when the phone follow-up reads the
    last name-stage run from SQLite to pick CPFs to query.
    """
    return TelegramParsedCandidate(
        cpf=str(payload.get("cpf") or "").strip(),
        nome=payload.get("nome"),
        data_nascimento=payload.get("data_nascimento"),
        endereco=payload.get("endereco"),
        match_score=int(payload.get("match_score") or 0),
        signals_used=list(payload.get("signals_used") or []),
        breakdown=dict(payload.get("breakdown") or {}),
        source_provider=source_provider,
        run_id=run_id,
    )


def prune_persisted_name_stage_candidates_for_lead(
    *, store: Any, table_id: str, lead: Lead, lead_ref: str
) -> list[TelegramParsedCandidate]:
    """Drop persisted name-stage CPF candidates that do not match lead.

    This prevents a stale row like lead "Julia" carrying "Gustavo"'s CPF
    from being offered in the phone flow. The raw text stays as audit
    trail, but ``extracted_candidates`` is rewritten to only the CPFs
    whose candidate name passes the same deterministic name gate used by
    the matcher.
    """
    lead_name = (lead.person_name or "").strip()
    rows = store.list_telegram_consults_for_lead(
        table_id, lead_ref, query_type="name"
    )
    for row in rows:
        original = list(row.extracted_candidates or [])
        if not original:
            continue
        kept: list[dict[str, Any]] = []
        for payload in original:
            if not isinstance(payload, dict):
                continue
            candidate_name = str(
                payload.get("nome") or row.extracted_nome or ""
            ).strip()
            passed, _reason, _detail = enforce_name_gate(lead_name, candidate_name)
            if passed:
                kept.append(dict(payload))
        if kept != original:
            primary = kept[0] if kept else {}
            store.save_telegram_consult(
                table_id=table_id,
                lead_ref=row.lead_ref,
                provider=row.provider,
                lead_name=row.lead_name,
                query=row.query,
                raw_text=row.raw_text,
                source_url=row.source_url,
                downloaded_at=row.downloaded_at,
                error=row.error,
                extracted_nome=primary.get("nome"),
                extracted_cpf=primary.get("cpf"),
                extracted_birth_date=primary.get("data_nascimento"),
                extracted_address=primary.get("endereco"),
                extracted_candidates=kept,
                match_score=max(
                    (int(item.get("match_score") or 0) for item in kept),
                    default=None,
                ),
                match_details=row.match_details,
                run_id=row.run_id,
                query_type=row.query_type,
                query_value=row.query_value,
                blocked_reason=row.blocked_reason,
            )
    return collect_name_stage_candidates(
        store=store, table_id=table_id, lead_ref=lead_ref
    )


# ---------------------------------------------------------------------------
# Pure helpers shared by the workflow steps and the server adapters.
# ---------------------------------------------------------------------------


def lead_ref_for(lead: Lead) -> str:
    """Return the canonical reference used to key per-lead persistence.

    Mirrors the front-end ``leadRef`` helper so the consult rows line up
    with the identifier the UI uses to address a lead.
    """
    return (lead.linkedin_url or lead.source_url or lead.person_name or "").strip()


def linkedin_experience_years(lead: Lead) -> list[int]:
    """Collect plausible LinkedIn experience years from a saved lead.

    The matcher uses these years as anchors when scoring a Telegram
    candidate's birth date against the lead's career stage.
    """
    years: list[int] = []
    for value in (
        lead.linkedin_experience_start_year,
        lead.linkedin_experience_end_year,
    ):
        if value is not None and 1900 < int(value) < 2100:
            years.append(int(value))
    return years


def parse_and_rank(
    result: TelegramConsultResult, lead: Lead
) -> tuple[TelegramExtraction, list[dict[str, Any]], MatchScore | None]:
    """Parse a raw consult text and rank its CPF candidates against
    ``lead``'s LinkedIn signals.

    Returns the parsed extraction (so the caller can persist the primary
    fields), the candidate dicts with score + breakdown attached, and
    the best score so the row can carry it as a headline metric.

    Scoring is delegated to
    :func:`beautiful_linkedin.storage.telegram_consult_matcher.score_candidate`
    on purpose — this function never invents new score logic, it only
    routes signals into the matcher. Keeping the calculation in one
    place is what makes the confidence audit-able.
    """
    if not result.raw_text:
        return TelegramExtraction(), [], None

    extraction = parse_telegram_text(result.raw_text, provider=result.provider)
    if not extraction.candidates:
        return extraction, [], None

    ranked: list[tuple[MatchScore, TelegramCandidate]] = []
    for candidate in extraction.candidates:
        score = score_candidate(
            candidate,
            lead_name=lead.person_name,
            linkedin_location=lead.linkedin_location,
            linkedin_education=lead.linkedin_education or None,
            linkedin_experience_title=_best_linkedin_title_for_gate(lead) or None,
            linkedin_experience_years=linkedin_experience_years(lead),
            linkedin_birthday=lead.linkedin_birthday,
            snippet_fallback=lead.snippet,
        )
        ranked.append((score, candidate))
    ranked.sort(key=lambda pair: pair[0].score, reverse=True)

    candidate_dicts: list[dict[str, Any]] = []
    for score, candidate in ranked:
        candidate_dicts.append(
            {
                **candidate.to_dict(),
                "match_score": score.score,
                "signals_used": list(score.signals_used),
                "breakdown": score.breakdown,
            }
        )
    return extraction, candidate_dicts, ranked[0][0]


# ---------------------------------------------------------------------------
# Workflow steps. Each function takes its collaborators by argument so
# the server can inject test-friendly fakes and tests can drive the flow
# without touching Playwright or DNS. Defaults preserve the prior
# inline behavior of ``server/app.py``.
# ---------------------------------------------------------------------------


def refresh_linkedin_signals_for_telegram(
    *,
    selected: list[Lead],
    selected_refs: list[str],
    table_id: str,
    store: Any,
    settings: Any,
    validation_runner: Callable[..., list[tuple[Lead, Any]]],
    cdp_probe: Callable[..., bool],
    select_leads: Callable[[list[Lead], list[str]], list[Lead]],
) -> list[Lead]:
    """Best-effort LinkedIn comparison bootstrap for Telegram consults.

    Telegram candidate ranking depends on LinkedIn location/education.
    Older saved leads often do not have those fields yet, so the
    Telegram flow opens the LinkedIn validation pipeline first,
    persists whatever it can read, and then re-loads the selected leads
    before scoring CPF candidates. Browser failures should not discard
    the Telegram consult itself; they simply leave the matcher with
    fewer signals.

    The callers in ``server/app.py`` inject themselves (the validation
    runner and the CDP probe live there) so test monkeypatches on those
    server-module names keep working transparently.
    """
    # Refresh when EITHER linkedin_location or linkedin_education is missing.
    # Location is the strongest single disambiguator against homonyms (two
    # "Gustavo Acacio" CPFs only diverge by city/state in many cases). If we
    # only refreshed when both were missing, a lead with education but no
    # location would silently fall back to "name + age + data_quality" — and
    # the operator never sees ``location`` in the matched signals.
    needs_signals = [
        lead
        for lead in selected
        if lead.linkedin_url
        and (not lead.linkedin_location or not lead.linkedin_education)
    ]
    if not needs_signals:
        return selected
    has_cdp = (
        getattr(settings, "linkedin_cdp_enabled", False)
        and getattr(settings, "linkedin_cdp_endpoint", None)
        and cdp_probe(settings.linkedin_cdp_endpoint, timeout=0.5)
    )
    if not has_cdp:
        logger.info(
            "Telegram consult: sem Chrome CDP ativo; "
            "comparação LinkedIn usará apenas sinais já salvos."
        )
        return selected

    try:
        updates = validation_runner(
            leads=needs_signals,
            settings=settings,
            max_leads=min(40, len(needs_signals)),
        )
    except RuntimeError as exc:
        logger.warning("Telegram consult: validação LinkedIn pulada: %s", exc)
        return selected

    if not updates:
        return selected

    try:
        store.apply_linkedin_profile_validation_updates(table_id, updates)
        refreshed = store.list_leads(table_id)
        return select_leads(refreshed, selected_refs) or selected
    except Exception as exc:
        logger.warning(
            "Telegram consult: falha ao persistir sinais LinkedIn: %s", exc
        )
        return selected


def is_rate_limit_error(error: str | None) -> bool:
    """Detect rate-limit signals from a Telegram consult error string.

    The Gon flow raises ``gon_rate_limit: <preview>`` which the consult
    wrapper formats as ``RuntimeError: gon_rate_limit: <preview>``; we
    match by substring so future drivers can use the same convention
    without re-listing every wording here.
    """
    if not error:
        return False
    lowered = error.lower()
    return any(token in lowered for token in _RATE_LIMIT_ERROR_TOKENS)


def is_provider_in_cooldown(
    state: Any, *, now: datetime | None = None
) -> bool:
    """True when ``state.cooldown_until`` is set and in the future.

    Accepts the storage row dataclass-like object so the pipeline does
    not depend on the concrete Pydantic class.
    """
    if state is None:
        return False
    cooldown_until = getattr(state, "cooldown_until", None)
    if not cooldown_until:
        return False
    try:
        until = datetime.fromisoformat(cooldown_until)
    except ValueError:
        return False
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    return until > reference


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _maybe_apply_cooldown(
    *,
    store: Any,
    result: TelegramConsultResult,
    cooldown_minutes: int,
    now: datetime,
) -> str | None:
    """When ``result.error`` is a rate-limit signal, persist cooldown
    state for the provider and return the cooldown-until ISO string.
    Returns ``None`` otherwise — non rate-limit errors do not cause a
    cooldown so a transient failure does not silently park the provider.
    """
    if not is_rate_limit_error(result.error):
        return None
    cooldown_until = (now + timedelta(minutes=cooldown_minutes)).isoformat()
    store.set_telegram_provider_cooldown(
        result.provider,
        cooldown_until=cooldown_until,
        last_error=result.error or "rate_limited",
    )
    return cooldown_until


def _synthetic_rate_limited_result(
    *, provider: str, lead_name: str, cooldown_until: str
) -> TelegramConsultResult:
    return TelegramConsultResult(
        provider=provider,
        lead_name=lead_name,
        query="",
        raw_text=None,
        source_url=None,
        downloaded_at=None,
        error=f"rate_limited:cooldown_until={cooldown_until}",
    )


def run_telegram_consult(
    *,
    selected: list[Lead],
    table_id: str,
    store: Any,
    lookup: Any,
    cooldown_minutes: int = TELEGRAM_RATE_LIMIT_COOLDOWN_MINUTES,
    now: Callable[[], datetime] | None = None,
    run_id: str | None = None,
) -> list[Any]:
    """Drive the configured providers sequentially per lead, parse the
    raw text into structured candidates, score each candidate against
    the LinkedIn signals, and persist one row per (lead, provider).

    Cooldown semantics: when the lookup advertises ``provider_names`` and
    ``consult_provider`` (the real :class:`TelegramConsultOrchestrator`
    does), each provider is dispatched individually so a provider in
    cooldown is skipped without burning Playwright on a guaranteed-fail
    consult. Legacy lookups exposing only ``consult(name) -> list`` go
    through the original single-call path so tests and stubs are not
    forced to grow new methods.

    Returns the persisted ``TelegramConsult`` rows in the order they
    were saved. The server layer wraps each row in its own Pydantic
    payload — the storage row already carries everything the UI needs.

    Crash safety: each provider's row is committed before the next
    provider runs. A failure mid-batch still preserves everything that
    already succeeded.
    """
    out: list[Any] = []
    clock = now or _now_utc
    provider_names: tuple[str, ...] | None
    if hasattr(lookup, "provider_names") and hasattr(lookup, "consult_provider"):
        candidate = lookup.provider_names
        # Allow either a property/tuple or a callable returning a tuple.
        try:
            provider_names = tuple(candidate() if callable(candidate) else candidate)
        except Exception:
            provider_names = None
    else:
        provider_names = None

    for lead in selected:
        name = (lead.person_name or "").strip()
        ref = lead_ref_for(lead) or name
        if not name or not ref:
            # Record the failure under provider='gon' so the UI shows a
            # single visible "couldn't consult" entry instead of two.
            logger.warning("[pipeline/nome] lead sem nome ou ref — pulando (ref=%r)", ref)
            saved = store.save_telegram_consult(
                table_id=table_id,
                lead_ref=ref or "(sem-ref)",
                provider="gon",
                lead_name=name or "(sem-nome)",
                query="",
                raw_text=None,
                source_url=None,
                downloaded_at=None,
                error="lead_sem_nome_ou_ref",
                run_id=run_id,
                query_type="name",
            )
            out.append(saved)
            continue

        logger.info("[pipeline/nome] iniciando consulta de nome — lead=%r ref=%r", name, ref)

        results: list[TelegramConsultResult]
        if provider_names:
            results = []
            for provider in provider_names:
                state = store.get_telegram_provider_state(provider)
                if is_provider_in_cooldown(state, now=clock()):
                    logger.info(
                        "[pipeline/nome] provider=%s em cooldown — pulando para %r",
                        provider,
                        name,
                    )
                    results.append(
                        _synthetic_rate_limited_result(
                            provider=provider,
                            lead_name=name,
                            cooldown_until=state.cooldown_until,
                        )
                    )
                    continue
                logger.info("[pipeline/nome] enviando /nome → provider=%s lead=%r", provider, name)
                result = lookup.consult_provider(provider, name)
                _maybe_apply_cooldown(
                    store=store,
                    result=result,
                    cooldown_minutes=cooldown_minutes,
                    now=clock(),
                )
                results.append(result)
        else:
            logger.info("[pipeline/nome] enviando /nome (multi-provider) → lead=%r", name)
            raw_results = lookup.consult(name)
            if not isinstance(raw_results, list):
                raw_results = [raw_results]
            results = list(raw_results)
            for result in results:
                _maybe_apply_cooldown(
                    store=store,
                    result=result,
                    cooldown_minutes=cooldown_minutes,
                    now=clock(),
                )

        for result in results:
            extraction, ranked_candidates, top_score = parse_and_rank(result, lead)
            cpf_found = extraction.primary_cpf or (ranked_candidates[0].cpf if ranked_candidates else None)
            logger.info(
                "[pipeline/nome] provider=%s lead=%r → cpf=%s score=%s error=%s",
                result.provider,
                name,
                cpf_found or "–",
                top_score.score if top_score else "–",
                result.error or "ok",
            )
            saved = store.save_telegram_consult(
                table_id=table_id,
                lead_ref=ref,
                provider=result.provider,
                lead_name=name,
                query=result.query,
                raw_text=result.raw_text,
                source_url=result.source_url,
                downloaded_at=result.downloaded_at,
                error=result.error,
                extracted_nome=extraction.primary_nome,
                extracted_cpf=extraction.primary_cpf,
                extracted_birth_date=extraction.primary_birth_date,
                extracted_address=extraction.primary_address,
                extracted_candidates=ranked_candidates,
                match_score=top_score.score if top_score else None,
                match_details=top_score.breakdown if top_score else None,
                run_id=run_id,
                query_type="name",
            )
            out.append(saved)
    return out


# ---------------------------------------------------------------------------
# Phone follow-up. Reads the name-stage CPF candidates persisted by the
# step above, picks the ones strong enough to deserve a paid /cpf query,
# and harvests phones from each resulting raw payload. The phone's
# confidence is inherited 1:1 from the CPF's match score — see the
# ``confidence`` field on :class:`TelegramPhoneCandidate`.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TelegramPhoneCandidate:
    """A phone harvested out of a Telegram ``/cpf`` follow-up.

    ``confidence`` is the matcher's CPF score, propagated unchanged so
    the operator can audit the phone using the same number that drove
    the CPF selection in the first place. Inflating this value here
    would lie to the UI — the workflow refuses to invent a new score.

    ``provenance`` carries the run id, originating CPF, source provider,
    and the matcher's breakdown so any UI surfacing the phone can show
    why it was trusted.
    """

    phone_raw: str
    phone_digits: str
    cpf: str
    confidence: int
    source_provider: str
    run_id: str
    lead_ref: str
    nome: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)


CpfConsultFn = Callable[[str], TelegramConsultResult]
"""Test seam: takes a CPF string, returns a raw consult result."""


import re

# Telegram consult payloads always carry the CPF in ``XXX.XXX.XXX-XX``
# format. The shared ``_PHONE_REGEX`` is permissive and gladly matches
# the leading ``XXX.XXX.XXX`` portion as if it were a Brazilian phone
# (country code + area + number). Mask CPF substrings before scanning
# so the harvest layer sees only actual phone candidates.
_CPF_MASK = re.compile(r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b")
_EMAIL_RE = re.compile(
    r"\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b",
    re.IGNORECASE,
)
_PHONE_CONTEXT_TOKENS = ("telefone", "fone", "celular", "whatsapp", "contato")
_DOCUMENT_CONTEXT_TOKENS = (
    "cpf",
    "cns",
    "rg",
    "cep",
    "documento",
    "documentos",
    "cartao nacional",
    "cartão nacional",
)


def extract_phones_from_text(text: str | None) -> list[tuple[str, str]]:
    """Return ``[(raw, digits_only)]`` for every plausible phone in
    ``text``. Reuses the shared phone regex + plausibility filter so the
    Telegram follow-up does not maintain a parallel definition of "looks
    like a phone".

    CPF strings are masked before scanning — see ``_CPF_MASK`` — so the
    permissive phone regex does not misread the ``XXX.XXX.XXX`` prefix
    of a Brazilian CPF as a phone number.
    """
    if not text:
        return []

    normalized_text = _normalize_telegram_phone_text(text)
    labeled = _extract_labeled_phone_values(normalized_text)
    if labeled:
        return labeled

    masked = _CPF_MASK.sub(lambda m: "_" * len(m.group(0)), normalized_text)
    masked = _drop_document_context_lines(masked)
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for match in _PHONE_REGEX.finditer(masked):
        raw = match.group(1).strip()
        digits = _digits_only(raw)
        if not _is_plausible_phone(digits):
            continue
        if digits in seen:
            continue
        seen.add(digits)
        out.append((raw, digits))
    return out


def extract_emails_from_text(text: str | None) -> list[str]:
    """Return every e-mail address found in a Telegram contact payload.

    CPF/SISREG results can include personal e-mails in the same
    ``Contatos`` section as ``TELEFONE 1`` / ``TELEFONE 2``. These are
    intentionally kept separate from the corporate ``Lead.email`` later
    in storage, but the parser should not discard them.
    """
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for match in _EMAIL_RE.finditer(text):
        email = match.group(0).strip().strip(".,;:").lower()
        if email in seen:
            continue
        seen.add(email)
        out.append(email)
    return out


def _normalize_telegram_phone_text(text: str) -> str:
    # Gonzales/SISREG renders DDD as ``((12))`` in some result cards.
    # The shared phone regex expects one parenthesis pair.
    return re.sub(r"\(\((\d{2,3})\)\)", r"(\1)", text)


def _extract_labeled_phone_values(text: str) -> list[tuple[str, str]]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    seen: set[str] = set()
    out: list[tuple[str, str]] = []

    def add_from_window(window: str) -> None:
        for match in _PHONE_REGEX.finditer(window):
            raw = match.group(1).strip()
            digits = _digits_only(raw)
            if not _is_plausible_phone(digits):
                continue
            if digits in seen:
                continue
            seen.add(digits)
            out.append((raw, digits))

    for index, line in enumerate(lines):
        lowered = line.lower()
        if not any(token in lowered for token in _PHONE_CONTEXT_TOKENS):
            continue
        window_parts = [line]
        for next_line in lines[index + 1 : index + 4]:
            next_lower = next_line.lower()
            if any(token in next_lower for token in _DOCUMENT_CONTEXT_TOKENS):
                break
            window_parts.append(next_line)
            if any(token in next_lower for token in _PHONE_CONTEXT_TOKENS):
                break
        add_from_window(" ".join(window_parts))
    return out


def _drop_document_context_lines(text: str) -> str:
    """Remove document-number lines before generic phone scanning.

    SISREG pages put CPF/CNS/CEP near the top. A 15-digit CNS passes the
    broad phone plausibility check unless we remove document context.
    """
    lines = text.splitlines()
    kept: list[str] = []
    skip_next_numeric = False
    for line in lines:
        lowered = line.strip().lower()
        digits = _digits_only(line)
        has_document_label = any(
            token in lowered for token in _DOCUMENT_CONTEXT_TOKENS
        )
        if has_document_label:
            skip_next_numeric = True
            continue
        if skip_next_numeric and digits and len(digits) >= 5:
            skip_next_numeric = False
            continue
        skip_next_numeric = False
        kept.append(line)
    return "\n".join(kept)


def _persist_phone_followup_row(
    *,
    store: Any,
    table_id: str,
    lead_ref: str,
    lead_name: str,
    candidate: TelegramParsedCandidate,
    result: TelegramConsultResult,
    run_id: str,
    blocked_reason: str | None = None,
) -> Any:
    """Persist a cpf-stage row in ``tabela_telegram``. Mirrors the
    name-stage persistence shape so the UI's existing list query can
    surface follow-ups by filtering on ``query_type='cpf'``.

    The ``extracted_candidates`` JSON for a cpf-stage row carries the
    phones we harvested out of the raw text, each tagged with the
    originating CPF's match score so the alternatives trail in
    :mod:`saved_leads` can adopt the same provenance shape.
    """
    phones = extract_phones_from_text(result.raw_text)
    emails = extract_emails_from_text(result.raw_text)
    candidate_dicts = [
        {
            "phone_raw": raw,
            "phone_digits": digits,
            "emails": emails,
            "cpf": candidate.cpf,
            "nome": candidate.nome,
            "match_score": candidate.match_score,
            "signals_used": list(candidate.signals_used),
            "breakdown": dict(candidate.breakdown),
        }
        for raw, digits in phones
    ]
    return store.save_telegram_consult(
        table_id=table_id,
        lead_ref=lead_ref,
        provider=result.provider,
        lead_name=lead_name,
        query=result.query or f"/cpf {candidate.cpf}",
        raw_text=result.raw_text,
        source_url=result.source_url,
        downloaded_at=result.downloaded_at,
        error=result.error,
        extracted_nome=candidate.nome,
        extracted_cpf=candidate.cpf,
        extracted_birth_date=candidate.data_nascimento,
        extracted_address=candidate.endereco,
        extracted_candidates=candidate_dicts,
        match_score=candidate.match_score,
        match_details=dict(candidate.breakdown),
        run_id=run_id,
        query_type="cpf",
        query_value=candidate.cpf,
        blocked_reason=blocked_reason,
    )


def collect_name_stage_candidates(
    *, store: Any, table_id: str, lead_ref: str
) -> list[TelegramParsedCandidate]:
    """Read the most recent name-stage candidates for one lead.

    Picks the row with the highest match_score per provider as the
    authoritative candidate list, then merges providers. Different
    providers occasionally produce the same CPF; we keep the higher
    score (the matcher rates them on the same scale, so a tie or higher
    score wins regardless of which provider produced it).
    """
    rows = store.list_telegram_consults_for_lead(
        table_id, lead_ref, query_type="name"
    )
    by_cpf: dict[str, TelegramParsedCandidate] = {}
    for row in rows:
        provider = row.provider
        run_id = row.run_id or ""
        primary_cpf = str(row.extracted_cpf or "").strip()
        if primary_cpf:
            primary = TelegramParsedCandidate(
                cpf=primary_cpf,
                nome=row.extracted_nome,
                data_nascimento=row.extracted_birth_date,
                endereco=row.extracted_address,
                match_score=int(row.match_score or 0),
                signals_used=["persisted_primary_cpf"],
                breakdown=dict(row.match_details or {}),
                source_provider=provider,
                run_id=run_id,
            )
            existing = by_cpf.get(primary_cpf)
            if existing is None or primary.match_score > existing.match_score:
                by_cpf[primary_cpf] = primary
        for raw_payload in row.extracted_candidates or []:
            if not isinstance(raw_payload, dict):
                continue
            cpf = str(raw_payload.get("cpf") or "").strip()
            if not cpf:
                continue
            candidate = parsed_candidate_from_dict(
                raw_payload, source_provider=provider, run_id=run_id
            )
            existing = by_cpf.get(cpf)
            if existing is None or candidate.match_score > existing.match_score:
                by_cpf[cpf] = candidate
    return list(by_cpf.values())


def latest_name_stage_consult(
    *, store: Any, table_id: str, lead_ref: str
) -> Any | None:
    """Return the newest persisted name-stage row for ``lead_ref``.

    The unified phone flow uses this when a verified CPF already exists:
    it can skip a fresh ``/nome`` query but still return the raw row that
    originally proved the CPF to the UI.
    """
    rows = store.list_telegram_consults_for_lead(
        table_id, lead_ref, query_type="name"
    )
    for row in rows:
        if row.extracted_candidates:
            return row
    return rows[0] if rows else None


def run_phone_followup(
    *,
    lead: Lead,
    table_id: str,
    store: Any,
    consult_fn: CpfConsultFn,
    target_titles: list[str] | None = None,
    run_id: str | None = None,
    provider_name: str = "gon_cpf",
    cooldown_minutes: int = TELEGRAM_RATE_LIMIT_COOLDOWN_MINUTES,
    min_score: int = TELEGRAM_FOLLOWUP_MIN_SCORE,
    max_candidates: int = TELEGRAM_FOLLOWUP_MAX_CANDIDATES,
    now: Callable[[], datetime] | None = None,
) -> list[TelegramPhoneCandidate]:
    """Run the CPF-stage follow-up and return harvested phone candidates.

    Pre-conditions: the name-stage pipeline already ran for this lead so
    ``tabela_telegram`` has rows with ``query_type='name'`` carrying
    ranked CPF candidates. This function only consults the bot for CPFs
    that survived the matcher's threshold — never for an arbitrary name.

    Confidence policy: the returned candidates inherit
    ``match_score`` from the originating CPF entry 1:1. The pipeline
    never multiplies, caps, or otherwise transforms that number. The
    operator's audit trail is the matcher's breakdown, which we forward
    in ``provenance``.

    Cooldown policy: when the provider is in cooldown the workflow does
    NOT call ``consult_fn``; instead it persists a blocked row so the UI
    can show "skipped: provider cooling down" and the operator can
    intervene. A successful consult clears the previous cooldown so the
    operator does not have to wait the full window when the bot
    recovers early.

    Returns the harvested :class:`TelegramPhoneCandidate` list in the
    same order as the eligible CPFs (highest CPF score first).
    """
    clock = now or _now_utc
    effective_run_id = run_id or f"phone-{int(clock().timestamp()*1000)}"
    lead_ref = lead_ref_for(lead) or (lead.person_name or "").strip()
    lead_name = (lead.person_name or "").strip()
    out: list[TelegramPhoneCandidate] = []

    # Title gate REMOVIDO: a seleção do operador já é a decisão de gastar
    # quota. Ver comentário em ``run_extract_phone_via_cpf`` para contexto.

    candidates = collect_name_stage_candidates(
        store=store, table_id=table_id, lead_ref=lead_ref
    )
    eligible = select_followup_candidates(
        candidates, min_score=min_score, max_candidates=max_candidates
    )
    logger.info(
        "[pipeline/cpf] lead=%r — %d candidatos totais, %d elegíveis (score≥%d)",
        lead_name,
        len(candidates),
        len(eligible),
        min_score,
    )
    if not eligible:
        # Persist a sentinel row when there were no eligible candidates
        # so the UI knows the follow-up actually ran (instead of being
        # silently skipped) and can show the threshold that was used.
        logger.info(
            "[pipeline/cpf] lead=%r — nenhum CPF acima de %d; pulando fase de telefone",
            lead_name,
            min_score,
        )
        store.save_telegram_consult(
            table_id=table_id,
            lead_ref=lead_ref or "(sem-ref)",
            provider=provider_name,
            lead_name=lead_name or "(sem-nome)",
            query="",
            raw_text=None,
            source_url=None,
            downloaded_at=None,
            error=f"no_eligible_cpf_above_{min_score}",
            run_id=effective_run_id,
            query_type="cpf",
            blocked_reason="no_eligible_cpf",
        )
        return out

    for candidate in eligible:
        state = store.get_telegram_provider_state(provider_name)
        if is_provider_in_cooldown(state, now=clock()):
            logger.info(
                "[pipeline/cpf] provider=%s em cooldown — pulando CPF %s para lead=%r",
                provider_name,
                candidate.cpf,
                lead_name,
            )
            result = _synthetic_rate_limited_result(
                provider=provider_name,
                lead_name=lead_name,
                cooldown_until=state.cooldown_until,
            )
            _persist_phone_followup_row(
                store=store,
                table_id=table_id,
                lead_ref=lead_ref,
                lead_name=lead_name,
                candidate=candidate,
                result=result,
                run_id=effective_run_id,
                blocked_reason="rate_limited",
            )
            continue

        logger.info(
            "[pipeline/cpf] enviando /cpf %s → provider=%s lead=%r (score=%d)",
            candidate.cpf,
            provider_name,
            lead_name,
            candidate.match_score,
        )
        try:
            result = consult_fn(candidate.cpf)
        except Exception as exc:
            logger.exception(
                "Telegram /cpf falhou para %s (cpf=%s)", lead_name, candidate.cpf
            )
            result = TelegramConsultResult(
                provider=provider_name,
                lead_name=lead_name,
                query=f"/cpf {candidate.cpf}",
                raw_text=None,
                source_url=None,
                downloaded_at=None,
                error=f"{type(exc).__name__}: {exc}",
            )

        _maybe_apply_cooldown(
            store=store,
            result=result,
            cooldown_minutes=cooldown_minutes,
            now=clock(),
        )
        _persist_phone_followup_row(
            store=store,
            table_id=table_id,
            lead_ref=lead_ref,
            lead_name=lead_name,
            candidate=candidate,
            result=result,
            run_id=effective_run_id,
        )

        if result.error or not result.raw_text:
            logger.info(
                "[pipeline/cpf] CPF %s lead=%r → sem resposta útil (error=%s)",
                candidate.cpf,
                lead_name,
                result.error or "sem raw_text",
            )
            continue
        phones = extract_phones_from_text(result.raw_text)
        if phones:
            logger.info(
                "[pipeline/cpf] CPF %s lead=%r → %d telefone(s) encontrado(s): %s",
                candidate.cpf,
                lead_name,
                len(phones),
                ", ".join(raw for raw, _ in phones),
            )
        else:
            logger.info(
                "[pipeline/cpf] CPF %s lead=%r → resposta recebida mas sem telefone",
                candidate.cpf,
                lead_name,
            )
        for raw, digits in phones:
            out.append(
                TelegramPhoneCandidate(
                    phone_raw=raw,
                    phone_digits=digits,
                    cpf=candidate.cpf,
                    confidence=candidate.match_score,
                    source_provider=result.provider,
                    run_id=effective_run_id,
                    lead_ref=lead_ref,
                    nome=candidate.nome,
                    provenance={
                        "score_source": "telegram_match_score",
                        "cpf_match_score": candidate.match_score,
                        "cpf_signals_used": list(candidate.signals_used),
                        "cpf_breakdown": dict(candidate.breakdown),
                        "name_stage_provider": candidate.source_provider,
                        "cpf_stage_provider": result.provider,
                        "raw_source_url": result.source_url,
                    },
                )
            )
    return out


# ---------------------------------------------------------------------------
# Unified workflow: name -> cpf -> phone in a single call. The two-step
# product UX (run name, then run cpf) leaks the pipeline's internal shape
# to the operator, so this function collapses both stages into one
# atomic operation and the UI exposes a single "pegar telefone" button.
# ---------------------------------------------------------------------------


@dataclass
class TelegramStageEvent:
    """One observable transition in the Telegram phone flow.

    ``stage`` is a stable identifier ("title_gate_passed", "gonzales_name_sent",
    "findex_email_parsed", "phones_persisted", "completed", ...) that the UI
    keys against to render a per-lead timeline. ``detail`` carries small
    structured context (provider name, e-mail source, blocked reason, phone
    count, ...). ``timestamp`` is ISO-8601 UTC.

    The pipeline emits one event per real transition so the operator can
    see EXACTLY where each lead stopped — clicking "Pegar telefone via
    Telegram" no longer feels like a black box that just "opens LinkedIn".
    """

    stage: str
    timestamp: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "timestamp": self.timestamp,
            "detail": dict(self.detail),
        }


@dataclass
class TelegramPhoneFlowResult:
    """Atomic outcome of one ``name -> cpf -> phone`` run for one lead.

    ``blocked_reason`` is set when the workflow short-circuited (cargo
    divergente, provider cooldown, no CPF found, no eligible CPF, ...).
    ``phone_candidates`` carries the harvested phones with their CPF
    provenance — ``confidence`` is inherited 1:1 from the CPF matcher.
    ``stages`` is the ordered event log emitted during this run.
    """

    lead_ref: str
    lead_name: str | None
    blocked_reason: str | None
    name_consult: Any | None
    cpf_consult: Any | None
    phone_candidates: list[TelegramPhoneCandidate]
    stages: list[TelegramStageEvent] = field(default_factory=list)

    @property
    def last_stage(self) -> str | None:
        return self.stages[-1].stage if self.stages else None


@dataclass
class TelegramNameStageResult:
    """Resultado da etapa 1 (somente ``/nome`` + matcher).

    O fluxo agora é dividido em duas etapas explícitas para o operador
    revisar quais CPFs serão consultados antes de queimar a quota do
    /cpf. Esta dataclass carrega TUDO que o operador precisa enxergar
    para a confirmação:

    - ``eligible_candidates``: CPFs já filtrados pelo gate de nome,
      ``min_score`` e ``max_candidates`` — são os "mais prováveis de
      ser a pessoa certa". A UI exibe esses pré-marcados.
    - ``all_candidates``: tudo que veio do bot, mesmo o que o matcher
      rejeitou (score=0 com ``rejected=True``, ou score < min_score).
      A UI pode oferecer "ver todos" para o operador adicionar
      manualmente se achar que o matcher errou.
    - ``blocked_reason`` curto-circuita o lead (gate de cargo, signals
      ausentes, cooldown, sem CPF na resposta). Quando setado, a UI
      pula direto pro próximo lead sem oferecer revisão.
    """

    lead_ref: str
    lead_name: str | None
    blocked_reason: str | None
    name_consult: Any | None
    eligible_candidates: list[TelegramParsedCandidate]
    all_candidates: list[TelegramParsedCandidate]


@dataclass
class TelegramCpfStageResult:
    """Resultado da etapa 2 (``/cpf`` em CPFs já escolhidos pela UI).

    Aceita um subconjunto de CPFs vindos do passo de confirmação humana.
    Cada CPF roda no fluxo SISREG-III e devolve telefones harvested.
    """

    lead_ref: str
    lead_name: str | None
    blocked_reason: str | None
    cpf_consult: Any | None
    phone_candidates: list[TelegramPhoneCandidate]


def _persist_cpf_stage_summary(
    *,
    store: Any,
    table_id: str,
    lead_ref: str,
    lead_name: str,
    cpf_provider_name: str,
    run_id: str,
    attempts: list[tuple[TelegramParsedCandidate, TelegramConsultResult, list[tuple[str, str]]]],
    blocked_reason: str | None = None,
) -> Any | None:
    """Aggregate every ``/cpf`` attempt of a single lead into one
    storage row.

    Storage shape: ``extracted_candidates`` carries one dict per CPF
    attempted, nesting the phones harvested for it; ``raw_text`` is a
    concatenation of every CPF's raw response with explicit separators
    so the operator can audit the page contents end-to-end without
    chasing multiple rows. The UNIQUE constraint on
    ``(table, lead_ref, provider, query_type)`` then upserts the
    summary on re-runs without losing earlier attempts within the same
    run.
    """
    if not attempts and blocked_reason is None:
        return None
    candidate_dicts: list[dict[str, Any]] = []
    raw_parts: list[str] = []
    last_result: TelegramConsultResult | None = None
    last_error: str | None = None
    for candidate, result, phones in attempts:
        last_result = result
        if result.error:
            last_error = result.error
        if result.raw_text:
            raw_parts.append(f"=== /cpf {candidate.cpf} ===\n{result.raw_text}")
        candidate_dicts.append(
            {
                "cpf": candidate.cpf,
                "nome": candidate.nome,
                "data_nascimento": candidate.data_nascimento,
                "endereco": candidate.endereco,
                "match_score": candidate.match_score,
                "signals_used": list(candidate.signals_used),
                "breakdown": dict(candidate.breakdown),
                "phones": [
                    {"raw": r, "digits": d} for r, d in phones
                ],
                "emails": extract_emails_from_text(result.raw_text),
                "cpf_query_error": result.error,
                "cpf_source_url": result.source_url,
            }
        )
    cpfs_str = ",".join(c.cpf for c, _, _ in attempts) if attempts else ""
    return store.save_telegram_consult(
        table_id=table_id,
        lead_ref=lead_ref,
        provider=cpf_provider_name,
        lead_name=lead_name,
        query=f"/cpf ({len(candidate_dicts)} CPFs)" if candidate_dicts else "",
        raw_text="\n\n".join(raw_parts) if raw_parts else None,
        source_url=last_result.source_url if last_result else None,
        downloaded_at=last_result.downloaded_at if last_result else None,
        error=last_error,
        extracted_candidates=candidate_dicts,
        match_score=max(
            (c.match_score for c, _, _ in attempts), default=None
        ),
        run_id=run_id,
        query_type="cpf",
        query_value=cpfs_str or None,
        blocked_reason=blocked_reason,
    )


def _run_cpf_stage_for_candidates(
    *,
    eligible: list[TelegramParsedCandidate],
    lead_ref: str,
    lead_name: str,
    table_id: str,
    store: Any,
    cpf_consult_fn: Callable[[str], TelegramConsultResult],
    cpf_provider_name: str,
    run_id: str,
    cooldown_minutes: int,
    clock: Callable[[], datetime],
) -> tuple[Any | None, list[TelegramPhoneCandidate], str | None]:
    """Run ``/cpf`` for already-selected CPF candidates.

    Shared by the normal name -> cpf path and by the short-circuit path
    where a previous run already persisted a verified CPF.
    """
    attempts: list[
        tuple[TelegramParsedCandidate, TelegramConsultResult, list[tuple[str, str]]]
    ] = []
    phone_candidates: list[TelegramPhoneCandidate] = []
    overall_blocked: str | None = None
    for candidate in eligible:
        _log_telegram_stage(
            "cpf_attempt_started",
            run_id=run_id,
            table_id=table_id,
            lead_ref=lead_ref,
            detail={
                "provider": cpf_provider_name,
                "cpf_score": candidate.match_score,
            },
        )
        cpf_state = store.get_telegram_provider_state(cpf_provider_name)
        if is_provider_in_cooldown(cpf_state, now=clock()):
            _log_telegram_stage(
                "cpf_attempt_rate_limited",
                run_id=run_id,
                table_id=table_id,
                lead_ref=lead_ref,
                detail={"provider": cpf_provider_name},
            )
            synth = _synthetic_rate_limited_result(
                provider=cpf_provider_name,
                lead_name=lead_name,
                cooldown_until=cpf_state.cooldown_until,
            )
            attempts.append((candidate, synth, []))
            overall_blocked = "rate_limited"
            continue

        try:
            cpf_result = cpf_consult_fn(candidate.cpf)
        except Exception as exc:
            logger.exception(
                "Telegram /cpf falhou para %s (cpf=%s)", lead_name, candidate.cpf
            )
            cpf_result = TelegramConsultResult(
                provider=cpf_provider_name,
                lead_name=lead_name,
                query=f"/cpf {candidate.cpf}",
                raw_text=None,
                source_url=None,
                downloaded_at=None,
                error=f"{type(exc).__name__}: {exc}",
            )
        _log_telegram_stage(
            "cpf_attempt_response_received",
            run_id=run_id,
            table_id=table_id,
            lead_ref=lead_ref,
            detail={
                "provider": cpf_result.provider,
                "has_raw_text": bool(cpf_result.raw_text),
                "error": bool(cpf_result.error),
            },
        )

        _maybe_apply_cooldown(
            store=store,
            result=cpf_result,
            cooldown_minutes=cooldown_minutes,
            now=clock(),
        )

        phones = (
            extract_phones_from_text(cpf_result.raw_text)
            if cpf_result.raw_text
            else []
        )
        emails = extract_emails_from_text(cpf_result.raw_text)
        _log_telegram_stage(
            "cpf_attempt_parsed",
            run_id=run_id,
            table_id=table_id,
            lead_ref=lead_ref,
            detail={
                "phones": len(phones),
                "contact_emails": len(emails),
                "error": bool(cpf_result.error),
            },
        )
        attempts.append((candidate, cpf_result, phones))
        for raw, digits in phones:
            phone_candidates.append(
                TelegramPhoneCandidate(
                    phone_raw=raw,
                    phone_digits=digits,
                    cpf=candidate.cpf,
                    confidence=candidate.match_score,
                    source_provider=cpf_result.provider,
                    run_id=run_id,
                    lead_ref=lead_ref,
                    nome=candidate.nome,
                    provenance={
                        "score_source": "telegram_match_score",
                        "cpf_match_score": candidate.match_score,
                        "cpf_signals_used": list(candidate.signals_used),
                        "cpf_breakdown": dict(candidate.breakdown),
                        "name_stage_provider": candidate.source_provider,
                        "cpf_stage_provider": cpf_result.provider,
                        "raw_source_url": cpf_result.source_url,
                        "contact_emails": emails,
                    },
                )
            )

    cpf_consult = _persist_cpf_stage_summary(
        store=store,
        table_id=table_id,
        lead_ref=lead_ref,
        lead_name=lead_name,
        cpf_provider_name=cpf_provider_name,
        run_id=run_id,
        attempts=attempts,
        blocked_reason=overall_blocked,
    )
    _log_telegram_stage(
        "cpf_stage_summary_persisted",
        run_id=run_id,
        table_id=table_id,
        lead_ref=lead_ref,
        detail={
            "attempts": len(attempts),
            "phones": len(phone_candidates),
            "blocked_reason": overall_blocked,
        },
    )
    return cpf_consult, phone_candidates, overall_blocked


def _run_findex_email_fallback(
    *,
    lead: Lead,
    lead_ref: str,
    lead_name: str,
    table_id: str,
    store: Any,
    email_consult_fn: Callable[[str], TelegramConsultResult] | None,
    email_provider_name: str,
    run_id: str,
    cooldown_minutes: int,
    clock: Callable[[], datetime],
) -> tuple[Any | None, list[TelegramPhoneCandidate], str | None]:
    """Run the Findex ``/email <email>`` fallback when name -> CPF fails."""
    email, email_source = resolve_findex_email(lead)
    if email_consult_fn is None or not email:
        return None, [], "no_email_for_findex_fallback"

    state = store.get_telegram_provider_state(email_provider_name)
    if is_provider_in_cooldown(state, now=clock()):
        result = _synthetic_rate_limited_result(
            provider=email_provider_name,
            lead_name=lead_name,
            cooldown_until=state.cooldown_until,
        )
        blocked_reason = "rate_limited"
    else:
        try:
            result = email_consult_fn(email)
        except Exception as exc:
            logger.exception(
                "Telegram Findex /email falhou para %s (email=%s)", lead_name, email
            )
            result = TelegramConsultResult(
                provider=email_provider_name,
                lead_name=lead_name,
                query=f"/email {email}",
                raw_text=None,
                source_url=None,
                downloaded_at=None,
                error=f"{type(exc).__name__}: {exc}",
            )
        _maybe_apply_cooldown(
            store=store,
            result=result,
            cooldown_minutes=cooldown_minutes,
            now=clock(),
        )
        blocked_reason = None

    phones = extract_phones_from_text(result.raw_text)
    emails = extract_emails_from_text(result.raw_text)
    candidate_dicts = [
        {
            "phone_raw": raw,
            "phone_digits": digits,
            "email": email,
            "match_score": TELEGRAM_EMAIL_FALLBACK_CONFIDENCE,
            "signals_used": ["email"],
            "breakdown": {
                "score_source": "findex_email_fallback",
                "email_source": email_source,
                "contact_emails": emails,
            },
        }
        for raw, digits in phones
    ]
    if result.error:
        blocked_reason = blocked_reason or f"email_stage_error:{result.error}"
    elif not phones:
        blocked_reason = blocked_reason or "email_stage_no_phone"

    row = store.save_telegram_consult(
        table_id=table_id,
        lead_ref=lead_ref,
        provider=result.provider or email_provider_name,
        lead_name=lead_name,
        query=result.query or f"/email {email}",
        raw_text=result.raw_text,
        source_url=result.source_url,
        downloaded_at=result.downloaded_at,
        error=result.error,
        extracted_candidates=candidate_dicts,
        match_score=TELEGRAM_EMAIL_FALLBACK_CONFIDENCE if phones else None,
        match_details=(
            {
                "score_source": "findex_email_fallback",
                "email_source": email_source,
            }
            if phones
            else None
        ),
        run_id=run_id,
        query_type="email",
        query_value=email,
        blocked_reason=blocked_reason,
    )

    phone_candidates = [
        TelegramPhoneCandidate(
            phone_raw=raw,
            phone_digits=digits,
            cpf="",
            confidence=TELEGRAM_EMAIL_FALLBACK_CONFIDENCE,
            source_provider=result.provider or email_provider_name,
            run_id=run_id,
            lead_ref=lead_ref,
            nome=lead_name,
            provenance={
                "score_source": "findex_email_fallback",
                "email": email,
                "email_source": email_source,
                "email_stage_provider": result.provider or email_provider_name,
                "raw_source_url": result.source_url,
                "contact_emails": emails,
            },
        )
        for raw, digits in phones
    ]
    return row, phone_candidates, blocked_reason if not phones else None


def resolve_findex_email(lead: Lead) -> tuple[str, str] | tuple[None, None]:
    """Return ``(email, source_label)`` for the Findex fallback, in priority order.

    Sources, in order:

    - ``primary`` — ``lead.email`` (the canonical address the operator sees).
    - ``linkedin_contact`` — ``lead.linkedin_contact_email`` (scraped from
      the LinkedIn profile's contact panel during the validation step).
    - ``alternatives`` — first usable entry in ``lead.email_alternatives``
      (a different source disagreed with the primary; the address itself is
      still a useful seed for Findex). Entries without an ``email`` value
      are skipped.

    Returning the source label lets the caller stamp
    ``provenance.email_source`` on the resulting phone candidate, so the UI
    can audit which channel paid for each phone.
    """
    primary = (lead.email or "").strip()
    if primary:
        return primary, "primary"
    contact = (lead.linkedin_contact_email or "").strip()
    if contact:
        return contact, "linkedin_contact"
    for entry in lead.email_alternatives or []:
        email_value = str((entry or {}).get("email") or "").strip()
        if email_value:
            return email_value, "alternatives"
    return None, None


def _email_for_findex_fallback(lead: Lead) -> str:
    """Backwards-compatible wrapper around :func:`resolve_findex_email`."""
    email, _source = resolve_findex_email(lead)
    return email or ""


def run_extract_phone_via_cpf(
    *,
    lead: Lead,
    table_id: str,
    store: Any,
    name_consult_fn: Callable[[str], TelegramConsultResult],
    cpf_consult_fn: Callable[[str], TelegramConsultResult],
    email_consult_fn: Callable[[str], TelegramConsultResult] | None = None,
    target_titles: list[str] | None = None,
    run_id: str | None = None,
    name_provider_name: str = "gon",
    cpf_provider_name: str = "gon_cpf",
    email_provider_name: str = "findex",
    cooldown_minutes: int = TELEGRAM_RATE_LIMIT_COOLDOWN_MINUTES,
    min_score: int = TELEGRAM_FOLLOWUP_MIN_SCORE,
    max_candidates: int = TELEGRAM_FOLLOWUP_MAX_CANDIDATES,
    now: Callable[[], datetime] | None = None,
) -> TelegramPhoneFlowResult:
    """Atomic ``name -> cpf -> phone`` workflow for one lead.

    Stage order:

    1. **Title gate** — if ``target_titles`` is set and the lead's
       current LinkedIn cargo no longer matches, the workflow stops
       before any Telegram traffic. Persists a marker row.
    2. **Name stage** — calls ``name_consult_fn(lead_name)`` (Gonzales
       ``/nome`` in production), parses the raw text into ranked CPF
       candidates against LinkedIn signals, persists one row with
       ``query_type='name'``.
    3. **CPF stage** — for each CPF above ``min_score`` (top
       ``max_candidates``), calls ``cpf_consult_fn(cpf)`` (Gonzales
       ``/cpf``), harvests phones from the response, aggregates every
       attempt into one row with ``query_type='cpf'``.

    Cooldown is enforced before each stage and per provider — a
    rate-limited Gonzales does not get hit again until the window
    expires. ``confidence`` of each returned phone is the originating
    CPF's matcher score, 1:1, never recomputed.
    """
    clock = now or _now_utc
    effective_run_id = run_id or f"flow-{int(clock().timestamp() * 1000)}"
    lead_ref = lead_ref_for(lead) or (lead.person_name or "").strip()
    lead_name = (lead.person_name or "").strip()
    _log_telegram_stage(
        "name_stage_started",
        run_id=effective_run_id,
        table_id=table_id,
        lead_ref=lead_ref or None,
    )

    stages: list[TelegramStageEvent] = []

    def emit(stage: str, **detail: Any) -> None:
        clean_detail = {k: v for k, v in detail.items() if v is not None}
        stages.append(
            TelegramStageEvent(
                stage=stage,
                timestamp=clock().isoformat(),
                detail=clean_detail,
            )
        )
        _log_telegram_stage(
            stage,
            run_id=effective_run_id,
            table_id=table_id,
            lead_ref=lead_ref or None,
            detail=clean_detail,
        )

    emit("run_started", run_id=effective_run_id, lead_ref=lead_ref or None)

    if not lead_name or not lead_ref:
        emit("blocked", reason="lead_sem_nome_ou_ref")
        return TelegramPhoneFlowResult(
            lead_ref=lead_ref or "(sem-ref)",
            lead_name=None,
            blocked_reason="lead_sem_nome_ou_ref",
            name_consult=None,
            cpf_consult=None,
            phone_candidates=[],
            stages=stages,
        )

    # Title gate REMOVIDO de propósito: o operador já selecionou esse lead
    # via checkbox, então a decisão de gastar quota Telegram já foi tomada.
    # Gatear por cargo do LinkedIn aqui só duplica essa decisão e produz
    # "linkedin_cargo_divergente" confusos quando o cargo é composto, foi
    # parseado errado, ou simplesmente não bate exato com o alias da tabela.
    # ``target_titles`` continua aceito na assinatura para compat com a UI,
    # mas não é mais consultado nesta etapa. Se reintroduzir um dia, faça
    # como soft signal (ex.: ranquear leads por afinidade de cargo) e não
    # como bloqueio.

    # 1b) Sinais LinkedIn (location/educação/experiência) precisam estar
    #     populados — senão o matcher cai para "só nome" e homônimos
    #     escapam. Em vez de bloquear silenciosamente, agora desviamos pra
    #     Findex quando há e-mail disponível: o /usa <email> não depende
    #     de signals do LinkedIn pra desambiguar. Só seguramos o lead
    #     quando nem sinais nem e-mail existem — aí o operador realmente
    #     precisa enriquecer o LinkedIn antes.
    signals_block = evaluate_linkedin_signals_gate(lead)
    if signals_block is not None:
        findex_email, findex_source = resolve_findex_email(lead)
        if email_consult_fn is not None and findex_email:
            emit(
                "signals_gate_routed_to_findex",
                reason=signals_block,
                email_source=findex_source,
            )
            marker = store.save_telegram_consult(
                table_id=table_id,
                lead_ref=lead_ref,
                provider=name_provider_name,
                lead_name=lead_name,
                query="",
                raw_text=None,
                source_url=None,
                downloaded_at=None,
                error=signals_block,
                run_id=effective_run_id,
                query_type="name",
                blocked_reason=f"{signals_block}_routed_to_findex",
            )
            emit("findex_email_sent", email_source=findex_source)
            email_row, email_phones, email_blocked = _run_findex_email_fallback(
                lead=lead,
                lead_ref=lead_ref,
                lead_name=lead_name,
                table_id=table_id,
                store=store,
                email_consult_fn=email_consult_fn,
                email_provider_name=email_provider_name,
                run_id=effective_run_id,
                cooldown_minutes=cooldown_minutes,
                clock=clock,
            )
            emit(
                "findex_email_parsed",
                phones=len(email_phones),
                blocked_reason=email_blocked,
            )
            if email_blocked is None and email_phones:
                emit("completed", phones=len(email_phones))
            else:
                emit("blocked", reason=email_blocked or "email_stage_no_phone")
            return TelegramPhoneFlowResult(
                lead_ref=lead_ref,
                lead_name=lead_name,
                blocked_reason=email_blocked,
                name_consult=marker,
                cpf_consult=email_row,
                phone_candidates=email_phones,
                stages=stages,
            )
        emit("signals_gate_blocked", reason=signals_block)
        marker = store.save_telegram_consult(
            table_id=table_id,
            lead_ref=lead_ref,
            provider=name_provider_name,
            lead_name=lead_name,
            query="",
            raw_text=None,
            source_url=None,
            downloaded_at=None,
            error=signals_block,
            run_id=effective_run_id,
            query_type="name",
            blocked_reason=signals_block,
        )
        emit("blocked", reason=signals_block)
        return TelegramPhoneFlowResult(
            lead_ref=lead_ref,
            lead_name=lead_name,
            blocked_reason=signals_block,
            name_consult=marker,
            cpf_consult=None,
            phone_candidates=[],
            stages=stages,
        )
    emit("signals_gate_passed")

    persisted_candidates = select_followup_candidates(
        collect_name_stage_candidates(
            store=store, table_id=table_id, lead_ref=lead_ref
        ),
        min_score=min_score,
        max_candidates=max_candidates,
    )
    if persisted_candidates:
        emit(
            "persisted_cpf_used",
            cpf_count=len(persisted_candidates),
            top_score=persisted_candidates[0].match_score,
        )
        name_consult = latest_name_stage_consult(
            store=store, table_id=table_id, lead_ref=lead_ref
        )
        emit("gonzales_cpf_sent", cpfs=len(persisted_candidates))
        cpf_consult, phone_candidates, blocked = _run_cpf_stage_for_candidates(
            eligible=persisted_candidates,
            lead_ref=lead_ref,
            lead_name=lead_name,
            table_id=table_id,
            store=store,
            cpf_consult_fn=cpf_consult_fn,
            cpf_provider_name=cpf_provider_name,
            run_id=effective_run_id,
            cooldown_minutes=cooldown_minutes,
            clock=clock,
        )
        emit(
            "gonzales_cpf_parsed",
            phones=len(phone_candidates),
            blocked_reason=blocked,
        )
        if not blocked and phone_candidates:
            emit("completed", phones=len(phone_candidates))
        else:
            emit("blocked", reason=blocked or "no_phone_from_cpf_stage")
        return TelegramPhoneFlowResult(
            lead_ref=lead_ref,
            lead_name=lead_name,
            blocked_reason=blocked,
            name_consult=name_consult,
            cpf_consult=cpf_consult,
            phone_candidates=phone_candidates,
            stages=stages,
        )

    # 2) Name stage — cooldown check first; skip the consult if the
    #    provider is parked, otherwise dispatch and persist.
    name_state = store.get_telegram_provider_state(name_provider_name)
    if is_provider_in_cooldown(name_state, now=clock()):
        synth = _synthetic_rate_limited_result(
            provider=name_provider_name,
            lead_name=lead_name,
            cooldown_until=name_state.cooldown_until,
        )
        emit(
            "rate_limited",
            provider=name_provider_name,
            cooldown_until=(
                name_state.cooldown_until.isoformat()
                if name_state.cooldown_until
                else None
            ),
        )
        marker = store.save_telegram_consult(
            table_id=table_id,
            lead_ref=lead_ref,
            provider=name_provider_name,
            lead_name=lead_name,
            query="",
            raw_text=None,
            source_url=None,
            downloaded_at=None,
            error=synth.error,
            run_id=effective_run_id,
            query_type="name",
            blocked_reason="rate_limited",
        )
        emit("blocked", reason="rate_limited")
        return TelegramPhoneFlowResult(
            lead_ref=lead_ref,
            lead_name=lead_name,
            blocked_reason="rate_limited",
            name_consult=marker,
            cpf_consult=None,
            phone_candidates=[],
            stages=stages,
        )

    emit("gonzales_name_sent", provider=name_provider_name)
    try:
        name_result = name_consult_fn(lead_name)
    except Exception as exc:
        logger.exception("Telegram /nome falhou para %s", lead_name)
        name_result = TelegramConsultResult(
            provider=name_provider_name,
            lead_name=lead_name,
            query=f"/nome {lead_name}",
            raw_text=None,
            source_url=None,
            downloaded_at=None,
            error=f"{type(exc).__name__}: {exc}",
        )

    _maybe_apply_cooldown(
        store=store,
        result=name_result,
        cooldown_minutes=cooldown_minutes,
        now=clock(),
    )

    extraction, ranked_candidates, top_score = parse_and_rank(name_result, lead)
    emit(
        "gonzales_name_parsed",
        candidates=len(ranked_candidates),
        top_score=top_score.score if top_score else None,
        error=name_result.error,
    )
    name_consult = store.save_telegram_consult(
        table_id=table_id,
        lead_ref=lead_ref,
        provider=name_result.provider,
        lead_name=lead_name,
        query=name_result.query,
        raw_text=name_result.raw_text,
        source_url=name_result.source_url,
        downloaded_at=name_result.downloaded_at,
        error=name_result.error,
        extracted_nome=extraction.primary_nome,
        extracted_cpf=extraction.primary_cpf,
        extracted_birth_date=extraction.primary_birth_date,
        extracted_address=extraction.primary_address,
        extracted_candidates=ranked_candidates,
        match_score=top_score.score if top_score else None,
        match_details=top_score.breakdown if top_score else None,
        run_id=effective_run_id,
        query_type="name",
    )

    if not ranked_candidates:
        findex_email, findex_source = resolve_findex_email(lead)
        if email_consult_fn is not None and findex_email:
            emit("findex_email_sent", email_source=findex_source)
        email_row, email_phones, email_blocked = _run_findex_email_fallback(
            lead=lead,
            lead_ref=lead_ref,
            lead_name=lead_name,
            table_id=table_id,
            store=store,
            email_consult_fn=email_consult_fn,
            email_provider_name=email_provider_name,
            run_id=effective_run_id,
            cooldown_minutes=cooldown_minutes,
            clock=clock,
        )
        if email_row is not None:
            emit(
                "findex_email_parsed",
                phones=len(email_phones),
                blocked_reason=email_blocked,
            )
            if not email_blocked and email_phones:
                emit("completed", phones=len(email_phones))
            else:
                emit("blocked", reason=email_blocked or "email_stage_no_phone")
            return TelegramPhoneFlowResult(
                lead_ref=lead_ref,
                lead_name=lead_name,
                blocked_reason=email_blocked,
                name_consult=name_consult,
                cpf_consult=email_row,
                phone_candidates=email_phones,
                stages=stages,
            )
        bail_reason = (
            f"name_stage_error:{name_result.error}"
            if name_result.error
            else "no_cpf_from_name_stage"
        )
        emit("blocked", reason=bail_reason)
        return TelegramPhoneFlowResult(
            lead_ref=lead_ref,
            lead_name=lead_name,
            blocked_reason=bail_reason,
            name_consult=name_consult,
            cpf_consult=None,
            phone_candidates=[],
            stages=stages,
        )

    candidates = [
        parsed_candidate_from_dict(
            payload, source_provider=name_result.provider, run_id=effective_run_id
        )
        for payload in ranked_candidates
    ]
    eligible = select_followup_candidates(
        candidates, min_score=min_score, max_candidates=max_candidates
    )
    if not eligible:
        findex_email, findex_source = resolve_findex_email(lead)
        if email_consult_fn is not None and findex_email:
            emit("findex_email_sent", email_source=findex_source)
        email_row, email_phones, email_blocked = _run_findex_email_fallback(
            lead=lead,
            lead_ref=lead_ref,
            lead_name=lead_name,
            table_id=table_id,
            store=store,
            email_consult_fn=email_consult_fn,
            email_provider_name=email_provider_name,
            run_id=effective_run_id,
            cooldown_minutes=cooldown_minutes,
            clock=clock,
        )
        if email_row is not None:
            emit(
                "findex_email_parsed",
                phones=len(email_phones),
                blocked_reason=email_blocked,
            )
            if not email_blocked and email_phones:
                emit("completed", phones=len(email_phones))
            else:
                emit("blocked", reason=email_blocked or "email_stage_no_phone")
            return TelegramPhoneFlowResult(
                lead_ref=lead_ref,
                lead_name=lead_name,
                blocked_reason=email_blocked,
                name_consult=name_consult,
                cpf_consult=email_row,
                phone_candidates=email_phones,
                stages=stages,
            )
        cpf_marker = _persist_cpf_stage_summary(
            store=store,
            table_id=table_id,
            lead_ref=lead_ref,
            lead_name=lead_name,
            cpf_provider_name=cpf_provider_name,
            run_id=effective_run_id,
            attempts=[],
            blocked_reason=f"no_eligible_cpf_above_{min_score}",
        )
        emit("blocked", reason="no_eligible_cpf")
        return TelegramPhoneFlowResult(
            lead_ref=lead_ref,
            lead_name=lead_name,
            blocked_reason="no_eligible_cpf",
            name_consult=name_consult,
            cpf_consult=cpf_marker,
            phone_candidates=[],
            stages=stages,
        )

    # 3) CPF stage — one /cpf per eligible CPF, harvest phones from each
    #    raw response, aggregate every attempt into a single summary row.
    emit("gonzales_cpf_sent", cpfs=len(eligible))
    cpf_consult, phone_candidates, overall_blocked = _run_cpf_stage_for_candidates(
        eligible=eligible,
        lead_ref=lead_ref,
        lead_name=lead_name,
        table_id=table_id,
        store=store,
        cpf_consult_fn=cpf_consult_fn,
        cpf_provider_name=cpf_provider_name,
        run_id=effective_run_id,
        cooldown_minutes=cooldown_minutes,
        clock=clock,
    )
    emit(
        "gonzales_cpf_parsed",
        phones=len(phone_candidates),
        blocked_reason=overall_blocked,
    )
    if not overall_blocked and phone_candidates:
        emit("completed", phones=len(phone_candidates))
    else:
        emit("blocked", reason=overall_blocked or "no_phone_from_cpf_stage")

    return TelegramPhoneFlowResult(
        lead_ref=lead_ref,
        lead_name=lead_name,
        blocked_reason=overall_blocked,
        name_consult=name_consult,
        cpf_consult=cpf_consult,
        phone_candidates=phone_candidates,
        stages=stages,
    )


# ---------------------------------------------------------------------------
# Two-step API: extrai CPFs primeiro, devolve para a UI revisar,
# e depois roda /cpf APENAS no subconjunto confirmado pelo operador.
# ---------------------------------------------------------------------------


def run_name_stage_only(
    *,
    lead: Lead,
    table_id: str,
    store: Any,
    name_consult_fn: Callable[[str], TelegramConsultResult],
    target_titles: list[str] | None = None,
    run_id: str | None = None,
    name_provider_name: str = "gon",
    cooldown_minutes: int = TELEGRAM_RATE_LIMIT_COOLDOWN_MINUTES,
    min_score: int = TELEGRAM_FOLLOWUP_MIN_SCORE,
    max_candidates: int = TELEGRAM_FOLLOWUP_MAX_CANDIDATES,
    now: Callable[[], datetime] | None = None,
) -> TelegramNameStageResult:
    """Etapa 1 do fluxo: roda os gates + ``/nome`` + matcher, mas NUNCA
    dispara ``/cpf``.

    Devolve dois conjuntos:

    - ``eligible_candidates``: CPFs já filtrados (score >= min_score,
      sem rejeição de gate de nome, top max_candidates). A UI deve
      pré-marcar todos para o operador clicar "Buscar telefones".
    - ``all_candidates``: tudo que o parser extraiu, com score+breakdown,
      para a UI poder mostrar "ver todos" se o operador quiser
      adicionar manualmente um CPF que o matcher descartou.

    ``blocked_reason`` curto-circuita o lead quando os gates pararam o
    fluxo antes de qualquer tráfego Telegram, ou quando o /nome
    devolveu erro/sem CPFs.
    """
    clock = now or _now_utc
    effective_run_id = run_id or f"flow-{int(clock().timestamp() * 1000)}"
    lead_ref = lead_ref_for(lead) or (lead.person_name or "").strip()
    lead_name = (lead.person_name or "").strip()

    empty = TelegramNameStageResult(
        lead_ref=lead_ref or "(sem-ref)",
        lead_name=lead_name or None,
        blocked_reason=None,
        name_consult=None,
        eligible_candidates=[],
        all_candidates=[],
    )

    if not lead_name or not lead_ref:
        _log_telegram_stage(
            "name_stage_blocked",
            run_id=effective_run_id,
            table_id=table_id,
            lead_ref=lead_ref or None,
            detail={"reason": "lead_sem_nome_ou_ref"},
        )
        return TelegramNameStageResult(
            **{**empty.__dict__, "blocked_reason": "lead_sem_nome_ou_ref"}
        )

    # Se já houver candidatos persistidos de um run anterior, reusa
    # — não vale gastar nova quota com /nome para o mesmo lead. Antes de
    # abrir a seleção, apagamos candidatos cujo nome não bate com o lead
    # (ex.: Julia carregando CPF do Gustavo).
    persisted_all = prune_persisted_name_stage_candidates_for_lead(
        store=store, table_id=table_id, lead=lead, lead_ref=lead_ref
    )
    if persisted_all:
        eligible = select_persisted_name_compatible_candidates(
            persisted_all, max_candidates=max_candidates
        )
        _log_telegram_stage(
            "name_stage_reused_persisted_candidates",
            run_id=effective_run_id,
            table_id=table_id,
            lead_ref=lead_ref,
            detail={
                "candidate_count": len(persisted_all),
                "eligible_count": len(eligible),
            },
        )
        name_consult = latest_name_stage_consult(
            store=store, table_id=table_id, lead_ref=lead_ref
        )
        return TelegramNameStageResult(
            lead_ref=lead_ref,
            lead_name=lead_name,
            blocked_reason=None if eligible else "no_eligible_cpf",
            name_consult=name_consult,
            eligible_candidates=eligible,
            all_candidates=persisted_all,
        )

    # Title gate REMOVIDO: seleção do operador já decide quota. Ver
    # ``run_extract_phone_via_cpf``.

    signals_block = evaluate_linkedin_signals_gate(lead)
    if signals_block is not None:
        _log_telegram_stage(
            "name_stage_blocked",
            run_id=effective_run_id,
            table_id=table_id,
            lead_ref=lead_ref,
            detail={"reason": signals_block},
        )
        marker = store.save_telegram_consult(
            table_id=table_id,
            lead_ref=lead_ref,
            provider=name_provider_name,
            lead_name=lead_name,
            query="",
            raw_text=None,
            source_url=None,
            downloaded_at=None,
            error=signals_block,
            run_id=effective_run_id,
            query_type="name",
            blocked_reason=signals_block,
        )
        return TelegramNameStageResult(
            lead_ref=lead_ref,
            lead_name=lead_name,
            blocked_reason=signals_block,
            name_consult=marker,
            eligible_candidates=[],
            all_candidates=[],
        )

    # Cooldown gate antes do /nome.
    name_state = store.get_telegram_provider_state(name_provider_name)
    if is_provider_in_cooldown(name_state, now=clock()):
        _log_telegram_stage(
            "name_stage_rate_limited",
            run_id=effective_run_id,
            table_id=table_id,
            lead_ref=lead_ref,
            detail={"provider": name_provider_name},
        )
        synth = _synthetic_rate_limited_result(
            provider=name_provider_name,
            lead_name=lead_name,
            cooldown_until=name_state.cooldown_until,
        )
        marker = store.save_telegram_consult(
            table_id=table_id,
            lead_ref=lead_ref,
            provider=name_provider_name,
            lead_name=lead_name,
            query="",
            raw_text=None,
            source_url=None,
            downloaded_at=None,
            error=synth.error,
            run_id=effective_run_id,
            query_type="name",
            blocked_reason="rate_limited",
        )
        return TelegramNameStageResult(
            lead_ref=lead_ref,
            lead_name=lead_name,
            blocked_reason="rate_limited",
            name_consult=marker,
            eligible_candidates=[],
            all_candidates=[],
        )

    try:
        _log_telegram_stage(
            "name_stage_query_sent",
            run_id=effective_run_id,
            table_id=table_id,
            lead_ref=lead_ref,
            detail={"provider": name_provider_name},
        )
        name_result = name_consult_fn(lead_name)
    except Exception as exc:
        logger.exception("Telegram /nome falhou para %s", lead_name)
        name_result = TelegramConsultResult(
            provider=name_provider_name,
            lead_name=lead_name,
            query=f"/nome {lead_name}",
            raw_text=None,
            source_url=None,
            downloaded_at=None,
            error=f"{type(exc).__name__}: {exc}",
        )

    _maybe_apply_cooldown(
        store=store,
        result=name_result,
        cooldown_minutes=cooldown_minutes,
        now=clock(),
    )

    extraction, ranked_candidates, top_score = parse_and_rank(name_result, lead)
    _log_telegram_stage(
        "name_stage_parsed",
        run_id=effective_run_id,
        table_id=table_id,
        lead_ref=lead_ref,
        detail={
            "candidate_count": len(ranked_candidates),
            "top_score": top_score.score if top_score else None,
            "error": bool(name_result.error),
        },
    )
    name_consult = store.save_telegram_consult(
        table_id=table_id,
        lead_ref=lead_ref,
        provider=name_result.provider,
        lead_name=lead_name,
        query=name_result.query,
        raw_text=name_result.raw_text,
        source_url=name_result.source_url,
        downloaded_at=name_result.downloaded_at,
        error=name_result.error,
        extracted_nome=extraction.primary_nome,
        extracted_cpf=extraction.primary_cpf,
        extracted_birth_date=extraction.primary_birth_date,
        extracted_address=extraction.primary_address,
        extracted_candidates=ranked_candidates,
        match_score=top_score.score if top_score else None,
        match_details=top_score.breakdown if top_score else None,
        run_id=effective_run_id,
        query_type="name",
    )

    if not ranked_candidates:
        bail_reason = (
            f"name_stage_error:{name_result.error}"
            if name_result.error
            else "no_cpf_from_name_stage"
        )
        _log_telegram_stage(
            "name_stage_blocked",
            run_id=effective_run_id,
            table_id=table_id,
            lead_ref=lead_ref,
            detail={"reason": bail_reason},
        )
        return TelegramNameStageResult(
            lead_ref=lead_ref,
            lead_name=lead_name,
            blocked_reason=bail_reason,
            name_consult=name_consult,
            eligible_candidates=[],
            all_candidates=[],
        )

    candidates = [
        parsed_candidate_from_dict(
            payload, source_provider=name_result.provider, run_id=effective_run_id
        )
        for payload in ranked_candidates
    ]
    eligible = select_followup_candidates(
        candidates, min_score=min_score, max_candidates=max_candidates
    )
    _log_telegram_stage(
        "name_stage_completed",
        run_id=effective_run_id,
        table_id=table_id,
        lead_ref=lead_ref,
        detail={
            "candidate_count": len(candidates),
            "eligible_count": len(eligible),
            "blocked_reason": None if eligible else "no_eligible_cpf",
        },
    )
    return TelegramNameStageResult(
        lead_ref=lead_ref,
        lead_name=lead_name,
        blocked_reason=None if eligible else "no_eligible_cpf",
        name_consult=name_consult,
        eligible_candidates=eligible,
        all_candidates=candidates,
    )


def run_cpf_stage_only(
    *,
    lead: Lead,
    table_id: str,
    store: Any,
    cpf_consult_fn: Callable[[str], TelegramConsultResult],
    selected_cpfs: list[str],
    run_id: str | None = None,
    cpf_provider_name: str = "gon_cpf",
    cooldown_minutes: int = TELEGRAM_RATE_LIMIT_COOLDOWN_MINUTES,
    now: Callable[[], datetime] | None = None,
) -> TelegramCpfStageResult:
    """Etapa 2 do fluxo: roda ``/cpf`` para a lista de CPFs escolhida
    pela UI.

    ``selected_cpfs`` deve ser um subconjunto dos CPFs que o ``/nome``
    extraiu para esse lead (validado pelo caller). CPFs não encontrados
    nos candidatos persistidos são silenciosamente ignorados — defesa
    contra entradas duplicadas ou stale.
    """
    clock = now or _now_utc
    effective_run_id = run_id or f"flow-{int(clock().timestamp() * 1000)}"
    lead_ref = lead_ref_for(lead) or (lead.person_name or "").strip()
    lead_name = (lead.person_name or "").strip()
    _log_telegram_stage(
        "cpf_stage_started",
        run_id=effective_run_id,
        table_id=table_id,
        lead_ref=lead_ref or None,
        detail={"requested_cpfs": len(selected_cpfs)},
    )

    if not lead_name or not lead_ref:
        _log_telegram_stage(
            "cpf_stage_blocked",
            run_id=effective_run_id,
            table_id=table_id,
            lead_ref=lead_ref or None,
            detail={"reason": "lead_sem_nome_ou_ref"},
        )
        return TelegramCpfStageResult(
            lead_ref=lead_ref or "(sem-ref)",
            lead_name=lead_name or None,
            blocked_reason="lead_sem_nome_ou_ref",
            cpf_consult=None,
            phone_candidates=[],
        )

    persisted = collect_name_stage_candidates(
        store=store, table_id=table_id, lead_ref=lead_ref
    )
    by_cpf = {candidate.cpf: candidate for candidate in persisted}
    chosen: list[TelegramParsedCandidate] = []
    for raw_cpf in selected_cpfs:
        key = (raw_cpf or "").strip()
        if key in by_cpf:
            chosen.append(by_cpf[key])

    if not chosen:
        _log_telegram_stage(
            "cpf_stage_blocked",
            run_id=effective_run_id,
            table_id=table_id,
            lead_ref=lead_ref,
            detail={"reason": "no_cpf_selected"},
        )
        return TelegramCpfStageResult(
            lead_ref=lead_ref,
            lead_name=lead_name,
            blocked_reason="no_cpf_selected",
            cpf_consult=None,
            phone_candidates=[],
        )

    cpf_consult, phone_candidates, blocked = _run_cpf_stage_for_candidates(
        eligible=chosen,
        lead_ref=lead_ref,
        lead_name=lead_name,
        table_id=table_id,
        store=store,
        cpf_consult_fn=cpf_consult_fn,
        cpf_provider_name=cpf_provider_name,
        run_id=effective_run_id,
        cooldown_minutes=cooldown_minutes,
        clock=clock,
    )
    _log_telegram_stage(
        "cpf_stage_completed",
        run_id=effective_run_id,
        table_id=table_id,
        lead_ref=lead_ref,
        detail={
            "chosen_cpfs": len(chosen),
            "phones": len(phone_candidates),
            "blocked_reason": blocked,
        },
    )
    return TelegramCpfStageResult(
        lead_ref=lead_ref,
        lead_name=lead_name,
        blocked_reason=blocked,
        cpf_consult=cpf_consult,
        phone_candidates=phone_candidates,
    )
