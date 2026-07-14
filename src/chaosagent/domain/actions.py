"""Proposed-action models — what an agent asks the policy engine to allow."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from chaosagent.domain.enums import ActionType, EnvironmentTier, FaultType, TargetKind
from chaosagent.domain.targets import _SLUG_RE


class WorkloadRef(BaseModel):
    """The scalable workload a capacity action targets.

    Lives in the domain layer (not chaosagent.capacity) so the executor can
    import it without a circular execute -> capacity -> execute chain.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["deployment", "statefulset"]
    name: str

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        if not _SLUG_RE.match(value):
            raise ValueError(
                f"workload name {value!r} must be a DNS label (lowercase alphanumeric and '-')"
            )
        return value


class ReplicaChange(BaseModel):
    """A requested change to a workload's replica count (capacity actions)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    current: int = Field(ge=0)
    desired: int = Field(ge=0)

    @property
    def pct_change(self) -> float:
        """Signed fractional change. Scaling up from 0 is treated as unbounded."""
        if self.current == 0:
            return float("inf") if self.desired > 0 else 0.0
        return (self.desired - self.current) / self.current


class HpaBoundsChange(BaseModel):
    """A requested change to an HPA's min/max bounds (capacity actions).

    Each bound reuses :class:`ReplicaChange` so the same replica-cap math the
    engine applies to direct scales applies to both autoscaler bounds.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_replicas: ReplicaChange
    max_replicas: ReplicaChange


