"""Composer: FaultSpec -> Chaos Mesh NetworkChaos CR, Kyverno-compatible by construction."""

from __future__ import annotations

import re

import pytest

from chaosagent.domain.actions import FaultSpec
from chaosagent.domain.enums import FaultType
from chaosagent.faults import UnsupportedFaultError, compose_networkchaos

_NAME_RE = re.compile(r"\Achaosagent-[a-z-]+-[0-9a-f]{8}\Z")


def _fault(**overrides: object) -> FaultSpec:
    base: dict[str, object] = {
        "fault_type": FaultType.NETWORK_LATENCY,
        "selector": {"app": "cartservice"},
        "ratio": 0.34,
        "duration_seconds": 60,
        "network": {"action": "delay", "latency_ms": 100},
    }
    base.update(overrides)
    return FaultSpec.model_validate(base)


def test_network_latency_composes_full_cr() -> None:
    cr = compose_networkchaos(_fault(), namespace="boutique")
    assert cr["apiVersion"] == "chaos-mesh.org/v1alpha1"
    assert cr["kind"] == "NetworkChaos"
    assert cr["metadata"]["namespace"] == "boutique"
    assert cr["metadata"]["labels"] == {"app.kubernetes.io/managed-by": "chaosagent"}
    assert cr["spec"]["action"] == "delay"
    assert cr["spec"]["mode"] == "fixed-percent"
    assert cr["spec"]["value"] == "34"
    assert cr["spec"]["duration"] == "60s"
    assert cr["spec"]["direction"] == "to"
    assert cr["spec"]["selector"] == {
        "namespaces": ["boutique"],
        "labelSelectors": {"app": "cartservice"},
    }
    assert cr["spec"]["delay"] == {"latency": "100ms"}


def test_delay_includes_jitter_and_correlation_when_set() -> None:
    fault = _fault(
        network={
            "action": "delay",
            "latency_ms": 100,
            "jitter_ms": 20,
            "correlation_percent": 50,
        }
    )
    cr = compose_networkchaos(fault, namespace="boutique")
    assert cr["spec"]["delay"] == {"latency": "100ms", "jitter": "20ms", "correlation": "50"}


def test_delay_requires_latency() -> None:
    with pytest.raises(ValueError, match="latency_ms"):
        compose_networkchaos(_fault(network={"action": "delay"}), namespace="boutique")


def test_network_loss_maps_loss_block() -> None:
    fault = _fault(
        fault_type=FaultType.NETWORK_LOSS,
        network={"action": "loss", "loss_percent": 10, "correlation_percent": 25},
    )
    cr = compose_networkchaos(fault, namespace="boutique")
    assert cr["spec"]["action"] == "loss"
    assert cr["spec"]["loss"] == {"loss": "10", "correlation": "25"}
    assert "delay" not in cr["spec"]


def test_loss_requires_loss_percent() -> None:
    fault = _fault(fault_type=FaultType.NETWORK_LOSS, network={"action": "loss"})
    with pytest.raises(ValueError, match="loss_percent"):
        compose_networkchaos(fault, namespace="boutique")


def test_partition_composes_with_no_sub_blocks() -> None:
    fault = _fault(fault_type=FaultType.NETWORK_PARTITION, network={"action": "partition"})
    cr = compose_networkchaos(fault, namespace="boutique")
    assert cr["spec"]["action"] == "partition"
    assert cr["spec"]["direction"] == "to"
    assert "delay" not in cr["spec"] and "loss" not in cr["spec"]


@pytest.mark.parametrize("direction", ["from", "both"])
def test_non_to_directions_are_refused(direction: str) -> None:
    # Chaos Mesh requires spec.target for 'from'/'both'; composing without it
    # would yield a CR the engine refuses or half-applies. Refuse loudly.
    fault = _fault(network={"action": "delay", "latency_ms": 100, "direction": direction})
    with pytest.raises(ValueError, match="target-side"):
        compose_networkchaos(fault, namespace="boutique")


def test_action_mismatching_fault_type_is_refused() -> None:
    fault = _fault(network={"action": "loss", "loss_percent": 10})  # type is network_latency
    with pytest.raises(ValueError, match="delay"):
        compose_networkchaos(fault, namespace="boutique")


def test_non_network_fault_raises() -> None:
    pod = FaultSpec(fault_type=FaultType.POD_KILL, selector={"app": "x"}, duration_seconds=30)
    with pytest.raises(UnsupportedFaultError):
        compose_networkchaos(pod, namespace="boutique")


def test_empty_selector_is_refused() -> None:
    with pytest.raises(ValueError, match="selector is empty"):
        compose_networkchaos(_fault(selector={}), namespace="boutique")


def test_generated_name_is_dns_label_safe_and_explicit_name_wins() -> None:
    generated = compose_networkchaos(_fault(), namespace="boutique")["metadata"]["name"]
    assert _NAME_RE.match(generated), generated
    assert len(generated) <= 63
    named = compose_networkchaos(_fault(), namespace="boutique", name="probe-ok")
    assert named["metadata"]["name"] == "probe-ok"


# -- Kyverno compatibility across the policy-passable input space ---------------
# Mirrors config/policies/kyverno/chaos/*: mode never "all", value <= 50,
# duration always present and <= 900s.


@pytest.mark.parametrize("ratio", [0.001, 0.01, 0.1, 0.25, 0.34, 0.5])
@pytest.mark.parametrize("duration", [1, 60, 300, 900])
def test_policy_passable_faults_pass_kyverno_caps(ratio: float, duration: int) -> None:
    cr = compose_networkchaos(
        _fault(ratio=ratio, duration_seconds=duration), namespace="boutique"
    )
    assert cr["spec"]["mode"] != "all"
    assert 1 <= int(cr["spec"]["value"]) <= 50
    seconds = int(cr["spec"]["duration"].removesuffix("s"))
    assert 0 < seconds <= 900
