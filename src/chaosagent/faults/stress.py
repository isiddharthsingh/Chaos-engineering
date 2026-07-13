"""Compose Chaos Mesh ``StressChaos`` CRs from stress-family fault specs.

Pure functions, Kyverno-compatible by construction (via ``base_chaos_cr``).
StressChaos has no ``action`` field — the stressor kind lives under
``spec.stressors.cpu`` / ``spec.stressors.memory`` (``kubectl explain
stresschaos.spec.stressors``); ``workers`` is required by the CRD for both.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from chaosagent.domain.actions import FaultSpec, StressFault
from chaosagent.domain.enums import FaultType
from chaosagent.faults.chaosmesh import UnsupportedFaultError, base_chaos_cr


def _cpu_stressor(stress: StressFault) -> dict[str, int]:
    if stress.memory_workers is not None or stress.memory_size is not None:
        raise ValueError("cpu_stress does not accept memory stressor parameters")
    if stress.cpu_workers is None:
        raise ValueError("cpu_stress requires stress.cpu_workers")
    stressor = {"workers": stress.cpu_workers}
    if stress.cpu_load_percent is not None:
        stressor["load"] = stress.cpu_load_percent
    return stressor


def _memory_stressor(stress: StressFault) -> dict[str, Any]:
    if stress.cpu_workers is not None or stress.cpu_load_percent is not None:
        raise ValueError("memory_stress does not accept cpu stressor parameters")
    if stress.memory_workers is None:
        raise ValueError("memory_stress requires stress.memory_workers")
    stressor: dict[str, Any] = {"workers": stress.memory_workers}
    if stress.memory_size is not None:
        stressor["size"] = stress.memory_size
    return stressor


def compose_stresschaos(
    fault: FaultSpec,
    *,
    namespace: str,
    name: str | None = None,
    container_names: Sequence[str] = (),
) -> dict[str, Any]:
    """Compose a Chaos Mesh ``StressChaos`` CR for a stress-family fault."""
    if fault.fault_type not in (FaultType.CPU_STRESS, FaultType.MEMORY_STRESS) or (
        fault.stress is None
    ):
        raise UnsupportedFaultError(
            f"fault type {fault.fault_type.value!r} has no StressChaos mapping"
        )
    slug = fault.fault_type.value.replace("_", "-")
    cr = base_chaos_cr("StressChaos", slug, fault, namespace=namespace, name=name)
    if fault.fault_type is FaultType.CPU_STRESS:
        cr["spec"]["stressors"] = {"cpu": _cpu_stressor(fault.stress)}
    else:
        cr["spec"]["stressors"] = {"memory": _memory_stressor(fault.stress)}
    if container_names:
        cr["spec"]["containerNames"] = list(container_names)
    return cr
