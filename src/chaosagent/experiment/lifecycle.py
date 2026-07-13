"""The lifecycle state machine that runs one experiment with no human in the loop.

Safety shape:
  * PREFLIGHT re-derives the ProposedAction from the registry and live probes,
    then runs the deterministic engine AND a server-side dry-run (Kyverno) —
    a denial at either layer means the executor never applies anything.
  * A breach in OBSERVE triggers the abort delete on the same tick, before any
    sleep. The whole machine is synchronous and beneath the LLM.
  * ROLLBACK always runs (idempotent delete + unbind), so no path leaves a CR
    or an active write binding behind.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from chaosagent.agents.permission import ActionBinding, PermissionGate
from chaosagent.clock import Clock
from chaosagent.domain.actions import ProposedAction
from chaosagent.domain.enums import ActionType
from chaosagent.domain.policy import PolicyDecision
from chaosagent.execute import AppliedExperiment, ExecutionDenied
from chaosagent.experiment.spec import ExperimentSpec
from chaosagent.faults import compose_podchaos
from chaosagent.observe import (
    HypothesisResult,
    ScalarSource,
    observe_until,
    sample_window,
)
from chaosagent.policy import PolicyEngine
from chaosagent.registry import TargetNotFoundError, TargetRegistry
from chaosagent.resolve import resolve_action


class ExperimentState(StrEnum):
    PLAN = "plan"
    PREFLIGHT = "preflight"
    BASELINE = "baseline"
    INJECT = "inject"
    OBSERVE = "observe"
    VERIFY = "verify"
    ABORT = "abort"
    ROLLBACK = "rollback"
    REPORT = "report"
    DONE = "done"
    FAILED = "failed"


class ExperimentExecutor(Protocol):
    """The slice of the executor the lifecycle drives (fakeable in tests)."""

    def dry_run(self, cr: dict[str, Any], binding: ActionBinding) -> None: ...

    def apply(self, cr: dict[str, Any], binding: ActionBinding) -> AppliedExperiment: ...

    def delete(self, applied: AppliedExperiment) -> None: ...


@dataclass
class LifecycleDeps:
    """Everything the state machine touches, injected for determinism."""

    registry: TargetRegistry
    engine: PolicyEngine
    gate: PermissionGate
    executor: ExperimentExecutor
    metrics: ScalarSource
    clock: Clock
    #: Live probes resolved outside the LLM: is the namespace opted in, and how
    #: many chaosagent experiments already exist there.
    namespace_chaos_enabled: Callable[[str], bool]
    concurrent_experiments: Callable[[str], int]
    #: Whether an alert/incident is firing for the namespace. None means "no
    #: probe wired" and is treated as no incident; the live runner supplies one.
    incident_active: Callable[[str], bool] | None = None


class StateTransition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    state: ExperimentState
    at: float


class ExperimentRun(BaseModel):
    """The full record of one run — everything the analyst needs to score it."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    spec: ExperimentSpec
    state: ExperimentState = ExperimentState.PLAN
    transitions: list[StateTransition] = Field(default_factory=list)
    baseline_results: list[HypothesisResult] = Field(default_factory=list)
    during_results: list[HypothesisResult] = Field(default_factory=list)
    recovery_results: list[HypothesisResult] = Field(default_factory=list)
    preflight: PolicyDecision | None = None
    cr_name: str | None = None
    cr_namespace: str | None = None
    injected_at: float | None = None
    breach_detected_at: float | None = None
    aborted_at: float | None = None
    abort_reason: str | None = None
    failure_reason: str | None = None
    #: Set if a teardown delete could not be confirmed (the fault self-reverts on
    #: its own duration regardless — this flags that we could not verify it).
    rollback_error: str | None = None
    completed_at: float | None = None

    @property
    def aborted(self) -> bool:
        return self.aborted_at is not None

    @property
    def failed_from(self) -> ExperimentState | None:
        """The state the run was in when it failed (None unless FAILED)."""
        if self.state is not ExperimentState.FAILED:
            return None
        for transition in reversed(self.transitions):
            if transition.state is not ExperimentState.FAILED:
                return transition.state
        return None


