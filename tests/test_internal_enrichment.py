"""TDD for the internal enrichment service.

The internal enrichment service infers a probable work e-mail for a lead
based on:
1. The company domain (from the lead, the table search_request, or table).
2. A library of common e-mail patterns (first, first.last, f.last, etc.).
3. A detected pattern from existing emails of the same company.
4. Technical validation (format + MX) and a confidence score.

Everything is in-process — no paid APIs. Tests pass a fake MX resolver so
they run offline.
"""

from __future__ import annotations

from beautiful_linkedin.storage.internal_enrichment import (
    EmailCandidate,
    EmailPatternGenerator,
    EmailValidationStatus,
    EmailValidator,
    EnrichmentPattern,
    EnrichmentStatus,
    MailboxVerificationResult,
    PERSONAL_EMAIL_DOMAINS,
    detect_company_pattern,
)


# ---- EmailPatternGenerator ------------------------------------------------


def test_generator_returns_empty_when_domain_missing() -> None:
    gen = EmailPatternGenerator()
    assert gen.generate("Ana Silva", "") == []
    assert gen.generate("Ana Silva", None) == []


def test_generator_returns_empty_when_name_missing() -> None:
    gen = EmailPatternGenerator()
    assert gen.generate("", "empresa.com") == []
    assert gen.generate(None, "empresa.com") == []


def test_generator_handles_two_word_name() -> None:
    candidates = EmailPatternGenerator().generate("Ana Silva", "empresa.com")
    locals_ = {c.local_part for c in candidates}
    assert "ana" in locals_
    assert "ana.silva" in locals_
    assert "asilva" in locals_
    assert "ana.s" in locals_
    assert "silva" in locals_
    # All carry the same domain.
    assert all(c.domain == "empresa.com" for c in candidates)


def test_generator_handles_three_word_name() -> None:
    """For 'Ana Maria Silva' we want to emit common Brazilian variants
    that include the middle name."""
    candidates = EmailPatternGenerator().generate("Ana Maria Silva", "empresa.com")
    locals_ = {c.local_part for c in candidates}
    assert "ana.silva" in locals_  # first + last
    assert "ana_maria.silva" in locals_  # first_middle.last
    assert "amaria" in locals_  # first-initial + middle
    assert "ana.maria.silva" in locals_  # full dotted


def test_generator_strips_accents_and_normalises_case() -> None:
    candidates = EmailPatternGenerator().generate("João Pádua", "EMPRESA.com")
    locals_ = {c.local_part for c in candidates}
    assert "joao.padua" in locals_
    assert all(c.domain == "empresa.com" for c in candidates)


def test_generator_attaches_pattern_label_to_each_candidate() -> None:
    """Each candidate carries the name of the pattern that generated it so
    the scorer/UI can explain why a given e-mail was preferred."""
    candidates = EmailPatternGenerator().generate("Ana Silva", "empresa.com")
    label_for = {c.local_part: c.pattern for c in candidates}
    assert label_for["ana.silva"] == EnrichmentPattern.FIRST_DOT_LAST
    assert label_for["asilva"] == EnrichmentPattern.FIRST_INITIAL_LAST


# ---- detect_company_pattern -----------------------------------------------


def test_detector_returns_none_when_no_existing_emails() -> None:
    assert detect_company_pattern([], "empresa.com") is None


def test_detector_ignores_emails_from_other_domains() -> None:
    samples = [
        ("Ana Silva", "ana.silva@OUTRO.com"),
        ("Bruno Costa", "bruno.costa@TERCEIRO.com"),
    ]
    assert detect_company_pattern(samples, "empresa.com") is None


def test_detector_identifies_first_dot_last_pattern() -> None:
    samples = [
        ("Ana Silva", "ana.silva@empresa.com"),
        ("Bruno Costa", "bruno.costa@empresa.com"),
        ("Carla Lima", "carla.lima@empresa.com"),
    ]
    pattern = detect_company_pattern(samples, "empresa.com")
    assert pattern == EnrichmentPattern.FIRST_DOT_LAST


