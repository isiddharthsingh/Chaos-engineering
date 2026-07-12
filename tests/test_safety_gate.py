"""Release-gating autonomous-safety invariants.

Per the plan, this test is what makes "fully autonomous" defensible. It asserts
the deterministic guardrails hold regardless of LLM behaviour:

  * a fault CANNOT execute outside a chaos-enabled namespace,
  * a capacity action CANNOT exceed the replica cap,
  * NO state-changing action can reach a prod target.

The observability auto-abort invariant (aborts within N seconds of a synthetic
SLO breach) is added alongside these in Phase 1 once the observe loop exists.
"""

from __future__ import annotations

import pytest

from chaosagent.config import load_policy_config
from chaosagent.domain.actions import FaultSpec, ProposedAction, ReplicaChange
from chaosagent.domain.enums import ActionType, EnvironmentTier, FaultType
from chaosagent.policy import PolicyEngine

# Uses the *shipped* config, not test defaults — this gates the real bundle.
ENGINE = PolicyEngine(config=load_policy_config())


def test_fault_outside_chaos_enabled_namespace_is_denied() -> None:
    action = ProposedAction(
        action_type=ActionType.INJECT_FAULT,
        target_id="cluster-a",
        environment=EnvironmentTier.DEV,
        namespace="default",
        namespace_chaos_enabled=False,
        fault=FaultSpec(fault_type=FaultType.POD_KILL, ratio=0.3, duration_seconds=60),
        ttl_seconds=300,
    )
    decision = ENGINE.evaluate(action)
    assert decision.allowed is False
    assert "require-chaos-namespace" in {v.rule for v in decision.violations}


@pytest.mark.parametrize("desired", [4, 5, 100])  # +100%, +150%, +2400% from 2
def test_replica_change_over_cap_is_denied(desired: int) -> None:
    action = ProposedAction(
        action_type=ActionType.SCALE_WORKLOAD,
        target_id="cluster-a",
        environment=EnvironmentTier.DEV,
        namespace="payments",
        replica_change=ReplicaChange(current=2, desired=desired),
    )
    decision = ENGINE.evaluate(action)
    assert decision.allowed is False
    assert "replica-cap" in {v.rule for v in decision.violations}


_POD_KILL = FaultSpec(fault_type=FaultType.POD_KILL, duration_seconds=60)


@pytest.mark.parametrize(
    "action_type,extra",
    [
        (
            ActionType.INJECT_FAULT,
            {"fault": _POD_KILL, "ttl_seconds": 300, "namespace_chaos_enabled": True},
        ),
        (ActionType.SCALE_WORKLOAD, {"replica_change": ReplicaChange(current=4, desired=5)}),
        (ActionType.RIGHT_SIZE, {"replica_change": ReplicaChange(current=4, desired=4)}),
        (ActionType.APPLY_LOAD, {"ttl_seconds": 300}),
    ],
)
def test_no_state_change_reaches_prod(action_type: ActionType, extra: dict[str, object]) -> None:
    action = ProposedAction(
        action_type=action_type,
        target_id="prod-cluster",
        environment=EnvironmentTier.PROD,
        namespace="payments",
        **extra,  # type: ignore[arg-type]
    )
    decision = ENGINE.evaluate(action)
    assert decision.allowed is False
    assert "env-scope" in {v.rule for v in decision.violations}


def test_engine_is_deterministic() -> None:
    action = ProposedAction(
        action_type=ActionType.INJECT_FAULT,
        target_id="cluster-a",
        environment=EnvironmentTier.DEV,
        namespace="payments",
        namespace_chaos_enabled=True,
        fault=FaultSpec(fault_type=FaultType.POD_KILL, ratio=0.3, duration_seconds=60),
        ttl_seconds=300,
    )
    first = ENGINE.evaluate(action)
    for _ in range(50):
        assert ENGINE.evaluate(action) == first
