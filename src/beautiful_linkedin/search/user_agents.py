"""Rotational user-agent pool with realistic browser fingerprints.

These are real, current user-agent strings from major browsers.
Each entry also carries matching Accept / Accept-Language headers
so the request looks like an organic browser visit.
"""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True)
class BrowserProfile:
    user_agent: str
    accept: str
    accept_language: str
    sec_ch_ua: str | None = None
    sec_ch_ua_platform: str | None = None


# Realistic profiles from Chrome, Firefox, Edge on Windows/Mac/Linux
_PROFILES: list[BrowserProfile] = [
    BrowserProfile(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        accept="text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        accept_language="pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        sec_ch_ua='"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
        sec_ch_ua_platform='"Windows"',
    ),
    BrowserProfile(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        accept="text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        accept_language="en-US,en;q=0.9,pt-BR;q=0.8,pt;q=0.7",
        sec_ch_ua='"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        sec_ch_ua_platform='"Windows"',
    ),
    BrowserProfile(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        accept="text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        accept_language="pt-BR,pt;q=0.9,en;q=0.8",
        sec_ch_ua='"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
        sec_ch_ua_platform='"macOS"',
    ),
    BrowserProfile(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) "
            "Gecko/20100101 Firefox/126.0"
        ),
        accept="text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        accept_language="pt-BR,pt;q=0.8,en-US;q=0.5,en;q=0.3",
    ),
    BrowserProfile(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:126.0) "
            "Gecko/20100101 Firefox/126.0"
        ),
        accept="text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        accept_language="en-US,en;q=0.9,pt-BR;q=0.7,pt;q=0.5",
    ),
    BrowserProfile(
        user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        accept="text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        accept_language="pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        sec_ch_ua='"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
        sec_ch_ua_platform='"Linux"',
    ),
    BrowserProfile(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0"
        ),
        accept="text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        accept_language="pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        sec_ch_ua='"Microsoft Edge";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
        sec_ch_ua_platform='"Windows"',
    ),
    BrowserProfile(
        user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64; rv:126.0) "
            "Gecko/20100101 Firefox/126.0"
        ),
        accept="text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        accept_language="en-US,en;q=0.5",
    ),
]


def random_profile() -> BrowserProfile:
    """Return a random browser profile for request headers."""
    return random.choice(_PROFILES)


def build_headers(profile: BrowserProfile | None = None) -> dict[str, str]:
    """Build a complete set of realistic HTTP headers from a browser profile."""
    p = profile or random_profile()
    headers: dict[str, str] = {
        "User-Agent": p.user_agent,
        "Accept": p.accept,
        "Accept-Language": p.accept_language,
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }
    if p.sec_ch_ua:
        headers["Sec-Ch-Ua"] = p.sec_ch_ua
    if p.sec_ch_ua_platform:
        headers["Sec-Ch-Ua-Platform"] = p.sec_ch_ua_platform
        headers["Sec-Ch-Ua-Mobile"] = "?0"
    if p.sec_ch_ua:
        headers["Sec-Fetch-Dest"] = "document"
        headers["Sec-Fetch-Mode"] = "navigate"
        headers["Sec-Fetch-Site"] = "none"
        headers["Sec-Fetch-User"] = "?1"
    return headers
