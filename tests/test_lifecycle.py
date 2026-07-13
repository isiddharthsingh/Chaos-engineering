"""The experiment lifecycle state machine, end to end over fakes."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from chaosagent.agents.permission import PermissionGate, RunMode
from chaosagent.domain.enums import EnvironmentTier, TargetKind
from chaosagent.domain.targets import CredentialRef, Target
from chaosagent.experiment import (
    ExperimentRun,
    ExperimentSpec,
    ExperimentState,
    LifecycleDeps,
    run_lifecycle,
)
from chaosagent.observe import PrometheusError
from chaosagent.policy import PolicyEngine
from chaosagent.registry import TargetRegistry
from fakes import FakeClock, FakeExecutor, ScriptedPrometheus

_QUERY = "replicas_available"


def _spec(**overrides: object) -> ExperimentSpec:
    base: dict[str, object] = {
        "title": "cartservice survives a one-third pod kill",
        "target_id": "kind-local",
        "namespace": "boutique",
        "fault": {
            "fault_type": "pod_kill",
            "selector": {"app": "cartservice"},
            "ratio": 0.34,
            "duration_seconds": 60,
        },
        "hypotheses": [
            {"name": "replicas", "query": _QUERY, "comparator": ">=", "threshold": 1.0}
        ],
        "ttl_seconds": 300,
        "observe_interval_seconds": 5.0,
        "baseline_seconds": 10,
        "recovery_seconds": 10,
    }
    base.update(overrides)
    return ExperimentSpec.model_validate(base)


def _deps(
    metrics: ScriptedPrometheus,
    *,
    clock: FakeClock | None = None,
    executor: FakeExecutor | None = None,
    chaos_enabled: bool = True,
    concurrent: int = 0,
) -> LifecycleDeps:
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
    return LifecycleDeps(
        registry=registry,
        engine=PolicyEngine(),
        gate=PermissionGate(mode=RunMode.EXPERIMENT, clock=clock),
        executor=executor or FakeExecutor(clock=clock),
        metrics=metrics,
        clock=clock,
        namespace_chaos_enabled=lambda ns: chaos_enabled,
        concurrent_experiments=lambda ns: concurrent,
    )


def _states(run: ExperimentRun) -> list[ExperimentState]:
    return [transition.state for transition in run.transitions]


def test_verified_run_walks_the_full_state_machine() -> None:
    clock = FakeClock()
    executor = FakeExecutor(clock=clock)
    deps = _deps(ScriptedPrometheus({_QUERY: [2.0]}), clock=clock, executor=executor)
    run = run_lifecycle(_spec(), deps)
    assert run.state is ExperimentState.DONE
    assert _states(run) == [
        ExperimentState.PLAN,
        ExperimentState.PREFLIGHT,
        ExperimentState.BASELINE,
        ExperimentState.INJECT,
        ExperimentState.OBSERVE,
        ExperimentState.VERIFY,
        ExperimentState.ROLLBACK,
        ExperimentState.REPORT,
        ExperimentState.DONE,
    ]
    assert run.preflight is not None and run.preflight.allowed
    assert run.aborted_at is None and run.abort_reason is None
    assert run.injected_at == 10.0  # after the 10s baseline window
    assert len(executor.dry_runs) == 1
    assert len(executor.applied) == 1
    assert len(executor.deleted) == 1  # rollback delete
    assert run.cr_name == executor.applied[0].name
    assert run.cr_namespace == "boutique"
    # Baseline 0/5/10, during 10..70 every 5s, recovery 70/75/80.
    assert len(run.baseline_results) == 3
    assert len(run.during_results) == 13
    assert len(run.recovery_results) == 3
    assert deps.gate.active_binding() is None
    assert run.completed_at == 80.0


def test_slo_breach_auto_aborts_before_any_sleep() -> None:
    journal: list[str] = []
    clock = FakeClock(journal=journal)
    executor = FakeExecutor(journal, clock=clock)
    metrics = ScriptedPrometheus({_QUERY: [2.0, 2.0, 2.0, 2.0, 0.0]}, journal=journal)
    run = run_lifecycle(_spec(), _deps(metrics, clock=clock, executor=executor))
    assert run.state is ExperimentState.DONE  # an aborted run still completes
    assert run.aborted_at is not None
    assert run.breach_detected_at == 15.0  # second observe tick
    assert run.aborted_at == 15.0  # same tick: no polling delay added
    assert run.abort_reason is not None and "replicas" in run.abort_reason
    assert ExperimentState.ABORT in _states(run)
    assert ExperimentState.VERIFY not in _states(run)
    # The delete is the very next thing after the breaching sample — nothing
    # (in particular no sleep) happens in between.
    breach_index = journal.index(f"scalar:{_QUERY}=0.0")
    assert journal[breach_index + 1] == "delete"
    assert len(executor.deleted) == 2  # abort delete + idempotent rollback delete


def test_preflight_denial_never_touches_the_executor() -> None:
    executor = FakeExecutor()
    deps = _deps(ScriptedPrometheus({_QUERY: [2.0]}), executor=executor, chaos_enabled=False)
    run = run_lifecycle(_spec(), deps)
    assert run.state is ExperimentState.FAILED
    assert run.failed_from is ExperimentState.PREFLIGHT
    assert run.preflight is not None and not run.preflight.allowed
    assert "require-chaos-namespace" in {v.rule for v in run.preflight.violations}
    assert executor.dry_runs == [] and executor.applied == [] and executor.journal == []


def test_concurrent_experiment_is_denied_preflight() -> None:
    deps = _deps(ScriptedPrometheus({_QUERY: [2.0]}), concurrent=1)
    run = run_lifecycle(_spec(), deps)
    assert run.state is ExperimentState.FAILED
    assert run.preflight is not None
    assert "single-experiment" in {v.rule for v in run.preflight.violations}


def test_disabled_namespace_skips_the_concurrency_probe() -> None:
    # The concurrency count lists chaos CRs as the experimenter SA, which has no
    # RBAC in namespaces that never opted in — probing there can only 403. The
    # engine denies such namespaces regardless, so the probe must not run.
    counted: list[str] = []
    deps = _deps(ScriptedPrometheus({_QUERY: [2.0]}), chaos_enabled=False)
    deps.concurrent_experiments = lambda ns: counted.append(ns) or 0  # type: ignore[func-returns-value]
    run = run_lifecycle(_spec(), deps)
    assert run.state is ExperimentState.FAILED
    assert run.preflight is not None
    assert "require-chaos-namespace" in {v.rule for v in run.preflight.violations}
    assert counted == []


def test_probe_failure_fails_closed_before_the_executor() -> None:
    executor = FakeExecutor()
    deps = _deps(ScriptedPrometheus({_QUERY: [2.0]}), executor=executor)

    def _forbidden(namespace: str) -> int:
        raise RuntimeError("403 cannot list podchaos")

    deps.concurrent_experiments = _forbidden
    run = run_lifecycle(_spec(), deps)
    assert run.state is ExperimentState.FAILED
    assert run.failed_from is ExperimentState.PREFLIGHT
    assert run.failure_reason is not None and "failing closed" in run.failure_reason
    assert executor.journal == []


def test_unregistered_target_fails_preflight() -> None:
    deps = _deps(ScriptedPrometheus({_QUERY: [2.0]}))
    run = run_lifecycle(_spec(target_id="ghost"), deps)
    assert run.state is ExperimentState.FAILED
    assert run.failed_from is ExperimentState.PREFLIGHT
    assert run.failure_reason is not None and "not registered" in run.failure_reason


def test_server_side_dry_run_denial_fails_and_unbinds() -> None:
    executor = FakeExecutor(deny_dry_run="[require-chaos-namespace] refused by webhook")
    deps = _deps(ScriptedPrometheus({_QUERY: [2.0]}), executor=executor)
    run = run_lifecycle(_spec(), deps)
    assert run.state is ExperimentState.FAILED
    assert run.failed_from is ExperimentState.PREFLIGHT
    assert run.failure_reason is not None and "require-chaos-namespace" in run.failure_reason
    assert executor.applied == []
    assert deps.gate.active_binding() is None


def test_composer_refusal_fails_preflight() -> None:
    # A delay without latency_ms passes the domain model but the composer
    # refuses it; the run fails in PREFLIGHT before anything is applied.
    spec = _spec(
        fault={
            "fault_type": "network_latency",
            "selector": {"app": "cartservice"},
            "ratio": 0.34,
            "duration_seconds": 60,
            "network": {"action": "delay"},
        }
    )
    executor = FakeExecutor()
    deps = _deps(ScriptedPrometheus({_QUERY: [2.0]}), executor=executor)
    run = run_lifecycle(spec, deps)
    assert run.state is ExperimentState.FAILED
    assert run.failed_from is ExperimentState.PREFLIGHT
    assert run.failure_reason is not None and "latency_ms" in run.failure_reason
    assert executor.applied == []
    assert deps.gate.active_binding() is None


def test_network_latency_runs_end_to_end() -> None:
    spec = _spec(
        title="cartservice survives 100ms latency",
        fault={
            "fault_type": "network_latency",
            "selector": {"app": "cartservice"},
            "ratio": 0.34,
            "duration_seconds": 60,
            "network": {"action": "delay", "latency_ms": 100},
        },
    )
    executor = FakeExecutor()
    deps = _deps(ScriptedPrometheus({_QUERY: [2.0]}), executor=executor)
    run = run_lifecycle(spec, deps)
    assert run.state is ExperimentState.DONE
    assert [cr["kind"] for cr in executor.dry_runs] == ["NetworkChaos"]
    assert executor.applied[0].kind == "NetworkChaos"
    assert executor.deleted[0].kind == "NetworkChaos"
    assert run.cr_name == executor.applied[0].name


_LOAD = {"script_configmap": "checkout-load", "duration_seconds": 60, "ttl_seconds": 300}


def test_load_rides_the_fault_and_rolls_back_with_it() -> None:
    executor = FakeExecutor()
    deps = _deps(ScriptedPrometheus({_QUERY: [2.0]}), executor=executor)
    run = run_lifecycle(_spec(load=_LOAD), deps)
    assert run.state is ExperimentState.DONE
    # Both CRs are server-side dry-run at PREFLIGHT, before anything is applied.
    assert [cr["kind"] for cr in executor.dry_runs] == ["PodChaos", "TestRun"]
    # One binding, two CRs: the TestRun rides the fault's policy-approved action.
    assert [a.kind for a in executor.applied] == ["PodChaos", "TestRun"]
    assert [d.kind for d in executor.deleted] == ["PodChaos", "TestRun"]
    assert deps.gate.active_binding() is None


def test_inadmissible_load_is_refused_before_any_injection() -> None:
    # A load the admission layer would deny must fail PREFLIGHT — never after
    # the fault is already live.
    executor = FakeExecutor(
        deny_dry_run="[require-chaos-namespace-k6] refused", deny_dry_run_kind="TestRun"
    )
    deps = _deps(ScriptedPrometheus({_QUERY: [2.0]}), executor=executor)
    run = run_lifecycle(_spec(load=_LOAD), deps)
    assert run.state is ExperimentState.FAILED
    assert run.failed_from is ExperimentState.PREFLIGHT
    assert run.failure_reason is not None and "require-chaos-namespace-k6" in run.failure_reason
    assert executor.applied == []
    assert deps.gate.active_binding() is None


def test_missing_script_configmap_is_refused_before_any_injection() -> None:
    # A TestRun referencing a missing ConfigMap is admitted but starts no load;
    # the run would score a false pass. The probe refuses at PREFLIGHT.
    executor = FakeExecutor()
    deps = _deps(ScriptedPrometheus({_QUERY: [2.0]}), executor=executor)
    deps.configmap_exists = lambda ns, name: False
    run = run_lifecycle(_spec(load=_LOAD), deps)
    assert run.state is ExperimentState.FAILED
    assert run.failed_from is ExperimentState.PREFLIGHT
    assert run.failure_reason is not None and "checkout-load" in run.failure_reason
    assert executor.applied == [] and executor.dry_runs == []


def test_failed_deletes_of_both_crs_are_both_recorded() -> None:
    executor = FakeExecutor(fail_delete="api server unavailable")
    deps = _deps(ScriptedPrometheus({_QUERY: [2.0]}), executor=executor)
    run = run_lifecycle(_spec(load=_LOAD), deps)
    assert run.state is ExperimentState.DONE
    assert run.rollback_error is not None
    # Append, never overwrite: the fault CR's failure must not be masked by the
    # load CR's. The fault delete comes first.
    fault_name, load_name = (applied.name for applied in executor.applied)
    assert fault_name in run.rollback_error and load_name in run.rollback_error
    assert run.rollback_error.index(fault_name) < run.rollback_error.index(load_name)


def test_load_ttl_must_sit_inside_the_experiment_ttl() -> None:
    with pytest.raises(ValidationError, match="load.ttl_seconds"):
        _spec(load={"script_configmap": "x", "duration_seconds": 60, "ttl_seconds": 400})


def test_spec_without_load_applies_nothing_extra() -> None:
    executor = FakeExecutor()
    deps = _deps(ScriptedPrometheus({_QUERY: [2.0]}), executor=executor)
    run = run_lifecycle(_spec(), deps)
    assert run.state is ExperimentState.DONE
    assert [a.kind for a in executor.applied] == ["PodChaos"]
    assert [d.kind for d in executor.deleted] == ["PodChaos"]


def test_abort_deletes_fault_and_load_on_the_breach_tick() -> None:
    journal: list[str] = []
    clock = FakeClock(journal=journal)
    executor = FakeExecutor(journal, clock=clock)
    metrics = ScriptedPrometheus({_QUERY: [2.0, 2.0, 2.0, 2.0, 0.0]}, journal=journal)
    run = run_lifecycle(_spec(load=_LOAD), _deps(metrics, clock=clock, executor=executor))
    assert run.aborted
    breach_index = journal.index(f"scalar:{_QUERY}=0.0")
    assert journal[breach_index + 1 : breach_index + 3] == ["delete", "delete"]
    assert [d.kind for d in executor.deleted[:2]] == ["PodChaos", "TestRun"]


def test_load_apply_failure_tears_down_the_fault() -> None:
    executor = FakeExecutor(deny_apply="k6 refused", deny_apply_kind="TestRun")
    deps = _deps(ScriptedPrometheus({_QUERY: [2.0]}), executor=executor)
    run = run_lifecycle(_spec(load=_LOAD), deps)
    assert run.state is ExperimentState.FAILED
    assert run.failed_from is ExperimentState.INJECT
    assert run.failure_reason is not None and "load apply failed" in run.failure_reason
    assert [d.kind for d in executor.deleted] == ["PodChaos"]
    assert deps.gate.active_binding() is None


def test_baseline_breach_refuses_to_inject() -> None:
    executor = FakeExecutor()
    deps = _deps(ScriptedPrometheus({_QUERY: [0.0]}), executor=executor)
    run = run_lifecycle(_spec(), deps)
    assert run.state is ExperimentState.FAILED
    assert run.failed_from is ExperimentState.BASELINE
    assert run.failure_reason is not None
    assert "steady state not met; refusing to inject" in run.failure_reason
    assert executor.applied == []
    assert deps.gate.active_binding() is None


def test_dry_run_only_stops_after_preflight() -> None:
    executor = FakeExecutor()
    metrics = ScriptedPrometheus({_QUERY: [2.0]})
    deps = _deps(metrics, executor=executor)
    run = run_lifecycle(_spec(), deps, dry_run_only=True)
    assert run.state is ExperimentState.DONE
    assert _states(run) == [
        ExperimentState.PLAN,
        ExperimentState.PREFLIGHT,
        ExperimentState.DONE,
    ]
    assert len(executor.dry_runs) == 1
    assert executor.applied == []
    assert metrics.journal == []  # no baseline sampling in dry-run mode
    assert deps.gate.active_binding() is None


def test_observation_failure_with_live_fault_tears_down() -> None:
    executor = FakeExecutor()
    # Healthy through baseline (3 ticks), then Prometheus goes away mid-fault.
    metrics = ScriptedPrometheus(
        {_QUERY: [2.0, 2.0, 2.0, PrometheusError("connection refused")]}
    )
    deps = _deps(metrics, executor=executor)
    run = run_lifecycle(_spec(), deps)
    assert run.state is ExperimentState.FAILED
    assert run.failure_reason is not None and "connection refused" in run.failure_reason
    assert len(executor.deleted) == 1  # the fault did not outlive our blindness
    assert deps.gate.active_binding() is None


@pytest.mark.parametrize("field", ["hypotheses"])
def test_spec_requires_at_least_one_hypothesis(field: str) -> None:
    with pytest.raises(ValueError):
        _spec(**{field: []})


def test_spec_rejects_duplicate_hypothesis_names() -> None:
    dup = [
        {"name": "slo", "query": _QUERY, "comparator": ">=", "threshold": 1.0},
        {"name": "slo", "query": "other", "comparator": "<", "threshold": 0.1},
    ]
    with pytest.raises(ValueError, match="unique"):
        _spec(hypotheses=dup)


def test_spec_rejects_ttl_not_exceeding_baseline() -> None:
    with pytest.raises(ValueError, match="ttl_seconds"):
        _spec(ttl_seconds=10, baseline_seconds=10)


def test_incident_freeze_denies_when_an_alert_is_firing() -> None:
    deps = _deps(ScriptedPrometheus({_QUERY: [2.0]}))
    deps.incident_active = lambda ns: True
    run = run_lifecycle(_spec(), deps)
    assert run.state is ExperimentState.FAILED
    assert run.preflight is not None
    assert "incident-freeze" in {v.rule for v in run.preflight.violations}


def test_teardown_delete_failure_does_not_escape() -> None:
    # A transient delete error must not leave the run crashing with the binding
    # bound; the run still completes, records the problem, and unbinds.
    clock = FakeClock()
    executor = FakeExecutor(clock=clock)
    original_delete = executor.delete
    calls = {"n": 0}

    def flaky_delete(applied: object) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("apiserver 500")
        original_delete(applied)

    executor.delete = flaky_delete  # type: ignore[method-assign]
    deps = _deps(ScriptedPrometheus({_QUERY: [2.0]}), clock=clock, executor=executor)
    run = run_lifecycle(_spec(), deps)
    assert run.state is ExperimentState.DONE
    assert run.rollback_error is not None and "apiserver 500" in run.rollback_error
    assert deps.gate.active_binding() is None


def test_recovery_sampling_failure_still_reports() -> None:
    clock = FakeClock()
    # Baseline (3 ticks) + observe (13 ticks) healthy, then Prometheus fails on
    # the first recovery tick (index 16). ScriptedPrometheus repeats the last.
    metrics = ScriptedPrometheus({_QUERY: [2.0] * 16 + [PrometheusError("gone")]})
    deps = _deps(metrics, clock=clock)
    run = run_lifecycle(_spec(), deps)
    assert run.state is ExperimentState.DONE
    assert len(run.during_results) == 13  # the experiment did complete
    assert run.rollback_error is not None and "recovery sampling failed" in run.rollback_error
    assert deps.gate.active_binding() is None


def test_baseline_prometheus_error_fails_without_crashing() -> None:
    executor = FakeExecutor()
    deps = _deps(ScriptedPrometheus({_QUERY: [PrometheusError("prom down")]}), executor=executor)
    run = run_lifecycle(_spec(), deps)
    assert run.state is ExperimentState.FAILED
    assert run.failed_from is ExperimentState.BASELINE
    assert run.failure_reason is not None and "baseline observation failed" in run.failure_reason
    assert executor.applied == []
    assert deps.gate.active_binding() is None


def test_container_kill_runs_when_names_are_supplied() -> None:
    executor = FakeExecutor()
    spec = _spec(
        fault={
            "fault_type": "container_kill",
            "selector": {"app": "cartservice"},
            "container_names": ["server"],
            "ratio": 0.34,
            "duration_seconds": 60,
        }
    )
    deps = _deps(ScriptedPrometheus({_QUERY: [2.0]}), executor=executor)
    run = run_lifecycle(spec, deps)
    assert run.state is ExperimentState.DONE
    assert executor.applied[0].kind == "PodChaos"
    assert executor.dry_runs[0]["spec"]["action"] == "container-kill"
    assert executor.dry_runs[0]["spec"]["containerNames"] == ["server"]