def _enter(run: ExperimentRun, state: ExperimentState, clock: Clock) -> None:
    run.state = state
    run.transitions.append(StateTransition(state=state, at=clock.now()))


def _fail(run: ExperimentRun, clock: Clock, reason: str) -> ExperimentRun:
    run.failure_reason = reason
    _enter(run, ExperimentState.FAILED, clock)
    run.completed_at = clock.now()
    return run


def _safe_delete(deps: LifecycleDeps, applied: AppliedExperiment, run: ExperimentRun) -> None:
    """Best-effort teardown that never raises. Moving toward safety must not be
    blockable; a failed delete is recorded (the fault still self-reverts on its
    own duration), and ROLLBACK retries the delete idempotently."""
    try:
        deps.executor.delete(applied)
    except Exception as exc:  # noqa: BLE001 — teardown must swallow everything
        run.rollback_error = f"delete of {applied.name!r} failed: {exc}"


def _describe_breach(spec: ExperimentSpec, breach: HypothesisResult) -> str:
    hypothesis = next(h for h in spec.hypotheses if h.name == breach.hypothesis_name)
    observed = "no data" if breach.value is None else f"value {breach.value}"
    return (
        f"steady-state hypothesis {breach.hypothesis_name!r} breached: {observed} "
        f"failed `{hypothesis.query} {hypothesis.comparator.value} {hypothesis.threshold}`"
    )


