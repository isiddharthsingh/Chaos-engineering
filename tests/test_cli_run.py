"""The `chaosagent run` path: exit codes, wiring, and the LLM-free fallback."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from chaosagent.agents.permission import PermissionGate, RunMode
from chaosagent.domain.enums import EnvironmentTier, TargetKind
from chaosagent.domain.targets import CredentialRef, Target
from chaosagent.experiment import ExperimentSpec, LifecycleDeps
from chaosagent.experiment.runner import RunnerDeps, RunSettings, run_experiment
from chaosagent.policy import PolicyEngine
from chaosagent.registry import TargetRegistry
from fakes import FakeClock, FakeExecutor, ScriptedPrometheus

_QUERY = "replicas_available"

_SPEC = {
    "title": "cartservice survives a one-third pod kill",
    "target_id": "kind-local",
    "namespace": "boutique",
    "fault": {
        "fault_type": "pod_kill",
        "selector": {"app": "cartservice"},
        "ratio": 0.34,
        "duration_seconds": 60,
    },
    "hypotheses": [{"name": "replicas", "query": _QUERY, "comparator": ">=", "threshold": 1.0}],
    "ttl_seconds": 300,
    "observe_interval_seconds": 5.0,
    "baseline_seconds": 10,
    "recovery_seconds": 10,
}


class FakePlanner:
    def __init__(self, spec: ExperimentSpec) -> None:
        self.spec = spec
        self.calls: list[tuple[str, str, str]] = []

    async def plan(self, intent: str, *, target: Target, namespace: str) -> ExperimentSpec:
        self.calls.append((intent, target.id, namespace))
        return self.spec


def _deps(
    values: list[float | None],
    *,
    chaos_enabled: bool = True,
    planner: FakePlanner | None = None,
) -> tuple[RunnerDeps, FakeExecutor]:
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
    lifecycle = LifecycleDeps(
        registry=registry,
        engine=PolicyEngine(),
        gate=PermissionGate(mode=RunMode.EXPERIMENT, clock=clock),
        executor=executor,
        metrics=ScriptedPrometheus({_QUERY: values}),
        clock=clock,
        namespace_chaos_enabled=lambda ns: chaos_enabled,
        concurrent_experiments=lambda ns: 0,
    )
    return RunnerDeps(lifecycle=lifecycle, planner=planner), executor


def _spec_file(tmp_path: Path, spec: dict[str, object] | None = None) -> Path:
    path = tmp_path / "experiment.json"
    path.write_text(json.dumps(spec or _SPEC))
    return path


def test_verified_run_exits_0_and_writes_the_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    deps, executor = _deps([2.0])
    output = tmp_path / "report.json"
    settings = RunSettings(
        target_id="kind-local", spec_file=_spec_file(tmp_path), output=output
    )
    assert run_experiment(settings, deps) == 0
    assert len(executor.applied) == 1
    out = capsys.readouterr().out
    assert "resilience : 100.0/100" in out
    report = json.loads(output.read_text())
    assert report["resilience_score"] == 100.0
    assert report["aborted"] is False


def test_preflight_denial_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    deps, executor = _deps([2.0], chaos_enabled=False)
    settings = RunSettings(target_id="kind-local", spec_file=_spec_file(tmp_path))
    assert run_experiment(settings, deps) == 2
    assert executor.applied == []
    assert "require-chaos-namespace" in capsys.readouterr().out


def test_slo_breach_exits_3(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    deps, executor = _deps([2.0, 2.0, 2.0, 2.0, 0.0])
    settings = RunSettings(target_id="kind-local", spec_file=_spec_file(tmp_path))
    assert run_experiment(settings, deps) == 3
    assert len(executor.deleted) == 2
    assert "ABORTED" in capsys.readouterr().out


def test_dry_run_exits_0_without_applying(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    deps, executor = _deps([2.0])
    settings = RunSettings(
        target_id="kind-local", spec_file=_spec_file(tmp_path), dry_run=True
    )
    assert run_experiment(settings, deps) == 0
    assert len(executor.dry_runs) == 1
    assert executor.applied == []
    out = capsys.readouterr().out
    assert "pre-flight passed" in out
    assert "resilience" not in out  # a scored report would be misleading here


def test_dry_run_denial_still_reports_and_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    deps, _ = _deps([2.0], chaos_enabled=False)
    settings = RunSettings(
        target_id="kind-local", spec_file=_spec_file(tmp_path), dry_run=True
    )
    assert run_experiment(settings, deps) == 2
    assert "require-chaos-namespace" in capsys.readouterr().out


def test_spec_target_mismatch_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    deps, _ = _deps([2.0])
    settings = RunSettings(target_id="other-cluster", spec_file=_spec_file(tmp_path))
    assert run_experiment(settings, deps) == 1
    assert "other-cluster" in capsys.readouterr().err


def test_malformed_spec_file_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    deps, _ = _deps([2.0])
    path = tmp_path / "bad.json"
    path.write_text("{not json")
    settings = RunSettings(target_id="kind-local", spec_file=path)
    assert run_experiment(settings, deps) == 1
    assert "spec" in capsys.readouterr().err


def test_cli_overrides_reach_the_spec(tmp_path: Path) -> None:
    deps, executor = _deps([2.0])
    settings = RunSettings(
        target_id="kind-local",
        spec_file=_spec_file(tmp_path),
        baseline_seconds=0,
        recovery_seconds=0,
        interval_seconds=1.0,
    )
    assert run_experiment(settings, deps) == 0
    # duration 60s at 1s interval: 61 during samples vs 13 at the spec default.
    clock = deps.lifecycle.clock
    assert isinstance(clock, FakeClock)
    assert clock.sleeps.count(1.0) > 0
    assert 5.0 not in clock.sleeps


def test_invalid_interval_override_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # model_copy would bypass the gt=0 constraint; re-validation must catch it.
    deps, executor = _deps([2.0])
    settings = RunSettings(
        target_id="kind-local", spec_file=_spec_file(tmp_path), interval_seconds=-1.0
    )
    assert run_experiment(settings, deps) == 1
    assert executor.applied == []
    assert "override" in capsys.readouterr().err


def test_namespace_conflicting_with_spec_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    deps, executor = _deps([2.0])
    settings = RunSettings(
        target_id="kind-local", spec_file=_spec_file(tmp_path), namespace="sandbox"
    )
    assert run_experiment(settings, deps) == 1
    assert executor.applied == []
    assert "conflicts" in capsys.readouterr().err


def test_intent_uses_the_planner_and_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    planner = FakePlanner(ExperimentSpec.model_validate(_SPEC))
    deps, executor = _deps([2.0], planner=planner)
    settings = RunSettings(
        target_id="kind-local",
        intent="test the cart service's resilience",
        namespace="boutique",
    )
    assert run_experiment(settings, deps) == 0
    assert planner.calls == [
        ("test the cart service's resilience", "kind-local", "boutique")
    ]
    assert len(executor.applied) == 1


def test_intent_requires_a_namespace(capsys: pytest.CaptureFixture[str]) -> None:
    deps, _ = _deps([2.0], planner=FakePlanner(ExperimentSpec.model_validate(_SPEC)))
    settings = RunSettings(target_id="kind-local", intent="break things")
    assert run_experiment(settings, deps) == 1
    assert "--namespace" in capsys.readouterr().err


def test_intent_without_the_sdk_exits_1_with_an_install_hint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    deps, _ = _deps([2.0])  # no planner injected -> the real SDK path is taken
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
    settings = RunSettings(
        target_id="kind-local", intent="break things", namespace="boutique"
    )
    assert run_experiment(settings, deps) == 1
    assert "--extra agent" in capsys.readouterr().err


def test_cli_run_subcommand_builds_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    import chaosagent.cli as cli

    captured: dict[str, RunSettings] = {}

    def fake_run(settings: RunSettings, deps: object = None) -> int:
        captured["settings"] = settings
        return 0

    monkeypatch.setattr(cli, "run_experiment", fake_run)
    code = cli.main(
        [
            "run",
            "--target",
            "kind-local",
            "--spec",
            "examples/experiment-cartservice.json",
            "--dry-run",
            "--prom-url",
            "http://localhost:9999",
            "--interval",
            "2.5",
        ]
    )
    assert code == 0
    settings = captured["settings"]
    assert settings.target_id == "kind-local"
    assert settings.spec_file == Path("examples/experiment-cartservice.json")
    assert settings.dry_run is True
    assert settings.prometheus_url == "http://localhost:9999"
    assert settings.interval_seconds == 2.5


def test_cli_run_requires_spec_or_intent() -> None:
    import chaosagent.cli as cli

    with pytest.raises(SystemExit):
        cli.main(["run", "--target", "kind-local"])


def test_example_spec_is_valid() -> None:
    path = Path(__file__).resolve().parents[1] / "examples" / "experiment-cartservice.json"
    spec = ExperimentSpec.model_validate_json(path.read_text())
    assert spec.target_id == "kind-local"
    assert spec.namespace == "boutique"
    assert spec.fault.fault_type.value == "pod_kill"
    # The example must sit inside every policy cap.
    assert spec.fault.ratio <= 0.5
    assert spec.fault.duration_seconds <= 900
    assert spec.ttl_seconds <= 3600