def test_detector_picks_predominant_pattern_when_mixed() -> None:
    """Two leads use first.last, one uses first — should pick first.last."""
    samples = [
        ("Ana Silva", "ana.silva@empresa.com"),
        ("Bruno Costa", "bruno.costa@empresa.com"),
        ("Carla Lima", "carla@empresa.com"),
    ]
    pattern = detect_company_pattern(samples, "empresa.com")
    assert pattern == EnrichmentPattern.FIRST_DOT_LAST


def test_detector_handles_first_initial_last_pattern() -> None:
    samples = [
        ("Ana Silva", "asilva@empresa.com"),
        ("Bruno Costa", "bcosta@empresa.com"),
    ]
    pattern = detect_company_pattern(samples, "empresa.com")
    assert pattern == EnrichmentPattern.FIRST_INITIAL_LAST


# ---- EmailValidator -------------------------------------------------------


def test_validator_rejects_invalid_format() -> None:
    validator = EmailValidator(mx_resolver=lambda _: True)
    result = validator.validate("not-an-email")
    assert result.status == EmailValidationStatus.UNKNOWN
    assert "format" in result.reason


def test_validator_marks_personal_domain_as_risky_for_work_email() -> None:
    validator = EmailValidator(mx_resolver=lambda _: True)
    for personal in PERSONAL_EMAIL_DOMAINS:
        result = validator.validate(f"user@{personal}")
        assert result.status == EmailValidationStatus.RISKY


def test_validator_returns_probable_when_mx_resolves() -> None:
    validator = EmailValidator(mx_resolver=lambda domain: domain == "empresa.com")
    result = validator.validate("ana.silva@empresa.com")
    assert result.status == EmailValidationStatus.PROBABLE
    assert result.has_mx is True


def test_validator_returns_risky_when_mx_does_not_resolve() -> None:
    validator = EmailValidator(mx_resolver=lambda _: False)
    result = validator.validate("ana.silva@empresa.com")
    assert result.status == EmailValidationStatus.RISKY
    assert result.has_mx is False


def test_validator_keeps_pattern_match_probable_without_mailbox_evidence() -> None:
    """A strong company pattern is evidence, but not proof that the mailbox
    exists. Without SMTP or public-site evidence, it should stay probable."""
    validator = EmailValidator(mx_resolver=lambda _: True)
    candidate = EmailCandidate(
        local_part="ana.silva",
        domain="empresa.com",
        pattern=EnrichmentPattern.FIRST_DOT_LAST,
    )
    result = validator.validate_candidate(candidate, matches_detected_pattern=True)
    assert result.status == EmailValidationStatus.PROBABLE
    assert result.reason == "matches_company_pattern"


def test_validator_returns_valid_when_mailbox_verifier_confirms_recipient() -> None:
    validator = EmailValidator(
        mx_resolver=lambda _: True,
        mailbox_verifier=lambda email: MailboxVerificationResult(
            status=EmailValidationStatus.VALID,
            reason="smtp_valid",
        ),
    )
    result = validator.validate("ana.silva@empresa.com")
    assert result.status == EmailValidationStatus.VALID
    assert result.mailbox_checked is True
    assert result.reason == "smtp_valid"


def test_validator_returns_risky_when_mailbox_verifier_rejects_recipient() -> None:
    validator = EmailValidator(
        mx_resolver=lambda _: True,
        mailbox_verifier=lambda email: MailboxVerificationResult(
            status=EmailValidationStatus.RISKY,
            reason="smtp_rejected",
        ),
    )
    result = validator.validate("ana@empresa.com")
    assert result.status == EmailValidationStatus.RISKY
    assert result.reason == "smtp_rejected"


