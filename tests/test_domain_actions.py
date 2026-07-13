"""ProposedAction / ReplicaChange / FaultSpec validation."""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from chaosagent.domain.actions import (
    FaultSpec,
    NetworkFault,
    ProposedAction,
    ReplicaChange,
    StressFault,
)
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


# -- Per-family fault parameter blocks (Phase 2) ---------------------------------

#: A minimal valid payload for each parameter block.
_BLOCKS: dict[str, dict[str, object]] = {
    "network": {"action": "delay", "latency_ms": 100},
    "stress": {"cpu_workers": 1, "cpu_load_percent": 50},
    "io": {"action": "latency", "volume_path": "/data", "delay_ms": 100},
    "dns": {"action": "error", "patterns": ("example.com",)},
    "time": {"time_offset": "-10m"},
}

_FAMILY_CASES = [
    (FaultType.NETWORK_LATENCY, "network"),
    (FaultType.NETWORK_LOSS, "network"),
    (FaultType.NETWORK_PARTITION, "network"),
    (FaultType.CPU_STRESS, "stress"),
    (FaultType.MEMORY_STRESS, "stress"),
    (FaultType.IO_STRESS, "io"),
    (FaultType.DNS_CHAOS, "dns"),
    (FaultType.TIME_SKEW, "time"),
]


@pytest.mark.parametrize(("fault_type", "block"), _FAMILY_CASES)
def test_fault_family_requires_its_block(fault_type: FaultType, block: str) -> None:
    with pytest.raises(ValidationError, match=f"requires the '{block}' parameter block"):
        FaultSpec(fault_type=fault_type, duration_seconds=30)
    spec = FaultSpec.model_validate(
        {"fault_type": fault_type, "duration_seconds": 30, block: _BLOCKS[block]}
    )
    assert getattr(spec, block) is not None


@pytest.mark.parametrize(("fault_type", "block"), _FAMILY_CASES)
def test_fault_family_rejects_foreign_block(fault_type: FaultType, block: str) -> None:
    foreign = "dns" if block != "dns" else "time"
    payload: dict[str, object] = {
        "fault_type": fault_type,
        "duration_seconds": 30,
        block: _BLOCKS[block],
        foreign: _BLOCKS[foreign],
    }
    with pytest.raises(ValidationError, match=f"does not accept the '{foreign}' parameter block"):
        FaultSpec.model_validate(payload)


def test_pod_family_takes_no_parameter_block() -> None:
    # Phase-1 pod specs are unchanged: no block required, none accepted.
    spec = FaultSpec(fault_type=FaultType.POD_KILL, duration_seconds=30)
    assert spec.network is None and spec.stress is None
    with pytest.raises(ValidationError, match="does not accept the 'network' parameter block"):
        FaultSpec.model_validate(
            {"fault_type": "pod_kill", "duration_seconds": 30, "network": _BLOCKS["network"]}
        )


def test_parameter_blocks_are_frozen_and_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        NetworkFault.model_validate({"action": "delay", "latency_ms": 100, "bogus": 1})
    block = NetworkFault(action="delay", latency_ms=100)
    with pytest.raises(ValidationError):
        block.action = "loss"


def test_network_percent_fields_are_bounded() -> None:
    with pytest.raises(ValidationError):
        NetworkFault(action="loss", loss_percent=150.0)
    with pytest.raises(ValidationError):
        NetworkFault(action="delay", latency_ms=100, correlation_percent=-1.0)


def test_no_op_fault_values_are_rejected() -> None:
    # loss_percent=0 / cpu_load_percent=0 would compose faults that do nothing
    # yet score as resilient — the same silent-no-op the value floor guards.
    with pytest.raises(ValidationError):
        NetworkFault(action="loss", loss_percent=0.0)
    with pytest.raises(ValidationError):
        StressFault(cpu_workers=1, cpu_load_percent=0)
