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
EnrichmentProviderName = Literal["apollo", "lusha", "snovio"]


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
    for provider in providers or ["apollo", "lusha", "snovio"]:
        name = provider.strip().lower()
        if name not in {"apollo", "lusha", "snovio"}:
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

    def enrich(
        self, leads: list[Lead], options: EnrichmentOptions
    ) -> list[EnrichmentUpdate]:
        if options.wants_phone and not options.apollo_webhook_url:
            raise ValueError(
                "Apollo exige apollo_webhook_url HTTPS para enriquecimento de telefone."
            )

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
        return updates


class LushaEnrichmentProvider:
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

    def enrich(
        self, leads: list[Lead], options: EnrichmentOptions
    ) -> list[EnrichmentUpdate]:
        updates: list[EnrichmentUpdate] = []
        headers = {
            "accept": "application/json",
            "Content-Type": "application/json",
            "api_key": self.api_key,
        }
        with httpx.Client(
            timeout=self.timeout_seconds, transport=self.transport, headers=headers
        ) as client:
            for lead in leads:
                payload = _lusha_payload(lead, options)
                data = _safe_post_json(
                    client,
                    self.endpoint,
                    provider="Lusha Enrichment",
                    json=payload,
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

    def enrich(
        self, leads: list[Lead], options: EnrichmentOptions
    ) -> list[EnrichmentUpdate]:
        if not options.wants_email:
            return []

        updates: list[EnrichmentUpdate] = []
        with httpx.Client(timeout=self.timeout_seconds, transport=self.transport) as client:
            token = self._access_token(client)
            if not token:
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
            )
            if isinstance(data, dict):
                result = data
                if data.get("status") == "completed" or _first_email(data):
                    return data
            if attempt < self.poll_attempts - 1:
                self.poll_sleep(1.0)
        return result


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


def _lusha_payload(lead: Lead, options: EnrichmentOptions) -> dict[str, Any]:
    contact: dict[str, Any] = {}
    filter_by = "nameAndCompany"
    if lead.linkedin_url:
        contact["linkedinUrl"] = lead.linkedin_url
        filter_by = "linkedinUrl"
    elif lead.email:
        contact["email"] = lead.email
        filter_by = "email"
    else:
        contact["fullName"] = lead.person_name or ""
        contact["companyName"] = lead.company_name
        if lead.company_domain:
            contact["companyDomain"] = lead.company_domain

    payload: dict[str, Any] = {
        "metadata": {"filterBy": filter_by},
        "contact": contact,
    }
    if options.wants_email:
        payload["revealEmails"] = True
    if options.wants_phone:
        payload["revealPhones"] = True
    return payload


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
) -> dict[str, Any]:
    try:
        response = client.post(endpoint, params=params, json=json, data=data, headers=headers)
        response.raise_for_status()
        parsed = response.json()
    except httpx.HTTPStatusError as exc:
        log_http_error(logger, provider=provider, error=exc, context=endpoint)
        return {}
    except (httpx.TransportError, ValueError) as exc:
        log_transport_error(logger, provider=provider, error=exc, endpoint=endpoint)
        return {}
    except Exception as exc:  # pragma: no cover - defensive boundary
        log_unexpected_error(logger, provider=provider, error=exc, context=endpoint)
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _safe_get_json(
    client: httpx.Client,
    endpoint: str,
    *,
    provider: str,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    try:
        response = client.get(endpoint, params=params, headers=headers)
        response.raise_for_status()
        parsed = response.json()
    except httpx.HTTPStatusError as exc:
        log_http_error(logger, provider=provider, error=exc, context=endpoint)
        return {}
    except (httpx.TransportError, ValueError) as exc:
        log_transport_error(logger, provider=provider, error=exc, endpoint=endpoint)
        return {}
    except Exception as exc:  # pragma: no cover - defensive boundary
        log_unexpected_error(logger, provider=provider, error=exc, context=endpoint)
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