def test_validator_marks_catch_all_as_probable_not_valid() -> None:
    validator = EmailValidator(
        mx_resolver=lambda _: True,
        mailbox_verifier=lambda email: MailboxVerificationResult(
            status=EmailValidationStatus.PROBABLE,
            reason="smtp_catch_all",
            is_catch_all=True,
        ),
    )
    candidate = EmailCandidate(
        local_part="ana.silva",
        domain="empresa.com",
        pattern=EnrichmentPattern.FIRST_DOT_LAST,
    )
    result = validator.validate_candidate(candidate, matches_detected_pattern=True)
    assert result.status == EmailValidationStatus.PROBABLE
    assert result.is_catch_all is True
    assert result.reason == "smtp_catch_all"


# ---- end-to-end service ---------------------------------------------------


def test_service_picks_best_candidate_using_detected_pattern() -> None:
    """When the company pattern is detected as first.last, the candidate
    ana.silva@empresa.com should win over ana@empresa.com."""
    from beautiful_linkedin.models import Lead
    from beautiful_linkedin.storage.internal_enrichment import (
        InternalLeadEnrichmentService,
    )

    def fake_mx(domain: str) -> bool:
        return domain == "empresa.com"

    service = InternalLeadEnrichmentService(
        validator=EmailValidator(mx_resolver=fake_mx),
    )

    lead = Lead(
        company_name="Empresa",
        company_domain="empresa.com",
        person_name="Ana Silva",
        title="Marketing Manager",
        linkedin_url="https://www.linkedin.com/in/ana/",
        source_url="https://www.linkedin.com/in/ana/",
        source_type="linkedin_people_search",
        snippet="Marketing Manager",
        confidence_score=80,
    )
    existing_emails = [
        ("Bruno Costa", "bruno.costa@empresa.com"),
        ("Carla Lima", "carla.lima@empresa.com"),
    ]

    update = service.enrich_lead(lead, existing_company_emails=existing_emails)

    assert update is not None
    assert update.email == "ana.silva@empresa.com"
    assert update.enrichment_status == EnrichmentStatus.ENRICHED
    assert update.enrichment_source == "internal"
    assert update.enrichment_confidence >= 80


def test_service_returns_partial_when_no_pattern_but_mx_resolves() -> None:
    from beautiful_linkedin.models import Lead
    from beautiful_linkedin.storage.internal_enrichment import (
        InternalLeadEnrichmentService,
    )

    service = InternalLeadEnrichmentService(
        validator=EmailValidator(mx_resolver=lambda _: True),
    )
    lead = Lead(
        company_name="Empresa",
        company_domain="empresa.com",
        person_name="Ana Silva",
        title="Marketing Manager",
        linkedin_url="https://www.linkedin.com/in/ana/",
        source_url="https://www.linkedin.com/in/ana/",
        source_type="linkedin_people_search",
        snippet="",
        confidence_score=80,
    )
    update = service.enrich_lead(lead, existing_company_emails=[])
    assert update is not None
    # No detected company pattern → falls back to first.last as the most
    # likely default, but tagged 'estimated' rather than 'enriched'.
    assert update.email is not None
    assert update.enrichment_status in {
        EnrichmentStatus.ESTIMATED,
        EnrichmentStatus.PARTIAL,
    }


def test_service_returns_failed_missing_domain_when_no_domain() -> None:
    from beautiful_linkedin.models import Lead
    from beautiful_linkedin.storage.internal_enrichment import (
        InternalLeadEnrichmentService,
    )

    service = InternalLeadEnrichmentService(
        validator=EmailValidator(mx_resolver=lambda _: True),
    )
    lead = Lead(
        company_name="Empresa",
        company_domain=None,
        person_name="Ana Silva",
        title="Marketing Manager",
        linkedin_url="https://www.linkedin.com/in/ana/",
        source_url="https://www.linkedin.com/in/ana/",
        source_type="linkedin_people_search",
        snippet="",
        confidence_score=80,
    )
    update = service.enrich_lead(lead, existing_company_emails=[])
    assert update is not None
    assert update.email is None
    assert update.enrichment_status == EnrichmentStatus.FAILED
    assert update.failure_reason == "missing_domain"