class NetworkFault(BaseModel):
    """Parameters for the network fault family (Chaos Mesh ``NetworkChaos``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: Literal["delay", "loss", "partition"]
    #: delay -> spec.delay.latency ("<n>ms").
    latency_ms: int | None = Field(default=None, gt=0)
    #: delay -> spec.delay.jitter ("<n>ms").
    jitter_ms: int = Field(default=0, ge=0)
    #: loss -> spec.loss.loss (percentage; 0 would be a no-op fault, refused).
    loss_percent: float | None = Field(default=None, gt=0.0, le=100.0)
    #: delay/loss -> spec.{delay,loss}.correlation (percentage, "0"-"100").
    correlation_percent: float = Field(default=0.0, ge=0.0, le=100.0)
    #: -> spec.direction (traffic direction relative to the selected pods).
    direction: Literal["to", "from", "both"] = "to"


class StressFault(BaseModel):
    """Parameters for the stress fault family (Chaos Mesh ``StressChaos``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: cpu_stress -> spec.stressors.cpu.workers.
    cpu_workers: int | None = Field(default=None, gt=0)
    #: cpu_stress -> spec.stressors.cpu.load (1-100 per worker; 0 is a no-op).
    cpu_load_percent: int | None = Field(default=None, ge=1, le=100)
    #: memory_stress -> spec.stressors.memory.workers.
    memory_workers: int | None = Field(default=None, gt=0)
    #: memory_stress -> spec.stressors.memory.size (e.g. "256MB").
    memory_size: str | None = Field(default=None, min_length=1)


class IOFault(BaseModel):
    """Parameters for the filesystem fault family (Chaos Mesh ``IOChaos``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: Literal["latency", "fault"]
    #: -> spec.volumePath (mount point of the injected volume; required).
    volume_path: str = Field(min_length=1)
    #: -> spec.path (glob of affected files; defaults to everything under volumePath).
    path_glob: str | None = Field(default=None, min_length=1)
    #: latency -> spec.delay ("<n>ms").
    delay_ms: int | None = Field(default=None, gt=0)
    #: fault -> spec.errno (POSIX errno returned to the workload).
    errno: int | None = Field(default=None, gt=0)
    #: -> spec.percent (probability an op is affected, 1-100).
    percent: int = Field(default=100, ge=1, le=100)


class DNSFault(BaseModel):
    """Parameters for the DNS fault family (Chaos Mesh ``DNSChaos``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: Literal["error", "random"]
    #: -> spec.patterns (hostname globs the fault applies to).
    patterns: tuple[str, ...] = ()


class TimeFault(BaseModel):
    """Parameters for the clock-skew fault family (Chaos Mesh ``TimeChaos``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: -> spec.timeOffset (e.g. "-10m", "100ns").
    time_offset: str = Field(min_length=1)
    #: -> spec.clockIds (defaults to CLOCK_REALTIME on the engine side).
    clock_ids: tuple[str, ...] = ()


#: FaultType -> the parameter block it requires (None for the pod family, which
#: is fully described by the shared selector/ratio/duration fields).
_PARAM_BLOCKS: dict[FaultType, str | None] = {
    FaultType.POD_KILL: None,
    FaultType.POD_FAILURE: None,
    FaultType.CONTAINER_KILL: None,
    FaultType.NETWORK_LATENCY: "network",
    FaultType.NETWORK_LOSS: "network",
    FaultType.NETWORK_PARTITION: "network",
    FaultType.CPU_STRESS: "stress",
    FaultType.MEMORY_STRESS: "stress",
    FaultType.IO_STRESS: "io",
    FaultType.DNS_CHAOS: "dns",
    FaultType.TIME_SKEW: "time",
}

_BLOCK_FIELDS = ("network", "stress", "io", "dns", "time")


class FaultSpec(BaseModel):
    """Parameters for a fault the planner wants an engine to inject.

    This is engine-agnostic intent; a composer turns it into a Chaos Mesh CRD or
    a Litmus experiment. Blast-radius fields exist so the policy engine can cap
    them without understanding any specific engine. Family-specific knobs live in
    exactly one typed parameter block, enforced to match ``fault_type``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    fault_type: FaultType
    #: Label selector identifying the blast radius (e.g. {"app": "payments"}).
    selector: dict[str, str] = Field(default_factory=dict)
    #: Fraction of matched pods to affect (0 < x <= 1) or an absolute count via mode.
    ratio: float = Field(default=1.0, gt=0.0, le=1.0)
    #: How long the fault runs before the engine self-reverts.
    duration_seconds: int = Field(gt=0)
    #: Container names for container-scoped faults (e.g. container_kill); the
    #: composer requires at least one for those and ignores it otherwise.
    container_names: tuple[str, ...] = ()
    #: Per-family parameters — exactly the block matching fault_type must be set.
    network: NetworkFault | None = None
    stress: StressFault | None = None
    io: IOFault | None = None
    dns: DNSFault | None = None
    time: TimeFault | None = None

    @model_validator(mode="after")
    def _block_matches_fault_type(self) -> FaultSpec:
        required = _PARAM_BLOCKS[self.fault_type]
        for field in _BLOCK_FIELDS:
            present = getattr(self, field) is not None
            if field == required and not present:
                raise ValueError(
                    f"{self.fault_type.value} requires the '{field}' parameter block"
                )
            if field != required and present:
                raise ValueError(
                    f"{self.fault_type.value} does not accept the '{field}' parameter block"
                )
        return self


class ProposedAction(BaseModel):
    """A single action an agent proposes, plus the resolved context needed to
    judge it. The agent fills this in *before* touching the target; the policy
    engine's verdict is the pre-flight self-check.
    """

    model_config = ConfigDict(extra="forbid")

    action_type: ActionType
    target_id: str = Field(min_length=1)
    #: Resolved from the target — never taken on trust from the LLM. Use
    #: ``chaosagent.resolve.resolve_action`` to bind these from the registry.
    environment: EnvironmentTier
    #: Kind of the target, resolved from the registry. None means "not resolved";
    #: rules that only apply to Kubernetes targets key off this.
    target_kind: TargetKind | None = None
    #: The target's declared namespace scope, resolved from the registry. Empty
    #: means "unrestricted / not resolved"; a non-empty tuple is enforced.
    target_allowed_namespaces: tuple[str, ...] = ()
    #: For K8s actions, the namespace the action lands in.
    namespace: str | None = None
    #: Whether that namespace carries the ``chaos-enabled=true`` label. Resolved
    #: from the live cluster by the executor/observer, or supplied in tests.
    namespace_chaos_enabled: bool = False
    #: The workload a capacity action targets. The scale executor refuses any
    #: write whose target differs from the bound action's workload.
    workload: WorkloadRef | None = None
    #: Present for capacity actions that scale a workload directly.
    replica_change: ReplicaChange | None = None
    #: Present for capacity actions that move an HPA's min/max bounds.
    hpa_bounds: HpaBoundsChange | None = None
    #: Present for fault injection.
    fault: FaultSpec | None = None
    #: Bounded lifetime for the whole action; None means "no TTL declared".
    ttl_seconds: int | None = Field(default=None, gt=0)
    #: True if an alert/incident is currently firing for this target.
    incident_active: bool = False
    #: How many experiments are already running against this target.
    concurrent_experiments: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _shape_matches_action(self) -> ProposedAction:
        if self.action_type is ActionType.INJECT_FAULT and self.fault is None:
            raise ValueError("inject_fault action requires a fault spec")
        needs_replicas = self.action_type in (ActionType.SCALE_WORKLOAD, ActionType.RIGHT_SIZE)
        if needs_replicas and self.replica_change is None and self.hpa_bounds is None:
            raise ValueError(
                f"{self.action_type.value} action requires a replica_change or hpa_bounds"
            )
        return self
