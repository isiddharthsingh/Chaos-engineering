"""Capacity specification — one bounded replica change, fully declared up front.

This is the ``--spec`` file format for ``chaosagent scale`` (and the contract a
future LLM capacity-planner must emit), so the deterministic path and any LLM
path feed the lifecycle the exact same typed object.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from chaosagent.domain.actions import WorkloadRef
from chaosagent.observe.hypothesis import SteadyStateHypothesis

__all__ = ["CapacitySpec", "WorkloadRef"]


class CapacitySpec(BaseModel):
    """Everything the capacity lifecycle needs to run one replica change."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    #: The single namespace the change lands in (policy re-verifies scope).
    namespace: str = Field(min_length=1)
    workload: WorkloadRef
    #: Scale-to-zero is refused at the model; the engine caps the rest.
    desired_replicas: int = Field(ge=1)
    #: Steady state that must hold before and after the change.
    hypotheses: tuple[SteadyStateHypothesis, ...] = Field(min_length=1)
    #: Binding TTL (the gate requires one even though the engine's require-ttl
    #: rule does not apply to capacity actions).
    ttl_seconds: int = Field(gt=0)
    observe_interval_seconds: float = Field(default=5.0, gt=0)
    #: How long the steady state must hold before anything is changed.
    baseline_seconds: int = Field(default=30, ge=0)
    #: Post-change window that must stay green before the change is kept.
    settle_seconds: int = Field(default=120, ge=0)

    @model_validator(mode="after")
    def _validate_shape(self) -> CapacitySpec:
        names = [hypothesis.name for hypothesis in self.hypotheses]
        if len(names) != len(set(names)):
            # Names index breach streaks and per-phase report stats; duplicates
            # would merge distinct hypotheses and mask a breach.
            raise ValueError("hypothesis names must be unique within a capacity spec")
        if self.ttl_seconds <= self.baseline_seconds + self.observe_interval_seconds:
            # The write binding is created at PREFLIGHT and must still be valid
            # at APPLY, which happens after the whole baseline window elapses —
            # and the observe loop can overshoot its deadline by up to one
            # interval (it sleeps, then samples).
            raise ValueError(
                f"ttl_seconds ({self.ttl_seconds}) must exceed baseline_seconds plus "
                f"one observe interval ({self.baseline_seconds} + "
                f"{self.observe_interval_seconds}); otherwise the write binding can "
                "expire before the change is applied"
            )
        return self
