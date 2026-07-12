"""Per-rule coverage of the deterministic policy engine."""

from __future__ import annotations

from chaosagent.domain.actions import FaultSpec, ProposedAction, ReplicaChange
from chaosagent.domain.enums import ActionType, EnvironmentTier, FaultType, TargetKind
from chaosagent.domain.policy import PolicyConfig
from chaosagent.policy import PolicyEngine


def _fault(duration: int = 60, ratio: float = 0.3) -> FaultSpec:
    return FaultSpec(
        fault_type=FaultType.POD_KILL,
        selector={"app": "payments"},
        ratio=ratio,
        duration_seconds=duration,
    )


def _inject(**overrides: object) -> ProposedAction:
    base: dict[str, object] = dict(
        action_type=ActionType.INJECT_FAULT,
        target_id="cluster-a",
        environment=EnvironmentTier.DEV,
        namespace="payments",
        namespace_chaos_enabled=True,
        fault=_fault(),
        ttl_seconds=300,
        concurrent_experiments=0,
        incident_active=False,
    )
    base.update(overrides)
    return ProposedAction(**base)  # type: ignore[arg-type]


def _rules(action: ProposedAction, config: PolicyConfig | None = None) -> set[str]:
    decision = PolicyEngine(config=config).evaluate(action)
    return {v.rule for v in decision.violations}


def test_valid_inject_is_allowed() -> None:
    assert PolicyEngine().evaluate(_inject()).allowed is True


def test_observe_is_always_allowed_even_on_prod() -> None:
    action = ProposedAction(
        action_type=ActionType.OBSERVE,
        target_id="cluster-a",
        environment=EnvironmentTier.PROD,
    )
    assert PolicyEngine().evaluate(action).allowed is True


def test_env_scope_blocks_prod_state_change() -> None:
    assert "env-scope" in _rules(_inject(environment=EnvironmentTier.PROD))


def test_require_chaos_namespace() -> None:
    assert "require-chaos-namespace" in _rules(_inject(namespace_chaos_enabled=False))


def test_require_namespace_scope() -> None:
    assert "require-namespace-scope" in _rules(_inject(namespace=None))


def test_replica_cap_blocks_large_change() -> None:
    action = ProposedAction(
        action_type=ActionType.SCALE_WORKLOAD,
        target_id="cluster-a",
        environment=EnvironmentTier.DEV,
        namespace="payments",
        replica_change=ReplicaChange(current=2, desired=6),  # +200%
    )
    assert "replica-cap" in _rules(action)


def test_replica_cap_allows_within_bound() -> None:
    action = ProposedAction(
        action_type=ActionType.SCALE_WORKLOAD,
        target_id="cluster-a",
        environment=EnvironmentTier.DEV,
        namespace="payments",
        replica_change=ReplicaChange(current=4, desired=6),  # +50% == cap
    )
    assert PolicyEngine().evaluate(action).allowed is True


def test_fault_duration_cap() -> None:
    assert "fault-duration-cap" in _rules(_inject(fault=_fault(duration=1200)))


def test_fault_blast_radius_cap() -> None:
    assert "fault-blast-radius" in _rules(_inject(fault=_fault(ratio=0.9)))


def test_require_ttl_missing() -> None:
    assert "require-ttl" in _rules(_inject(ttl_seconds=None))


def test_require_ttl_over_ceiling() -> None:
    assert "require-ttl" in _rules(_inject(ttl_seconds=99999))


def test_single_experiment() -> None:
    assert "single-experiment" in _rules(_inject(concurrent_experiments=1))


def test_incident_freeze() -> None:
    assert "incident-freeze" in _rules(_inject(incident_active=True))


def test_all_violations_reported_together() -> None:
    # A maximally-bad action should surface every relevant rule, not just the first.
    rules = _rules(
        _inject(
            environment=EnvironmentTier.PROD,
            namespace_chaos_enabled=False,
            fault=_fault(duration=5000, ratio=0.99),
            ttl_seconds=None,
            concurrent_experiments=3,
            incident_active=True,
        )
    )
    assert {
        "env-scope",
        "require-chaos-namespace",
        "fault-duration-cap",
        "fault-blast-radius",
        "require-ttl",
        "single-experiment",
        "incident-freeze",
    } <= rules


def test_config_is_respected() -> None:
    loose = PolicyConfig(max_fault_ratio=1.0, max_fault_duration_seconds=10000)
    action = _inject(fault=_fault(duration=5000, ratio=0.99))
    assert PolicyEngine(config=loose).evaluate(action).allowed


def test_apply_load_requires_chaos_namespace() -> None:
    # Load is also disruptive; the namespace opt-in must gate it too.
    action = ProposedAction(
        action_type=ActionType.APPLY_LOAD,
        target_id="cluster-a",
        environment=EnvironmentTier.DEV,
        namespace="payments",
        namespace_chaos_enabled=False,
        ttl_seconds=300,
    )
    assert "require-chaos-namespace" in _rules(action)


def test_incident_freeze_covers_capacity_actions() -> None:
    # A scale during a firing incident could compound the outage — freeze it.
    action = ProposedAction(
        action_type=ActionType.SCALE_WORKLOAD,
        target_id="cluster-a",
        environment=EnvironmentTier.DEV,
        namespace="payments",
        replica_change=ReplicaChange(current=4, desired=5),
        incident_active=True,
    )
    assert "incident-freeze" in _rules(action)


def test_namespace_scope_denies_out_of_scope() -> None:
    action = _inject(
        namespace="boutique",
        target_allowed_namespaces=("payments",),
    )
    assert "namespace-scope" in _rules(action)


def test_namespace_scope_allows_in_scope() -> None:
    action = _inject(
        namespace="payments",
        target_allowed_namespaces=("payments", "boutique"),
    )
    assert PolicyEngine().evaluate(action).allowed is True


def test_non_k8s_target_is_exempt_from_namespace_scope() -> None:
    # A VM-group right-size has no namespace and must not be denied for lacking one.
    action = ProposedAction(
        action_type=ActionType.RIGHT_SIZE,
        target_id="vm-fleet",
        environment=EnvironmentTier.DEV,
        target_kind=TargetKind.VM_GROUP,
        namespace=None,
        replica_change=ReplicaChange(current=4, desired=5),
    )
    assert PolicyEngine().evaluate(action).allowed is True
