from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlparse, urlunparse


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_text = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    lowered = ascii_text.lower()
    return re.sub(r"\s+", " ", lowered).strip()


def normalize_url(value: str | None) -> str:
    if not value:
        return ""
    parsed = urlparse(value.strip())
    scheme = parsed.scheme.lower() or "https"
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    return urlunparse((scheme, netloc, path, "", "", ""))


def normalize_source_url(value: str | None) -> str:
    if not value:
        return ""
    parsed = urlparse(value.strip())
    scheme = parsed.scheme.lower() or "https"
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    return urlunparse((scheme, netloc, path, "", "", parsed.fragment))


def normalize_domain(value: str | None) -> str:
    if not value:
        return ""

    candidate = value.strip()
    if not candidate:
        return ""
    if "://" not in candidate:
        candidate = f"https://{candidate}"

    parsed = urlparse(candidate)
    domain = parsed.netloc.lower().strip()
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def contains_linkedin_profile_url(value: str | None) -> bool:
    normalized = normalize_url(value)
    parsed = urlparse(normalized)
    return parsed.netloc.endswith("linkedin.com") and parsed.path.startswith("/in/")
