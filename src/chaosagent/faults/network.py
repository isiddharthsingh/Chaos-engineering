"""Compose Chaos Mesh ``NetworkChaos`` CRs from network-family fault specs.

Pure functions like the PodChaos composer: FaultSpec in, CR dict out, no I/O,
Kyverno-compatible by construction (via ``base_chaos_cr``). Field names follow
the installed CRD (``kubectl explain networkchaos.spec``): duration values are
strings like ``"100ms"``, percentages are strings like ``"10"``.
"""

from __future__ import annotations

from typing import Any

from chaosagent.domain.actions import FaultSpec, NetworkFault
from chaosagent.domain.enums import FaultType
from chaosagent.faults.chaosmesh import UnsupportedFaultError, base_chaos_cr

# FaultType -> NetworkChaos spec.action.
_ACTIONS: dict[FaultType, str] = {
    FaultType.NETWORK_LATENCY: "delay",
    FaultType.NETWORK_LOSS: "loss",
    FaultType.NETWORK_PARTITION: "partition",
}


def _delay_block(network: NetworkFault) -> dict[str, str]:
    if network.latency_ms is None:
        raise ValueError("network delay requires latency_ms")
    block = {"latency": f"{network.latency_ms}ms"}
    if network.jitter_ms:
        block["jitter"] = f"{network.jitter_ms}ms"
    if network.correlation_percent:
        block["correlation"] = f"{network.correlation_percent:g}"
    return block


def _loss_block(network: NetworkFault) -> dict[str, str]:
    if network.loss_percent is None:
        raise ValueError("network loss requires loss_percent")
    block = {"loss": f"{network.loss_percent:g}"}
    if network.correlation_percent:
        block["correlation"] = f"{network.correlation_percent:g}"
    return block


def compose_networkchaos(
    fault: FaultSpec,
    *,
    namespace: str,
    name: str | None = None,
) -> dict[str, Any]:
    """Compose a Chaos Mesh ``NetworkChaos`` CR for a network-family fault."""
    action = _ACTIONS.get(fault.fault_type)
    if action is None or fault.network is None:
        raise UnsupportedFaultError(
            f"fault type {fault.fault_type.value!r} has no NetworkChaos mapping"
        )
    if fault.network.action != action:
        raise ValueError(
            f"{fault.fault_type.value} maps to NetworkChaos action {action!r}, "
            f"but the network block says {fault.network.action!r}"
        )
    if fault.network.direction != "to":
        # Chaos Mesh requires spec.target for 'from'/'both'; without it the CR is
        # refused by the engine webhook (or half-applied). Refuse rather than
        # compose a fault that silently differs from the declared intent.
        raise ValueError(
            f"network direction {fault.network.direction!r} needs target-side support "
            "(NetworkChaos spec.target), which is not composable yet; use 'to'"
        )
    cr = base_chaos_cr("NetworkChaos", action, fault, namespace=namespace, name=name)
    cr["spec"]["action"] = action
    cr["spec"]["direction"] = fault.network.direction
    if action == "delay":
        cr["spec"]["delay"] = _delay_block(fault.network)
    elif action == "loss":
        cr["spec"]["loss"] = _loss_block(fault.network)
    return cr
