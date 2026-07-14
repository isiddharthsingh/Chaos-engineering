"""The engines behind ``chaosagent recommend`` (read-only) and ``chaosagent
scale`` (the capacity loop).

``scale`` exit codes mirror ``run``:

    0  change verified and kept    2  policy/pre-flight denied
    3  auto-reverted               1  operational error

``recommend`` never binds and never writes — its dependency set carries no
executor and no gate — and exits 0, or 1 on an operational error.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from chaosagent.agents.permission import PermissionGate, RunMode
from chaosagent.capacity.lifecycle import (
    CapacityDeps,
    CapacityRun,
    CapacityState,
    run_capacity_lifecycle,
)
from chaosagent.capacity.opencost import OpenCostClient, estimate_monthly_delta
from chaosagent.capacity.recommend import Recommendation, recommend_replicas
from chaosagent.capacity.report import build_capacity_report, render_capacity_text
from chaosagent.capacity.signals import (
    SignalError,
    VpaRecommendation,
    fetch_usage,
    read_live_vpa_recommendations,
)
from chaosagent.capacity.spec import CapacitySpec, WorkloadRef
from chaosagent.clock import SystemClock
from chaosagent.config import load_policy_config
from chaosagent.execute import ScaleExecutor, build_scale_api
from chaosagent.observe import PrometheusClient, PrometheusError, ScalarSource
from chaosagent.policy import PolicyEngine
from chaosagent.registry import TargetNotFoundError, TargetRegistry

_DEFAULT_PROMETHEUS_URL = "http://localhost:9090"


class CostSource(Protocol):
    """Anything that can answer "what does this workload cost per month"."""

    def workload_monthly_cost(self, namespace: str, workload: WorkloadRef) -> float | None: ...


@dataclass
class CapacitySettings:
    """Everything `chaosagent recommend`/`scale` collected from the command line."""

    target_id: str
    store: Path | None = None
    #: scale path
    spec_file: Path | None = None
    dry_run: bool = False
    #: recommend path
    namespace: str | None = None
    workload: str | None = None
    target_utilization: float = 0.6
    lookback_minutes: int = 60
    opencost_url: str | None = None
    #: shared
    prometheus_url: str | None = None
    kubeconfig: str | None = None
    context: str | None = None
    output: Path | None = None
    policy: Path | None = None


@dataclass
class RecommendDeps:
    """Read-only dependency set — deliberately no executor and no gate."""

    metrics: ScalarSource
    registry: TargetRegistry
    cost: CostSource | None = None
    #: Optional VPA signal source — (namespace, workload) -> recommendations.
    vpa_reader: Callable[[str, WorkloadRef], tuple[VpaRecommendation, ...]] | None = None


@dataclass
class ScaleDeps:
    """Injected dependency set — tests supply fakes, production builds live ones."""

    lifecycle: CapacityDeps


def _error(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)


def _prometheus_url(settings: CapacitySettings) -> str:
    return (
        settings.prometheus_url
        or os.environ.get("CHAOSAGENT_PROMETHEUS_URL")
        or _DEFAULT_PROMETHEUS_URL
    )


def _parse_workload(value: str) -> WorkloadRef | None:
    kind, sep, name = value.partition("/")
    if not sep:
        return None
    try:
        return WorkloadRef.model_validate({"kind": kind, "name": name})
    except ValidationError:
        return None


def _render_recommendation(recommendation: Recommendation) -> str:
    workload = recommendation.workload
    arrow = f"{recommendation.current_replicas} -> {recommendation.desired_replicas}"
    if recommendation.desired_replicas == recommendation.current_replicas:
        arrow += " (no change)"
    lines = [
        f"workload   : {workload.kind}/{workload.name}  ns={recommendation.namespace}",
        f"replicas   : {arrow}",
        f"observed   : {recommendation.observed_utilization:.0%} vs target "
        f"{recommendation.target_utilization:.0%}"
        if recommendation.observed_utilization is not None
        else f"observed   : no signal (target {recommendation.target_utilization:.0%})",
        "rationale  :",
    ]
    for line in recommendation.rationale:
        lines.append(f"  - {line}")
    if recommendation.estimated_monthly_delta is not None:
        lines.append(
            f"cost       : {recommendation.estimated_monthly_delta:+.2f}/month "
            "(estimated, advisory)"
        )
    return "\n".join(lines)


def run_recommend(settings: CapacitySettings, deps: RecommendDeps | None = None) -> int:
    """Signals -> recommender -> printed recommendation. Read-only by shape."""
    if settings.namespace is None or settings.workload is None:
        _error("recommend requires --namespace and --workload")
        return 1
    if not 0.0 < settings.target_utilization <= 1.0:
        _error(
            f"--target-utilization must be in (0, 1], got {settings.target_utilization}"
        )
        return 1
    if settings.lookback_minutes <= 0:
        _error(f"--lookback must be a positive number of minutes, got {settings.lookback_minutes}")
        return 1
    ref = _parse_workload(settings.workload)
    if ref is None:
        _error(
            f"--workload {settings.workload!r} must be deployment/<name> or "
            "statefulset/<name>"
        )
        return 1
    opencost: OpenCostClient | None = None
    metrics_to_close: PrometheusClient | None = None
    if deps is None:

        def _live_vpa(namespace: str, workload: WorkloadRef) -> tuple[VpaRecommendation, ...]:
            # The VPA signal is advisory: no kubeconfig, no CRD, or any read
            # failure must not break a read-only recommendation.
            try:
                return read_live_vpa_recommendations(
                    namespace,
                    workload,
                    kubeconfig=settings.kubeconfig,
                    context=settings.context,
                )
            except Exception:  # noqa: BLE001 — advisory signal, never fatal
                return ()

        metrics_to_close = PrometheusClient(_prometheus_url(settings))
        opencost_url = settings.opencost_url or os.environ.get("CHAOSAGENT_OPENCOST_URL")
        if opencost_url:
            opencost = OpenCostClient(opencost_url)
        deps = RecommendDeps(
            metrics=metrics_to_close,
            registry=TargetRegistry(settings.store),
            cost=opencost,
            vpa_reader=_live_vpa,
        )
    try:
        try:
            deps.registry.get(settings.target_id)
        except TargetNotFoundError:
            _error(f"target {settings.target_id!r} is not registered; cannot verify scope")
            return 1
        vpa = deps.vpa_reader(settings.namespace, ref) if deps.vpa_reader is not None else ()
        try:
            usage = fetch_usage(
                deps.metrics,
                settings.namespace,
                ref,
                lookback_minutes=settings.lookback_minutes,
                vpa=vpa,
            )
        except (SignalError, PrometheusError) as exc:
            _error(str(exc))
            return 1
        recommendation = recommend_replicas(
            usage,
            target_utilization=settings.target_utilization,
            config=load_policy_config(settings.policy),
        )
        if deps.cost is not None:
            monthly = deps.cost.workload_monthly_cost(settings.namespace, ref)
            if monthly is not None:
                recommendation = recommendation.model_copy(
                    update={
                        "estimated_monthly_delta": estimate_monthly_delta(
                            monthly,
                            current_replicas=recommendation.current_replicas,
                            desired_replicas=recommendation.desired_replicas,
                        )
                    }
                )
    finally:
        if metrics_to_close is not None:
            metrics_to_close.close()
        if opencost is not None:
            opencost.close()
    print(_render_recommendation(recommendation))
    if settings.output is not None:
        settings.output.write_text(recommendation.model_dump_json(indent=2))
        print(f"recommendation written to {settings.output}")
    return 0


def build_capacity_deps(
    settings: CapacitySettings, registry: TargetRegistry
) -> CapacityDeps | int:
    try:
        api = build_scale_api(kubeconfig=settings.kubeconfig, context=settings.context)
    except ImportError:
        _error(
            "the kubernetes client is required to execute capacity changes; install "
            "the agent extra: uv sync --extra agent"
        )
        return 1
    policy_config = load_policy_config(settings.policy)
    clock = SystemClock()
    gate = PermissionGate(mode=RunMode.EXPERIMENT, clock=clock)
    executor = ScaleExecutor(
        api, gate, clock=clock, revert_step_pct=policy_config.max_replica_pct_change
    )
    metrics = PrometheusClient(_prometheus_url(settings))

    def _incident_active(namespace: str) -> bool:
        # Feeds the engine's incident-freeze rule: refuse to scale while an
        # alert is firing for the namespace. Watchdog is the always-on
        # heartbeat alert, so it is excluded.
        firing = metrics.instant(
            f'ALERTS{{namespace="{namespace}",alertstate="firing",alertname!="Watchdog"}}'
        )
        return len(firing) > 0

    return CapacityDeps(
        registry=registry,
        engine=PolicyEngine(config=policy_config),
        gate=gate,
        executor=executor,
        metrics=metrics,
        clock=clock,
        incident_active=_incident_active,
    )


def capacity_exit_code(run: CapacityRun) -> int:
    """Map a run record to its process exit code (0/2/3/1, see module docstring).

    Exit 2 is a terminal "denied" verdict, so it is reserved for genuine
    denials (engine, admission, or unverifiable scope); operational PREFLIGHT
    failures (an unreachable API server or Prometheus) are exit 1 like every
    other transient error, so automation retries instead of giving up. A
    breach whose revert could not be confirmed is exit 1, not 3 — the workload
    may still be at the new count.
    """
    if run.reverted:
        return 3 if run.revert_error is None else 1
    if run.state is CapacityState.DONE:
        return 0
    if run.failed_from in (CapacityState.PLAN, CapacityState.PREFLIGHT):
        if run.preflight is not None and not run.preflight.allowed:
            return 2
        reason = run.failure_reason or ""
        # These prefixes are produced by chaosagent.capacity.lifecycle.
        if reason.startswith("server-side dry-run denied") or "not registered" in reason:
            return 2
        return 1
    return 1


def _finish(run: CapacityRun, settings: CapacitySettings) -> int:
    if settings.dry_run and run.state is CapacityState.DONE:
        # No metrics were sampled, so a full report would be misleading.
        print(
            f"pre-flight passed for {run.spec.title!r}: policy engine and "
            "server-side dry-run both admitted the scale patch; nothing was applied"
        )
        return 0
    report = build_capacity_report(run)
    print(render_capacity_text(report))
    if settings.output is not None:
        settings.output.write_text(report.model_dump_json(indent=2))
        print(f"report written to {settings.output}")
    return capacity_exit_code(run)


def run_scale(settings: CapacitySettings, deps: ScaleDeps | None = None) -> int:
    """Run one capacity change end to end and return the process exit code."""
    if settings.spec_file is None:
        _error("scale requires --spec")
        return 1
    try:
        spec = CapacitySpec.model_validate_json(Path(settings.spec_file).read_text())
    except (OSError, ValidationError) as exc:
        _error(f"could not load spec {settings.spec_file}: {exc}")
        return 1
    if spec.target_id != settings.target_id:
        _error(f"spec targets {spec.target_id!r} but --target is {settings.target_id!r}")
        return 1

    if deps is not None:
        run = run_capacity_lifecycle(spec, deps.lifecycle, dry_run_only=settings.dry_run)
        return _finish(run, settings)

    lifecycle_deps = build_capacity_deps(settings, TargetRegistry(settings.store))
    if isinstance(lifecycle_deps, int):
        return lifecycle_deps
    metrics = lifecycle_deps.metrics
    try:
        run = run_capacity_lifecycle(spec, lifecycle_deps, dry_run_only=settings.dry_run)
    finally:
        if isinstance(metrics, PrometheusClient):
            metrics.close()
    return _finish(run, settings)
