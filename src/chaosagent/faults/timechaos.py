"""Compose Chaos Mesh ``TimeChaos`` CRs from clock-skew fault specs.

Pure functions, Kyverno-compatible by construction (via ``base_chaos_cr``).
``timeOffset`` is required by the CRD; ``clockIds`` defaults to CLOCK_REALTIME
on the engine side, so it is only emitted when the spec names clocks.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from chaosagent.domain.actions import FaultSpec
from chaosagent.domain.enums import FaultType
from chaosagent.faults.chaosmesh import UnsupportedFaultError, base_chaos_cr


def compose_timechaos(
    fault: FaultSpec,
    *,
    namespace: str,
    name: str | None = None,
    container_names: Sequence[str] = (),
) -> dict[str, Any]:
    """Compose a Chaos Mesh ``TimeChaos`` CR for a clock-skew fault."""
    if fault.fault_type is not FaultType.TIME_SKEW or fault.time is None:
        raise UnsupportedFaultError(
            f"fault type {fault.fault_type.value!r} has no TimeChaos mapping"
        )
    cr = base_chaos_cr("TimeChaos", "time-skew", fault, namespace=namespace, name=name)
    cr["spec"]["timeOffset"] = fault.time.time_offset
    if fault.time.clock_ids:
        cr["spec"]["clockIds"] = list(fault.time.clock_ids)
    if container_names:
        cr["spec"]["containerNames"] = list(container_names)
    return cr
