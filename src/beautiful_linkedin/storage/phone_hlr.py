"""HLR / MNP probe interface.

Real HLR (Home Location Register) and MNP (Mobile Number Portability)
lookups are *paid* services (hlr-lookups.com, Neutrino, AbstractAPI,
XConnect). They confirm that a number is currently attached to a
subscriber, identify the carrier after portability, and report
reachability — without sending an SMS.

This module ships **interface + offline NOOP only**. No paid call is
made unless the user wires a real probe in their environment. The
shape is identical to what a paid implementation needs, so when the
user later decides to plug in, e.g., hlr-lookups.com, it's a single
adapter class behind the same protocol.

Why interface-first: the orchestrator needs to be able to *check* if
HLR is enabled without conditionals at every call site. By always
having a probe instance (NOOP by default), the orchestrator code
stays branch-free.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class HlrStatus(str, Enum):
    REACHABLE = "reachable"      # number is attached to a subscriber + reachable
    UNREACHABLE = "unreachable"  # number exists but unreachable (off / out of coverage)
    ABSENT = "absent"            # number is not assigned
    UNKNOWN = "unknown"          # probe did not run (NOOP / failed)


@dataclass(frozen=True)
class HlrResult:
    status: HlrStatus
    carrier: str | None = None
    country: str | None = None
    is_ported: bool = False
    reason: str = ""


class HlrProbeProvider(Protocol):
    """Common shape for any HLR/MNP probe (paid or otherwise).

    ``name`` is for diagnostics; ``probe(e164)`` runs the lookup.
    Implementations must be fail-soft: any network/parse exception
    should resolve to ``HlrStatus.UNKNOWN`` with the reason recorded.
    """

    name: str

    def probe(self, e164: str) -> HlrResult:
        ...


class NoopHlrProbe:
    """Default probe — never calls the network.

    Returns ``UNKNOWN`` for every input. The orchestrator treats
    ``UNKNOWN`` as "no signal" so behavior collapses to
    "offline-only validation" — exactly what the project does today.
    """

    name = "noop"

    def probe(self, e164: str) -> HlrResult:  # noqa: ARG002
        return HlrResult(
            status=HlrStatus.UNKNOWN,
            reason="hlr_not_configured",
        )


def default_hlr_probe() -> HlrProbeProvider:
    """Factory used by the server's DI helpers.

    Returns :class:`NoopHlrProbe` by default. When the project later
    grows ``Settings.hlr_provider`` + ``Settings.hlr_api_key``, this
    function will branch on those to return a real adapter (kept
    behind opt-in env vars so no test or production run can
    accidentally trigger a paid probe).
    """
    return NoopHlrProbe()
