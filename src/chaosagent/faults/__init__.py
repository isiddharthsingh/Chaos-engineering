"""Fault composers — engine-agnostic FaultSpec intent -> concrete engine CRs.

``compose_cr`` is the single entry point: it dispatches on ``fault_type`` to the
per-kind builder, so callers (the lifecycle) never pick an engine kind
themselves and intent can never silently degrade to the wrong CR.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from chaosagent.domain.actions import FaultSpec
from chaosagent.domain.enums import FaultType
from chaosagent.faults.chaosmesh import UnsupportedFaultError, compose_podchaos
from chaosagent.faults.dns import compose_dnschaos
from chaosagent.faults.io import compose_iochaos
from chaosagent.faults.network import compose_networkchaos
from chaosagent.faults.stress import compose_stresschaos
from chaosagent.faults.timechaos import compose_timechaos

__all__ = [
    "UnsupportedFaultError",
    "compose_cr",
    "compose_dnschaos",
    "compose_iochaos",
    "compose_networkchaos",
    "compose_podchaos",
    "compose_stresschaos",
    "compose_timechaos",
]

_Composer = Callable[..., dict[str, Any]]

_COMPOSERS: dict[FaultType, _Composer] = {
    FaultType.POD_KILL: compose_podchaos,
    FaultType.POD_FAILURE: compose_podchaos,
    FaultType.CONTAINER_KILL: compose_podchaos,
    FaultType.NETWORK_LATENCY: compose_networkchaos,
    FaultType.NETWORK_LOSS: compose_networkchaos,
    FaultType.NETWORK_PARTITION: compose_networkchaos,
    FaultType.CPU_STRESS: compose_stresschaos,
    FaultType.MEMORY_STRESS: compose_stresschaos,
    FaultType.IO_STRESS: compose_iochaos,
    FaultType.DNS_CHAOS: compose_dnschaos,
    FaultType.TIME_SKEW: compose_timechaos,
}

#: NetworkChaos is the one kind whose CRD has no containerNames field.
_NO_CONTAINER_NAMES = (compose_networkchaos,)


def compose_cr(
    fault: FaultSpec,
    *,
    namespace: str,
    name: str | None = None,
    container_names: Sequence[str] = (),
) -> dict[str, Any]:
    """Compose the Chaos Mesh CR for any supported fault type."""
    composer = _COMPOSERS.get(fault.fault_type)
    if composer is None:
        raise UnsupportedFaultError(
            f"fault type {fault.fault_type.value!r} has no composer"
        )
    if composer in _NO_CONTAINER_NAMES:
        if container_names:
            # Dropping the scoping would widen the blast radius beyond the
            # declared intent — refuse instead of silently degrading.
            raise ValueError(
                f"{fault.fault_type.value} cannot be scoped to containers "
                "(NetworkChaos has no containerNames field); remove container_names"
            )
        return composer(fault, namespace=namespace, name=name)
    return composer(fault, namespace=namespace, name=name, container_names=container_names)
