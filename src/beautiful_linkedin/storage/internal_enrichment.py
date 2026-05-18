"""In-process e-mail enrichment — no paid APIs.

This is the first layer of the enrichment stack: given a lead's full name
plus the company domain, generate the common work-email patterns, validate
them technically (format + MX), and return the best candidate with a
confidence score and provenance metadata.

The service is intentionally conservative:

- It never overwrites an existing ``lead.email``.
- It rejects personal-mail domains (gmail, hotmail, outlook, ...) as work
  e-mails — they may belong to the person, but they're not a "work"
  address and shouldn't be persisted as one.
- It always records *why* it picked a candidate (the pattern name and
  whether that pattern matches the company's detected convention).

External I/O (DNS MX lookup) is injected via ``mx_resolver`` so tests run
offline and stay deterministic.
"""

from __future__ import annotations

import re
import smtplib
import threading
import unicodedata
import uuid
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol

from beautiful_linkedin.models import Lead


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class EnrichmentPattern(str, Enum):
    FIRST = "first"  # ana
    LAST = "last"  # silva
    FIRST_DOT_LAST = "first.last"  # ana.silva
    FIRST_DOT_MIDDLE_DOT_LAST = "first.middle.last"  # ana.maria.silva
    FIRST_UNDERSCORE_MIDDLE_DOT_LAST = "first_middle.last"  # ana_maria.silva
    FIRST_INITIAL_LAST = "flast"  # asilva
    FIRST_DOT_LAST_INITIAL = "first.l"  # ana.s
    FIRST_INITIAL_MIDDLE = "fmiddle"  # amaria


class EnrichmentStatus(str, Enum):
    NOT_ENRICHED = "not_enriched"
    ESTIMATED = "estimated"
    ENRICHED = "enriched"
    PARTIAL = "partial"
    FAILED = "failed"


class EmailValidationStatus(str, Enum):
    VALID = "valid"
    PROBABLE = "probable"
    RISKY = "risky"
    UNKNOWN = "unknown"


PERSONAL_EMAIL_DOMAINS: frozenset[str] = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "hotmail.com",
        "hotmail.com.br",
        "outlook.com",
        "outlook.com.br",
        "live.com",
        "msn.com",
        "yahoo.com",
        "yahoo.com.br",
        "icloud.com",
        "me.com",
        "uol.com.br",
        "bol.com.br",
        "terra.com.br",
        "ig.com.br",
        "globo.com",
        "protonmail.com",
        "proton.me",
    }
)


# Loose RFC-5322-ish format check. Internal enrichment values come from
# our own generator, so we don't need full RFC compliance — we just want
# to reject "obviously broken" strings.
_EMAIL_REGEX = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EmailCandidate:
    local_part: str
    domain: str
    pattern: EnrichmentPattern

    @property
    def email(self) -> str:
        return f"{self.local_part}@{self.domain}"


@dataclass(frozen=True)
class ValidationResult:
    status: EmailValidationStatus
    has_mx: bool
    reason: str = ""
    mailbox_checked: bool = False
    is_catch_all: bool = False


@dataclass(frozen=True)
class MailboxVerificationResult:
    status: EmailValidationStatus
    reason: str
    is_catch_all: bool = False


class CompanyDomainResolver:
    """Resolve a probable company domain for a lead.

    Order of preference:
    1. ``lead.company_domain`` (the cleanest signal).
    2. ``search_request["company_domain"]`` (the user filled it in).
    3. The table-level fallback domain, if provided.
    """

    def __init__(
        self,
        *,
        search_request: dict | None = None,
        table_domain: str | None = None,
    ) -> None:
        self._search_request = search_request or {}
        self._table_domain = (table_domain or "").strip().lower() or None

    def resolve(self, lead: Lead) -> str | None:
        if lead.company_domain and lead.company_domain.strip():
            return lead.company_domain.strip().lower()
        req_domain = self._search_request.get("company_domain")
        if isinstance(req_domain, str) and req_domain.strip():
            return req_domain.strip().lower()
        return self._table_domain


@dataclass
class EnrichmentUpdate:
    """Outcome of trying to enrich a single lead."""

    email: str | None = None
    enrichment_source: str = "internal"
    enrichment_status: EnrichmentStatus = EnrichmentStatus.NOT_ENRICHED
    enrichment_confidence: int = 0
    email_type: str = "unknown"  # work | personal | unknown
    email_validation_status: EmailValidationStatus = EmailValidationStatus.UNKNOWN
    matched_pattern: EnrichmentPattern | None = None
    company_pattern: EnrichmentPattern | None = None
    skipped_existing_email: bool = False
    failure_reason: str | None = None
    discarded_candidates: list[str] = field(default_factory=list)
    # Which domain the winning candidate came from, plus the full list of
    # domains the service attempted before giving up. Surfacing both lets
    # the UI explain "tried acme.com and acme.io; accepted acme.io" and
    # helps debugging when a lead lands on the wrong domain.
    chosen_domain: str | None = None
    tested_domains: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Email pattern generator
# ---------------------------------------------------------------------------


class EmailPatternGenerator:
    """Generate plausible local-parts for a person + domain combination."""

    def generate(
        self, full_name: str | None, domain: str | None
    ) -> list[EmailCandidate]:
        domain = (domain or "").strip().lower()
        full_name = (full_name or "").strip()
        if not domain or not full_name:
            return []

        parts = self._split_name(full_name)
        if not parts:
            return []

        first = parts[0]
        last = parts[-1] if len(parts) > 1 else ""
        middle = parts[1] if len(parts) >= 3 else ""

        candidates: list[EmailCandidate] = []
        seen: set[str] = set()

        def add(local: str, pattern: EnrichmentPattern) -> None:
            if not local or local in seen:
                return
            seen.add(local)
            candidates.append(
                EmailCandidate(local_part=local, domain=domain, pattern=pattern)
            )

        add(first, EnrichmentPattern.FIRST)
        if last:
            add(f"{first}.{last}", EnrichmentPattern.FIRST_DOT_LAST)
            add(f"{first[0]}{last}", EnrichmentPattern.FIRST_INITIAL_LAST)
            add(f"{first}.{last[0]}", EnrichmentPattern.FIRST_DOT_LAST_INITIAL)
            add(last, EnrichmentPattern.LAST)
        if middle and last:
            add(
                f"{first}.{middle}.{last}",
                EnrichmentPattern.FIRST_DOT_MIDDLE_DOT_LAST,
            )
            add(
                f"{first}_{middle}.{last}",
                EnrichmentPattern.FIRST_UNDERSCORE_MIDDLE_DOT_LAST,
            )
            add(f"{first[0]}{middle}", EnrichmentPattern.FIRST_INITIAL_MIDDLE)

        return candidates

    @staticmethod
    def _split_name(full_name: str) -> list[str]:
        stripped = unicodedata.normalize("NFD", full_name)
        ascii_text = "".join(ch for ch in stripped if not unicodedata.combining(ch))
        cleaned = re.sub(r"[^A-Za-z0-9 \-]+", " ", ascii_text)
        tokens = [t.lower() for t in cleaned.split() if t.strip()]
        return tokens