# ---- infer_pattern_from_locals --------------------------------------------


def test_infer_pattern_first_dot_last() -> None:
    from beautiful_linkedin.storage.internal_enrichment import (
        infer_pattern_from_locals,
    )

    pattern = infer_pattern_from_locals(["ana.silva", "bruno.costa", "carla.lima"])
    assert pattern == EnrichmentPattern.FIRST_DOT_LAST


def test_infer_pattern_first_initial_last() -> None:
    from beautiful_linkedin.storage.internal_enrichment import (
        infer_pattern_from_locals,
    )

    pattern = infer_pattern_from_locals(["asilva", "bcosta", "clima"])
    assert pattern == EnrichmentPattern.FIRST_INITIAL_LAST


def test_infer_pattern_first_only() -> None:
    from beautiful_linkedin.storage.internal_enrichment import (
        infer_pattern_from_locals,
    )

    pattern = infer_pattern_from_locals(["ana", "bruno", "carla"])
    assert pattern == EnrichmentPattern.FIRST


def test_infer_pattern_returns_none_on_role_aliases() -> None:
    """Generic mailboxes (contato, vendas, suporte, ...) should NOT drive
    the pattern — they are not people. The function returns None when no
    person-shaped local-part is found."""
    from beautiful_linkedin.storage.internal_enrichment import (
        infer_pattern_from_locals,
    )

    pattern = infer_pattern_from_locals(
        ["contato", "vendas", "suporte", "atendimento", "info"]
    )
    assert pattern is None


def test_infer_pattern_picks_majority_when_mixed() -> None:
    from beautiful_linkedin.storage.internal_enrichment import (
        infer_pattern_from_locals,
    )

    # 3x first.last, 1x first → majority wins.
    pattern = infer_pattern_from_locals(
        ["ana.silva", "bruno.costa", "carla.lima", "danilo"]
    )
    assert pattern == EnrichmentPattern.FIRST_DOT_LAST


def test_service_uses_harvested_emails_to_detect_pattern() -> None:
    """When existing_company_emails has no on-domain match but harvested
    emails reveal first.last, the service must still pick the right
    candidate."""
    from beautiful_linkedin.models import Lead
    from beautiful_linkedin.storage.internal_enrichment import (
        InternalLeadEnrichmentService,
    )

    service = InternalLeadEnrichmentService(
        validator=EmailValidator(mx_resolver=lambda _: True),
    )
    lead = Lead(
        company_name="Empresa",
        company_domain="empresa.com",
        person_name="Ana Silva",
        title="Marketing Manager",
        linkedin_url="https://www.linkedin.com/in/ana/",
        source_url="https://www.linkedin.com/in/ana/",
        source_type="linkedin_people_search",
        snippet="",
        confidence_score=80,
    )

    update = service.enrich_lead(
        lead,
        existing_company_emails=[],
        harvested_locals=["bruno.costa", "carla.lima", "danilo.santos"],
    )

    assert update.email == "ana.silva@empresa.com"
    # Detected pattern from harvested locals is "first.last" and we found a
    # candidate that matches it → status should be ENRICHED, not ESTIMATED.
    assert update.enrichment_status == EnrichmentStatus.ENRICHED