def run_lifecycle(
    spec: ExperimentSpec, deps: LifecycleDeps, *, dry_run_only: bool = False
) -> ExperimentRun:
    """Run one experiment through the full state machine and return its record.

    Never raises for policy or steady-state refusals — those end in FAILED with
    the reason recorded, because a refusal is a *result*, not an error.
    """
    clock = deps.clock
    run = ExperimentRun(run_id=secrets.token_hex(6), spec=spec)
    _enter(run, ExperimentState.PLAN, clock)

    # -- PREFLIGHT: registry -> engine -> bind -> server-side dry-run ----------
    _enter(run, ExperimentState.PREFLIGHT, clock)
    try:
        target = deps.registry.get(spec.target_id)
    except TargetNotFoundError:
        return _fail(
            run, clock, f"target {spec.target_id!r} is not registered; cannot verify scope"
        )
    try:
        chaos_enabled = deps.namespace_chaos_enabled(spec.namespace)
        # Counting experiments lists chaos CRs as the experimenter SA, whose
        # RBAC only exists in opted-in namespaces — skip it where chaos is off
        # (the engine denies those regardless, with the right rule id).
        concurrent = deps.concurrent_experiments(spec.namespace) if chaos_enabled else 0
        incident = bool(deps.incident_active(spec.namespace)) if deps.incident_active else False
    except Exception as exc:
        return _fail(
            run,
            clock,
            f"pre-flight probe failed for namespace {spec.namespace!r}: {exc} "
            "(failing closed)",
        )
    action = ProposedAction(
        action_type=ActionType.INJECT_FAULT,
        target_id=spec.target_id,
        environment=target.environment,
        namespace=spec.namespace,
        namespace_chaos_enabled=chaos_enabled,
        fault=spec.fault,
        ttl_seconds=spec.ttl_seconds,
        concurrent_experiments=concurrent,
        incident_active=incident,
    )
    action = resolve_action(action, target)
    decision = deps.engine.evaluate(action)
    run.preflight = decision
    if not decision.allowed:
        return _fail(run, clock, f"policy pre-flight denied: {decision.reason()}")
    try:
        cr = compose_podchaos(
            spec.fault, namespace=spec.namespace, container_names=spec.fault.container_names
        )
    except ValueError as exc:  # includes UnsupportedFaultError
        return _fail(run, clock, str(exc))
    run.cr_name = str(cr["metadata"]["name"])
    run.cr_namespace = spec.namespace
    binding = deps.gate.bind(action, decision)
    try:
        deps.executor.dry_run(cr, binding)
    except ExecutionDenied as exc:
        deps.gate.unbind(binding)
        return _fail(run, clock, f"server-side dry-run denied: {exc}")
    if dry_run_only:
        deps.gate.unbind(binding)
        _enter(run, ExperimentState.DONE, clock)
        run.completed_at = clock.now()
        return run

    # -- BASELINE: the steady state must hold before we may inject -------------
    _enter(run, ExperimentState.BASELINE, clock)
    try:
        baseline = observe_until(
            spec.hypotheses,
            deps.metrics,
            clock,
            deadline=clock.now() + spec.baseline_seconds,
            interval_seconds=spec.observe_interval_seconds,
        )
    except Exception as exc:
        # Nothing injected yet; just release the binding and report the failure.
        deps.gate.unbind(binding)
        return _fail(run, clock, f"baseline observation failed: {exc}")
    run.baseline_results = list(baseline.results)
    if baseline.breached and baseline.breach is not None:
        deps.gate.unbind(binding)
        return _fail(
            run,
            clock,
            "steady state not met; refusing to inject "
            f"({_describe_breach(spec, baseline.breach)})",
        )

    # -- INJECT -----------------------------------------------------------------
    _enter(run, ExperimentState.INJECT, clock)
    try:
        applied = deps.executor.apply(cr, binding)
    except Exception as exc:
        deps.gate.unbind(binding)
        return _fail(run, clock, f"inject failed: {exc}")
    run.injected_at = applied.applied_at

    # -- OBSERVE: poll until the fault's duration elapses or the SLO breaks ----
    _enter(run, ExperimentState.OBSERVE, clock)
    try:
        outcome = observe_until(
            spec.hypotheses,
            deps.metrics,
            clock,
            deadline=run.injected_at + spec.fault.duration_seconds,
            interval_seconds=spec.observe_interval_seconds,
        )
    except Exception as exc:
        # Blind with a live fault is not a state we stay in: tear down, fail.
        _safe_delete(deps, applied, run)
        deps.gate.unbind(binding)
        return _fail(
            run, clock, f"observation failed with the fault live; deleted CR: {exc}"
        )
    run.during_results = list(outcome.results)

    if outcome.breached and outcome.breach is not None:
        # AUTO-ABORT: the delete happens on the breaching tick, before any
        # sleep or bookkeeping. This path is deterministic and beneath the LLM.
        _safe_delete(deps, applied, run)
        run.breach_detected_at = outcome.breach.at
        run.aborted_at = clock.now()
        run.abort_reason = _describe_breach(spec, outcome.breach)
        _enter(run, ExperimentState.ABORT, clock)
    else:
        _enter(run, ExperimentState.VERIFY, clock)

    # -- ROLLBACK: idempotent delete, release the write slot, watch recovery ---
    # Every teardown step is best-effort and the binding is always released, so
    # no path leaves a CR live *and* a write binding held.
    _enter(run, ExperimentState.ROLLBACK, clock)
    _safe_delete(deps, applied, run)
    deps.gate.unbind(binding)
    try:
        run.recovery_results = list(
            sample_window(
                spec.hypotheses,
                deps.metrics,
                clock,
                deadline=clock.now() + spec.recovery_seconds,
                interval_seconds=spec.observe_interval_seconds,
            )
        )
    except Exception as exc:
        # The experiment already ran and rolled back; losing recovery samples
        # must not discard the whole run — record it and still report.
        run.rollback_error = (run.rollback_error or "") + f" recovery sampling failed: {exc}"

    _enter(run, ExperimentState.REPORT, clock)
    _enter(run, ExperimentState.DONE, clock)
    run.completed_at = clock.now()
    return run
