"""The deterministic recommender — pure math from utilization signals.

Beneath the LLM by design: same usage snapshot and config, same
recommendation. Every emitted change is admissible by construction — clamped
to the replica cap AND to the revert-admissible floor the engine enforces —
so the recommender can never propose a change the engine would deny.
"""

from __future__ import annotations

import math

from pydantic import BaseModel, ConfigDict

from chaosagent.capacity.signals import WorkloadUsage
from chaosagent.capacity.spec import WorkloadRef
from chaosagent.domain.policy import PolicyConfig


class Recommendation(BaseModel):
    """A bounded replica recommendation plus the rationale that produced it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    namespace: str
    workload: WorkloadRef
    current_replicas: int
    desired_replicas: int
    target_utilization: float
    #: The signal the sizing used: the max of the available avg utilizations
    #: (the binding constraint). None when no utilization signal was available.
    observed_utilization: float | None
    #: Rule ids of the caps that bounded the raw proportional size, in the
    #: order they were applied.
    clamps: tuple[str, ...] = ()
    #: Reproducible inputs and decisions, rendered into reports verbatim.
    rationale: tuple[str, ...] = ()
    #: Signed monthly cost delta; populated when an OpenCost client is wired,
    #: and advisory either way — a cost number can never raise a cap.
    estimated_monthly_delta: float | None = None


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def recommend_replicas(
    usage: WorkloadUsage, *, target_utilization: float = 0.6, config: PolicyConfig
) -> Recommendation:
    """Proportional sizing toward the target utilization, clamped to the caps.

    ``ceil(current * observed / target)``, where ``observed`` is the max of the
    available CPU/memory averages, then clamped to the replica cap (upscales)
    and the revert-admissible floor (downscales; also implies min 1).
    """
    current = usage.current_replicas
    rationale: list[str] = [
        f"signals over {usage.lookback_minutes}m: "
        f"cpu avg {_pct(usage.cpu_avg)} p95 {_pct(usage.cpu_p95)}, "
        f"memory avg {_pct(usage.memory_avg)} p95 {_pct(usage.memory_p95)}"
    ]
    if usage.vpa:
        targets = ", ".join(
            f"{rec.container}: cpu={rec.cpu or 'n/a'} memory={rec.memory or 'n/a'}"
            for rec in usage.vpa
        )
        rationale.append(
            f"VPA recommends requests {targets} (recommend-only; vertical writes "
            "are a Phase 4 decision)"
        )
    candidates = [v for v in (usage.cpu_avg, usage.memory_avg) if v is not None]
    observed = max(candidates) if candidates else None
    clamps: list[str] = []

    if current == 0:
        desired = 0
        rationale.append(
            "workload is scaled to zero; autonomous scale-from-zero is out of scope"
        )
    elif observed is None:
        desired = current
        rationale.append("no utilization signal available; keeping the current count")
    else:
        # round() before ceil/floor guards against float dust (0.9/0.6 is not
        # exactly 1.5) flipping a boundary case to the wrong integer.
        desired = math.ceil(round(current * observed / target_utilization, 9))
        rationale.append(
            f"observed {_pct(observed)} vs target {_pct(target_utilization)}: "
            f"proportional size ceil({current} * {observed:.2f} / "
            f"{target_utilization:.2f}) = {desired}"
        )
        cap = config.max_replica_pct_change
        ceiling = math.floor(round(current * (1 + cap), 9))
        floor = math.ceil(round(current / (1 + cap), 9))
        if desired > ceiling:
            rationale.append(f"clamped {desired} -> {ceiling} by replica-cap (+/-{cap:.0%})")
            clamps.append("replica-cap")
            desired = ceiling
        if desired < floor:
            rationale.append(
                f"clamped {desired} -> {floor} by revert-admissible "
                f"(the revert may not exceed +{cap:.0%})"
            )
            clamps.append("revert-admissible")
            desired = floor

    return Recommendation(
        namespace=usage.namespace,
        workload=usage.workload,
        current_replicas=current,
        desired_replicas=desired,
        target_utilization=target_utilization,
        observed_utilization=observed,
        clamps=tuple(clamps),
        rationale=tuple(rationale),
    )
