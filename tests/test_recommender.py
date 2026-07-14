"""Utilization signals and the deterministic recommender."""

from __future__ import annotations

import pytest

from chaosagent.capacity import WorkloadRef
from chaosagent.capacity.recommend import Recommendation, recommend_replicas
from chaosagent.capacity.signals import (
    VPA_GROUP,
    VPA_PLURAL,
    VPA_VERSION,
    SignalError,
    VpaRecommendation,
    WorkloadUsage,
    cpu_avg_utilization_query,
    cpu_p95_utilization_query,
    fetch_usage,
    memory_avg_utilization_query,
    memory_p95_utilization_query,
    read_vpa_recommendations,
    replicas_query,
)
from chaosagent.domain.actions import ProposedAction, ReplicaChange
from chaosagent.domain.enums import ActionType, EnvironmentTier
from chaosagent.domain.policy import PolicyConfig
from chaosagent.policy import PolicyEngine
from fakes import FakeCustomObjectsApi, ScriptedPrometheus

_REF = WorkloadRef(kind="deployment", name="cartservice")
_CONFIG = PolicyConfig()


def _usage(current: int = 4, **overrides: object) -> WorkloadUsage:
    base: dict[str, object] = {
        "namespace": "boutique",
        "workload": _REF,
        "current_replicas": current,
        "lookback_minutes": 60,
    }
    base.update(overrides)
    return WorkloadUsage.model_validate(base)


def _recommend(usage: WorkloadUsage) -> Recommendation:
    return recommend_replicas(usage, config=_CONFIG)


# -- recommender golden cases -------------------------------------------------


def test_over_provisioned_downscale_stops_at_the_revert_floor() -> None:
    # 20% of requests used: proportional sizing wants 2 of 6, but the revert
    # floor (at most -33% at the 0.5 cap) stops the downscale at 4.
    rec = _recommend(_usage(current=6, cpu_avg=0.2))
    assert rec.desired_replicas == 4
    assert rec.clamps == ("revert-admissible",)


def test_under_provisioned_upscale_within_the_cap() -> None:
    rec = _recommend(_usage(current=4, cpu_avg=0.9))
    assert rec.desired_replicas == 6  # ceil(4 * 0.9 / 0.6), exactly at the cap
    assert rec.clamps == ()


def test_heavy_overload_is_clamped_by_the_replica_cap() -> None:
    rec = _recommend(_usage(current=4, cpu_avg=1.2))
    assert rec.desired_replicas == 6  # raw 8 clamped to +50%
    assert rec.clamps == ("replica-cap",)


def test_already_right_sized_keeps_the_count() -> None:
    rec = _recommend(_usage(current=4, cpu_avg=0.6))
    assert rec.desired_replicas == 4
    assert rec.clamps == ()


def test_memory_bound_workload_sizes_on_the_binding_signal() -> None:
    # CPU is idle but memory runs hot: the max of the averages drives sizing.
    rec = _recommend(_usage(current=4, cpu_avg=0.2, memory_avg=0.9))
    assert rec.observed_utilization == 0.9
    assert rec.desired_replicas == 6


def test_no_signal_keeps_the_count_and_says_so() -> None:
    rec = _recommend(_usage(current=4))
    assert rec.desired_replicas == 4
    assert rec.observed_utilization is None
    assert any("no utilization signal" in line for line in rec.rationale)


def test_scaled_to_zero_workload_is_left_alone() -> None:
    rec = _recommend(_usage(current=0, cpu_avg=0.9))
    assert rec.desired_replicas == 0
    assert any("scale-from-zero" in line for line in rec.rationale)


def test_recommendation_is_deterministic() -> None:
    first = _recommend(_usage(current=6, cpu_avg=0.2, memory_avg=0.5, cpu_p95=0.4))
    for _ in range(10):
        assert _recommend(_usage(current=6, cpu_avg=0.2, memory_avg=0.5, cpu_p95=0.4)) == first


