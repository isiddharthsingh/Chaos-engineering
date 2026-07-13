"""Compose Chaos Mesh ``IOChaos`` CRs from filesystem-fault specs.

Pure functions, Kyverno-compatible by construction (via ``base_chaos_cr``).
Field names follow the installed CRD (``kubectl explain iochaos.spec``):
``volumePath`` is required, ``delay`` is a duration string like ``"100ms"``,
``errno`` is an integer, ``percent`` bounds how many I/O ops are affected.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from chaosagent.domain.actions import FaultSpec, IOFault
from chaosagent.domain.enums import FaultType
from chaosagent.faults.chaosmesh import UnsupportedFaultError, base_chaos_cr


def _action_fields(io: IOFault) -> dict[str, Any]:
    if io.action == "latency":
        if io.errno is not None:
            raise ValueError("io latency does not accept errno")
        if io.delay_ms is None:
            raise ValueError("io latency requires delay_ms")
        return {"delay": f"{io.delay_ms}ms"}
    if io.delay_ms is not None:
        raise ValueError("io fault does not accept delay_ms")
    if io.errno is None:
        raise ValueError("io fault requires errno")
    return {"errno": io.errno}


def compose_iochaos(
    fault: FaultSpec,
    *,
    namespace: str,
    name: str | None = None,
    container_names: Sequence[str] = (),
) -> dict[str, Any]:
    """Compose a Chaos Mesh ``IOChaos`` CR for a filesystem fault."""
    if fault.fault_type is not FaultType.IO_STRESS or fault.io is None:
        raise UnsupportedFaultError(
            f"fault type {fault.fault_type.value!r} has no IOChaos mapping"
        )
    cr = base_chaos_cr(
        "IOChaos", f"io-{fault.io.action}", fault, namespace=namespace, name=name
    )
    cr["spec"]["action"] = fault.io.action
    cr["spec"]["volumePath"] = fault.io.volume_path
    cr["spec"]["percent"] = fault.io.percent
    if fault.io.path_glob is not None:
        cr["spec"]["path"] = fault.io.path_glob
    cr["spec"].update(_action_fields(fault.io))
    if container_names:
        cr["spec"]["containerNames"] = list(container_names)
    return cr