# ---------------------------------------------------------------------------
# Company pattern detection
# ---------------------------------------------------------------------------


def detect_company_pattern(
    samples: list[tuple[str, str]],
    domain: str,
) -> EnrichmentPattern | None:
    """Infer the predominant e-mail pattern from existing same-company mails.

    Args:
        samples: ``(full_name, email)`` pairs.
        domain: company domain (samples from other domains are ignored).

    Returns the most common ``EnrichmentPattern`` matched across samples,
    or ``None`` when there's no clear majority or no samples on-domain.
    """
    target_domain = (domain or "").strip().lower()
    if not target_domain:
        return None

    matches: list[EnrichmentPattern] = []
    for full_name, email in samples:
        if "@" not in email:
            continue
        local, email_domain = email.lower().rsplit("@", 1)
        if email_domain.strip() != target_domain:
            continue
        pattern = _which_pattern(full_name, local)
        if pattern is not None:
            matches.append(pattern)

    if not matches:
        return None
    counts = Counter(matches)
    most_common, _ = counts.most_common(1)[0]
    return most_common


def _which_pattern(full_name: str, local_part: str) -> EnrichmentPattern | None:
    candidates = EmailPatternGenerator().generate(full_name, "x.com")
    for candidate in candidates:
        if candidate.local_part == local_part:
            return candidate.pattern
    return None


# Generic role-style mailboxes that are NOT people. These shouldn't
# drive pattern detection: ``contato@empresa.com`` doesn't tell us
# anything about how the company names individual employees.
_ROLE_ALIAS_LOCALS: frozenset[str] = frozenset(
    {
        "contato",
        "contact",
        "vendas",
        "sales",
        "support",
        "suporte",
        "atendimento",
        "ola",
        "hello",
        "hi",
        "info",
        "marketing",
        "press",
        "imprensa",
        "rh",
        "hr",
        "jobs",
        "vagas",
        "trabalhe-conosco",
        "no-reply",
        "noreply",
        "no_reply",
        "admin",
        "team",
        "equipe",
        "ouvidoria",
        "financeiro",
        "billing",
        "abuse",
        "privacy",
        "privacidade",
    }
)


def infer_pattern_from_locals(local_parts: list[str]) -> EnrichmentPattern | None:
    """Classify a list of local-parts by **shape** and return the majority.

    Unlike :func:`detect_company_pattern`, this works WITHOUT knowing each
    e-mail's owner — we just look at how the local-part is built. The
    intuition: if 4 out of 5 emails harvested from a company website look
    like ``letters.letters``, the company's convention is almost certainly
    ``first.last`` even if we can't pair each address with a name.
    """
    if not local_parts:
        return None

    votes: Counter[EnrichmentPattern] = Counter()
    for raw in local_parts:
        local = (raw or "").strip().lower()
        if not local or local in _ROLE_ALIAS_LOCALS:
            continue
        shape = _classify_local_shape(local)
        if shape is not None:
            votes[shape] += 1

    if not votes:
        return None
    most_common, _ = votes.most_common(1)[0]
    return most_common


def _classify_local_shape(local: str) -> EnrichmentPattern | None:
    """Map a single local-part to its most likely structural pattern.

    Examples:
        ``"ana.silva"`` → FIRST_DOT_LAST
        ``"ana.maria.silva"`` → FIRST_DOT_MIDDLE_DOT_LAST
        ``"asilva"`` → FIRST_INITIAL_LAST
        ``"ana"`` → FIRST
    """
    # Strip plus-addressing (rare on people emails but be safe).
    local = local.split("+", 1)[0]

    if "_" in local and "." in local:
        return EnrichmentPattern.FIRST_UNDERSCORE_MIDDLE_DOT_LAST

    parts = local.split(".")
    if len(parts) == 3 and all(p.isalpha() and len(p) >= 2 for p in parts):
        return EnrichmentPattern.FIRST_DOT_MIDDLE_DOT_LAST
    if len(parts) == 2:
        first, second = parts
        if not first.isalpha() or not second.isalpha():
            return None
        if len(first) == 1:
            # 'a.silva' shape — uncommon, treat as first.last for now.
            return EnrichmentPattern.FIRST_DOT_LAST
        if len(second) == 1:
            return EnrichmentPattern.FIRST_DOT_LAST_INITIAL
        return EnrichmentPattern.FIRST_DOT_LAST
    if len(parts) == 1 and local.isalpha():
        # Either a single given-name (FIRST) or initial+lastname (FLAST).
        # Heuristic: very short locals (≤ 5 chars like "ana", "bruno") are
        # almost always single given names. Six or more chars are usually
        # initial+lastname patterns ("asilva", "bcosta").
        if len(local) <= 5:
            return EnrichmentPattern.FIRST
        return EnrichmentPattern.FIRST_INITIAL_LAST
    return None


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


MxResolver = Callable[[str], bool]
MailboxVerifier = Callable[[str], MailboxVerificationResult]


