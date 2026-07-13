"""Composer: FaultSpec -> Chaos Mesh StressChaos CR, Kyverno-compatible by construction."""

from __future__ import annotations

import pytest

from chaosagent.domain.actions import FaultSpec
from chaosagent.domain.enums import FaultType
from chaosagent.faults import UnsupportedFaultError, compose_stresschaos


def _fault(**overrides: object) -> FaultSpec:
    base: dict[str, object] = {
        "fault_type": FaultType.CPU_STRESS,
        "selector": {"app": "cartservice"},
        "ratio": 0.34,
        "duration_seconds": 60,
        "stress": {"cpu_workers": 2, "cpu_load_percent": 50},
    }
    base.update(overrides)
    return FaultSpec.model_validate(base)


def test_cpu_stress_composes_full_cr() -> None:
    cr = compose_stresschaos(_fault(), namespace="boutique")
    assert cr["apiVersion"] == "chaos-mesh.org/v1alpha1"
    assert cr["kind"] == "StressChaos"
    assert cr["metadata"]["namespace"] == "boutique"
    assert cr["metadata"]["labels"] == {"app.kubernetes.io/managed-by": "chaosagent"}
    assert cr["spec"]["mode"] == "fixed-percent"
    assert cr["spec"]["value"] == "34"
    assert cr["spec"]["duration"] == "60s"
    assert cr["spec"]["selector"] == {
        "namespaces": ["boutique"],
        "labelSelectors": {"app": "cartservice"},
    }
    assert cr["spec"]["stressors"] == {"cpu": {"workers": 2, "load": 50}}


def test_cpu_load_is_optional() -> None:
    cr = compose_stresschaos(_fault(stress={"cpu_workers": 1}), namespace="boutique")
    assert cr["spec"]["stressors"] == {"cpu": {"workers": 1}}


def test_memory_stress_emits_memory_stressor() -> None:
    fault = _fault(
        fault_type=FaultType.MEMORY_STRESS,
        stress={"memory_workers": 1, "memory_size": "256MB"},
    )
    cr = compose_stresschaos(fault, namespace="boutique")
    assert cr["spec"]["stressors"] == {"memory": {"workers": 1, "size": "256MB"}}


def test_cpu_stress_requires_cpu_workers() -> None:
    fault = _fault(stress={"cpu_load_percent": 50})
    with pytest.raises(ValueError, match="cpu_workers"):
        compose_stresschaos(fault, namespace="boutique")


def test_memory_stress_requires_memory_workers() -> None:
    fault = _fault(fault_type=FaultType.MEMORY_STRESS, stress={"memory_size": "256MB"})
    with pytest.raises(ValueError, match="memory_workers"):
        compose_stresschaos(fault, namespace="boutique")


def test_foreign_family_params_are_refused() -> None:
    # cpu_stress intent carrying memory knobs (or vice versa) is a planner bug;
    # refuse instead of silently dropping half the request.
    fault = _fault(stress={"cpu_workers": 1, "memory_workers": 1})
    with pytest.raises(ValueError, match="memory"):
        compose_stresschaos(fault, namespace="boutique")
    fault = _fault(
        fault_type=FaultType.MEMORY_STRESS,
        stress={"memory_workers": 1, "cpu_load_percent": 10},
    )
    with pytest.raises(ValueError, match="cpu"):
        compose_stresschaos(fault, namespace="boutique")


def test_container_names_are_set_when_given() -> None:
    cr = compose_stresschaos(_fault(), namespace="boutique", container_names=["server"])
    assert cr["spec"]["containerNames"] == ["server"]
    assert "containerNames" not in compose_stresschaos(_fault(), namespace="boutique")["spec"]


def test_non_stress_fault_raises() -> None:
    pod = FaultSpec(fault_type=FaultType.POD_KILL, selector={"app": "x"}, duration_seconds=30)
    with pytest.raises(UnsupportedFaultError):
        compose_stresschaos(pod, namespace="boutique")


def test_empty_selector_is_refused() -> None:
    with pytest.raises(ValueError, match="selector is empty"):
        compose_stresschaos(_fault(selector={}), namespace="boutique")


@pytest.mark.parametrize("ratio", [0.001, 0.01, 0.34, 0.5])
@pytest.mark.parametrize("duration", [1, 300, 900])
def test_policy_passable_faults_pass_kyverno_caps(ratio: float, duration: int) -> None:
    cr = compose_stresschaos(
        _fault(ratio=ratio, duration_seconds=duration), namespace="boutique"
    )
    assert cr["spec"]["mode"] != "all"
    assert 1 <= int(cr["spec"]["value"]) <= 50
    seconds = int(cr["spec"]["duration"].removesuffix("s"))
    assert 0 < seconds <= 900
