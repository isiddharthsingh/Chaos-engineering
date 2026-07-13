"""One command, one experiment: the engine behind ``chaosagent run``.

Bridges the async edge (the optional LLM planner) to the sync core with a
single ``asyncio.run()``, builds the live dependency set, drives the lifecycle,
and maps the run record to exit codes:

    0  verified          2  policy/pre-flight denied
    3  auto-aborted      1  operational error
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from chaosagent.agents.permission import PermissionGate, RunMode
from chaosagent.agents.planner import PlannerError
from chaosagent.analyze import build_report, render_text
from chaosagent.clock import SystemClock
from chaosagent.config import load_policy_config
from chaosagent.domain.targets import Target
from chaosagent.execute import (
    ChaosMeshExecutor,
    build_experimenter_api,
    read_namespace_chaos_enabled,
)
from chaosagent.experiment.lifecycle import (
    ExperimentRun,
    ExperimentState,
    LifecycleDeps,
    run_lifecycle,
)
from chaosagent.experiment.spec import ExperimentSpec
from chaosagent.observe import PrometheusClient
from chaosagent.policy import PolicyEngine
from chaosagent.registry import TargetNotFoundError, TargetRegistry

_DEFAULT_PROMETHEUS_URL = "http://localhost:9090"

_SDK_HINT = (
    "--intent needs the LLM planner; install the agent extra first: "
    "uv sync --extra agent  (or: pip install 'chaosagent[agent]')"
)


class PlannerLike(Protocol):
    """The slice of PlannerHarness the runner drives (fakeable in tests)."""

    async def plan(self, intent: str, *, target: Target, namespace: str) -> ExperimentSpec: ...


@dataclass
class RunSettings:
    """Everything `chaosagent run` collected from the command line."""

    target_id: str
    store: Path | None = None
    spec_file: Path | None = None
    intent: str | None = None
    namespace: str | None = None
    prometheus_url: str | None = None
    kubeconfig: str | None = None
    context: str | None = None
    interval_seconds: float | None = None
    baseline_seconds: int | None = None
    recovery_seconds: int | None = None
    dry_run: bool = False
    output: Path | None = None
    policy: Path | None = None
    model: str = "claude-opus-4-8"


@dataclass
class RunnerDeps:
    """Injected dependency set — tests supply fakes, production builds live ones."""

    lifecycle: LifecycleDeps
    planner: PlannerLike | None = None


def _error(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)


def _resolve_spec(
    settings: RunSettings, registry: TargetRegistry, planner: PlannerLike | None
) -> ExperimentSpec | int:
    """Load the spec from file or plan it from intent; int is an exit code."""
    if settings.spec_file is not None:
        try:
            return ExperimentSpec.model_validate_json(Path(settings.spec_file).read_text())
        except (OSError, ValidationError) as exc:
            _error(f"could not load spec {settings.spec_file}: {exc}")
            return 1
    assert settings.intent is not None
    if settings.namespace is None:
        _error("--intent requires --namespace (the planner is confined to one namespace)")
        return 1
    try:
        target = registry.get(settings.target_id)
    except TargetNotFoundError:
        _error(f"target {settings.target_id!r} is not registered; cannot verify scope")
        return 2
    if planner is None:
        try:
            import claude_agent_sdk  # noqa: F401
        except ImportError:
            _error(_SDK_HINT)
            return 1
        from chaosagent.agents.planner import PlannerHarness

        planner = PlannerHarness(model=settings.model)
    try:
        return asyncio.run(
            planner.plan(settings.intent, target=target, namespace=settings.namespace)
        )
    except PlannerError as exc:
        _error(f"planner failed: {exc}")
        return 1


def _apply_overrides(spec: ExperimentSpec, settings: RunSettings) -> ExperimentSpec | int:
    overrides: dict[str, object] = {}
    if settings.interval_seconds is not None:
        overrides["observe_interval_seconds"] = settings.interval_seconds
    if settings.baseline_seconds is not None:
        overrides["baseline_seconds"] = settings.baseline_seconds
    if settings.recovery_seconds is not None:
        overrides["recovery_seconds"] = settings.recovery_seconds
    if not overrides:
        return spec
    # Re-validate rather than model_copy(update=...), which bypasses the field
    # constraints (interval > 0, baseline/recovery >= 0, ttl > baseline).
    try:
        return ExperimentSpec.model_validate({**spec.model_dump(mode="json"), **overrides})
    except ValidationError as exc:
        _error(f"invalid --interval/--baseline/--recovery override: {exc}")
        return 1


def _build_live_deps(settings: RunSettings, registry: TargetRegistry) -> LifecycleDeps | int:
    try:
        api = build_experimenter_api(kubeconfig=settings.kubeconfig, context=settings.context)
    except ImportError:
        _error(
            "the kubernetes client is required to execute experiments; install the "
            "agent extra: uv sync --extra agent"
        )
        return 1
    clock = SystemClock()
    gate = PermissionGate(mode=RunMode.EXPERIMENT, clock=clock)
    executor = ChaosMeshExecutor(api, gate, clock=clock)
    prometheus_url = (
        settings.prometheus_url
        or os.environ.get("CHAOSAGENT_PROMETHEUS_URL")
        or _DEFAULT_PROMETHEUS_URL
    )
    metrics = PrometheusClient(prometheus_url)

    def _chaos_enabled(namespace: str) -> bool:
        return read_namespace_chaos_enabled(
            namespace, kubeconfig=settings.kubeconfig, context=settings.context
        )

    def _incident_active(namespace: str) -> bool:
        # Feeds the engine's incident-freeze rule: refuse to inject while an
        # alert is firing for the namespace. Watchdog is the always-on
        # heartbeat alert, so it is excluded.
        firing = metrics.instant(
            f'ALERTS{{namespace="{namespace}",alertstate="firing",alertname!="Watchdog"}}'
        )
        return len(firing) > 0

    return LifecycleDeps(
        registry=registry,
        engine=PolicyEngine(config=load_policy_config(settings.policy)),
        gate=gate,
        executor=executor,
        metrics=metrics,
        clock=clock,
        namespace_chaos_enabled=_chaos_enabled,
        concurrent_experiments=executor.count_running,
        incident_active=_incident_active,
    )


def _finish(run: ExperimentRun, settings: RunSettings) -> int:
    if settings.dry_run and run.state is ExperimentState.DONE:
        # No metrics were sampled, so a scored report would be misleading.
        print(
            f"pre-flight passed for {run.spec.title!r}: policy engine and "
            "server-side dry-run both admitted the CR; nothing was injected"
        )
        return 0
    report = build_report(run)
    print(render_text(report))
    if settings.output is not None:
        settings.output.write_text(report.model_dump_json(indent=2))
        print(f"report written to {settings.output}")
    if run.aborted:
        return 3
    if run.state is ExperimentState.DONE:
        return 0
    if run.failed_from in (ExperimentState.PLAN, ExperimentState.PREFLIGHT):
        return 2
    return 1


def run_experiment(settings: RunSettings, deps: RunnerDeps | None = None) -> int:
    """Run one experiment end to end and return the process exit code."""
    registry = deps.lifecycle.registry if deps is not None else TargetRegistry(settings.store)
    spec = _resolve_spec(settings, registry, deps.planner if deps is not None else None)
    if isinstance(spec, int):
        return spec
    if spec.target_id != settings.target_id:
        _error(f"spec targets {spec.target_id!r} but --target is {settings.target_id!r}")
        return 1
    # --namespace is the planner's confinement on the --intent path; if it is
    # also passed alongside --spec it must agree, not be silently ignored.
    if (
        settings.spec_file is not None
        and settings.namespace is not None
        and settings.namespace != spec.namespace
    ):
        _error(
            f"--namespace {settings.namespace!r} conflicts with the spec's "
            f"namespace {spec.namespace!r}"
        )
        return 1
    overridden = _apply_overrides(spec, settings)
    if isinstance(overridden, int):
        return overridden
    spec = overridden

    if deps is not None:
        run = run_lifecycle(spec, deps.lifecycle, dry_run_only=settings.dry_run)
        return _finish(run, settings)

    lifecycle_deps = _build_live_deps(settings, registry)
    if isinstance(lifecycle_deps, int):
        return lifecycle_deps
    metrics = lifecycle_deps.metrics
    try:
        run = run_lifecycle(spec, lifecycle_deps, dry_run_only=settings.dry_run)
    finally:
        if isinstance(metrics, PrometheusClient):
            metrics.close()
    return _finish(run, settings)