class EmailValidator:
    """Format + domain + MX + optional mailbox validation.

    MX proves only that the domain receives mail. The optional mailbox
    verifier can add SMTP-style evidence that a specific recipient is
    accepted, rejected, or hidden behind catch-all behavior.
    """

    def __init__(
        self,
        *,
        mx_resolver: MxResolver,
        mailbox_verifier: MailboxVerifier | None = None,
    ) -> None:
        self._mx_resolver = mx_resolver
        self._mailbox_verifier = mailbox_verifier

    def validate(self, email: str) -> ValidationResult:
        if not email or "@" not in email or not _EMAIL_REGEX.match(email):
            return ValidationResult(
                status=EmailValidationStatus.UNKNOWN, has_mx=False, reason="format"
            )
        domain = email.split("@", 1)[1].lower()
        if domain in PERSONAL_EMAIL_DOMAINS:
            return ValidationResult(
                status=EmailValidationStatus.RISKY,
                has_mx=False,
                reason="personal_domain",
            )
        has_mx = bool(self._mx_resolver(domain))
        if not has_mx:
            return ValidationResult(
                status=EmailValidationStatus.RISKY, has_mx=False, reason="no_mx"
            )
        if self._mailbox_verifier is not None:
            mailbox = self._mailbox_verifier(email)
            if mailbox.status == EmailValidationStatus.VALID:
                return ValidationResult(
                    status=EmailValidationStatus.VALID,
                    has_mx=True,
                    reason=mailbox.reason,
                    mailbox_checked=True,
                    is_catch_all=mailbox.is_catch_all,
                )
            if mailbox.status == EmailValidationStatus.RISKY:
                return ValidationResult(
                    status=EmailValidationStatus.RISKY,
                    has_mx=True,
                    reason=mailbox.reason,
                    mailbox_checked=True,
                    is_catch_all=mailbox.is_catch_all,
                )
            # Catch-all, greylisting, timeouts, and blocked SMTP probes are
            # useful risk signals, but not proof that the pattern is wrong.
            return ValidationResult(
                status=EmailValidationStatus.PROBABLE,
                has_mx=True,
                reason=mailbox.reason,
                mailbox_checked=True,
                is_catch_all=mailbox.is_catch_all,
            )
        return ValidationResult(
            status=EmailValidationStatus.PROBABLE, has_mx=True, reason="mx_ok"
        )

    def validate_candidate(
        self,
        candidate: EmailCandidate,
        *,
        matches_detected_pattern: bool,
        is_published_email: bool = False,
    ) -> ValidationResult:
        base = self.validate(candidate.email)
        if (
            is_published_email
            and base.has_mx
            and base.status in {EmailValidationStatus.VALID, EmailValidationStatus.PROBABLE}
        ):
            return ValidationResult(
                status=EmailValidationStatus.VALID,
                has_mx=True,
                reason="published_on_company_site",
                mailbox_checked=base.mailbox_checked,
                is_catch_all=base.is_catch_all,
            )
        if (
            matches_detected_pattern
            and base.status == EmailValidationStatus.PROBABLE
            and not base.mailbox_checked
        ):
            return ValidationResult(
                status=EmailValidationStatus.PROBABLE,
                has_mx=True,
                reason="matches_company_pattern",
            )
        return base


MxHostsResolver = Callable[[str], list[str]]
SmtpFactory = Callable[[str, float], Any]
RandomLocalFactory = Callable[[], str]

_SMTP_ACCEPT_CODES = {250, 251, 252}
_SMTP_REJECT_CODES = {500, 501, 503, 550, 551, 552, 553, 554}


@dataclass(frozen=True)
class _DomainCacheEntry:
    """Per-domain facts learned during an earlier SMTP probe.

    ``kind`` encodes what we know:

    - ``"catch_all"``: random local-parts get 250, so any candidate at
      this domain returns ``PROBABLE / catch_all`` without a roundtrip.
    - ``"no_mx"``: no MX records resolve → permanently UNKNOWN.
    - ``"unreachable"``: every MX host refused our connection during the
      run → UNKNOWN with the original error reason.
    - ``"strict"``: random probe was rejected, meaning the server
      enforces real mailbox names. We don't short-circuit candidate
      verification here — every new local-part still needs its own
      RCPT — but we keep the entry around for diagnostics/observability.
    """

    kind: str
    reason: str

    def materialize_for(self, email: str) -> MailboxVerificationResult | None:
        if self.kind == "catch_all":
            return MailboxVerificationResult(
                status=EmailValidationStatus.PROBABLE,
                reason="smtp_catch_all_cached",
                is_catch_all=True,
            )
        if self.kind == "no_mx":
            return MailboxVerificationResult(
                status=EmailValidationStatus.UNKNOWN,
                reason="no_mx_host",
            )
        if self.kind == "unreachable":
            return MailboxVerificationResult(
                status=EmailValidationStatus.UNKNOWN,
                reason=self.reason,
            )
        # "strict" and any future kinds: fall through to a real probe.
        return None


