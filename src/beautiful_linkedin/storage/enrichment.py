from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from beautiful_linkedin.api_logging import (
    log_http_error,
    log_transport_error,
    log_unexpected_error,
)
from beautiful_linkedin.models import Lead

logger = logging.getLogger(__name__)

EnrichmentField = Literal["email", "phone", "both"]
EnrichmentProviderName = Literal["apollo", "lusha", "snovio", "pdl"]
_VALID_PROVIDERS = {"apollo", "lusha", "snovio", "pdl"}


@dataclass(frozen=True)
class EnrichmentOptions:
    fields: EnrichmentField
    providers: list[str] = field(default_factory=list)
    credit_costs_brl: dict[str, float] = field(default_factory=dict)
    apollo_webhook_url: str | None = None

    @property
    def wants_email(self) -> bool:
        return self.fields in {"email", "both"}

    @property
    def wants_phone(self) -> bool:
        return self.fields in {"phone", "both"}


@dataclass(frozen=True)
class ProviderEstimate:
    provider: str
    selected_leads: int
    estimated_credits: int
    estimated_brl: float
    fields: list[str]


@dataclass(frozen=True)
class EnrichmentEstimate:
    selected_leads: int
    provider_estimates: list[ProviderEstimate]
    total_estimated_credits: int
    total_estimated_brl: float
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class EnrichmentUpdate:
    lead: Lead
    provider: str
    email: str | None = None
    phone: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    note: str | None = None


@dataclass(frozen=True)
class EnrichmentRunSummary:
    requested_leads: int
    enriched_leads: int
    updated_leads: int
    providers_used: list[str]
    errors: list[str] = field(default_factory=list)
    provider_logs: list["ProviderRunLog"] = field(default_factory=list)


@dataclass(frozen=True)
class ProviderRunLog:
    provider: str
    requested_leads: int
    matched_leads: int
    updated_leads: int
    estimated_credits: int
    estimated_brl: float
    status: Literal["updated", "no_data", "error", "skipped"]
    message: str


def estimate_enrichment_cost(
    selected_leads: list[Lead],
    options: EnrichmentOptions,
) -> EnrichmentEstimate:
    provider_names = _normalized_providers(options.providers)
    warnings: list[str] = []
    estimates: list[ProviderEstimate] = []

    for provider in provider_names:
        if provider == "snovio" and not options.wants_email:
            warnings.append("Snovio não enriquece telefone; provedor ignorado.")
            continue

        fields = _provider_fields(provider, options)
        if not fields:
            continue
        credits = len(selected_leads) * len(fields)
        cost_per_credit = max(0.0, float(options.credit_costs_brl.get(provider, 0.0)))
        estimates.append(
            ProviderEstimate(
                provider=provider,
                selected_leads=len(selected_leads),
                estimated_credits=credits,
                estimated_brl=round(credits * cost_per_credit, 2),
                fields=fields,
            )
        )

    return EnrichmentEstimate(
        selected_leads=len(selected_leads),
        provider_estimates=estimates,
        total_estimated_credits=sum(item.estimated_credits for item in estimates),
        total_estimated_brl=round(sum(item.estimated_brl for item in estimates), 2),
        warnings=warnings,
    )


def merge_enrichment_update(lead: Lead, update: EnrichmentUpdate) -> Lead:
    notes = [lead.consultation_note] if lead.consultation_note else []
    note = update.note or f"Enriquecido via {update.provider}."
    if note not in notes:
        notes.append(note)
    return lead.model_copy(
        update={
            "email": lead.email or update.email,
            "phone": lead.phone or update.phone,
            "consultation_note": " ".join(notes).strip(),
        }
    )


