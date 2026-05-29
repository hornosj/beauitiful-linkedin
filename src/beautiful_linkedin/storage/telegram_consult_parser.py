"""Parse Telegram-group consult raw text into structured candidates.

Both providers (Gon HTML scrape and Unix .txt download) ultimately
produce free-form text where the same person's record is rendered as
labeled lines: "Nome:", "CPF:", "Nascimento:" / "Data de Nascimento:",
"Endereço:" / "Logradouro:". A single result page can carry MULTIPLE
matches (when the name maps to several CPFs), so the parser emits a
list of :class:`TelegramCandidate` and also exposes a "primary"
extraction with the first candidate's fields for the UI's quick view.

This module is intentionally regex-only — no LLM, no HTML parsing. The
upstream pages already give us human-readable text, and any structural
HTML differences between providers are erased by the time we get the
``raw_text``. If a provider switches output format the regex set is the
only place to adjust.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any


logger = logging.getLogger(__name__)


# ---- regex catalog ---------------------------------------------------------
#
# Patterns are intentionally lenient: the upstream providers wrap labels
# in different ways (".", ":", "→", emojis) and may interleave HTML
# leftovers (``\xa0`` non-breaking spaces, etc.). We normalize before
# matching and accept whatever surrounding noise the page hands us.

_CPF_FORMATTED = re.compile(r"\b(\d{3}\.\d{3}\.\d{3}-\d{2})\b")
_CPF_DIGITS = re.compile(r"(?<!\d)(\d{11})(?!\d)")
_DATE_BR = re.compile(r"\b(\d{2})/(\d{2})/(\d{4})\b")
_DATE_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")

# Label-based capture. Each pattern is anchored to a known label and
# captures the rest of the line. The set is unioned per-field so any
# variant works without re-writing the parser.
_NAME_LABELS = (
    r"nome\s*completo",
    r"nome",
)
_BIRTH_LABELS = (
    r"data\s*de\s*nascimento",
    r"nascimento",
    r"nasc\.?",
    r"data\s*nasc\.?",
    r"dt\.?\s*nasc\.?",
)
_ADDRESS_LABELS = (
    r"endere[çc]o\s*completo",
    r"endere[çc]o",
    r"logradouro",
)


def _build_label_pattern(label_variants: tuple[str, ...]) -> re.Pattern[str]:
    """Compile a regex that matches any of ``label_variants`` followed
    by ``:``, ``→``, whitespace, or Unix-style dot leaders
    (``NOME....................:``), then captures the rest of the
    line. Case-insensitive so Telegram's casing quirks don't break us."""
    joined = "|".join(label_variants)
    pattern = (
        rf"(?im)^\s*(?:[-•*]\s*)?(?:{joined})(?!\s+d[ae]\s+m[aã]e)\s*"
        rf"(?:\.{{2,}}\s*:|[:\-–→]|\s+)\s*(.+?)\s*$"
    )
    return re.compile(pattern)


_NAME_RE = _build_label_pattern(_NAME_LABELS)
_BIRTH_RE = _build_label_pattern(_BIRTH_LABELS)
_ADDRESS_RE = _build_label_pattern(_ADDRESS_LABELS)


@dataclass
class TelegramCandidate:
    """One CPF candidate parsed out of the raw text."""

    cpf: str
    nome: str | None = None
    data_nascimento: str | None = None  # canonical 'DD/MM/YYYY'
    endereco: str | None = None
    raw_block: str | None = None  # the chunk we extracted this candidate from

    def to_dict(self) -> dict[str, Any]:
        return {
            "cpf": self.cpf,
            "nome": self.nome,
            "data_nascimento": self.data_nascimento,
            "endereco": self.endereco,
        }


@dataclass
class TelegramExtraction:
    """Aggregated parse output for one raw consult text.

    ``primary_*`` mirror the first candidate so the UI can show one
    quick line per consult without unpacking ``candidates``. When the
    text has no CPF at all, ``candidates`` is empty and the ``primary_*``
    fields are still populated from any standalone Nome/Nascimento/etc.
    so the operator at least sees what the parser found.
    """

    candidates: list[TelegramCandidate] = field(default_factory=list)
    primary_nome: str | None = None
    primary_cpf: str | None = None
    primary_birth_date: str | None = None
    primary_address: str | None = None


def parse_telegram_text(raw_text: str | None, *, provider: str) -> TelegramExtraction:
    """Best-effort regex parser. Same shape for Gon and Unix — they
    both emit labeled lines once the binary/HTML wrapper is stripped.

    ``provider`` is accepted today only for logging and future format
    branching; the current heuristics are shared. When a provider's
    output diverges enough to warrant a dedicated parser, branch here.
    """
    if not raw_text or not raw_text.strip():
        return TelegramExtraction()

    normalized = _normalize_for_parse(raw_text)
    candidates = _extract_candidates(normalized, provider=provider)

    fallback_nome, fallback_nome_source = _first_name_match(normalized)
    primary_nome = candidates[0].nome if candidates else fallback_nome
    primary_birth = (
        candidates[0].data_nascimento
        if candidates
        else _canonicalize_date(_first_match(_BIRTH_RE, normalized))
    )
    primary_address = (
        candidates[0].endereco if candidates else _first_match(_ADDRESS_RE, normalized)
    )
    primary_cpf = candidates[0].cpf if candidates else None

    logger.debug(
        "Telegram parse (%s): %s candidate(s); primary_cpf=%s primary_name_source=%s",
        provider,
        len(candidates),
        primary_cpf,
        "candidate" if candidates and candidates[0].nome else fallback_nome_source,
    )

    return TelegramExtraction(
        candidates=candidates,
        primary_nome=primary_nome,
        primary_cpf=primary_cpf,
        primary_birth_date=primary_birth,
        primary_address=primary_address,
    )


# ---- internals ------------------------------------------------------------


def _normalize_for_parse(text: str) -> str:
    """Collapse whitespace variants and strip HTML leftovers without
    losing line structure (we anchor regexes on ``^...$``)."""
    # Replace non-breaking spaces and stray tabs with ordinary spaces;
    # leave newlines intact.
    cleaned = (
        text.replace(" ", " ")
        .replace(" ", " ")
        .replace("\t", " ")
    )
    # Trim trailing spaces per line so end-of-line anchors don't miss.
    cleaned = "\n".join(line.rstrip() for line in cleaned.splitlines())
    return cleaned


def _extract_candidates(text: str, *, provider: str) -> list[TelegramCandidate]:
    """Locate every CPF in the text and attribute its surrounding
    Nome/Nascimento/Endereço from the SAME record block.

    Strategy: split the text into "blocks" delimited by the next
    appearance of a Nome label (consult outputs repeat that header
    once per record). Each block then carries at most one Nome and
    ideally one CPF. Blocks without a CPF are dropped; blocks with
    multiple CPFs spawn multiple candidates that share the block's
    metadata. This avoids the older window approach silently sticking
    record #1's name onto record #2's CPF when blocks overlap.
    """
    blocks = _split_into_record_blocks(text)
    candidates: list[TelegramCandidate] = []
    seen_cpfs: set[str] = set()
    for block_index, block in enumerate(blocks):
        block_cpfs = _find_cpf_positions(block)
        if not block_cpfs:
            continue
        nome, nome_source = _first_name_match(block)
        birth = _canonicalize_date(_first_match(_BIRTH_RE, block))
        endereco = _first_match(_ADDRESS_RE, block)
        for raw_cpf, _pos in block_cpfs:
            canonical = _canonical_cpf(raw_cpf)
            if canonical in seen_cpfs:
                continue
            seen_cpfs.add(canonical)
            logger.debug(
                "Telegram parse candidate provider=%s block=%d name_source=%s "
                "has_birth=%s has_address=%s raw_block_chars=%d",
                provider,
                block_index,
                nome_source,
                bool(birth),
                bool(endereco),
                len(block),
            )
            candidates.append(
                TelegramCandidate(
                    cpf=_format_cpf(canonical),
                    nome=nome,
                    data_nascimento=birth,
                    endereco=endereco,
                    raw_block=block,
                )
            )
    return candidates


def _split_into_record_blocks(text: str) -> list[str]:
    """Cut the raw text at every Nome label, returning one chunk per
    record. The chunk INCLUDES its leading Nome line so the regex still
    matches it. Text before the first Nome label is also returned so a
    CPF-only block (no Nome label) still produces a candidate."""
    lines = text.splitlines()
    name_label_re = re.compile(
        rf"(?i)^\s*(?:[-•*]\s*)?(?:{'|'.join(_NAME_LABELS)})\s*[:\-–→]"
    )
    block_starts: list[int] = []
    for idx, line in enumerate(lines):
        if name_label_re.match(line) or _NAME_RE.match(line):
            block_starts.append(idx)
    if not block_starts:
        # No labeled records — treat the whole text as a single block.
        return [text]

    blocks: list[str] = []
    # Preserve any preamble before the first Nome label so leading
    # CPF-only records (rare, but possible) still get parsed.
    if block_starts[0] > 0:
        preamble = "\n".join(lines[: block_starts[0]])
        if preamble.strip():
            blocks.append(preamble)
    for i, start in enumerate(block_starts):
        end = block_starts[i + 1] if i + 1 < len(block_starts) else len(lines)
        blocks.append("\n".join(lines[start:end]))
    return blocks


def _find_cpf_positions(text: str) -> list[tuple[str, int]]:
    """Return ``[(matched_cpf, char_offset)]`` preserving document order
    and de-duplicated by canonical form (digits only)."""
    seen: set[str] = set()
    out: list[tuple[str, int]] = []
    # Match formatted CPFs first — they're the more reliable signal.
    for m in _CPF_FORMATTED.finditer(text):
        digits = _canonical_cpf(m.group(1))
        if digits in seen or not _looks_like_cpf(digits):
            continue
        seen.add(digits)
        out.append((m.group(1), m.start(1)))
    for m in _CPF_DIGITS.finditer(text):
        digits = m.group(1)
        if digits in seen or not _looks_like_cpf(digits):
            continue
        seen.add(digits)
        out.append((digits, m.start(1)))
    out.sort(key=lambda pair: pair[1])
    return out


def _looks_like_cpf(digits: str) -> bool:
    """Lightweight CPF sanity check: 11 digits, not all the same. We
    intentionally do NOT run the full check-digit validation — the
    sources occasionally emit valid-but-test CPFs and we'd rather show
    the candidate than silently drop it.
    """
    if len(digits) != 11 or not digits.isdigit():
        return False
    return len(set(digits)) > 1


def _canonical_cpf(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def _format_cpf(digits: str) -> str:
    if len(digits) != 11:
        return digits
    return f"{digits[0:3]}.{digits[3:6]}.{digits[6:9]}-{digits[9:11]}"


def _canonicalize_date(value: str | None) -> str | None:
    """Force any matched date to ``DD/MM/YYYY``. Returns ``None`` when
    the match doesn't look like a real Brazilian date (impossible
    month/day or 2-digit year)."""
    if not value:
        return None
    m = _DATE_BR.search(value)
    if m:
        day, month, year = m.group(1), m.group(2), m.group(3)
    else:
        iso = _DATE_ISO.search(value)
        if not iso:
            return None
        year, month, day = iso.group(1), iso.group(2), iso.group(3)
    try:
        d, mo, y = int(day), int(month), int(year)
    except ValueError:
        return None
    if not (1 <= d <= 31 and 1 <= mo <= 12 and 1900 <= y <= 2100):
        return None
    return f"{day}/{month}/{year}"


def _first_match(pattern: re.Pattern[str], text: str) -> str | None:
    m = pattern.search(text)
    if not m:
        return None
    return _clean_label_value(m.group(1))


_NAME_METADATA_VALUE_RE = re.compile(
    r"(?i)^(?:consultad[oa]|pesquisad[oa]|buscad[oa]|informad[oa])\b"
)


def _first_name_match(text: str) -> tuple[str | None, str]:
    value = _first_match(_NAME_RE, text)
    if not value:
        return None, "missing"
    if _NAME_METADATA_VALUE_RE.match(value):
        return None, "metadata_ignored"
    return value, "label"


def _clean_label_value(value: str) -> str:
    """Trim noise that often trails a labeled value: trailing emojis,
    dangling separators, double spaces."""
    cleaned = unicodedata.normalize("NFKC", value).strip()
    # Drop trailing separators and bullet leftovers.
    cleaned = re.sub(r"[•\-–—·•|]+\s*$", "", cleaned).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or value.strip()