class SmtpMailboxVerifier:
    """Validate a specific mailbox with a conservative SMTP RCPT probe.

    This never sends message content: it connects to an MX host, performs
    ``MAIL FROM`` + ``RCPT TO`` for the candidate, and then probes a random
    local-part at the same domain to detect catch-all behavior.

    Two pieces of state survive across calls within a single verifier
    instance:

    - ``_cache``: per-email memoization (covers literal repeats).
    - ``_domain_cache``: per-domain memoization of facts that don't
      depend on the local-part — catch-all behavior, "no MX" verdicts,
      and "MX unreachable" so we don't reconnect for every colleague.
    - ``_host_semaphores``: per-MX-host concurrency cap so we never open
      more than ``max_per_host`` simultaneous SMTP conversations to the
      same server (Gmail/Outlook actively rate-limit, and a single noisy
      bulk run can get the source IP greylisted for hours).
    """

    def __init__(
        self,
        *,
        mx_hosts_resolver: MxHostsResolver,
        timeout_seconds: float = 4.0,
        max_hosts: int = 2,
        max_per_host: int = 2,
        sender: str = "probe@beautiful-linkedin.invalid",
        local_hostname: str = "beautiful-linkedin.local",
        smtp_factory: SmtpFactory | None = None,
        random_local_factory: RandomLocalFactory | None = None,
    ) -> None:
        self._mx_hosts_resolver = mx_hosts_resolver
        self._timeout_seconds = timeout_seconds
        self._max_hosts = max(1, max_hosts)
        self._max_per_host = max(1, max_per_host)
        self._sender = sender
        # A real FQDN on EHLO is needed — many MX servers (Gmail, Outlook,
        # corporate Postfix) reject the conversation entirely when HELO
        # comes from a bare "localhost".
        self._local_hostname = local_hostname
        self._smtp_factory = smtp_factory or self._default_smtp_factory
        self._random_local_factory = random_local_factory or self._random_local
        self._cache: dict[str, MailboxVerificationResult] = {}
        self._domain_cache: dict[str, _DomainCacheEntry] = {}
        self._host_semaphores: dict[str, threading.Semaphore] = {}
        self._lock = threading.Lock()

    def __call__(self, email: str) -> MailboxVerificationResult:
        return self.verify(email)

    def verify(self, email: str) -> MailboxVerificationResult:
        cleaned = (email or "").strip().lower()
        with self._lock:
            cached = self._cache.get(cleaned)
        if cached is not None:
            return cached

        # Short-circuit using anything we already learned about this
        # domain — catch-all status and dead MX are properties of the
        # domain, not of any specific mailbox.
        domain = cleaned.rsplit("@", 1)[1] if "@" in cleaned else ""
        domain_cached = self._domain_cache_get(domain) if domain else None
        if domain_cached is not None:
            short_circuit = domain_cached.materialize_for(cleaned)
            if short_circuit is not None:
                with self._lock:
                    self._cache[cleaned] = short_circuit
                return short_circuit
            # Fell through (e.g. "strict" domain): a real probe is needed
            # for this specific mailbox.

        result = self._verify_uncached(cleaned)
        with self._lock:
            self._cache[cleaned] = result
        return result

    def _verify_uncached(self, email: str) -> MailboxVerificationResult:
        if not email or "@" not in email:
            return MailboxVerificationResult(
                status=EmailValidationStatus.UNKNOWN,
                reason="format",
            )
        _, domain = email.rsplit("@", 1)
        hosts = self._safe_hosts(domain)
        if not hosts:
            self._domain_cache_set(
                domain, _DomainCacheEntry(kind="no_mx", reason="no_mx_host")
            )
            return MailboxVerificationResult(
                status=EmailValidationStatus.UNKNOWN,
                reason="no_mx_host",
            )

        random_email = f"{self._random_local_factory()}@{domain}"
        last_reason = "smtp_unavailable"
        for host in hosts[: self._max_hosts]:
            sem = self._semaphore_for(host)
            sem.acquire()
            try:
                smtp = self._smtp_factory(host, self._timeout_seconds)
                try:
                    self._hello(smtp)
                    candidate_code = self._rcpt_code(smtp, email)
                    if candidate_code in _SMTP_REJECT_CODES:
                        # A reject for one mailbox doesn't generalize — the
                        # next mailbox at the same domain may exist.
                        return MailboxVerificationResult(
                            status=EmailValidationStatus.RISKY,
                            reason="smtp_rejected",
                        )
                    if candidate_code not in _SMTP_ACCEPT_CODES:
                        last_reason = f"smtp_unknown_{candidate_code}"
                        continue

                    random_code = self._rcpt_code(smtp, random_email)
                    if random_code in _SMTP_ACCEPT_CODES:
                        # Catch-all is a domain-wide property. Cache it so
                        # the next colleague at the same domain skips the
                        # SMTP roundtrip entirely.
                        self._domain_cache_set(
                            domain,
                            _DomainCacheEntry(
                                kind="catch_all", reason="smtp_catch_all"
                            ),
                        )
                        return MailboxVerificationResult(
                            status=EmailValidationStatus.PROBABLE,
                            reason="smtp_catch_all",
                            is_catch_all=True,
                        )
                    if random_code in _SMTP_REJECT_CODES:
                        # Strong domain-level signal: the server enforces
                        # real mailbox names. We can't cache "every mail
                        # is valid", but we know the random probe will
                        # always reject — speeds up future verifications.
                        self._domain_cache_set(
                            domain,
                            _DomainCacheEntry(
                                kind="strict",
                                reason="smtp_random_rejected",
                            ),
                        )
                        return MailboxVerificationResult(
                            status=EmailValidationStatus.VALID,
                            reason="smtp_valid",
                        )
                    return MailboxVerificationResult(
                        status=EmailValidationStatus.PROBABLE,
                        reason=f"smtp_accepted_random_unknown_{random_code}",
                    )
                finally:
                    self._close(smtp)
            except Exception as exc:
                last_reason = f"smtp_error:{exc.__class__.__name__}"
                continue
            finally:
                sem.release()

        self._domain_cache_set(
            domain,
            _DomainCacheEntry(kind="unreachable", reason=last_reason),
        )
        return MailboxVerificationResult(
            status=EmailValidationStatus.UNKNOWN,
            reason=last_reason,
        )

    def _semaphore_for(self, host: str) -> threading.Semaphore:
        with self._lock:
            sem = self._host_semaphores.get(host)
            if sem is None:
                sem = threading.Semaphore(self._max_per_host)
                self._host_semaphores[host] = sem
            return sem

    def _domain_cache_get(self, domain: str) -> "_DomainCacheEntry | None":
        with self._lock:
            return self._domain_cache.get(domain)

    def _domain_cache_set(self, domain: str, entry: "_DomainCacheEntry") -> None:
        with self._lock:
            self._domain_cache[domain] = entry

    def _safe_hosts(self, domain: str) -> list[str]:
        try:
            return [host for host in self._mx_hosts_resolver(domain) if host]
        except Exception:
            return []

    def _hello(self, smtp: Any) -> None:
        if hasattr(smtp, "ehlo_or_helo_if_needed"):
            smtp.ehlo_or_helo_if_needed()
            return
        if hasattr(smtp, "ehlo"):
            smtp.ehlo()

    def _rcpt_code(self, smtp: Any, recipient: str) -> int:
        if hasattr(smtp, "rset"):
            smtp.rset()
        mail_code, _ = smtp.mail(self._sender)
        if int(mail_code) >= 400:
            return int(mail_code)
        rcpt_code, _ = smtp.rcpt(recipient)
        return int(rcpt_code)

    def _close(self, smtp: Any) -> None:
        try:
            if hasattr(smtp, "quit"):
                smtp.quit()
                return
        except Exception:
            pass
        try:
            if hasattr(smtp, "close"):
                smtp.close()
        except Exception:
            pass

    def _default_smtp_factory(self, host: str, timeout_seconds: float) -> smtplib.SMTP:
        return smtplib.SMTP(
            host=host,
            timeout=timeout_seconds,
            local_hostname=self._local_hostname,
        )

    @staticmethod
    def _random_local() -> str:
        return f"bl-probe-{uuid.uuid4().hex}"


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def score_enrichment(
    *,
    has_mx: bool,
    matches_company_pattern: bool,
    is_common_pattern: bool,
    validation_status: EmailValidationStatus = EmailValidationStatus.UNKNOWN,
    is_published_email: bool = False,
    is_catch_all: bool = False,
) -> int:
    if validation_status == EmailValidationStatus.VALID:
        return 98 if is_published_email else 95
    if not has_mx:
        return 30 if matches_company_pattern else 0
    if is_catch_all:
        return 70 if matches_company_pattern else 55
    if matches_company_pattern:
        return 90
    if is_common_pattern:
        return 75
    return 60


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