def _normalized_providers(providers: list[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for provider in providers or list(_VALID_PROVIDERS):
        name = provider.strip().lower()
        if name not in _VALID_PROVIDERS:
            continue
        if name in seen:
            continue
        seen.add(name)
        cleaned.append(name)
    return cleaned


def _provider_fields(provider: str, options: EnrichmentOptions) -> list[str]:
    fields: list[str] = []
    if options.wants_email:
        fields.append("email")
    if options.wants_phone and provider != "snovio":
        fields.append("phone")
    return fields


class ApolloEnrichmentProvider:
    name = "apollo"
    endpoint = "https://api.apollo.io/api/v1/people/match"

    def __init__(
        self,
        api_key: str,
        *,
        timeout_seconds: float = 20.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self.errors: list[str] = []

    def enrich(
        self, leads: list[Lead], options: EnrichmentOptions
    ) -> list[EnrichmentUpdate]:
        if options.wants_phone and not options.apollo_webhook_url:
            raise ValueError(
                "Apollo exige apollo_webhook_url HTTPS para enriquecimento de telefone."
            )

        self.errors = []
        updates: list[EnrichmentUpdate] = []
        headers = {
            "accept": "application/json",
            "Cache-Control": "no-cache",
            "x-api-key": self.api_key,
        }
        with httpx.Client(
            timeout=self.timeout_seconds, transport=self.transport, headers=headers
        ) as client:
            for lead in leads:
                params = _apollo_params(lead, options)
                data = _safe_post_json(
                    client,
                    self.endpoint,
                    provider="Apollo Enrichment",
                    params=params,
                    json=None,
                    errors=self.errors,
                )
                if not data:
                    continue
                email = _first_email(data)
                phone = _first_phone(data)
                if email or phone:
                    updates.append(
                        EnrichmentUpdate(
                            lead=lead,
                            provider=self.name,
                            email=email,
                            phone=phone,
                            raw=data,
                        )
                    )
        # Apollo returns the same error message for every lead when the API
        # key lacks the /people/match scope. Dedupe so the UI shows it once.
        self.errors = list(dict.fromkeys(self.errors))
        return updates


class LushaEnrichmentProvider:
    """Lusha Contact Enrichment v2 (batch).

    A API ``/v2/person`` é batch: aceita ``contacts: [...]`` (1..100)
    e ``metadata.filterBy`` ∈ {"emailAddresses", "phoneNumbers"} — esse
    filtro define qual tipo de dado deve estar presente no resultado,
    não como casar o input. Para enriquecer e-mail E telefone fazemos
    duas chamadas (uma por filtro) porque a API não aceita ambos.
    """

    name = "lusha"
    endpoint = "https://api.lusha.com/v2/person"

    def __init__(
        self,
        api_key: str,
        *,
        timeout_seconds: float = 20.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self.errors: list[str] = []

    def enrich(
        self, leads: list[Lead], options: EnrichmentOptions
    ) -> list[EnrichmentUpdate]:
        self.errors = []
        if not leads:
            return []

        wanted_filters: list[str] = []
        if options.wants_email:
            wanted_filters.append("emailAddresses")
        if options.wants_phone:
            wanted_filters.append("phoneNumbers")
        if not wanted_filters:
            return []

        headers = {
            "accept": "application/json",
            "Content-Type": "application/json",
            "api_key": self.api_key,
        }
        # contactId -> {"email": ..., "phone": ..., "raw": {...}}
        per_contact: dict[str, dict[str, Any]] = {}

        with httpx.Client(
            timeout=self.timeout_seconds, transport=self.transport, headers=headers
        ) as client:
            for filter_by in wanted_filters:
                payload = _lusha_batch_payload(leads, filter_by)
                data = _safe_post_json(
                    client,
                    self.endpoint,
                    provider="Lusha Enrichment",
                    json=payload,
                    errors=self.errors,
                )
                _merge_lusha_response(per_contact, data, filter_by)
        self.errors = list(dict.fromkeys(self.errors))

        updates: list[EnrichmentUpdate] = []
        for index, lead in enumerate(leads):
            entry = per_contact.get(str(index))
            if not entry:
                continue
            email = entry.get("email") if options.wants_email else None
            phone = entry.get("phone") if options.wants_phone else None
            if email or phone:
                updates.append(
                    EnrichmentUpdate(
                        lead=lead,
                        provider=self.name,
                        email=email,
                        phone=phone,
                        raw=entry.get("raw") or {},
                    )
                )
        return updates


class SnovioEnrichmentProvider:
    name = "snovio"
    token_endpoint = "https://api.snov.io/v1/oauth/access_token"
    start_endpoint = "https://api.snov.io/v2/emails-by-domain-by-name/start"
    result_endpoint = "https://api.snov.io/v2/emails-by-domain-by-name/result"

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        timeout_seconds: float = 20.0,
        transport: httpx.BaseTransport | None = None,
        poll_attempts: int = 3,
        poll_sleep: Any = time.sleep,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self.poll_attempts = poll_attempts
        self.poll_sleep = poll_sleep
        self.errors: list[str] = []

    def enrich(
        self, leads: list[Lead], options: EnrichmentOptions
    ) -> list[EnrichmentUpdate]:
        self.errors = []
        if not options.wants_email:
            return []

        updates: list[EnrichmentUpdate] = []
        with httpx.Client(timeout=self.timeout_seconds, transport=self.transport) as client:
            token = self._access_token(client)
            if not token:
                self.errors = list(dict.fromkeys(self.errors))
                return []
            headers = {"authorization": f"Bearer {token}"}
            for lead in leads:
                if not lead.company_domain or not lead.person_name:
                    continue
                started = _safe_post_json(
                    client,
                    self.start_endpoint,
                    provider="Snovio Enrichment",
                    data=_snovio_start_payload(lead),
                    headers=headers,
                    errors=self.errors,
                )
                task_hash = _dig(started, "data", "task_hash")
                if not isinstance(task_hash, str) or not task_hash.strip():
                    continue
                result = self._poll_result(client, task_hash, headers)
                email = _first_email(result)
                if email:
                    updates.append(
                        EnrichmentUpdate(
                            lead=lead,
                            provider=self.name,
                            email=email,
                            raw=result,
                        )
                    )
        self.errors = list(dict.fromkeys(self.errors))
        return updates

    def _access_token(self, client: httpx.Client) -> str | None:
        data = _safe_post_json(
            client,
            self.token_endpoint,
            provider="Snovio Enrichment",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            errors=self.errors,
        )
        token = data.get("access_token") if isinstance(data, dict) else None
        return token.strip() if isinstance(token, str) and token.strip() else None

    def _poll_result(
        self, client: httpx.Client, task_hash: str, headers: dict[str, str]
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for attempt in range(max(1, self.poll_attempts)):
            data = _safe_get_json(
                client,
                self.result_endpoint,
                provider="Snovio Enrichment",
                params={"task_hash": task_hash},
                headers=headers,
                errors=self.errors,
            )
            if isinstance(data, dict):
                result = data
                if data.get("status") == "completed" or _first_email(data):
                    return data
            if attempt < self.poll_attempts - 1:
                self.poll_sleep(1.0)
        return result


class PdlEnrichmentProvider:
    """People Data Labs Person Enrichment API (v5).

    Endpoint ``GET /v5/person/enrich`` (também aceita POST). Cobra 1
    crédito por match bem-sucedido (``status == 200``). Quando não acha,
    retorna ``status == 404`` e não cobra. Auth via header ``X-Api-Key``.
    Retorna e-mail e telefone na mesma chamada quando disponíveis.
    """

    name = "pdl"
    endpoint = "https://api.peopledatalabs.com/v5/person/enrich"

    def __init__(
        self,
        api_key: str,
        *,
        timeout_seconds: float = 20.0,
        transport: httpx.BaseTransport | None = None,
        min_likelihood: int = 6,
    ) -> None:
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self.min_likelihood = min_likelihood
        self.errors: list[str] = []

    def enrich(
        self, leads: list[Lead], options: EnrichmentOptions
    ) -> list[EnrichmentUpdate]:
        self.errors = []
        if not leads or (not options.wants_email and not options.wants_phone):
            return []

        updates: list[EnrichmentUpdate] = []
        headers = {
            "accept": "application/json",
            "X-Api-Key": self.api_key,
        }
        with httpx.Client(
            timeout=self.timeout_seconds, transport=self.transport, headers=headers
        ) as client:
            for lead in leads:
                params = _pdl_params(lead, self.min_likelihood)
                if not params:
                    continue
                data = _safe_get_json(
                    client,
                    self.endpoint,
                    provider="PDL Enrichment",
                    params=params,
                    errors=self.errors,
                )
                if not data or data.get("status") != 200:
                    continue
                person = data.get("data")
                if not isinstance(person, dict):
                    continue
                email = _pdl_pick_email(person) if options.wants_email else None
                phone = _pdl_pick_phone(person) if options.wants_phone else None
                if email or phone:
                    updates.append(
                        EnrichmentUpdate(
                            lead=lead,
                            provider=self.name,
                            email=email,
                            phone=phone,
                            raw=data,
                        )
                    )
        self.errors = list(dict.fromkeys(self.errors))
        return updates


def _pdl_params(lead: Lead, min_likelihood: int) -> dict[str, Any]:
    """Build PDL v5 person/enrich query params.

    PDL needs at least one strong identifier (profile/email/pdl_id) OR
    name+company to consider the match valid. We return an empty dict
    when none is available so the lead is skipped (no credit burned).
    """
    params: dict[str, Any] = {"min_likelihood": min_likelihood, "pretty": "false"}
    has_anchor = False
    if lead.linkedin_url:
        params["profile"] = lead.linkedin_url
        has_anchor = True
    if lead.email:
        params["email"] = lead.email
        has_anchor = True
    if lead.person_name:
        params["name"] = lead.person_name
    if lead.company_name:
        params["company"] = lead.company_name
    if lead.company_domain:
        # PDL accepts comma-separated; passing one is fine.
        params["company"] = params.get("company") or lead.company_domain
    if not has_anchor and not (lead.person_name and (lead.company_name or lead.company_domain)):
        return {}
    return params


def _pdl_pick_email(person: dict[str, Any]) -> str | None:
    work_email = person.get("work_email")
    if isinstance(work_email, str) and "@" in work_email:
        return work_email.strip()
    for key in ("emails", "personal_emails", "recommended_personal_email"):
        value = person.get(key)
        if isinstance(value, str) and "@" in value:
            return value.strip()
        if isinstance(value, list):
            for entry in value:
                if isinstance(entry, str) and "@" in entry:
                    return entry.strip()
                if isinstance(entry, dict):
                    candidate = entry.get("address") or entry.get("email")
                    if isinstance(candidate, str) and "@" in candidate:
                        return candidate.strip()
    return None


def _pdl_pick_phone(person: dict[str, Any]) -> str | None:
    mobile = person.get("mobile_phone")
    if isinstance(mobile, str) and any(ch.isdigit() for ch in mobile):
        return mobile.strip()
    phones = person.get("phone_numbers")
    if isinstance(phones, list):
        for entry in phones:
            if isinstance(entry, str) and any(ch.isdigit() for ch in entry):
                return entry.strip()
            if isinstance(entry, dict):
                candidate = entry.get("number") or entry.get("phone")
                if isinstance(candidate, str) and any(ch.isdigit() for ch in candidate):
                    return candidate.strip()
    return None


def _apollo_params(lead: Lead, options: EnrichmentOptions) -> dict[str, Any]:
    params: dict[str, Any] = {
        "reveal_personal_emails": options.wants_email,
        "reveal_phone_number": options.wants_phone,
    }
    if lead.person_name:
        params["name"] = lead.person_name
    if lead.email:
        params["email"] = lead.email
    if lead.linkedin_url:
        params["linkedin_url"] = lead.linkedin_url
    if lead.company_domain:
        params["domain"] = lead.company_domain
    if lead.company_name:
        params["organization_name"] = lead.company_name
    if options.wants_phone and options.apollo_webhook_url:
        params["webhook_url"] = options.apollo_webhook_url
    return params


def _lusha_batch_payload(leads: list[Lead], filter_by: str) -> dict[str, Any]:
    contacts: list[dict[str, Any]] = []
    for index, lead in enumerate(leads):
        contact: dict[str, Any] = {"contactId": str(index)}
        if lead.linkedin_url:
            contact["linkedinUrl"] = lead.linkedin_url
        if lead.person_name:
            contact["fullName"] = lead.person_name
        if lead.company_name or lead.company_domain:
            company: dict[str, Any] = {"isCurrent": True}
            if lead.company_name:
                company["name"] = lead.company_name
            if lead.company_domain:
                company["domain"] = lead.company_domain
            contact["companies"] = [company]
        if lead.email and not lead.linkedin_url:
            # only fall back to email when we have no LinkedIn URL to anchor by
            contact["email"] = lead.email
        contacts.append(contact)
    return {
        "contacts": contacts,
        "metadata": {"filterBy": filter_by},
    }


def _merge_lusha_response(
    per_contact: dict[str, dict[str, Any]],
    response: dict[str, Any],
    filter_by: str,
) -> None:
    contacts_block = response.get("contacts")
    if not isinstance(contacts_block, dict):
        return
    for contact_id, entry in contacts_block.items():
        if not isinstance(entry, dict):
            continue
        bucket = per_contact.setdefault(str(contact_id), {"raw": {}})
        # Keep last raw response for debugging; merge if there were two calls.
        raw_bucket = bucket.setdefault("raw", {})
        if isinstance(raw_bucket, dict):
            raw_bucket[filter_by] = entry
        data = entry.get("data") if isinstance(entry, dict) else None
        if not isinstance(data, dict):
            continue
        if filter_by == "emailAddresses":
            email = _first_email(data.get("emailAddresses")) or _first_email(data)
            if email and not bucket.get("email"):
                bucket["email"] = email
        elif filter_by == "phoneNumbers":
            phone = _first_phone(data.get("phoneNumbers")) or _first_phone(data)
            if phone and not bucket.get("phone"):
                bucket["phone"] = phone


def _snovio_start_payload(lead: Lead) -> dict[str, str]:
    first_name, last_name = _split_name(lead.person_name or "")
    return {
        "first_name": first_name,
        "last_name": last_name,
        "domain": lead.company_domain or "",
    }


def _split_name(full_name: str) -> tuple[str, str]:
    parts = [part for part in full_name.split() if part.strip()]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _safe_post_json(
    client: httpx.Client,
    endpoint: str,
    *,
    provider: str,
    params: dict[str, Any] | None = None,
    json: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    errors: list[str] | None = None,
) -> dict[str, Any]:
    try:
        response = client.post(endpoint, params=params, json=json, data=data, headers=headers)
        response.raise_for_status()
        parsed = response.json()
    except httpx.HTTPStatusError as exc:
        log_http_error(logger, provider=provider, error=exc, context=endpoint)
        if errors is not None:
            errors.append(_summarize_http_error(exc))
        return {}
    except (httpx.TransportError, ValueError) as exc:
        log_transport_error(logger, provider=provider, error=exc, endpoint=endpoint)
        if errors is not None:
            errors.append(f"{type(exc).__name__}: {exc}")
        return {}
    except Exception as exc:  # pragma: no cover - defensive boundary
        log_unexpected_error(logger, provider=provider, error=exc, context=endpoint)
        if errors is not None:
            errors.append(f"{type(exc).__name__}: {exc}")
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _summarize_http_error(exc: httpx.HTTPStatusError) -> str:
    """Compact human-readable summary of an HTTP error for UI display."""
    status = exc.response.status_code
    body = (exc.response.text or "").strip()
    snippet = body[:240]
    # Try to extract Apollo-style {"error": "...", "error_code": "..."}.
    try:
        parsed = exc.response.json()
        if isinstance(parsed, dict):
            for key in ("error", "message", "detail"):
                if isinstance(parsed.get(key), str):
                    snippet = parsed[key][:240]
                    break
            code = parsed.get("error_code")
            if isinstance(code, str):
                snippet = f"{snippet} ({code})"
    except (ValueError, AttributeError):
        pass
    return f"HTTP {status}: {snippet}".strip()


def _safe_get_json(
    client: httpx.Client,
    endpoint: str,
    *,
    provider: str,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    errors: list[str] | None = None,
) -> dict[str, Any]:
    try:
        response = client.get(endpoint, params=params, headers=headers)
        response.raise_for_status()
        parsed = response.json()
    except httpx.HTTPStatusError as exc:
        log_http_error(logger, provider=provider, error=exc, context=endpoint)
        if errors is not None:
            errors.append(_summarize_http_error(exc))
        return {}
    except (httpx.TransportError, ValueError) as exc:
        log_transport_error(logger, provider=provider, error=exc, endpoint=endpoint)
        if errors is not None:
            errors.append(f"{type(exc).__name__}: {exc}")
        return {}
    except Exception as exc:  # pragma: no cover - defensive boundary
        log_unexpected_error(logger, provider=provider, error=exc, context=endpoint)
        if errors is not None:
            errors.append(f"{type(exc).__name__}: {exc}")
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _first_email(data: Any) -> str | None:
    if isinstance(data, str) and "@" in data:
        return data.strip()
    if isinstance(data, list):
        for item in data:
            email = _first_email(item)
            if email:
                return email
        return None
    if not isinstance(data, dict):
        return None

    for key in ("email", "emailAddress", "email_address", "work_email"):
        value = data.get(key)
        if isinstance(value, str) and "@" in value:
            return value.strip()
        nested = _first_email(value)
        if nested:
            return nested
    for key in ("emails", "email_addresses", "personal_emails", "data", "person", "result"):
        email = _first_email(data.get(key))
        if email:
            return email
    return None


def _first_phone(data: Any) -> str | None:
    if isinstance(data, str) and any(ch.isdigit() for ch in data):
        return data.strip()
    if isinstance(data, list):
        for item in data:
            phone = _first_phone(item)
            if phone:
                return phone
        return None
    if not isinstance(data, dict):
        return None

    for key in (
        "phone",
        "phone_number",
        "sanitized_phone",
        "direct_phone",
        "mobile_phone",
        "number",
    ):
        value = data.get(key)
        if isinstance(value, str) and any(ch.isdigit() for ch in value):
            return value.strip()
        nested = _first_phone(value)
        if nested:
            return nested
    for key in ("phones", "phoneNumbers", "phone_numbers", "data", "person", "result"):
        phone = _first_phone(data.get(key))
        if phone:
            return phone
    return None


def _dig(data: Any, *keys: str) -> Any:
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current
