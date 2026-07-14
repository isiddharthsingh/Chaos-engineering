"""CapacitySpec — shape validation, refusals, and the --spec JSON round-trip."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from chaosagent.capacity import CapacitySpec, WorkloadRef

_QUERY = "replicas_available"


def _spec(**overrides: object) -> CapacitySpec:
    base: dict[str, object] = {
        "title": "right-size cartservice to observed load",
        "target_id": "kind-local",
        "namespace": "boutique",
        "workload": {"kind": "deployment", "name": "cartservice"},
        "desired_replicas": 3,
        "hypotheses": [
            {"name": "replicas", "query": _QUERY, "comparator": ">=", "threshold": 1.0}
        ],
        "ttl_seconds": 300,
    }
    base.update(overrides)
    return CapacitySpec.model_validate(base)


def test_valid_spec_with_defaults() -> None:
    spec = _spec()
    assert spec.workload.kind == "deployment"
    assert spec.workload.name == "cartservice"
    assert spec.desired_replicas == 3
    assert spec.observe_interval_seconds == 5.0
    assert spec.baseline_seconds == 30
    assert spec.settle_seconds == 120


def test_json_round_trip_is_lossless() -> None:
    # The --spec file format: what the CLI reads must reproduce the model exactly.
    spec = _spec(settle_seconds=60, observe_interval_seconds=2.5)
    assert CapacitySpec.model_validate_json(spec.model_dump_json()) == spec


def test_scale_to_zero_is_refused_at_the_model() -> None:
    with pytest.raises(ValidationError):
        _spec(desired_replicas=0)


def test_statefulset_workload_is_accepted() -> None:
    spec = _spec(workload={"kind": "statefulset", "name": "redis-cart"})
    assert spec.workload.kind == "statefulset"


def test_unknown_workload_kind_is_refused() -> None:
    with pytest.raises(ValidationError):
        _spec(workload={"kind": "daemonset", "name": "cartservice"})


@pytest.mark.parametrize("name", ["CartService", "cart_service", "cart\n", "-cart", ""])
def test_workload_name_must_be_a_dns_label(name: str) -> None:
    with pytest.raises(ValidationError):
        WorkloadRef(kind="deployment", name=name)


def test_spec_requires_at_least_one_hypothesis() -> None:
    with pytest.raises(ValidationError):
        _spec(hypotheses=[])


def test_spec_rejects_duplicate_hypothesis_names() -> None:
    dup = [
        {"name": "slo", "query": _QUERY, "comparator": ">=", "threshold": 1.0},
        {"name": "slo", "query": "other", "comparator": "<", "threshold": 0.1},
    ]
    with pytest.raises(ValidationError, match="unique"):
        _spec(hypotheses=dup)


def test_spec_rejects_ttl_not_exceeding_baseline() -> None:
    # The binding is created at PREFLIGHT and must survive to APPLY.
    with pytest.raises(ValidationError, match="ttl_seconds"):
        _spec(ttl_seconds=30, baseline_seconds=30)


def test_spec_rejects_ttl_inside_the_observe_overshoot() -> None:
    # The baseline loop can overshoot its deadline by one interval: with
    # ttl=35, baseline=30, interval=20 the loop returns at t=40 > ttl, so the
    # binding would already be expired at APPLY.
    with pytest.raises(ValidationError, match="observe interval"):
        _spec(ttl_seconds=35, baseline_seconds=30, observe_interval_seconds=20.0)
    spec = _spec(ttl_seconds=51, baseline_seconds=30, observe_interval_seconds=20.0)
    assert spec.ttl_seconds == 51


def test_spec_is_frozen_and_rejects_extras() -> None:
    spec = _spec()
    with pytest.raises(ValidationError):
        spec.desired_replicas = 5  # type: ignore[misc]
    with pytest.raises(ValidationError):
        _spec(recovery_seconds=60)  # an ExperimentSpec field, not a capacity one