# Patterns considered "common defaults" when there's no detected company
# pattern — used to fall back to a sensible guess at lower confidence.
_COMMON_DEFAULT_PATTERNS: tuple[EnrichmentPattern, ...] = (
    EnrichmentPattern.FIRST_DOT_LAST,
    EnrichmentPattern.FIRST_INITIAL_LAST,
    EnrichmentPattern.FIRST,
)


class InternalLeadEnrichmentService:
    """End-to-end enrichment: domain → generate → validate → pick best."""

    def __init__(
        self,
        *,
        validator: EmailValidator,
        generator: EmailPatternGenerator | None = None,
    ) -> None:
        self._validator = validator
        self._generator = generator or EmailPatternGenerator()

    def enrich_lead_multi_domain(
        self,
        lead: Lead,
        *,
        domains: list[str],
        existing_company_emails: list[tuple[str, str]] | None = None,
        harvested_locals_by_domain: dict[str, list[str]] | None = None,
        published_emails_by_domain: dict[str, list[str]] | None = None,
    ) -> EnrichmentUpdate:
        """Try a ranked list of domains 1-by-1 until one validates.

        Used when the lead's own ``company_domain`` may be missing or
        wrong. The orchestrator collects every domain known for the
        company (from existing emails + the company_domain column) and
        passes them in ranked order — most-frequent first.

        Returns the best update across domains. A VALID hit short-circuits
        the loop; otherwise we keep the highest-scoring PROBABLE one and
        record every domain we touched so the UI can be transparent.
        """
        if lead.email and "@" in lead.email:
            return EnrichmentUpdate(
                enrichment_status=EnrichmentStatus.NOT_ENRICHED,
                skipped_existing_email=True,
            )
        cleaned = [d for d in (domain.strip().lower() for domain in domains) if d]
        if not cleaned:
            return EnrichmentUpdate(
                enrichment_status=EnrichmentStatus.FAILED,
                failure_reason="missing_domain",
            )

        best: EnrichmentUpdate | None = None
        tested: list[str] = []
        all_discarded: list[str] = []
        harvested_by_domain = harvested_locals_by_domain or {}
        published_by_domain = published_emails_by_domain or {}

        for domain in cleaned:
            if domain in tested:
                continue
            tested.append(domain)
            lead_for_domain = lead.model_copy(update={"company_domain": domain})
            update = self.enrich_lead(
                lead_for_domain,
                existing_company_emails=existing_company_emails,
                harvested_locals=harvested_by_domain.get(domain),
                published_emails=published_by_domain.get(domain),
            )
            update.tested_domains = list(tested)
            if update.email:
                update.chosen_domain = domain
            all_discarded.extend(update.discarded_candidates)

            if (
                update.email
                and update.email_validation_status == EmailValidationStatus.VALID
            ):
                update.discarded_candidates = all_discarded
                return update

            if _is_better_update(update, best):
                best = update

        if best is None or not best.email:
            failure = EnrichmentUpdate(
                enrichment_status=EnrichmentStatus.FAILED,
                failure_reason="no_valid_candidate",
                discarded_candidates=all_discarded,
                tested_domains=tested,
            )
            return failure

        best.discarded_candidates = all_discarded
        best.tested_domains = tested
        return best

    def enrich_lead(
        self,
        lead: Lead,
        *,
        existing_company_emails: list[tuple[str, str]] | None = None,
        harvested_locals: list[str] | None = None,
        published_emails: list[str] | set[str] | None = None,
    ) -> EnrichmentUpdate:
        # 1. Do not overwrite an existing email.
        if lead.email and "@" in lead.email:
            return EnrichmentUpdate(
                enrichment_status=EnrichmentStatus.NOT_ENRICHED,
                skipped_existing_email=True,
            )

        # 2. Resolve domain — required.
        domain = (lead.company_domain or "").strip().lower()
        if not domain:
            return EnrichmentUpdate(
                enrichment_status=EnrichmentStatus.FAILED,
                failure_reason="missing_domain",
            )

        # 3. Detect the company's predominant pattern. The table-level
        #    name+email pairs are the strongest signal; harvested locals
        #    from the company website are the fallback when the table
        #    has no on-domain matches yet.
        company_pattern = detect_company_pattern(
            existing_company_emails or [], domain
        )
        if company_pattern is None and harvested_locals:
            company_pattern = infer_pattern_from_locals(harvested_locals)

        # 4. Generate candidates.
        candidates = self._generator.generate(lead.person_name, domain)
        if not candidates:
            return EnrichmentUpdate(
                enrichment_status=EnrichmentStatus.FAILED,
                failure_reason="missing_name",
            )

        # 5. Validate each one, prioritising exact public evidence, then
        #    the company pattern, common defaults, and the rest.
        published_set = {
            email.strip().lower()
            for email in (published_emails or [])
            if isinstance(email, str) and "@" in email
        }
        ordered = _order_candidates(candidates, company_pattern, published_set)
        best: EnrichmentUpdate | None = None
        discarded: list[str] = []

        for candidate in ordered:
            matches = company_pattern is not None and candidate.pattern == company_pattern
            is_published = candidate.email.lower() in published_set
            result = self._validator.validate_candidate(
                candidate,
                matches_detected_pattern=matches,
                is_published_email=is_published,
            )
            if result.status in {
                EmailValidationStatus.VALID,
                EmailValidationStatus.PROBABLE,
            }:
                score = score_enrichment(
                    has_mx=result.has_mx,
                    matches_company_pattern=matches,
                    is_common_pattern=candidate.pattern in _COMMON_DEFAULT_PATTERNS,
                    validation_status=result.status,
                    is_published_email=is_published,
                    is_catch_all=result.is_catch_all,
                )
                status = (
                    EnrichmentStatus.ENRICHED
                    if (matches or result.status == EmailValidationStatus.VALID)
                    and result.has_mx
                    else EnrichmentStatus.ESTIMATED
                    if result.has_mx
                    else EnrichmentStatus.PARTIAL
                )
                best = EnrichmentUpdate(
                    email=candidate.email,
                    enrichment_status=status,
                    enrichment_confidence=score,
                    email_type="work",
                    email_validation_status=result.status,
                    matched_pattern=candidate.pattern,
                    company_pattern=company_pattern,
                )
                break
            discarded.append(candidate.email)

        if best is None:
            return EnrichmentUpdate(
                enrichment_status=EnrichmentStatus.FAILED,
                failure_reason="no_valid_candidate",
                discarded_candidates=discarded,
            )

        best.discarded_candidates = discarded
        if best.email:
            best.chosen_domain = domain
            best.tested_domains = [domain]
        return best