def test_service_prioritizes_exact_email_published_on_company_site() -> None:
    """If the company website publishes the exact generated e-mail, that is
    stronger evidence than the default first-name guess."""
    from beautiful_linkedin.models import Lead
    from beautiful_linkedin.storage.internal_enrichment import (
        InternalLeadEnrichmentService,
    )

    service = InternalLeadEnrichmentService(
        validator=EmailValidator(mx_resolver=lambda _: True),
    )
    lead = Lead(
        company_name="Empresa",
        company_domain="empresa.com",
        person_name="Ana Silva",
        title="Marketing Manager",
        linkedin_url="https://www.linkedin.com/in/ana/",
        source_url="https://www.linkedin.com/in/ana/",
        source_type="linkedin_people_search",
        snippet="",
        confidence_score=80,
    )

    update = service.enrich_lead(
        lead,
        existing_company_emails=[],
        harvested_locals=[],
        published_emails=["ana.silva@empresa.com"],
    )

    assert update.email == "ana.silva@empresa.com"
    assert update.email_validation_status == EmailValidationStatus.VALID
    assert update.enrichment_confidence >= 95


def test_service_tries_next_candidate_when_mailbox_rejects_first_guess() -> None:
    """SMTP evidence should let the service skip a bad default candidate and
    keep searching instead of persisting the first MX-valid guess."""
    from beautiful_linkedin.models import Lead
    from beautiful_linkedin.storage.internal_enrichment import (
        InternalLeadEnrichmentService,
    )

    def verify(email: str) -> MailboxVerificationResult:
        if email == "ana@empresa.com":
            return MailboxVerificationResult(
                status=EmailValidationStatus.RISKY,
                reason="smtp_rejected",
            )
        if email == "ana.silva@empresa.com":
            return MailboxVerificationResult(
                status=EmailValidationStatus.VALID,
                reason="smtp_valid",
            )
        return MailboxVerificationResult(
            status=EmailValidationStatus.UNKNOWN,
            reason="smtp_unknown",
        )

    service = InternalLeadEnrichmentService(
        validator=EmailValidator(
            mx_resolver=lambda _: True,
            mailbox_verifier=verify,
        ),
    )
    lead = Lead(
        company_name="Empresa",
        company_domain="empresa.com",
        person_name="Ana Silva",
        title="Marketing Manager",
        linkedin_url="https://www.linkedin.com/in/ana/",
        source_url="https://www.linkedin.com/in/ana/",
        source_type="linkedin_people_search",
        snippet="",
        confidence_score=80,
    )

    update = service.enrich_lead(lead, existing_company_emails=[])

    assert update.email == "ana.silva@empresa.com"
    assert update.email_validation_status == EmailValidationStatus.VALID
    assert "ana@empresa.com" in update.discarded_candidates


# ---- CompanyDomainResolver ------------------------------------------------


def test_domain_resolver_prefers_lead_domain_when_present() -> None:
    from beautiful_linkedin.models import Lead
    from beautiful_linkedin.storage.internal_enrichment import (
        CompanyDomainResolver,
    )

    resolver = CompanyDomainResolver()
    lead = Lead(
        company_name="Nubank",
        company_domain="nubank.com.br",
        person_name="Ana",
        title="Marketing",
        linkedin_url="https://www.linkedin.com/in/ana/",
        source_url="https://www.linkedin.com/in/ana/",
        source_type="x",
        snippet="",
        confidence_score=80,
    )
    assert resolver.resolve(lead) == "nubank.com.br"


def test_domain_resolver_falls_back_to_search_request_domain() -> None:
    from beautiful_linkedin.models import Lead
    from beautiful_linkedin.storage.internal_enrichment import (
        CompanyDomainResolver,
    )

    resolver = CompanyDomainResolver(
        search_request={"company_domain": "fallback.com"}
    )
    lead = Lead(
        company_name="Nubank",
        company_domain=None,
        person_name="Ana",
        title="Marketing",
        linkedin_url="https://www.linkedin.com/in/ana/",
        source_url="https://www.linkedin.com/in/ana/",
        source_type="x",
        snippet="",
        confidence_score=80,
    )
    assert resolver.resolve(lead) == "fallback.com"


