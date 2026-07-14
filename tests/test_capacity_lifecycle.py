"""The capacity lifecycle state machine — auto-revert beneath the LLM.

Uses the real gate and the real ScaleExecutor over a FakeScaleApi, so the
whole spine (engine -> bind -> dry-run -> patch -> revert) is exercised; only
the cluster, metrics, and clock are faked.
"""

from __future__ import annotations

from chaosagent.agents.permission import PermissionGate, RunMode
from chaosagent.capacity import (
    CapacityDeps,
    CapacityRun,
    CapacitySpec,
    CapacityState,
    run_capacity_lifecycle,
)
from chaosagent.domain.enums import EnvironmentTier, TargetKind
from chaosagent.domain.targets import CredentialRef, Target
from chaosagent.execute import ScaleExecutor
from chaosagent.observe import PrometheusError
from chaosagent.policy import PolicyEngine
from chaosagent.registry import TargetRegistry
from fakes import FakeApiException, FakeClock, FakeScaleApi, ScriptedPrometheus

_QUERY = "latency_p95"


def _spec(**overrides: object) -> CapacitySpec:
    base: dict[str, object] = {
        "title": "right-size cartservice to observed load",
        "target_id": "kind-local",
        "namespace": "boutique",
        "workload": {"kind": "deployment", "name": "cartservice"},
        "desired_replicas": 3,
        "hypotheses": [
            {"name": "latency", "query": _QUERY, "comparator": "<", "threshold": 200.0}
        ],
        "ttl_seconds": 300,
        "observe_interval_seconds": 5.0,
        "baseline_seconds": 10,
        "settle_seconds": 30,
    }
    base.update(overrides)
    return CapacitySpec.model_validate(base)


def _deps(
    metrics: ScriptedPrometheus,
    *,
    clock: FakeClock | None = None,
    api: FakeScaleApi | None = None,
) -> tuple[CapacityDeps, FakeScaleApi]:
    clock = clock or FakeClock()
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
    gate = PermissionGate(mode=RunMode.EXPERIMENT, clock=clock)
    api = api if api is not None else FakeScaleApi(current=4)
    deps = CapacityDeps(
        registry=registry,
        engine=PolicyEngine(),
        gate=gate,
        executor=ScaleExecutor(api, gate, clock=clock),
        metrics=metrics,
        clock=clock,
    )
    return deps, api


def _states(run: CapacityRun) -> list[CapacityState]:
    return [transition.state for transition in run.transitions]


def test_verified_change_walks_the_full_state_machine_and_keeps_it() -> None:
    clock = FakeClock()
    deps, api = _deps(ScriptedPrometheus({_QUERY: [100.0]}), clock=clock)
    run = run_capacity_lifecycle(_spec(), deps)
    assert run.state is CapacityState.DONE
    assert _states(run) == [
        CapacityState.PLAN,
        CapacityState.PREFLIGHT,
        CapacityState.BASELINE,
        CapacityState.APPLY,
        CapacityState.OBSERVE,
        CapacityState.VERIFY,
        CapacityState.REPORT,
        CapacityState.DONE,
    ]
    assert run.preflight is not None and run.preflight.allowed
    assert run.previous_replicas == 4 and run.desired_replicas == 3
    assert run.applied_at == 10.0  # after the 10s baseline window
    assert run.reverted_at is None and run.revert_reason is None
    # Success keeps the change: the workload stays at the new count.
    assert api.current == 3
    # PREFLIGHT dry-run, then APPLY = dry-run + real patch. Nothing else.
    assert [patch[3:] for patch in api.patches] == [(3, "All"), (3, "All"), (3, "")]
    # Baseline 0/5/10, settle 10..40 every 5s.
    assert len(run.baseline_results) == 3
    assert len(run.settle_results) == 7
    assert deps.gate.active_binding() is None
    assert run.completed_at == 40.0