@dataclass(frozen=True)
class EnrichmentCostEstimate:
    """Per-provider cost estimate used by the UI to pre-compute spend."""

    provider: str
    selected_leads: int
    estimated_credits: int
    estimated_brl: float


class LeadEnrichmentProvider(Protocol):
    """Common interface for *any* enrichment provider — paid or internal.

    The waterfall/parallel orchestrator only needs to know:
    - ``name`` (for routing + UI labels);
    - ``estimate(...)`` so the UI can show R$ cost before confirming;
    - ``enrich(...)`` to actually populate fields on the leads.

    The internal provider returns zero-cost estimates. The paid providers
    keep their per-credit math.
    """

    name: str

    def estimate(
        self,
        leads: list[Lead],
        *,
        fields: str,
    ) -> EnrichmentCostEstimate:
        ...

    def enrich(
        self,
        leads: list[Lead],
        *,
        fields: str,
        existing_company_emails: list[tuple[str, str]] | None = None,
    ) -> list[tuple[Lead, "EnrichmentUpdate"]]:
        ...


class InternalProviderAdapter:
    """Adapter that exposes :class:`InternalLeadEnrichmentService` through
    the :class:`LeadEnrichmentProvider` Protocol.

    Kept in this module so the future paid-provider adapters can mirror
    the same shape without circular imports. The orchestrator just sees
    a list of ``LeadEnrichmentProvider`` instances.
    """

    name = "internal"

    def __init__(self, service: "InternalLeadEnrichmentService") -> None:
        self._service = service

    def estimate(
        self, leads: list[Lead], *, fields: str
    ) -> EnrichmentCostEstimate:
        return EnrichmentCostEstimate(
            provider=self.name,
            selected_leads=len(leads),
            # Internal provider has no per-credit cost — it does local
            # pattern generation + DNS MX. Surface zero so the UI can
            # display "grátis" / R$0,00.
            estimated_credits=0,
            estimated_brl=0.0,
        )

    def enrich(
        self,
        leads: list[Lead],
        *,
        fields: str,
        existing_company_emails: list[tuple[str, str]] | None = None,
    ) -> list[tuple[Lead, EnrichmentUpdate]]:
        if fields != "email":
            return [
                (
                    lead,
                    EnrichmentUpdate(
                        enrichment_status=EnrichmentStatus.FAILED,
                        failure_reason="internal_supports_only_email",
                    ),
                )
                for lead in leads
            ]
        return [
            (
                lead,
                self._service.enrich_lead(
                    lead, existing_company_emails=existing_company_emails or []
                ),
            )
            for lead in leads
        ]


# ---------------------------------------------------------------------------
# Per-company domain collection
# ---------------------------------------------------------------------------


def _normalize_company_key(name: str | None) -> str:
    """Map company names to a stable lookup key.

    Strips accents, lowercases, removes punctuation, and collapses
    whitespace so ``"Acme  Inc."`` and ``"acme inc"`` collide. Trailing
    common suffixes (``inc``, ``ltda``, ``sa``) are dropped — they're
    legal boilerplate, not naming differences.
    """
    if not name:
        return ""
    stripped = unicodedata.normalize("NFD", name)
    ascii_text = "".join(ch for ch in stripped if not unicodedata.combining(ch))
    cleaned = re.sub(r"[^A-Za-z0-9 ]+", " ", ascii_text).lower()
    tokens = [t for t in cleaned.split() if t]
    while tokens and tokens[-1] in _COMPANY_SUFFIX_NOISE:
        tokens.pop()
    return " ".join(tokens)


_COMPANY_SUFFIX_NOISE: frozenset[str] = frozenset(
    {
        "inc",
        "incorporated",
        "ltd",
        "limited",
        "ltda",
        "llc",
        "sa",
        "s",  # leftover from "S.A." after punctuation stripping
        "co",
        "corp",
        "corporation",
        "company",
        "gmbh",
        "bv",
        "oy",
        "ag",
    }
)


def collect_company_domains(leads: list[Lead]) -> dict[str, list[str]]:
    """Build a ``company_key -> [domains]`` map ordered by evidence.

    Domain sources, in priority order:

    1. The local-part of any saved ``lead.email`` (it has been validated
       by whoever filled it in — Apollo, Lusha, internal enrichment, the
       user, etc.).
    2. The ``lead.company_domain`` column on each lead.

    Within each company the result is deduped and ranked by how many
    leads support the domain. The orchestrator iterates that list 1-by-1
    when a lead's own ``company_domain`` is missing or fails to validate
    — so a colleague's Apollo-derived ``@nubank.com.br`` becomes a
    domain we'll try for everyone else at Nubank.
    """
    votes: dict[str, Counter[str]] = {}
    for lead in leads:
        key = _normalize_company_key(getattr(lead, "company_name", None))
        if not key:
            continue
        bucket = votes.setdefault(key, Counter())
        email = (getattr(lead, "email", None) or "").strip().lower()
        if email and "@" in email:
            domain = email.rsplit("@", 1)[1]
            if domain and domain not in PERSONAL_EMAIL_DOMAINS:
                # Email-derived domains are the strongest signal — they
                # already exist in the wild. Weight them so they outrank
                # the company_domain column even when the column has more
                # leads behind it.
                bucket[domain] += 3
        col = (getattr(lead, "company_domain", None) or "").strip().lower()
        if col and col not in PERSONAL_EMAIL_DOMAINS:
            bucket[col] += 1
    return {
        key: [domain for domain, _ in bucket.most_common()]
        for key, bucket in votes.items()
        if bucket
    }


