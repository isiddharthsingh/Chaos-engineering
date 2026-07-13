"""GameDay suites: strictly sequential runs, stop-on-abort, worst exit code."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from chaosagent.agents.permission import PermissionGate, RunMode
from chaosagent.domain.enums import EnvironmentTier, TargetKind
from chaosagent.domain.targets import CredentialRef, Target
from chaosagent.experiment import ExperimentState, LifecycleDeps
from chaosagent.experiment.runner import RunnerDeps
from chaosagent.experiment.schedule import (
    SuiteSettings,
    SuiteSpec,
    run_suite,
    run_suite_command,
)
from chaosagent.policy import PolicyEngine
from chaosagent.registry import TargetRegistry
from fakes import FakeClock, FakeExecutor, ScriptedPrometheus

_HEALTHY = [2.0]
_BREACHING = [2.0, 2.0, 2.0, 2.0, 0.0]  # holds through baseline, breaks during


def _experiment(title: str, query: str, target_id: str = "kind-local") -> dict[str, object]:
    return {
        "title": title,
        "target_id": target_id,
        "namespace": "boutique",
        "fault": {
            "fault_type": "pod_kill",
            "selector": {"app": "cartservice"},
            "ratio": 0.34,
            "duration_seconds": 60,
        },
        "hypotheses": [{"name": "replicas", "query": query, "comparator": ">=", "threshold": 1.0}],
        "ttl_seconds": 300,
        "observe_interval_seconds": 5.0,
        "baseline_seconds": 10,
        "recovery_seconds": 10,
    }


def _suite(*experiments: dict[str, object]) -> SuiteSpec:
    return SuiteSpec.model_validate({"title": "gameday", "experiments": list(experiments)})


def _deps(
    series: dict[str, list[float | None | Exception]],
) -> tuple[LifecycleDeps, FakeExecutor]:
    clock = FakeClock()
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
    executor = FakeExecutor(clock=clock)
    deps = LifecycleDeps(
        registry=registry,
        engine=PolicyEngine(),
        gate=PermissionGate(mode=RunMode.EXPERIMENT, clock=clock),
        executor=executor,
        metrics=ScriptedPrometheus(dict(series)),
        clock=clock,
        namespace_chaos_enabled=lambda ns: True,
        concurrent_experiments=lambda ns: 0,
    )
    return deps, executor


def test_suite_runs_every_experiment_sequentially() -> None:
    deps, executor = _deps({"q1": _HEALTHY, "q2": _HEALTHY})
    suite = _suite(_experiment("first", "q1"), _experiment("second", "q2"))
    outcome = run_suite(suite, deps)
    assert [run.state for run in outcome.runs] == [ExperimentState.DONE] * 2
    assert [report.title for report in outcome.reports] == ["first", "second"]
    assert outcome.stopped_early is False
    assert outcome.exit_code == 0
    # Sequential, never concurrent: each run applied then deleted before the next.
    assert executor.journal.count("apply") == 2
    assert executor.journal.index("delete") < executor.journal.index(
        "apply", executor.journal.index("delete")
    )
    assert deps.gate.active_binding() is None


def test_abort_stops_the_suite_by_default() -> None:
    deps, executor = _deps({"q1": _BREACHING, "q2": _HEALTHY})
    suite = _suite(_experiment("first", "q1"), _experiment("second", "q2"))
    outcome = run_suite(suite, deps)
    assert len(outcome.runs) == 1
    assert outcome.runs[0].aborted
    assert outcome.stopped_early is True
    assert outcome.exit_code == 3
    assert executor.journal.count("apply") == 1  # the second never injected


def test_continue_on_abort_runs_the_remaining_experiments() -> None:
    deps, executor = _deps({"q1": _BREACHING, "q2": _HEALTHY})
    suite = _suite(_experiment("first", "q1"), _experiment("second", "q2"))
    outcome = run_suite(suite, deps, continue_on_abort=True)
    assert len(outcome.runs) == 2
    assert outcome.runs[0].aborted and not outcome.runs[1].aborted
    assert outcome.stopped_early is False
    assert outcome.exit_code == 3  # the abort is still the worst outcome


def test_operational_error_stops_the_suite_by_default() -> None:
    # Metrics die mid-fault in run 1: the fault is torn down blind and the
    # system's health is unverified — injecting run 2 would be reckless.
    deps, executor = _deps(
        {"q1": [2.0, 2.0, 2.0, 2.0, RuntimeError("prometheus down")], "q2": _HEALTHY}
    )
    suite = _suite(_experiment("first", "q1"), _experiment("second", "q2"))
    outcome = run_suite(suite, deps)
    assert len(outcome.runs) == 1
    assert outcome.stopped_early is True
    assert outcome.exit_code == 1
    assert executor.journal.count("apply") == 1  # the second never injected


def test_worst_exit_code_ranks_denial_above_abort() -> None:
    # 1 (error) > 2 (denied) > 3 (aborted) > 0: harness/config breakage outranks
    # an abort, because an abort is the safety loop *working*.
    deps, _ = _deps({"q1": _BREACHING, "q2": _HEALTHY})
    suite = _suite(
        _experiment("aborts", "q1"),
        _experiment("denied", "q2", target_id="ghost"),  # not registered -> 2
    )
    outcome = run_suite(suite, deps, continue_on_abort=True)
    assert outcome.exit_code == 2


def test_suite_spec_requires_at_least_one_experiment() -> None:
    with pytest.raises(ValidationError):
        SuiteSpec.model_validate({"title": "empty", "experiments": []})


def test_run_suite_command_loads_file_and_returns_worst_code(tmp_path: Path) -> None:
    deps, _ = _deps({"q1": _HEALTHY, "q2": _HEALTHY})
    suite_file = tmp_path / "suite.json"
    suite_file.write_text(
        json.dumps(
            {"title": "gameday", "experiments": [_experiment("first", "q1")]}
        )
    )
    output = tmp_path / "report.json"
    settings = SuiteSettings(target_id="kind-local", spec_file=suite_file, output=output)
    code = run_suite_command(settings, RunnerDeps(lifecycle=deps))
    assert code == 0
    payload = json.loads(output.read_text())
    assert payload["title"] == "gameday"
    assert len(payload["reports"]) == 1


def test_run_suite_command_refuses_a_target_mismatch(tmp_path: Path) -> None:
    deps, executor = _deps({"q1": _HEALTHY})
    suite_file = tmp_path / "suite.json"
    suite_file.write_text(
        json.dumps({"title": "gameday", "experiments": [_experiment("first", "q1")]})
    )
    settings = SuiteSettings(target_id="other", spec_file=suite_file)
    assert run_suite_command(settings, RunnerDeps(lifecycle=deps)) == 1
    assert executor.applied == []
