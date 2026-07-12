"""Proposed-action models — what an agent asks the policy engine to allow."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from chaosagent.domain.enums import ActionType, EnvironmentTier, FaultType, TargetKind


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


class FaultSpec(BaseModel):
    """Parameters for a fault the planner wants an engine to inject.

    This is engine-agnostic intent; a composer turns it into a Chaos Mesh CRD or
    a Litmus experiment. Blast-radius fields exist so the policy engine can cap
    them without understanding any specific engine.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    fault_type: FaultType
    #: Label selector identifying the blast radius (e.g. {"app": "payments"}).
    selector: dict[str, str] = Field(default_factory=dict)
    #: Fraction of matched pods to affect (0 < x <= 1) or an absolute count via mode.
    ratio: float = Field(default=1.0, gt=0.0, le=1.0)
    #: How long the fault runs before the engine self-reverts.
    duration_seconds: int = Field(gt=0)


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
    #: Present for capacity actions.
    replica_change: ReplicaChange | None = None
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
        if needs_replicas and self.replica_change is None:
            raise ValueError(f"{self.action_type.value} action requires a replica_change")
        return self