def _is_better_update(
    candidate: EnrichmentUpdate, current: EnrichmentUpdate | None
) -> bool:
    """Return True when ``candidate`` should replace ``current`` as the
    best non-VALID match so far. VALID hits short-circuit the loop, so
    here we just rank by confidence; ties go to the existing pick."""
    if current is None:
        return True
    if not candidate.email:
        return False
    if not current.email:
        return True
    return candidate.enrichment_confidence > current.enrichment_confidence


# ---------------------------------------------------------------------------
# Parallel orchestrator
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DomainContext:
    """Per-domain artifacts shared across leads of the same company."""

    domain: str
    published_emails: list[str]
    company_pattern: EnrichmentPattern | None


HarvestFn = Callable[[str], list[str]]
ProgressFn = Callable[[dict[str, Any]], None]


class _DomainDiscovererProto(Protocol):
    """Minimal contract :class:`InternalEnrichmentOrchestrator` needs.

    Kept local so this module does not depend on
    ``beautiful_linkedin.storage.domain_discovery`` at import time —
    callers (the server) wire the real implementation in.
    """

    def discover(self, seed: str) -> Any: ...


class InternalEnrichmentOrchestrator:
    """Runs harvest + enrichment in parallel and emits progress events.

    Two phases:

    1. Harvest each unique domain once (parallel, bounded fan-out). Detect
       the company pattern from harvested locals + table-internal pairings.
    2. Enrich each lead in parallel against the domain context.

    Both phases are bounded thread pools — I/O bound work (HTTP/DNS/SMTP)
    benefits from concurrency without thrashing remote endpoints.

    ``on_event`` receives small dicts the UI can render directly:

    - ``{"type": "phase", "phase": "harvesting"|"validating"|"completed"}``
    - ``{"type": "domain", "domain": ..., "harvested": N, "pattern": ...}``
    - ``{"type": "lead", "lead_ref": ..., "person_name": ...,
        "status": "enriched"|"skipped"|"failed", "email": ... or None}``
    - ``{"type": "progress", "completed": N, "total": M}``
    """

    def __init__(
        self,
        *,
        service: InternalLeadEnrichmentService,
        harvest_fn: HarvestFn,
        discoverer: _DomainDiscovererProto | None = None,
        domain_concurrency: int = 6,
        lead_concurrency: int = 6,
        on_event: ProgressFn | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> None:
        self._service = service
        self._harvest_fn = harvest_fn
        self._discoverer = discoverer
        self._domain_concurrency = max(1, domain_concurrency)
        self._lead_concurrency = max(1, lead_concurrency)
        self._on_event = on_event or (lambda event: None)
        self._cancel_check = cancel_check or (lambda: False)

    def run(
        self,
        leads: list[Lead],
        *,
        existing_company_emails: list[tuple[str, str]],
        company_domains: dict[str, list[str]] | None = None,
    ) -> list[tuple[Lead, EnrichmentUpdate]]:
        """Validate leads against every known domain for their company.

        ``company_domains`` is a ``company_key -> [domain, ...]`` map
        (typically from :func:`collect_company_domains`). When provided,
        each lead is tried against every known domain for its company in
        order until one validates, falling back to the lead's own
        ``company_domain`` column when the map has nothing.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        if not leads:
            self._emit({"type": "phase", "phase": "completed"})
            return []

        company_domains = dict(company_domains or {})

        # ---- Phase 0: domain discovery (free signals) -------------------
        # Expand the per-company domain list with cert-transparency,
        # SPF/DMARC, and ccTLD-variant lookups. Each company is probed
        # once using its strongest known domain as the seed. Mutates
        # company_domains in place so the harvest/validate phases pick
        # up the new candidates automatically.
        if self._discoverer is not None and company_domains:
            self._run_discovery_phase(company_domains, leads)
            if self._cancel_check():
                return []

        # The set of domains we need a per-domain context for is the
        # UNION of every domain we may try: the lead's column AND every
        # domain known for its company. This is wider than before — a
        # company with leads at acme.com + acme.io now harvests both.
        # Lead is a Pydantic model and not hashable, so we key by index.
        domains_per_lead: list[list[str]] = [
            _ranked_domains_for_lead(lead, company_domains) for lead in leads
        ]
        unique_domains: list[str] = sorted(
            {domain for domains in domains_per_lead for domain in domains}
        )

        # ---- Phase 1: harvest each unique domain in parallel -------------
        self._emit({"type": "phase", "phase": "harvesting"})
        contexts: dict[str, DomainContext] = {}
        with ThreadPoolExecutor(max_workers=self._domain_concurrency) as pool:
            future_to_domain = {
                pool.submit(self._harvest_safe, domain): domain
                for domain in unique_domains
            }
            for future in as_completed(future_to_domain):
                if self._cancel_check():
                    for pending in future_to_domain:
                        pending.cancel()
                    self._emit({"type": "phase", "phase": "cancelled"})
                    return []
                domain = future_to_domain[future]
                published = future.result()
                pattern = detect_company_pattern(existing_company_emails, domain)
                if pattern is None and published:
                    pattern = infer_pattern_from_locals(
                        [email.split("@", 1)[0] for email in published if "@" in email]
                    )
                contexts[domain] = DomainContext(
                    domain=domain,
                    published_emails=published,
                    company_pattern=pattern,
                )
                self._emit(
                    {
                        "type": "domain",
                        "domain": domain,
                        "harvested": len(published),
                        "pattern": pattern.value if pattern else None,
                    }
                )

        # ---- Phase 2: enrich each lead in parallel -----------------------
        self._emit({"type": "phase", "phase": "validating"})
        total = len(leads)
        completed = 0
        results: list[tuple[Lead, EnrichmentUpdate]] = []
        with ThreadPoolExecutor(max_workers=self._lead_concurrency) as pool:
            future_to_lead = {
                pool.submit(
                    self._enrich_one,
                    lead,
                    domains_per_lead[idx],
                    contexts,
                    existing_company_emails,
                ): lead
                for idx, lead in enumerate(leads)
            }
            for future in as_completed(future_to_lead):
                if self._cancel_check():
                    for pending in future_to_lead:
                        pending.cancel()
                    self._emit({"type": "phase", "phase": "cancelled"})
                    break
                lead = future_to_lead[future]
                update = future.result()
                results.append((lead, update))
                completed += 1
                self._emit(
                    {
                        "type": "lead",
                        "lead_ref": lead.linkedin_url or lead.source_url,
                        "person_name": lead.person_name,
                        "company_name": lead.company_name,
                        "status": _summarize_update(update),
                        "email": update.email,
                        "confidence": update.enrichment_confidence,
                        "chosen_domain": update.chosen_domain,
                        "tested_domains": list(update.tested_domains),
                    }
                )
                self._emit(
                    {"type": "progress", "completed": completed, "total": total}
                )

        self._emit({"type": "phase", "phase": "completed"})
        return results

    def _run_discovery_phase(
        self,
        company_domains: dict[str, list[str]],
        leads: list[Lead],
    ) -> None:
        """Probe each company's strongest seed and merge new domains in.

        We seed with the company's top-ranked existing domain (already
        sorted by evidence weight) — that's the one most likely to share
        TLS certs and SPF policy with siblings. Discovered domains are
        appended at lower priority than seeds so the orchestrator still
        tries known-good options first.

        Emits ``{"type": "discovery", "company": key, "seed": ...,
        "discovered": N, "sources": {"crt_sh": N, ...}}`` per company
        for the UI's discovery counter.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        # Map every company_key we touch back to a human-readable name
        # so the event payload doesn't expose the internal slug.
        company_name_by_key: dict[str, str] = {}
        for lead in leads:
            key = _normalize_company_key(getattr(lead, "company_name", None))
            if key and key not in company_name_by_key:
                company_name_by_key[key] = getattr(lead, "company_name", None) or key

        self._emit({"type": "phase", "phase": "discovering"})
        with ThreadPoolExecutor(max_workers=self._domain_concurrency) as pool:
            futures = {}
            for key, domains in company_domains.items():
                if not domains:
                    continue
                seed = domains[0]
                futures[pool.submit(self._discover_safe, seed)] = (key, seed)

            for future in as_completed(futures):
                if self._cancel_check():
                    for pending in futures:
                        pending.cancel()
                    self._emit({"type": "phase", "phase": "cancelled"})
                    return
                key, seed = futures[future]
                result = future.result()
                new_domains = [
                    d for d in (result.domains if result else [])
                    if d not in company_domains.get(key, [])
                ]
                if new_domains:
                    company_domains[key] = list(company_domains.get(key, [])) + new_domains
                sources = (
                    {src: list(items) for src, items in (result.sources or {}).items()}
                    if result
                    else {}
                )
                self._emit(
                    {
                        "type": "discovery",
                        "company": company_name_by_key.get(key, key),
                        "seed": seed,
                        "discovered": len(new_domains),
                        "domains": new_domains,
                        "sources": sources,
                    }
                )

    def _discover_safe(self, seed: str) -> Any:
        try:
            return self._discoverer.discover(seed) if self._discoverer else None
        except Exception:
            return None

    def _harvest_safe(self, domain: str) -> list[str]:
        try:
            return self._harvest_fn(domain)
        except Exception:
            return []

    def _enrich_one(
        self,
        lead: Lead,
        ranked_domains: list[str],
        contexts: dict[str, DomainContext],
        existing_company_emails: list[tuple[str, str]],
    ) -> EnrichmentUpdate:
        if not ranked_domains:
            return self._service.enrich_lead(
                lead, existing_company_emails=existing_company_emails
            )
        harvested_by_domain: dict[str, list[str]] = {}
        published_by_domain: dict[str, list[str]] = {}
        for domain in ranked_domains:
            ctx = contexts.get(domain)
            if ctx is None:
                continue
            published_by_domain[domain] = ctx.published_emails
            harvested_by_domain[domain] = [
                email.split("@", 1)[0]
                for email in ctx.published_emails
                if "@" in email
            ]
        return self._service.enrich_lead_multi_domain(
            lead,
            domains=ranked_domains,
            existing_company_emails=existing_company_emails,
            harvested_locals_by_domain=harvested_by_domain,
            published_emails_by_domain=published_by_domain,
        )

    def _emit(self, event: dict[str, Any]) -> None:
        try:
            self._on_event(event)
        except Exception:
            # Progress emission must never break the run.
            pass