def test_domain_resolver_returns_none_when_no_signal() -> None:
    from beautiful_linkedin.models import Lead
    from beautiful_linkedin.storage.internal_enrichment import (
        CompanyDomainResolver,
    )

    resolver = CompanyDomainResolver()
    lead = Lead(
        company_name="Sem Domínio",
        company_domain=None,
        person_name="Ana",
        title="Marketing",
        linkedin_url="https://www.linkedin.com/in/ana/",
        source_url="https://www.linkedin.com/in/ana/",
        source_type="x",
        snippet="",
        confidence_score=80,
    )
    assert resolver.resolve(lead) is None


def test_service_does_not_overwrite_existing_email() -> None:
    from beautiful_linkedin.models import Lead
    from beautiful_linkedin.storage.internal_enrichment import (
        InternalLeadEnrichmentService,
    )

    service = InternalLeadEnrichmentService(
        validator=EmailValidator(mx_resolver=lambda _: True),
    )
    lead = Lead(
        company_name="Empresa",
        company_domain="empresa.com",
        person_name="Ana Silva",
        title="Marketing Manager",
        email="ana.previa@empresa.com",
        linkedin_url="https://www.linkedin.com/in/ana/",
        source_url="https://www.linkedin.com/in/ana/",
        source_type="linkedin_people_search",
        snippet="",
        confidence_score=80,
    )
    update = service.enrich_lead(lead, existing_company_emails=[])
    assert update is not None
    # No new email — we don't override.
    assert update.email is None
    assert update.skipped_existing_email is True


# ---- SmtpMailboxVerifier --------------------------------------------------


def test_smtp_mailbox_verifier_confirms_mailbox_when_random_local_rejected() -> None:
    from beautiful_linkedin.storage.internal_enrichment import SmtpMailboxVerifier

    class FakeSmtp:
        def __init__(self, host: str, timeout: float) -> None:
            self.rcpts: list[str] = []

        def ehlo_or_helo_if_needed(self):
            return (250, b"ok")

        def rset(self):
            return (250, b"ok")

        def mail(self, sender: str):
            return (250, b"ok")

        def rcpt(self, recipient: str):
            self.rcpts.append(recipient)
            if recipient.startswith("ana.silva@"):
                return (250, b"ok")
            return (550, b"unknown user")

        def quit(self):
            return (221, b"bye")

        def close(self):
            return None

    verifier = SmtpMailboxVerifier(
        mx_hosts_resolver=lambda domain: ["mx.empresa.com"],
        smtp_factory=FakeSmtp,
        random_local_factory=lambda: "definitely-not-real",
    )

    result = verifier("ana.silva@empresa.com")

    assert result.status == EmailValidationStatus.VALID
    assert result.reason == "smtp_valid"


def test_smtp_mailbox_verifier_marks_catch_all_when_random_local_accepted() -> None:
    from beautiful_linkedin.storage.internal_enrichment import SmtpMailboxVerifier

    class FakeSmtp:
        def __init__(self, host: str, timeout: float) -> None:
            pass

        def ehlo_or_helo_if_needed(self):
            return (250, b"ok")

        def rset(self):
            return (250, b"ok")

        def mail(self, sender: str):
            return (250, b"ok")

        def rcpt(self, recipient: str):
            return (250, b"ok")

        def quit(self):
            return (221, b"bye")

        def close(self):
            return None

    verifier = SmtpMailboxVerifier(
        mx_hosts_resolver=lambda domain: ["mx.empresa.com"],
        smtp_factory=FakeSmtp,
        random_local_factory=lambda: "definitely-not-real",
    )

    result = verifier("ana.silva@empresa.com")

    assert result.status == EmailValidationStatus.PROBABLE
    assert result.is_catch_all is True
    assert result.reason == "smtp_catch_all"