def test_rationale_records_inputs_and_clamps() -> None:
    rec = _recommend(_usage(current=6, cpu_avg=0.2))
    text = "\n".join(rec.rationale)
    assert "20%" in text  # the observed signal
    assert "revert-admissible" in text  # the clamp that bounded it


def test_never_emits_a_change_the_engine_would_deny() -> None:
    # Property: over a grid of counts and utilizations, every recommendation
    # is admissible under the same config it was clamped with.
    engine = PolicyEngine(config=_CONFIG)
    for current in range(0, 25):
        for tenths in range(0, 31):  # observed utilization 0.0 .. 3.0
            usage = _usage(current=current, cpu_avg=tenths / 10)
            rec = recommend_replicas(usage, config=_CONFIG)
            action = ProposedAction(
                action_type=ActionType.SCALE_WORKLOAD,
                target_id="cluster-a",
                environment=EnvironmentTier.DEV,
                namespace="boutique",
                replica_change=ReplicaChange(
                    current=current, desired=rec.desired_replicas
                ),
            )
            decision = engine.evaluate(action)
            assert decision.allowed, (
                f"{current} -> {rec.desired_replicas} at {tenths / 10:.1f}: "
                f"{decision.reason()}"
            )


def test_vpa_signal_folds_into_the_rationale_without_changing_the_size() -> None:
    # Recommend-only this phase: the VPA target informs the report, never the
    # replica math (vertical writes are a Phase 4 decision).
    vpa = (VpaRecommendation(container="server", cpu="250m", memory="256Mi"),)
    with_vpa = _recommend(_usage(current=4, cpu_avg=0.9, vpa=vpa))
    without = _recommend(_usage(current=4, cpu_avg=0.9))
    assert with_vpa.desired_replicas == without.desired_replicas
    text = "\n".join(with_vpa.rationale)
    assert "VPA" in text and "250m" in text and "256Mi" in text


def test_no_vpa_signal_changes_nothing() -> None:
    rec = _recommend(_usage(current=4, cpu_avg=0.9))
    assert not any("VPA" in line for line in rec.rationale)


# -- signals ------------------------------------------------------------------


def test_replicas_query_uses_the_kind_specific_series() -> None:
    # max() collapses duplicate series from HA kube-state-metrics setups.
    assert (
        replicas_query("boutique", _REF)
        == 'max(kube_deployment_spec_replicas{namespace="boutique",deployment="cartservice"})'
    )
    sts = WorkloadRef(kind="statefulset", name="redis-cart")
    assert (
        replicas_query("boutique", sts)
        == 'max(kube_statefulset_replicas{namespace="boutique",statefulset="redis-cart"})'
    )


def test_utilization_queries_are_scoped_to_namespace_and_workload() -> None:
    for builder in (
        cpu_avg_utilization_query,
        cpu_p95_utilization_query,
        memory_avg_utilization_query,
        memory_p95_utilization_query,
    ):
        query = builder("boutique", _REF, lookback_minutes=30)
        assert 'namespace="boutique"' in query
        assert 'pod=~"cartservice-[a-z0-9]+-[a-z0-9]+"' in query
        assert "30m" in query


def test_pod_pattern_cannot_swallow_a_sibling_workload() -> None:
    # Deployment `frontend` must not match `frontend-v2`'s pods (rs-hash and
    # pod-hash contain no dash); statefulset ordinals are digits only.
    import re

    from chaosagent.capacity.signals import _pod_re

    dep = re.compile(_pod_re(WorkloadRef(kind="deployment", name="frontend")) + r"\Z")
    assert dep.match("frontend-5d8f9c7b6-abcde")
    assert not dep.match("frontend-v2-5d8f9c7b6-abcde")
    sts = re.compile(_pod_re(WorkloadRef(kind="statefulset", name="redis-cart")) + r"\Z")
    assert sts.match("redis-cart-0")
    assert not sts.match("redis-cart-v2-0")


