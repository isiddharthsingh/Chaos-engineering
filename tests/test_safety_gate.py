"""Release-gating autonomous-safety invariants.

Per the plan, this test is what makes "fully autonomous" defensible. It asserts
the deterministic guardrails hold regardless of LLM behaviour:

  * a fault CANNOT execute outside a chaos-enabled namespace,
  * a capacity action CANNOT exceed the replica cap,
  * NO state-changing action can reach a prod target,
  * the loop auto-aborts within the deadline of a synthetic SLO breach,
  * an unbound write CANNOT reach the cluster.
"""

from __future__ import annotations

import pytest

from chaosagent.agents.permission import ActionBinding, PermissionGate, RunMode
from chaosagent.config import load_policy_config
from chaosagent.domain.actions import FaultSpec, ProposedAction, ReplicaChange
from chaosagent.domain.enums import ActionType, EnvironmentTier, FaultType, TargetKind
from chaosagent.domain.policy import PolicyDecision
from chaosagent.domain.targets import CredentialRef, Target
from chaosagent.execute import ChaosMeshExecutor, ExecutionDenied
from chaosagent.experiment import ExperimentSpec, ExperimentState, LifecycleDeps, run_lifecycle
from chaosagent.faults import compose_podchaos
from chaosagent.policy import PolicyEngine
from chaosagent.registry import TargetRegistry
from fakes import FakeClock, FakeCustomObjectsApi, FakeExecutor, ScriptedPrometheus

# Uses the *shipped* config, not test defaults — this gates the real bundle.
ENGINE = PolicyEngine(config=load_policy_config())

#: The abort must land within one observe interval of the breaching sample.
ABORT_DEADLINE_SECONDS = 5.0


def test_fault_outside_chaos_enabled_namespace_is_denied() -> None:
    action = ProposedAction(
        action_type=ActionType.INJECT_FAULT,
        target_id="cluster-a",
        environment=EnvironmentTier.DEV,
        namespace="default",
        namespace_chaos_enabled=False,
        fault=FaultSpec(fault_type=FaultType.POD_KILL, ratio=0.3, duration_seconds=60),
        ttl_seconds=300,
    )
    decision = ENGINE.evaluate(action)
    assert decision.allowed is False
    assert "require-chaos-namespace" in {v.rule for v in decision.violations}


@pytest.mark.parametrize("desired", [4, 5, 100])  # +100%, +150%, +2400% from 2
def test_replica_change_over_cap_is_denied(desired: int) -> None:
    action = ProposedAction(
        action_type=ActionType.SCALE_WORKLOAD,
        target_id="cluster-a",
        environment=EnvironmentTier.DEV,
        namespace="payments",
        replica_change=ReplicaChange(current=2, desired=desired),
    )
    decision = ENGINE.evaluate(action)
    assert decision.allowed is False
    assert "replica-cap" in {v.rule for v in decision.violations}


_POD_KILL = FaultSpec(fault_type=FaultType.POD_KILL, duration_seconds=60)


@pytest.mark.parametrize(
    "action_type,extra",
    [
        (
            ActionType.INJECT_FAULT,
            {"fault": _POD_KILL, "ttl_seconds": 300, "namespace_chaos_enabled": True},
        ),
        (ActionType.SCALE_WORKLOAD, {"replica_change": ReplicaChange(current=4, desired=5)}),
        (ActionType.RIGHT_SIZE, {"replica_change": ReplicaChange(current=4, desired=4)}),
        (ActionType.APPLY_LOAD, {"ttl_seconds": 300}),
    ],
)
def test_no_state_change_reaches_prod(action_type: ActionType, extra: dict[str, object]) -> None:
    action = ProposedAction(
        action_type=action_type,
        target_id="prod-cluster",
        environment=EnvironmentTier.PROD,
        namespace="payments",
        **extra,  # type: ignore[arg-type]
    )
    decision = ENGINE.evaluate(action)
    assert decision.allowed is False
    assert "env-scope" in {v.rule for v in decision.violations}


def test_admitted_capacity_change_is_always_revertible_under_the_same_caps() -> None:
    # The capacity analogue of the abort invariant: any replica change the
    # shipped engine admits, it also admits reversing — the deterministic
    # auto-revert can never be blocked by our own guardrails.
    def _capacity(current: int, desired: int) -> ProposedAction:
        return ProposedAction(
            action_type=ActionType.SCALE_WORKLOAD,
            target_id="cluster-a",
            environment=EnvironmentTier.DEV,
            namespace="payments",
            replica_change=ReplicaChange(current=current, desired=desired),
        )

    for current in range(1, 25):
        for desired in range(1, 25):
            if ENGINE.evaluate(_capacity(current, desired)).allowed:
                revert = ENGINE.evaluate(_capacity(desired, current))
                assert revert.allowed, (
                    f"{current}->{desired} was admitted but its revert was denied: "
                    f"{revert.reason()}"
                )


