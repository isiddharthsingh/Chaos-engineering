"""Enumerations shared across the platform."""

from __future__ import annotations

from enum import StrEnum


class EnvironmentTier(StrEnum):
    """Sensitivity tier of a target. The autonomy boundary is drawn here.

    dev/staging are fully autonomous (gated only by policy + RBAC + auto-abort).
    prod is excluded by a separate credential boundary — the deterministic policy
    engine also refuses any destructive action against it as defense in depth.
    """

    DEV = "dev"
    STAGING = "staging"
    PROD = "prod"

    @property
    def is_autonomous(self) -> bool:
        """True where agents may act without per-run human approval."""
        return self in (EnvironmentTier.DEV, EnvironmentTier.STAGING)


class TargetKind(StrEnum):
    """What kind of infrastructure a registered target represents."""

    KUBERNETES = "kubernetes"
    CLOUD_ACCOUNT = "cloud_account"
    VM_GROUP = "vm_group"


class ActionType(StrEnum):
    """What an agent proposes to do against a target.

    ``OBSERVE`` is read-only and permitted everywhere; every other action is
    state-changing and subject to the full policy bundle.
    """

    OBSERVE = "observe"
    INJECT_FAULT = "inject_fault"
    APPLY_LOAD = "apply_load"
    SCALE_WORKLOAD = "scale_workload"
    RIGHT_SIZE = "right_size"

    @property
    def is_state_changing(self) -> bool:
        return self is not ActionType.OBSERVE

    @property
    def is_chaos(self) -> bool:
        return self in (ActionType.INJECT_FAULT, ActionType.APPLY_LOAD)


class FaultType(StrEnum):
    """Fault families the platform can parameterize on an engine.

    Maps onto Chaos Mesh CRDs (primary) and LitmusChaos experiments. We never
    build fault injection ourselves — the planner emits a spec that an engine
    executes and self-reverts.
    """

    POD_KILL = "pod_kill"
    POD_FAILURE = "pod_failure"
    CONTAINER_KILL = "container_kill"
    NETWORK_LATENCY = "network_latency"
    NETWORK_LOSS = "network_loss"
    NETWORK_PARTITION = "network_partition"
    CPU_STRESS = "cpu_stress"
    MEMORY_STRESS = "memory_stress"
    IO_STRESS = "io_stress"
    DNS_CHAOS = "dns_chaos"
    TIME_SKEW = "time_skew"
