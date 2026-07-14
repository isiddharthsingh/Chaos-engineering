"""Capacity — the second action family through the guardrail spine.

The agent observes utilization, recommends a bounded replica change, applies it
through the same resolve -> engine -> dry-run -> executor path as chaos, and
auto-reverts deterministically on breach. Cost is a signal here, never an
authority.
"""

from chaosagent.capacity.lifecycle import (
    CapacityDeps,
    CapacityRun,
    CapacityState,
    CapacityTransition,
    run_capacity_lifecycle,
)
from chaosagent.capacity.recommend import Recommendation, recommend_replicas
from chaosagent.capacity.report import (
    CapacityOutcome,
    CapacityReport,
    build_capacity_report,
    render_capacity_text,
)
from chaosagent.capacity.signals import SignalError, WorkloadUsage, fetch_usage
from chaosagent.capacity.spec import CapacitySpec, WorkloadRef

__all__ = [
    "CapacityDeps",
    "CapacityOutcome",
    "CapacityReport",
    "CapacityRun",
    "CapacitySpec",
    "CapacityState",
    "CapacityTransition",
    "Recommendation",
    "SignalError",
    "WorkloadRef",
    "WorkloadUsage",
    "build_capacity_report",
    "fetch_usage",
    "recommend_replicas",
    "render_capacity_text",
    "run_capacity_lifecycle",
]
