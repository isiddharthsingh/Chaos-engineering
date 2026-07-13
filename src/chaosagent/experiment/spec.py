"""Experiment specification — one experiment, fully declared up front.

This is simultaneously the planner's output contract (its JSON schema is
embedded in the planner prompt) and the ``--spec`` file format, so the LLM path
and the LLM-free path feed the lifecycle the exact same typed object.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from chaosagent.domain.actions import FaultSpec
from chaosagent.observe.hypothesis import SteadyStateHypothesis


class ExperimentSpec(BaseModel):
    """Everything the lifecycle needs to run one experiment autonomously."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    #: The single namespace the experiment lands in (policy re-verifies scope).
    namespace: str = Field(min_length=1)
    fault: FaultSpec
    #: The steady state that must hold before, during, and after the fault.
    hypotheses: tuple[SteadyStateHypothesis, ...] = Field(min_length=1)
    #: Bound lifetime of the whole action; also the permission-gate binding TTL.
    ttl_seconds: int = Field(gt=0)
    observe_interval_seconds: float = Field(default=5.0, gt=0)
    #: How long the steady state must hold before injection is allowed.
    baseline_seconds: int = Field(default=30, ge=0)
    #: How long to keep sampling after rollback to score recovery.
    recovery_seconds: int = Field(default=60, ge=0)

    @model_validator(mode="after")
    def _validate_shape(self) -> ExperimentSpec:
        names = [hypothesis.name for hypothesis in self.hypotheses]
        if len(names) != len(set(names)):
            # Names index breach streaks and per-phase report stats; duplicates
            # would merge distinct hypotheses and mask a breach.
            raise ValueError("hypothesis names must be unique within an experiment")
        if self.ttl_seconds <= self.baseline_seconds:
            # The write binding is created at PREFLIGHT and must still be valid
            # at INJECT, which happens after the whole baseline window elapses.
            raise ValueError(
                f"ttl_seconds ({self.ttl_seconds}) must exceed baseline_seconds "
                f"({self.baseline_seconds}); otherwise the write binding expires "
                "before injection"
            )
        return self