def test_breach_reverts_on_the_same_tick_before_any_sleep() -> None:
    journal: list[str] = []
    clock = FakeClock(journal=journal)
    api = FakeScaleApi(current=4, journal=journal)
    metrics = ScriptedPrometheus(
        {_QUERY: [100.0, 100.0, 100.0, 100.0, 500.0]}, journal=journal
    )
    deps, _ = _deps(metrics, clock=clock, api=api)
    run = run_capacity_lifecycle(_spec(), deps)
    assert run.state is CapacityState.DONE  # a reverted run still completes
    assert run.reverted_at is not None and run.reverted
    assert run.breach_detected_at == 15.0  # second settle tick
    assert run.reverted_at == 15.0  # same tick: no polling delay added
    assert run.revert_reason is not None and "latency" in run.revert_reason
    assert CapacityState.REVERT in _states(run)
    assert CapacityState.VERIFY not in _states(run)
    # The revert patch is the very next thing after the breaching sample —
    # nothing (in particular no sleep) happens in between.
    breach_index = journal.index(f"scalar:{_QUERY}=500.0")
    assert journal[breach_index + 1] == "patch_scale:4"
    assert api.current == 4  # back at the recorded known-good count
    assert deps.gate.active_binding() is None


def test_revert_inadmissible_spec_fails_preflight() -> None:
    # 4->2 fits the replica cap, but its revert (2->4) would be +100%: the
    # engine refuses at PREFLIGHT so the auto-revert can never be blocked.
    deps, api = _deps(ScriptedPrometheus({_QUERY: [100.0]}))
    run = run_capacity_lifecycle(_spec(desired_replicas=2), deps)
    assert run.state is CapacityState.FAILED
    assert run.failed_from is CapacityState.PREFLIGHT
    assert run.preflight is not None
    assert "revert-admissible" in {v.rule for v in run.preflight.violations}
    assert api.patches == []
    assert deps.gate.active_binding() is None


def test_incident_freeze_blocks_before_any_write() -> None:
    deps, api = _deps(ScriptedPrometheus({_QUERY: [100.0]}))
    deps.incident_active = lambda ns: True
    run = run_capacity_lifecycle(_spec(), deps)
    assert run.state is CapacityState.FAILED
    assert run.failed_from is CapacityState.PREFLIGHT
    assert run.preflight is not None
    assert "incident-freeze" in {v.rule for v in run.preflight.violations}
    assert api.patches == []


def test_incident_probe_failure_fails_closed() -> None:
    deps, api = _deps(ScriptedPrometheus({_QUERY: [100.0]}))

    def _down(namespace: str) -> bool:
        raise RuntimeError("alertmanager unreachable")

    deps.incident_active = _down
    run = run_capacity_lifecycle(_spec(), deps)
    assert run.state is CapacityState.FAILED
    assert run.failed_from is CapacityState.PREFLIGHT
    assert run.failure_reason is not None and "failing closed" in run.failure_reason
    assert api.patches == []


def test_unregistered_target_fails_preflight() -> None:
    deps, api = _deps(ScriptedPrometheus({_QUERY: [100.0]}))
    run = run_capacity_lifecycle(_spec(target_id="ghost"), deps)
    assert run.state is CapacityState.FAILED
    assert run.failed_from is CapacityState.PREFLIGHT
    assert run.failure_reason is not None and "not registered" in run.failure_reason
    assert api.patches == []


def test_unreadable_replica_count_fails_before_any_write() -> None:
    api = FakeScaleApi(current=4)
    api.read_error = RuntimeError("403 cannot get deployments/scale")
    deps, _ = _deps(ScriptedPrometheus({_QUERY: [100.0]}), api=api)
    run = run_capacity_lifecycle(_spec(), deps)
    assert run.state is CapacityState.FAILED
    assert run.failed_from is CapacityState.PREFLIGHT
    assert run.failure_reason is not None and "replica count" in run.failure_reason
    assert api.patches == []


