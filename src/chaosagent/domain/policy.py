"""Policy decision types and the tunable policy configuration."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class Violation(BaseModel):
    """One reason an action was denied. Rule ids match the Kyverno policy names
    so a Python pre-flight denial and a server-side denial read identically.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    rule: str = Field(description="Stable rule id, e.g. 'env-scope' or 'replica-cap'.")
    message: str


class PolicyDecision(BaseModel):
    """The verdict of the deterministic pre-flight check."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed: bool
    violations: tuple[Violation, ...] = ()

    @classmethod
    def allow(cls) -> PolicyDecision:
        return cls(allowed=True, violations=())

    @classmethod
    def deny(cls, violations: list[Violation]) -> PolicyDecision:
        return cls(allowed=False, violations=tuple(violations))

    def reason(self) -> str:
        if self.allowed:
            return "allowed"
        return "; ".join(f"[{v.rule}] {v.message}" for v in self.violations)


class PolicyConfig(BaseModel):
    """Tunable caps for the policy engine. Loaded from config so the same numbers
    can be rendered into the Kyverno bundle and asserted in the safety tests.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Maximum absolute fractional replica change a capacity action may request.
    max_replica_pct_change: float = Field(default=0.5, gt=0.0)
    #: Longest lifetime any single fault/experiment may declare.
    max_ttl_seconds: int = Field(default=3600, gt=0)
    #: A fault must self-revert within this window (defense against runaway CRs).
    max_fault_duration_seconds: int = Field(default=900, gt=0)
    #: Largest blast-radius fraction a single fault may target.
    max_fault_ratio: float = Field(default=0.5, gt=0.0, le=1.0)
    #: Concurrency ceiling: how many experiments may run against one target.
    max_concurrent_experiments: int = Field(default=1, ge=1)
