"""ProposedAction / ReplicaChange / FaultSpec validation."""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from chaosagent.domain.actions import FaultSpec, ProposedAction, ReplicaChange
from chaosagent.domain.enums import ActionType, EnvironmentTier, FaultType


def test_replica_change_pct() -> None:
    assert ReplicaChange(current=4, desired=6).pct_change == pytest.approx(0.5)
    assert ReplicaChange(current=4, desired=2).pct_change == pytest.approx(-0.5)


def test_scale_from_zero_is_unbounded() -> None:
    assert math.isinf(ReplicaChange(current=0, desired=3).pct_change)
    assert ReplicaChange(current=0, desired=0).pct_change == 0.0


def test_fault_ratio_bounds() -> None:
    with pytest.raises(ValidationError):
        FaultSpec(fault_type=FaultType.POD_KILL, ratio=1.5, duration_seconds=30)
    with pytest.raises(ValidationError):
        FaultSpec(fault_type=FaultType.POD_KILL, ratio=0.0, duration_seconds=30)


def test_inject_fault_requires_fault_spec() -> None:
    with pytest.raises(ValidationError, match="requires a fault spec"):
        ProposedAction(
            action_type=ActionType.INJECT_FAULT,
            target_id="c",
            environment=EnvironmentTier.DEV,
            namespace="payments",
        )


def test_scale_requires_replica_change() -> None:
    with pytest.raises(ValidationError, match="requires a replica_change"):
        ProposedAction(
            action_type=ActionType.SCALE_WORKLOAD,
            target_id="c",
            environment=EnvironmentTier.DEV,
            namespace="payments",
        )


def test_action_type_predicates() -> None:
    assert not ActionType.OBSERVE.is_state_changing
    assert ActionType.INJECT_FAULT.is_state_changing
    assert ActionType.INJECT_FAULT.is_chaos
    assert ActionType.APPLY_LOAD.is_chaos
    assert not ActionType.SCALE_WORKLOAD.is_chaos