def test_smtp_verifier_caches_catch_all_per_domain_and_short_circuits() -> None:
    """Once we learn a domain is catch-all, every subsequent mailbox at
    that domain should resolve from cache — no new SMTP connections.
    Catches the regression where each colleague at the same company
    triggered its own random-local probe (4×N SMTP roundtrips)."""
    from beautiful_linkedin.storage.internal_enrichment import SmtpMailboxVerifier

    connections: list[str] = []

    class FakeSmtp:
        def __init__(self, host: str, timeout: float) -> None:
            connections.append(host)

        def ehlo_or_helo_if_needed(self):
            return (250, b"ok")

        def rset(self):
            return (250, b"ok")

        def mail(self, sender: str):
            return (250, b"ok")

        def rcpt(self, recipient: str):
            return (250, b"ok")  # accept everything → catch-all

        def quit(self):
            return (221, b"bye")

        def close(self):
            return None

    verifier = SmtpMailboxVerifier(
        mx_hosts_resolver=lambda domain: ["mx.empresa.com"],
        smtp_factory=FakeSmtp,
        random_local_factory=lambda: "definitely-not-real",
    )

    first = verifier("ana.silva@empresa.com")
    assert first.is_catch_all is True
    assert len(connections) == 1

    second = verifier("bruno.costa@empresa.com")
    assert second.is_catch_all is True
    assert second.reason == "smtp_catch_all_cached"
    # No new SMTP connection — the cached domain verdict served it.
    assert len(connections) == 1


def test_smtp_verifier_caches_no_mx_per_domain_and_short_circuits() -> None:
    """When a domain has zero MX records, future mailboxes at the same
    domain must skip the DNS query and the SMTP attempt altogether."""
    from beautiful_linkedin.storage.internal_enrichment import SmtpMailboxVerifier

    dns_calls: list[str] = []

    def resolver(domain: str) -> list[str]:
        dns_calls.append(domain)
        return []

    verifier = SmtpMailboxVerifier(
        mx_hosts_resolver=resolver,
        smtp_factory=lambda host, timeout: None,
        random_local_factory=lambda: "x",
    )

    a = verifier("ana@empresa-sem-mx.com")
    b = verifier("bruno@empresa-sem-mx.com")
    assert a.status == EmailValidationStatus.UNKNOWN
    assert a.reason == "no_mx_host"
    assert b.status == EmailValidationStatus.UNKNOWN
    # MX resolver was hit only once.
    assert dns_calls == ["empresa-sem-mx.com"]


def test_smtp_verifier_caps_concurrent_connections_per_host() -> None:
    """The per-host semaphore protects us from getting throttled by the
    same MX. With max_per_host=1, two parallel verifications hitting the
    same host serialize even when they run on different threads."""
    import threading
    import time

    from beautiful_linkedin.storage.internal_enrichment import SmtpMailboxVerifier

    active = 0
    peak = 0
    lock = threading.Lock()

    class SlowSmtp:
        def __init__(self, host: str, timeout: float) -> None:
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)

        def ehlo_or_helo_if_needed(self):
            return (250, b"ok")

        def rset(self):
            return (250, b"ok")

        def mail(self, sender: str):
            return (250, b"ok")

        def rcpt(self, recipient: str):
            # Reject the random probe so we don't poison the cache with
            # catch-all (which would skip subsequent connections entirely).
            if recipient.startswith("def-not-real"):
                return (550, b"unknown")
            return (250, b"ok")

        def quit(self):
            nonlocal active
            with lock:
                active -= 1
            return (221, b"bye")

        def close(self):
            return None

    verifier = SmtpMailboxVerifier(
        mx_hosts_resolver=lambda d: ["mx.shared.example"],
        smtp_factory=SlowSmtp,
        random_local_factory=lambda: "def-not-real",
        max_per_host=1,
    )

    # Different domains so the per-domain cache doesn't short-circuit
    # subsequent verifications. Both share the SAME MX host though.
    threads = [
        threading.Thread(target=verifier, args=(f"ana@co{i}.example",))
        for i in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert peak == 1  # never opened > 1 connection to mx.shared.example
