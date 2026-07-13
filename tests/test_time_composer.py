"""Composer: FaultSpec -> Chaos Mesh TimeChaos CR, Kyverno-compatible by construction."""

from __future__ import annotations

import pytest

from chaosagent.domain.actions import FaultSpec
from chaosagent.domain.enums import FaultType
from chaosagent.faults import UnsupportedFaultError, compose_timechaos


def _fault(**overrides: object) -> FaultSpec:
    base: dict[str, object] = {
        "fault_type": FaultType.TIME_SKEW,
        "selector": {"app": "cartservice"},
        "ratio": 0.34,
        "duration_seconds": 60,
        "time": {"time_offset": "-10m"},
    }
    base.update(overrides)
    return FaultSpec.model_validate(base)


def test_time_skew_composes_full_cr() -> None:
    cr = compose_timechaos(_fault(), namespace="boutique")
    assert cr["apiVersion"] == "chaos-mesh.org/v1alpha1"
    assert cr["kind"] == "TimeChaos"
    assert cr["metadata"]["labels"] == {"app.kubernetes.io/managed-by": "chaosagent"}
    assert cr["spec"]["mode"] == "fixed-percent"
    assert cr["spec"]["value"] == "34"
    assert cr["spec"]["duration"] == "60s"
    assert cr["spec"]["timeOffset"] == "-10m"
    assert "clockIds" not in cr["spec"]


def test_clock_ids_are_set_when_given() -> None:
    fault = _fault(time={"time_offset": "-10m", "clock_ids": ("CLOCK_REALTIME",)})
    cr = compose_timechaos(fault, namespace="boutique")
    assert cr["spec"]["clockIds"] == ["CLOCK_REALTIME"]


def test_container_names_are_set_when_given() -> None:
    cr = compose_timechaos(_fault(), namespace="boutique", container_names=["server"])
    assert cr["spec"]["containerNames"] == ["server"]


def test_non_time_fault_raises() -> None:
    pod = FaultSpec(fault_type=FaultType.POD_KILL, selector={"app": "x"}, duration_seconds=30)
    with pytest.raises(UnsupportedFaultError):
        compose_timechaos(pod, namespace="boutique")


def test_empty_selector_is_refused() -> None:
    with pytest.raises(ValueError, match="selector is empty"):
        compose_timechaos(_fault(selector={}), namespace="boutique")


@pytest.mark.parametrize("ratio", [0.001, 0.34, 0.5])
@pytest.mark.parametrize("duration", [1, 300, 900])
def test_policy_passable_faults_pass_kyverno_caps(ratio: float, duration: int) -> None:
    cr = compose_timechaos(_fault(ratio=ratio, duration_seconds=duration), namespace="boutique")
    assert cr["spec"]["mode"] != "all"
    assert 1 <= int(cr["spec"]["value"]) <= 50
    seconds = int(cr["spec"]["duration"].removesuffix("s"))
    assert 0 < seconds <= 900