def _ranked_domains_for_lead(
    lead: Lead, company_domains: dict[str, list[str]]
) -> list[str]:
    """Combine the lead's own column with the company-wide list, deduped."""
    own = (getattr(lead, "company_domain", None) or "").strip().lower()
    key = _normalize_company_key(getattr(lead, "company_name", None))
    company_list = company_domains.get(key, []) if key else []

    out: list[str] = []
    seen: set[str] = set()
    if own and own not in PERSONAL_EMAIL_DOMAINS:
        out.append(own)
        seen.add(own)
    for domain in company_list:
        if domain and domain not in seen and domain not in PERSONAL_EMAIL_DOMAINS:
            out.append(domain)
            seen.add(domain)
    return out


def _summarize_update(update: EnrichmentUpdate) -> str:
    if update.skipped_existing_email:
        return "skipped_existing_email"
    if update.enrichment_status == EnrichmentStatus.FAILED:
        if update.failure_reason == "missing_domain":
            return "failed_missing_domain"
        return "failed"
    if update.email:
        return "enriched"
    return "no_change"


def _order_candidates(
    candidates: list[EmailCandidate],
    company_pattern: EnrichmentPattern | None,
    published_emails: set[str] | None = None,
) -> list[EmailCandidate]:
    """Order candidates by evidence strength.

    Exact e-mails published on the company's own site win first, then the
    detected company pattern, common defaults, and the rest in generation
    order.
    """
    published = published_emails or set()
    rank: dict[EmailCandidate, int] = {}
    for candidate in candidates:
        if candidate.email.lower() in published:
            rank[candidate] = 0
        elif company_pattern and candidate.pattern == company_pattern:
            rank[candidate] = 1
        elif candidate.pattern in _COMMON_DEFAULT_PATTERNS:
            rank[candidate] = 2
        else:
            rank[candidate] = 3
    return sorted(candidates, key=lambda c: (rank[c], candidates.index(c)))
