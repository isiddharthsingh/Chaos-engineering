"""Composer: FaultSpec -> Chaos Mesh IOChaos CR, Kyverno-compatible by construction."""

from __future__ import annotations

import pytest

from chaosagent.domain.actions import FaultSpec
from chaosagent.domain.enums import FaultType
from chaosagent.faults import UnsupportedFaultError, compose_iochaos


def _fault(**overrides: object) -> FaultSpec:
    base: dict[str, object] = {
        "fault_type": FaultType.IO_STRESS,
        "selector": {"app": "cartservice"},
        "ratio": 0.34,
        "duration_seconds": 60,
        "io": {"action": "latency", "volume_path": "/data", "delay_ms": 100},
    }
    base.update(overrides)
    return FaultSpec.model_validate(base)


def test_io_latency_composes_full_cr() -> None:
    cr = compose_iochaos(_fault(), namespace="boutique")
    assert cr["apiVersion"] == "chaos-mesh.org/v1alpha1"
    assert cr["kind"] == "IOChaos"
    assert cr["metadata"]["labels"] == {"app.kubernetes.io/managed-by": "chaosagent"}
    assert cr["spec"]["action"] == "latency"
    assert cr["spec"]["mode"] == "fixed-percent"
    assert cr["spec"]["value"] == "34"
    assert cr["spec"]["duration"] == "60s"
    assert cr["spec"]["volumePath"] == "/data"
    assert cr["spec"]["delay"] == "100ms"
    assert cr["spec"]["percent"] == 100
    assert "errno" not in cr["spec"]


def test_io_fault_maps_errno_and_path_glob() -> None:
    fault = _fault(
        io={
            "action": "fault",
            "volume_path": "/data",
            "path_glob": "/data/**/*.db",
            "errno": 5,
            "percent": 50,
        }
    )
    cr = compose_iochaos(fault, namespace="boutique")
    assert cr["spec"]["action"] == "fault"
    assert cr["spec"]["errno"] == 5
    assert cr["spec"]["path"] == "/data/**/*.db"
    assert cr["spec"]["percent"] == 50
    assert "delay" not in cr["spec"]


def test_io_latency_requires_delay_ms() -> None:
    fault = _fault(io={"action": "latency", "volume_path": "/data"})
    with pytest.raises(ValueError, match="delay_ms"):
        compose_iochaos(fault, namespace="boutique")


def test_io_fault_requires_errno() -> None:
    fault = _fault(io={"action": "fault", "volume_path": "/data"})
    with pytest.raises(ValueError, match="errno"):
        compose_iochaos(fault, namespace="boutique")


def test_foreign_action_params_are_refused() -> None:
    fault = _fault(io={"action": "latency", "volume_path": "/data", "delay_ms": 5, "errno": 5})
    with pytest.raises(ValueError, match="errno"):
        compose_iochaos(fault, namespace="boutique")
    fault = _fault(io={"action": "fault", "volume_path": "/data", "errno": 5, "delay_ms": 5})
    with pytest.raises(ValueError, match="delay_ms"):
        compose_iochaos(fault, namespace="boutique")


def test_container_names_are_set_when_given() -> None:
    cr = compose_iochaos(_fault(), namespace="boutique", container_names=["server"])
    assert cr["spec"]["containerNames"] == ["server"]


def test_non_io_fault_raises() -> None:
    pod = FaultSpec(fault_type=FaultType.POD_KILL, selector={"app": "x"}, duration_seconds=30)
    with pytest.raises(UnsupportedFaultError):
        compose_iochaos(pod, namespace="boutique")


def test_empty_selector_is_refused() -> None:
    with pytest.raises(ValueError, match="selector is empty"):
        compose_iochaos(_fault(selector={}), namespace="boutique")


@pytest.mark.parametrize("ratio", [0.001, 0.34, 0.5])
@pytest.mark.parametrize("duration", [1, 300, 900])
def test_policy_passable_faults_pass_kyverno_caps(ratio: float, duration: int) -> None:
    cr = compose_iochaos(_fault(ratio=ratio, duration_seconds=duration), namespace="boutique")
    assert cr["spec"]["mode"] != "all"
    assert 1 <= int(cr["spec"]["value"]) <= 50
    seconds = int(cr["spec"]["duration"].removesuffix("s"))
    assert 0 < seconds <= 900
