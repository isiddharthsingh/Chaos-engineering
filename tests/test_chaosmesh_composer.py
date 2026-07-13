"""Composer: FaultSpec -> Chaos Mesh PodChaos CR, Kyverno-compatible by construction."""

from __future__ import annotations

import re

import pytest

from chaosagent.domain.actions import FaultSpec
from chaosagent.domain.enums import FaultType
from chaosagent.faults import UnsupportedFaultError, compose_podchaos

_NAME_RE = re.compile(r"\Achaosagent-[a-z-]+-[0-9a-f]{8}\Z")


def _fault(**overrides: object) -> FaultSpec:
    base: dict[str, object] = {
        "fault_type": FaultType.POD_KILL,
        "selector": {"app": "cartservice"},
        "ratio": 0.34,
        "duration_seconds": 60,
    }
    base.update(overrides)
    return FaultSpec.model_validate(base)


def test_pod_kill_composes_full_cr() -> None:
    cr = compose_podchaos(_fault(), namespace="boutique")
    assert cr["apiVersion"] == "chaos-mesh.org/v1alpha1"
    assert cr["kind"] == "PodChaos"
    assert cr["metadata"]["namespace"] == "boutique"
    assert cr["metadata"]["labels"] == {"app.kubernetes.io/managed-by": "chaosagent"}
    assert cr["spec"]["action"] == "pod-kill"
    assert cr["spec"]["mode"] == "fixed-percent"
    assert cr["spec"]["value"] == "34"
    assert cr["spec"]["duration"] == "60s"
    assert cr["spec"]["selector"] == {
        "namespaces": ["boutique"],
        "labelSelectors": {"app": "cartservice"},
    }


def test_generated_name_is_dns_label_safe_and_unique() -> None:
    first = compose_podchaos(_fault(), namespace="boutique")["metadata"]["name"]
    second = compose_podchaos(_fault(), namespace="boutique")["metadata"]["name"]
    for name in (first, second):
        assert _NAME_RE.match(name), name
        assert len(name) <= 63
    assert first != second


def test_explicit_name_is_used_verbatim() -> None:
    cr = compose_podchaos(_fault(), namespace="boutique", name="probe-ok")
    assert cr["metadata"]["name"] == "probe-ok"


def test_pod_failure_maps_action() -> None:
    cr = compose_podchaos(_fault(fault_type=FaultType.POD_FAILURE), namespace="boutique")
    assert cr["spec"]["action"] == "pod-failure"


def test_container_kill_requires_and_sets_container_names() -> None:
    fault = _fault(fault_type=FaultType.CONTAINER_KILL)
    with pytest.raises(ValueError, match="container"):
        compose_podchaos(fault, namespace="boutique")
    cr = compose_podchaos(fault, namespace="boutique", container_names=["server"])
    assert cr["spec"]["action"] == "container-kill"
    assert cr["spec"]["containerNames"] == ["server"]


def test_container_kill_reads_names_from_the_fault_spec() -> None:
    # FaultSpec carries container_names so container_kill is usable end to end
    # (the lifecycle passes fault.container_names straight through).
    fault = _fault(fault_type=FaultType.CONTAINER_KILL, container_names=("server",))
    cr = compose_podchaos(
        fault, namespace="boutique", container_names=fault.container_names
    )
    assert cr["spec"]["containerNames"] == ["server"]


def test_empty_selector_is_refused() -> None:
    with pytest.raises(ValueError, match="selector is empty"):
        compose_podchaos(_fault(selector={}), namespace="boutique")


@pytest.mark.parametrize("ratio", [0.001, 0.004, 0.0049])
def test_sub_one_percent_ratio_floors_value_at_1(ratio: float) -> None:
    # Rounding would give "0" (a fault that selects no pods yet "succeeds").
    cr = compose_podchaos(_fault(ratio=ratio), namespace="boutique")
    assert cr["spec"]["value"] == "1"


@pytest.mark.parametrize(
    "fault_type",
    [
        FaultType.NETWORK_LATENCY,
        FaultType.NETWORK_LOSS,
        FaultType.NETWORK_PARTITION,
        FaultType.CPU_STRESS,
        FaultType.MEMORY_STRESS,
        FaultType.IO_STRESS,
        FaultType.DNS_CHAOS,
        FaultType.TIME_SKEW,
    ],
)
def test_non_pod_faults_raise_until_phase_2(fault_type: FaultType) -> None:
    with pytest.raises(UnsupportedFaultError):
        compose_podchaos(_fault(fault_type=fault_type), namespace="boutique")


# -- Kyverno compatibility across the policy-passable input space ---------------
# Mirrors config/policies/kyverno/chaos/*: mode never "all", value <= 50,
# duration always present and <= 900s (the working CR in verify-guardrails.sh
# uses fixed-percent 34 / 300s).


@pytest.mark.parametrize("ratio", [0.01, 0.1, 0.25, 0.34, 0.5])
@pytest.mark.parametrize("duration", [1, 60, 300, 900])
def test_policy_passable_faults_pass_kyverno_caps(ratio: float, duration: int) -> None:
    cr = compose_podchaos(
        _fault(ratio=ratio, duration_seconds=duration), namespace="boutique"
    )
    assert cr["spec"]["mode"] == "fixed-percent"  # never "all"
    assert int(cr["spec"]["value"]) <= 50  # cap-blast-radius
    assert cr["spec"]["duration"].endswith("s")  # require-experiment-ttl
    assert int(cr["spec"]["duration"][:-1]) <= 900  # fault-duration-cap
