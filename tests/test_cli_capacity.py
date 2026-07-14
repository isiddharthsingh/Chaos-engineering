"""The `chaosagent recommend` and `chaosagent scale` paths: exit codes and wiring."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chaosagent.agents.permission import PermissionGate, RunMode
from chaosagent.capacity import CapacityDeps, CapacitySpec, WorkloadRef
from chaosagent.capacity.runner import (
    CapacitySettings,
    RecommendDeps,
    ScaleDeps,
    run_recommend,
    run_scale,
)
from chaosagent.capacity.signals import (
    cpu_avg_utilization_query,
    cpu_p95_utilization_query,
    memory_avg_utilization_query,
    memory_p95_utilization_query,
    replicas_query,
)
from chaosagent.domain.enums import EnvironmentTier, TargetKind
from chaosagent.domain.targets import CredentialRef, Target
from chaosagent.execute import ScaleExecutor
from chaosagent.policy import PolicyEngine
from chaosagent.registry import TargetRegistry
from fakes import FakeClock, FakeScaleApi, ScriptedPrometheus

_QUERY = "latency_p95"
_REF = WorkloadRef(kind="deployment", name="cartservice")

_SPEC = {
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


def _registry() -> TargetRegistry:
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
    return registry


def _scale_deps(
    values: list[float | None], *, current: int = 4
) -> tuple[ScaleDeps, FakeScaleApi]:
    clock = FakeClock()
    gate = PermissionGate(mode=RunMode.EXPERIMENT, clock=clock)
    api = FakeScaleApi(current=current)
    lifecycle = CapacityDeps(
        registry=_registry(),
        engine=PolicyEngine(),
        gate=gate,
        executor=ScaleExecutor(api, gate, clock=clock),
        metrics=ScriptedPrometheus({_QUERY: values}),
        clock=clock,
    )
    return ScaleDeps(lifecycle=lifecycle), api


def _spec_file(tmp_path: Path, spec: dict[str, object] | None = None) -> Path:
    path = tmp_path / "capacity.json"
    path.write_text(json.dumps(spec or _SPEC))
    return path


class FakeCost:
    def __init__(self, monthly: float | None) -> None:
        self.monthly = monthly
        self.calls: list[tuple[str, str]] = []

    def workload_monthly_cost(self, namespace: str, workload: WorkloadRef) -> float | None:
        self.calls.append((namespace, workload.name))
        return self.monthly


def _recommend_deps(
    replicas: float | None = 4.0,
    cpu_avg: float | None = 0.9,
    cost: FakeCost | None = None,
) -> RecommendDeps:
    series: dict[str, list[float | None | Exception]] = {
        replicas_query("boutique", _REF): [replicas],
        cpu_avg_utilization_query("boutique", _REF, lookback_minutes=60): [cpu_avg],
        cpu_p95_utilization_query("boutique", _REF, lookback_minutes=60): [None],
        memory_avg_utilization_query("boutique", _REF, lookback_minutes=60): [None],
        memory_p95_utilization_query("boutique", _REF, lookback_minutes=60): [None],
    }
    return RecommendDeps(
        metrics=ScriptedPrometheus(series), registry=_registry(), cost=cost
    )


# -- scale --------------------------------------------------------------------


def test_verified_scale_exits_0_and_writes_the_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    deps, api = _scale_deps([100.0])
    output = tmp_path / "report.json"
    settings = CapacitySettings(
        target_id="kind-local", spec_file=_spec_file(tmp_path), output=output
    )
    assert run_scale(settings, deps) == 0
    assert api.current == 3  # the change was kept
    out = capsys.readouterr().out
    assert "change kept" in out
    report = json.loads(output.read_text())
    assert report["outcome"] == "kept"
    assert report["final_replicas"] == 3


def test_preflight_denial_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # 4->2: the revert-admissible rule refuses before anything is written.
    deps, api = _scale_deps([100.0])
    spec = dict(_SPEC, desired_replicas=2)
    settings = CapacitySettings(target_id="kind-local", spec_file=_spec_file(tmp_path, spec))
    assert run_scale(settings, deps) == 2
    assert api.current == 4 and api.patches == []
    assert "revert-admissible" in capsys.readouterr().out


def test_settle_breach_exits_3(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    deps, api = _scale_deps([100.0, 100.0, 100.0, 100.0, 500.0])
    settings = CapacitySettings(target_id="kind-local", spec_file=_spec_file(tmp_path))
    assert run_scale(settings, deps) == 3
    assert api.current == 4  # auto-reverted
    assert "auto-reverted" in capsys.readouterr().out


def test_failed_revert_exits_1_not_3(tmp_path: Path) -> None:
    # Exit 3 promises "auto-reverted"; if the revert patch failed, the workload
    # may still be at the new count — that is an error, not a revert.
    deps, api = _scale_deps([100.0, 100.0, 100.0, 100.0, 500.0])
    original_patch = api.patch_scale

    def failing_revert(
        kind: str, name: str, namespace: str, replicas: int, *, dry_run: str | None = None
    ) -> None:
        if replicas == 4:  # only the revert targets the previous count
            raise RuntimeError("apiserver 500")
        original_patch(kind, name, namespace, replicas, dry_run=dry_run)

    api.patch_scale = failing_revert  # type: ignore[method-assign]
    settings = CapacitySettings(target_id="kind-local", spec_file=_spec_file(tmp_path))
    assert run_scale(settings, deps) == 1
    assert api.current == 3  # the change is still live


def test_operational_preflight_failure_exits_1_not_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Exit 2 is a terminal "denied" verdict; an unreachable API server during
    # PREFLIGHT is transient and must map to 1 so automation retries.
    deps, api = _scale_deps([100.0])
    api.read_error = RuntimeError("apiserver unreachable")
    settings = CapacitySettings(target_id="kind-local", spec_file=_spec_file(tmp_path))
    assert run_scale(settings, deps) == 1
    assert "replica count" in capsys.readouterr().out


def test_unregistered_target_still_exits_2(tmp_path: Path) -> None:
    deps, _ = _scale_deps([100.0])
    spec = dict(_SPEC, target_id="ghost")
    settings = CapacitySettings(target_id="ghost", spec_file=_spec_file(tmp_path, spec))
    assert run_scale(settings, deps) == 2


def test_scale_dry_run_exits_0_with_zero_writes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    deps, api = _scale_deps([100.0])
    settings = CapacitySettings(
        target_id="kind-local", spec_file=_spec_file(tmp_path), dry_run=True
    )
    assert run_scale(settings, deps) == 0
    assert api.current == 4
    assert [patch[4] for patch in api.patches] == ["All"]  # dry-run only
    out = capsys.readouterr().out
    assert "pre-flight passed" in out


def test_malformed_spec_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    deps, _ = _scale_deps([100.0])
    path = tmp_path / "bad.json"
    path.write_text("{not json")
    settings = CapacitySettings(target_id="kind-local", spec_file=path)
    assert run_scale(settings, deps) == 1
    assert "spec" in capsys.readouterr().err


def test_spec_target_mismatch_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    deps, _ = _scale_deps([100.0])
    settings = CapacitySettings(target_id="other-cluster", spec_file=_spec_file(tmp_path))
    assert run_scale(settings, deps) == 1
    assert "other-cluster" in capsys.readouterr().err


# -- recommend ------------------------------------------------------------------


def test_recommend_prints_a_bounded_recommendation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = CapacitySettings(
        target_id="kind-local", namespace="boutique", workload="deployment/cartservice"
    )
    assert run_recommend(settings, _recommend_deps()) == 0
    out = capsys.readouterr().out
    assert "4 -> 6" in out  # ceil(4 * 0.9 / 0.6)
    assert "target 60%" in out or "60%" in out


def test_recommend_never_writes_and_writes_the_output_json(tmp_path: Path) -> None:
    output = tmp_path / "recommendation.json"
    settings = CapacitySettings(
        target_id="kind-local",
        namespace="boutique",
        workload="deployment/cartservice",
        output=output,
    )
    # RecommendDeps carries no executor and no gate — the path cannot write.
    assert run_recommend(settings, _recommend_deps()) == 0
    recommendation = json.loads(output.read_text())
    assert recommendation["desired_replicas"] == 6
    assert recommendation["rationale"]


def test_recommend_renders_the_cost_signal_when_wired(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cost = FakeCost(monthly=30.0)
    settings = CapacitySettings(
        target_id="kind-local", namespace="boutique", workload="deployment/cartservice"
    )
    assert run_recommend(settings, _recommend_deps(cost=cost)) == 0
    assert cost.calls == [("boutique", "cartservice")]
    out = capsys.readouterr().out
    assert "+15.00" in out  # 4 -> 6 at 30.0/month


def test_recommend_without_cost_data_changes_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = CapacitySettings(
        target_id="kind-local", namespace="boutique", workload="deployment/cartservice"
    )
    assert run_recommend(settings, _recommend_deps(cost=FakeCost(monthly=None))) == 0
    assert "month" not in capsys.readouterr().out


def test_recommend_folds_a_supplied_vpa_signal_into_the_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from chaosagent.capacity.signals import VpaRecommendation

    deps = _recommend_deps()
    deps.vpa_reader = lambda namespace, ref: (
        VpaRecommendation(container="server", cpu="250m", memory="256Mi"),
    )
    settings = CapacitySettings(
        target_id="kind-local", namespace="boutique", workload="deployment/cartservice"
    )
    assert run_recommend(settings, deps) == 0
    out = capsys.readouterr().out
    assert "VPA" in out and "250m" in out


def test_recommend_missing_replica_signal_exits_1(
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = CapacitySettings(
        target_id="kind-local", namespace="boutique", workload="deployment/cartservice"
    )
    assert run_recommend(settings, _recommend_deps(replicas=None)) == 1
    assert "replica count" in capsys.readouterr().err


def test_recommend_unregistered_target_exits_1(
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = CapacitySettings(
        target_id="ghost", namespace="boutique", workload="deployment/cartservice"
    )
    assert run_recommend(settings, _recommend_deps()) == 1
    assert "not registered" in capsys.readouterr().err


def test_recommend_rejects_a_malformed_workload(
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = CapacitySettings(
        target_id="kind-local", namespace="boutique", workload="cartservice"
    )
    assert run_recommend(settings, _recommend_deps()) == 1
    assert "deployment/<name>" in capsys.readouterr().err


@pytest.mark.parametrize("target_utilization", [0.0, -0.5, 1.5])
def test_recommend_rejects_an_out_of_range_target_utilization(
    target_utilization: float, capsys: pytest.CaptureFixture[str]
) -> None:
    # --target-utilization 0 would divide by zero; negatives produce nonsense.
    settings = CapacitySettings(
        target_id="kind-local",
        namespace="boutique",
        workload="deployment/cartservice",
        target_utilization=target_utilization,
    )
    assert run_recommend(settings, _recommend_deps()) == 1
    assert "target-utilization" in capsys.readouterr().err


# -- CLI wiring -----------------------------------------------------------------


def test_cli_scale_subcommand_builds_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    import chaosagent.cli as cli

    captured: dict[str, CapacitySettings] = {}

    def fake_scale(settings: CapacitySettings, deps: object = None) -> int:
        captured["settings"] = settings
        return 0

    monkeypatch.setattr(cli, "run_scale", fake_scale)
    code = cli.main(
        [
            "scale",
            "--target",
            "kind-local",
            "--spec",
            "examples/capacity-cartservice.json",
            "--dry-run",
            "--prom-url",
            "http://localhost:9999",
        ]
    )
    assert code == 0
    settings = captured["settings"]
    assert settings.target_id == "kind-local"
    assert settings.spec_file == Path("examples/capacity-cartservice.json")
    assert settings.dry_run is True
    assert settings.prometheus_url == "http://localhost:9999"


def test_cli_recommend_subcommand_builds_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    import chaosagent.cli as cli

    captured: dict[str, CapacitySettings] = {}

    def fake_recommend(settings: CapacitySettings, deps: object = None) -> int:
        captured["settings"] = settings
        return 0

    monkeypatch.setattr(cli, "run_recommend", fake_recommend)
    code = cli.main(
        [
            "recommend",
            "--target",
            "kind-local",
            "--namespace",
            "boutique",
            "--workload",
            "deployment/cartservice",
            "--opencost-url",
            "http://localhost:9003",
            "--kubeconfig",
            "/tmp/kubeconfig",
            "--context",
            "kind-chaosagent",
        ]
    )
    assert code == 0
    settings = captured["settings"]
    assert settings.namespace == "boutique"
    assert settings.workload == "deployment/cartservice"
    assert settings.opencost_url == "http://localhost:9003"
    # The VPA signal read must follow --target's cluster, not whatever the
    # user's current kubectl context happens to be.
    assert settings.kubeconfig == "/tmp/kubeconfig"
    assert settings.context == "kind-chaosagent"


def test_example_capacity_spec_is_valid() -> None:
    path = Path(__file__).resolve().parents[1] / "examples" / "capacity-cartservice.json"
    spec = CapacitySpec.model_validate_json(path.read_text())
    assert spec.target_id == "kind-local"
    assert spec.namespace == "boutique"
    assert spec.workload == _REF
    # The example must sit inside every policy cap.
    assert spec.ttl_seconds <= 3600
    assert spec.ttl_seconds > spec.baseline_seconds