def test_auto_abort_within_deadline_of_synthetic_breach() -> None:
    # Real engine (shipped config), real gate, real lifecycle + observe loop;
    # only the cluster, metrics, and clock are faked. The steady state holds
    # through baseline, then collapses two ticks into the fault.
    query = "replicas_available"
    journal: list[str] = []
    clock = FakeClock(journal=journal)
    registry = TargetRegistry()
    registry.register(
        Target(
            id="kind-local",
            name="Local kind rig",
            kind=TargetKind.KUBERNETES,
            environment=EnvironmentTier.DEV,
            allowed_namespaces=["boutique"],
            credential=CredentialRef(service_account="agent-experimenter"),
        )
    )
    deps = LifecycleDeps(
        registry=registry,
        engine=ENGINE,
        gate=PermissionGate(mode=RunMode.EXPERIMENT, clock=clock),
        executor=FakeExecutor(journal, clock=clock),
        metrics=ScriptedPrometheus({query: [2.0, 2.0, 2.0, 2.0, 0.0]}, journal=journal),
        clock=clock,
        namespace_chaos_enabled=lambda ns: True,
        concurrent_experiments=lambda ns: 0,
    )
    spec = ExperimentSpec.model_validate(
        {
            "title": "synthetic breach",
            "target_id": "kind-local",
            "namespace": "boutique",
            "fault": {
                "fault_type": "pod_kill",
                "selector": {"app": "probe"},
                "ratio": 0.34,
                "duration_seconds": 60,
            },
            "hypotheses": [
                {"name": "replicas", "query": query, "comparator": ">=", "threshold": 1.0}
            ],
            "ttl_seconds": 300,
            "observe_interval_seconds": ABORT_DEADLINE_SECONDS,
            "baseline_seconds": 10,
            "recovery_seconds": 10,
        }
    )
    run = run_lifecycle(spec, deps)
    assert run.aborted_at is not None and run.breach_detected_at is not None
    assert run.aborted_at - run.breach_detected_at <= ABORT_DEADLINE_SECONDS
    assert ExperimentState.ABORT in {t.state for t in run.transitions}
    # The abort delete precedes any subsequent sleep: with the breach detected,
    # the very next cross-component event is the CR delete.
    breach_index = journal.index(f"scalar:{query}=0.0")
    assert journal[breach_index + 1] == "delete"


def test_unbound_write_cannot_reach_the_cluster() -> None:
    # Even a forged binding receipt is useless: the gate holds no active
    # binding, so the executor denies before a single API call is made.
    clock = FakeClock()
    gate = PermissionGate(mode=RunMode.EXPERIMENT, clock=clock)
    api = FakeCustomObjectsApi()
    executor = ChaosMeshExecutor(api, gate, clock=clock)
    fault = FaultSpec(
        fault_type=FaultType.POD_KILL,
        selector={"app": "probe"},
        ratio=0.34,
        duration_seconds=60,
    )
    action = ProposedAction(
        action_type=ActionType.INJECT_FAULT,
        target_id="kind-local",
        environment=EnvironmentTier.DEV,
        namespace="boutique",
        namespace_chaos_enabled=True,
        fault=fault,
        ttl_seconds=300,
    )
    forged = ActionBinding(
        token="forged", action=action, decision=PolicyDecision.allow(), expires_at=9e9
    )
    cr = compose_podchaos(fault, namespace="boutique")
    with pytest.raises(ExecutionDenied, match="policy-approved action"):
        executor.apply(cr, forged)
    with pytest.raises(ExecutionDenied, match="policy-approved action"):
        executor.dry_run(cr, forged)
    assert api.calls == []


def test_engine_is_deterministic() -> None:
    action = ProposedAction(
        action_type=ActionType.INJECT_FAULT,
        target_id="cluster-a",
        environment=EnvironmentTier.DEV,
        namespace="payments",
        namespace_chaos_enabled=True,
        fault=FaultSpec(fault_type=FaultType.POD_KILL, ratio=0.3, duration_seconds=60),
        ttl_seconds=300,
    )
    first = ENGINE.evaluate(action)
    for _ in range(50):
        assert ENGINE.evaluate(action) == first
