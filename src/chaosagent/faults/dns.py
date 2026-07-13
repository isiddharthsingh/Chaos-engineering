"""Compose Chaos Mesh ``DNSChaos`` CRs from DNS-fault specs.

Pure functions, Kyverno-compatible by construction (via ``base_chaos_cr``).
``patterns`` must be non-empty: the CRD treats an unset patterns list as "every
domain the pod resolves", which is the DNS analogue of an empty label selector —
an unbounded blast radius we refuse at composition time.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from chaosagent.domain.actions import FaultSpec
from chaosagent.domain.enums import FaultType
from chaosagent.faults.chaosmesh import UnsupportedFaultError, base_chaos_cr


def compose_dnschaos(
    fault: FaultSpec,
    *,
    namespace: str,
    name: str | None = None,
    container_names: Sequence[str] = (),
) -> dict[str, Any]:
    """Compose a Chaos Mesh ``DNSChaos`` CR for a DNS fault."""
    if fault.fault_type is not FaultType.DNS_CHAOS or fault.dns is None:
        raise UnsupportedFaultError(
            f"fault type {fault.fault_type.value!r} has no DNSChaos mapping"
        )
    if not fault.dns.patterns:
        raise ValueError(
            "dns_chaos requires at least one pattern; an empty patterns list "
            "affects every domain the selected pods resolve"
        )
    cr = base_chaos_cr(
        "DNSChaos", f"dns-{fault.dns.action}", fault, namespace=namespace, name=name
    )
    cr["spec"]["action"] = fault.dns.action
    cr["spec"]["patterns"] = list(fault.dns.patterns)
    if container_names:
        cr["spec"]["containerNames"] = list(container_names)
    return cr
