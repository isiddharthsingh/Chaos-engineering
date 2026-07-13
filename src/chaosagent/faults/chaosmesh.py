"""Compose Chaos Mesh custom resources from engine-agnostic fault specs.

Pure functions: FaultSpec in, CR dict out, no I/O. The composer is Kyverno-
compatible by construction — it only ever emits ``mode: fixed-percent`` (never
``all``) and always sets ``spec.duration``, so any fault the policy engine can
pass (ratio <= 0.5, duration <= 900s) yields a CR the admission bundle admits.
"""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from typing import Any

from chaosagent.domain.actions import FaultSpec
from chaosagent.domain.enums import FaultType

API_VERSION = "chaos-mesh.org/v1alpha1"
MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
MANAGED_BY_VALUE = "chaosagent"

# FaultType -> PodChaos spec.action. Everything else is Phase 2 (NetworkChaos,
# StressChaos, ...) and refused here so intent can never silently degrade.
_POD_ACTIONS: dict[FaultType, str] = {
    FaultType.POD_KILL: "pod-kill",
    FaultType.POD_FAILURE: "pod-failure",
    FaultType.CONTAINER_KILL: "container-kill",
}


class UnsupportedFaultError(ValueError):
    """Raised for fault types this composer cannot express yet."""


def compose_podchaos(
    fault: FaultSpec,
    *,
    namespace: str,
    name: str | None = None,
    container_names: Sequence[str] = (),
) -> dict[str, Any]:
    """Compose a Chaos Mesh ``PodChaos`` CR for a pod-family fault.

    The generated name is a DNS label (``chaosagent-<action>-<8hex>``) and the
    CR carries the managed-by label so the executor can find and count runs.
    """
    action = _POD_ACTIONS.get(fault.fault_type)
    if action is None:
        raise UnsupportedFaultError(
            f"fault type {fault.fault_type.value!r} has no PodChaos mapping; "
            "only pod faults are supported in Phase 1"
        )
    if fault.fault_type is FaultType.CONTAINER_KILL and not container_names:
        raise ValueError("container_kill requires at least one container name")
    if not fault.selector:
        # An empty labelSelectors matches EVERY pod in the namespace, so the
        # blast-radius cap would silently apply to unrelated workloads. Refuse.
        raise ValueError(
            "fault selector is empty; a PodChaos with no labelSelectors targets "
            "every pod in the namespace. Provide a selector to bound the blast radius."
        )
    spec: dict[str, Any] = {
        "action": action,
        "mode": "fixed-percent",
        # Floor at 1: a sub-1% ratio would round to "0", producing a fault that
        # selects no pods yet still "succeeds" — a run that scores as resilient
        # without any fault having occurred.
        "value": str(max(1, round(fault.ratio * 100))),
        "duration": f"{fault.duration_seconds}s",
        "selector": {
            "namespaces": [namespace],
            "labelSelectors": dict(fault.selector),
        },
    }
    if container_names:
        spec["containerNames"] = list(container_names)
    return {
        "apiVersion": API_VERSION,
        "kind": "PodChaos",
        "metadata": {
            "name": name or f"chaosagent-{action}-{secrets.token_hex(4)}",
            "namespace": namespace,
            "labels": {MANAGED_BY_LABEL: MANAGED_BY_VALUE},
        },
        "spec": spec,
    }