def test_server_side_dry_run_denial_fails_and_unbinds() -> None:
    api = FakeScaleApi(current=4)
    api.patch_error = FakeApiException(
        400, body='{"message": "[cap-replica-change] denied"}'
    )
    deps, _ = _deps(ScriptedPrometheus({_QUERY: [100.0]}), api=api)
    run = run_capacity_lifecycle(_spec(), deps)
    assert run.state is CapacityState.FAILED
    assert run.failed_from is CapacityState.PREFLIGHT
    assert run.failure_reason is not None and "cap-replica-change" in run.failure_reason
    assert api.current == 4
    assert deps.gate.active_binding() is None


def test_server_side_dry_run_crash_fails_closed_and_unbinds() -> None:
    # A connection error mid-dry-run is not a denial, but it must end the same
    # way: FAILED with the reason recorded and the write slot released.
    api = FakeScaleApi(current=4)
    api.patch_error = RuntimeError("connection reset by peer")
    deps, _ = _deps(ScriptedPrometheus({_QUERY: [100.0]}), api=api)
    run = run_capacity_lifecycle(_spec(), deps)
    assert run.state is CapacityState.FAILED
    assert run.failed_from is CapacityState.PREFLIGHT
    assert run.failure_reason is not None and "failing closed" in run.failure_reason
    assert api.current == 4
    assert deps.gate.active_binding() is None


def test_baseline_breach_refuses_to_scale() -> None:
    deps, api = _deps(ScriptedPrometheus({_QUERY: [500.0]}))
    run = run_capacity_lifecycle(_spec(), deps)
    assert run.state is CapacityState.FAILED
    assert run.failed_from is CapacityState.BASELINE
    assert run.failure_reason is not None
    assert "steady state not met; refusing to scale" in run.failure_reason
    assert api.current == 4  # only the PREFLIGHT dry-run ran; nothing applied
    assert deps.gate.active_binding() is None


def test_dry_run_only_stops_after_preflight() -> None:
    metrics = ScriptedPrometheus({_QUERY: [100.0]})
    deps, api = _deps(metrics)
    run = run_capacity_lifecycle(_spec(), deps, dry_run_only=True)
    assert run.state is CapacityState.DONE
    assert _states(run) == [
        CapacityState.PLAN,
        CapacityState.PREFLIGHT,
        CapacityState.DONE,
    ]
    assert [patch[3:] for patch in api.patches] == [(3, "All")]
    assert api.current == 4
    assert metrics.journal == []  # no baseline sampling in dry-run mode
    assert deps.gate.active_binding() is None


def test_failed_revert_is_recorded_not_raised() -> None:
    api = FakeScaleApi(current=4)
    original_patch = api.patch_scale

    def failing_revert(
        kind: str, name: str, namespace: str, replicas: int, *, dry_run: str | None = None
    ) -> None:
        if replicas == 4:  # only the revert targets the previous count
            raise RuntimeError("apiserver 500")
        original_patch(kind, name, namespace, replicas, dry_run=dry_run)

    api.patch_scale = failing_revert  # type: ignore[method-assign]
    metrics = ScriptedPrometheus({_QUERY: [100.0, 100.0, 100.0, 100.0, 500.0]})
    deps, _ = _deps(metrics, api=api)
    run = run_capacity_lifecycle(_spec(), deps)
    assert run.state is CapacityState.DONE
    assert run.reverted
    assert run.revert_error is not None and "apiserver 500" in run.revert_error
    assert api.current == 3  # the failed revert left the new count in place
    assert deps.gate.active_binding() is None


def test_observation_failure_after_apply_reverts_and_fails() -> None:
    # Blind with a fresh change applied is not a state we stay in: move back to
    # the recorded known-good count, then fail.
    api = FakeScaleApi(current=4)
    metrics = ScriptedPrometheus(
        {_QUERY: [100.0, 100.0, 100.0, PrometheusError("connection refused")]}
    )
    deps, _ = _deps(metrics, api=api)
    run = run_capacity_lifecycle(_spec(), deps)
    assert run.state is CapacityState.FAILED
    assert run.failed_from is CapacityState.OBSERVE
    assert run.failure_reason is not None and "connection refused" in run.failure_reason
    assert api.current == 4  # reverted
    assert run.reverted_at is None  # an error, not a breach-triggered revert
    assert deps.gate.active_binding() is None