def test_fetch_usage_assembles_the_snapshot() -> None:
    series: dict[str, list[float | None | Exception]] = {
        replicas_query("boutique", _REF): [4.0],
        cpu_avg_utilization_query("boutique", _REF, lookback_minutes=30): [0.42],
        cpu_p95_utilization_query("boutique", _REF, lookback_minutes=30): [0.8],
        memory_avg_utilization_query("boutique", _REF, lookback_minutes=30): [0.5],
        memory_p95_utilization_query("boutique", _REF, lookback_minutes=30): [0.7],
    }
    usage = fetch_usage(ScriptedPrometheus(series), "boutique", _REF, lookback_minutes=30)
    assert usage.current_replicas == 4
    assert usage.cpu_avg == 0.42 and usage.cpu_p95 == 0.8
    assert usage.memory_avg == 0.5 and usage.memory_p95 == 0.7
    assert usage.lookback_minutes == 30


def test_fetch_usage_requires_a_replica_count() -> None:
    series: dict[str, list[float | None | Exception]] = {
        replicas_query("boutique", _REF): [None],
        cpu_avg_utilization_query("boutique", _REF, lookback_minutes=60): [0.4],
        cpu_p95_utilization_query("boutique", _REF, lookback_minutes=60): [0.4],
        memory_avg_utilization_query("boutique", _REF, lookback_minutes=60): [0.4],
        memory_p95_utilization_query("boutique", _REF, lookback_minutes=60): [0.4],
    }
    with pytest.raises(SignalError, match="replica count"):
        fetch_usage(ScriptedPrometheus(series), "boutique", _REF)


def _vpa_object(name: str, target_kind: str, target_name: str) -> dict[str, object]:
    return {
        "apiVersion": f"{VPA_GROUP}/{VPA_VERSION}",
        "kind": "VerticalPodAutoscaler",
        "metadata": {"name": name, "namespace": "boutique"},
        "spec": {"targetRef": {"kind": target_kind, "name": target_name}},
        "status": {
            "recommendation": {
                "containerRecommendations": [
                    {"containerName": "server", "target": {"cpu": "250m", "memory": "256Mi"}}
                ]
            }
        },
    }


def test_read_vpa_recommendations_matches_only_the_target_workload() -> None:
    api = FakeCustomObjectsApi()
    for name, kind, target in (
        ("cart-vpa", "Deployment", "cartservice"),
        ("other-vpa", "Deployment", "otherservice"),
        ("sts-vpa", "StatefulSet", "cartservice"),  # same name, wrong kind
    ):
        api.create_namespaced_custom_object(
            VPA_GROUP, VPA_VERSION, "boutique", VPA_PLURAL, _vpa_object(name, kind, target)
        )
    recommendations = read_vpa_recommendations(api, "boutique", _REF)
    assert recommendations == (
        VpaRecommendation(container="server", cpu="250m", memory="256Mi"),
    )


def test_read_vpa_recommendations_treats_an_absent_crd_as_no_signal() -> None:
    api = FakeCustomObjectsApi()
    api.absent_plurals.add(VPA_PLURAL)
    assert read_vpa_recommendations(api, "boutique", _REF) == ()


def test_missing_utilization_signals_stay_none() -> None:
    # Utilization is advisory: absent series must not fail the snapshot.
    series: dict[str, list[float | None | Exception]] = {
        replicas_query("boutique", _REF): [3.0],
        cpu_avg_utilization_query("boutique", _REF, lookback_minutes=60): [None],
        cpu_p95_utilization_query("boutique", _REF, lookback_minutes=60): [None],
        memory_avg_utilization_query("boutique", _REF, lookback_minutes=60): [None],
        memory_p95_utilization_query("boutique", _REF, lookback_minutes=60): [None],
    }
    usage = fetch_usage(ScriptedPrometheus(series), "boutique", _REF)
    assert usage.current_replicas == 3
    assert usage.cpu_avg is None and usage.memory_avg is None
