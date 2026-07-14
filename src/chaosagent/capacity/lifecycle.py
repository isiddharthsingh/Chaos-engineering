"""The capacity lifecycle — one bounded replica change, auto-revert on breach.

Safety shape (the capacity analogue of the experiment lifecycle):
  * PREFLIGHT re-derives the ProposedAction from the registry, live probes,
    and the workload's *live* replica count, then runs the deterministic
    engine AND a server-side dry-run of the scale patch (the Kyverno
    ``cap-replica-change`` policy matches the ``/scale`` subresource) — a
    denial at either layer means nothing is ever written.
  * A breach in OBSERVE triggers the revert on the same tick, before any
    sleep. The whole machine is synchronous and beneath the LLM.
  * Success keeps the change; a verified right-size is the deliverable, not an
    experiment to undo. Every exit path releases the write binding.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from chaosagent.agents.permission import ActionBinding, PermissionGate
from chaosagent.capacity.spec import CapacitySpec, WorkloadRef
from chaosagent.clock import Clock
from chaosagent.domain.actions import ProposedAction, ReplicaChange
from chaosagent.domain.enums import ActionType
from chaosagent.domain.policy import PolicyDecision
from chaosagent.execute import AppliedScale, ExecutionDenied
from chaosagent.observe import HypothesisResult, ScalarSource, observe_until
from chaosagent.policy import PolicyEngine
from chaosagent.registry import TargetNotFoundError, TargetRegistry
from chaosagent.resolve import resolve_action


class CapacityState(StrEnum):
    PLAN = "plan"
    PREFLIGHT = "preflight"
    BASELINE = "baseline"
    APPLY = "apply"
    OBSERVE = "observe"
    VERIFY = "verify"
    REVERT = "revert"
    REPORT = "report"
    DONE = "done"
    FAILED = "failed"


class CapacityExecutor(Protocol):
    """The slice of the scale executor the lifecycle drives (fakeable in tests)."""

    def read_replicas(self, ref: WorkloadRef, namespace: str) -> int: ...

    def dry_run(
        self, ref: WorkloadRef, namespace: str, replicas: int, binding: ActionBinding
    ) -> None: ...

    def apply(
        self, ref: WorkloadRef, namespace: str, replicas: int, binding: ActionBinding
    ) -> AppliedScale: ...

    def revert(self, applied: AppliedScale) -> None: ...


@dataclass
class CapacityDeps:
    """Everything the state machine touches, injected for determinism."""

    registry: TargetRegistry
    engine: PolicyEngine
    gate: PermissionGate
    executor: CapacityExecutor
    metrics: ScalarSource
    clock: Clock
    #: Whether an alert/incident is firing for the namespace — the engine's
    #: incident-freeze covers capacity actions. None means "no probe wired" and
    #: is treated as no incident; the live runner supplies one.
    incident_active: Callable[[str], bool] | None = None


class CapacityTransition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    state: CapacityState
    at: float


class CapacityRun(BaseModel):
    """The full record of one capacity run — everything the report needs."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    spec: CapacitySpec
    state: CapacityState = CapacityState.PLAN
    transitions: list[CapacityTransition] = Field(default_factory=list)
    baseline_results: list[HypothesisResult] = Field(default_factory=list)
    settle_results: list[HypothesisResult] = Field(default_factory=list)
    preflight: PolicyDecision | None = None
    #: The live count read at PREFLIGHT — the known-good state a revert restores.
    previous_replicas: int | None = None
    desired_replicas: int | None = None
    applied_at: float | None = None
    breach_detected_at: float | None = None
    reverted_at: float | None = None
    revert_reason: str | None = None
    failure_reason: str | None = None
    #: Set if a revert patch could not be confirmed — the workload may still be
    #: at the new count even though the run decided to revert.
    revert_error: str | None = None
    completed_at: float | None = None

    @property
    def reverted(self) -> bool:
        return self.reverted_at is not None

    @property
    def failed_from(self) -> CapacityState | None:
        """The state the run was in when it failed (None unless FAILED)."""
        if self.state is not CapacityState.FAILED:
            return None
        for transition in reversed(self.transitions):
            if transition.state is not CapacityState.FAILED:
                return transition.state
        return None


def _enter(run: CapacityRun, state: CapacityState, clock: Clock) -> None:
    run.state = state
    run.transitions.append(CapacityTransition(state=state, at=clock.now()))


def _fail(run: CapacityRun, clock: Clock, reason: str) -> CapacityRun:
    run.failure_reason = reason
    _enter(run, CapacityState.FAILED, clock)
    run.completed_at = clock.now()
    return run


def _safe_revert(deps: CapacityDeps, applied: AppliedScale, run: CapacityRun) -> None:
    """Best-effort move back to the recorded known-good count; never raises.
    Moving toward safety must not be blockable; a failed revert is recorded
    (append, never overwrite)."""
    try:
        deps.executor.revert(applied)
    except Exception as exc:  # noqa: BLE001 — the revert path must swallow everything
        message = (
            f"revert of {applied.kind}/{applied.name} to {applied.previous} failed: {exc}"
        )
        run.revert_error = f"{run.revert_error}; {message}" if run.revert_error else message


def _describe_breach(spec: CapacitySpec, breach: HypothesisResult) -> str:
    hypothesis = next(h for h in spec.hypotheses if h.name == breach.hypothesis_name)
    observed = "no data" if breach.value is None else f"value {breach.value}"
    return (
        f"steady-state hypothesis {breach.hypothesis_name!r} breached: {observed} "
        f"failed `{hypothesis.query} {hypothesis.comparator.value} {hypothesis.threshold}`"
    )


def run_capacity_lifecycle(
    spec: CapacitySpec, deps: CapacityDeps, *, dry_run_only: bool = False
) -> CapacityRun:
    """Run one capacity change through the full state machine and return its
    record. Never raises for policy or steady-state refusals — those end in
    FAILED with the reason recorded, because a refusal is a *result*.
    """
    clock = deps.clock
    run = CapacityRun(run_id=secrets.token_hex(6), spec=spec)
    _enter(run, CapacityState.PLAN, clock)

    # -- PREFLIGHT: registry -> probes -> live count -> engine -> bind -> dry-run
    _enter(run, CapacityState.PREFLIGHT, clock)
    try:
        target = deps.registry.get(spec.target_id)
    except TargetNotFoundError:
        return _fail(
            run, clock, f"target {spec.target_id!r} is not registered; cannot verify scope"
        )
    try:
        incident = bool(deps.incident_active(spec.namespace)) if deps.incident_active else False
    except Exception as exc:
        return _fail(
            run,
            clock,
            f"pre-flight probe failed for namespace {spec.namespace!r}: {exc} "
            "(failing closed)",
        )
    try:
        current = deps.executor.read_replicas(spec.workload, spec.namespace)
    except Exception as exc:
        return _fail(
            run,
            clock,
            f"could not read the current replica count of "
            f"{spec.workload.kind}/{spec.workload.name}: {exc}",
        )
    run.previous_replicas = current
    run.desired_replicas = spec.desired_replicas
    action = ProposedAction(
        action_type=ActionType.SCALE_WORKLOAD,
        target_id=spec.target_id,
        environment=target.environment,
        namespace=spec.namespace,
        workload=spec.workload,
        replica_change=ReplicaChange(current=current, desired=spec.desired_replicas),
        ttl_seconds=spec.ttl_seconds,
        incident_active=incident,
    )
    action = resolve_action(action, target)
    decision = deps.engine.evaluate(action)
    run.preflight = decision
    if not decision.allowed:
        return _fail(run, clock, f"policy pre-flight denied: {decision.reason()}")
    binding = deps.gate.bind(action, decision)
    try:
        deps.executor.dry_run(spec.workload, spec.namespace, spec.desired_replicas, binding)
    except ExecutionDenied as exc:
        deps.gate.unbind(binding)
        return _fail(run, clock, f"server-side dry-run denied: {exc}")
    except Exception as exc:
        # Fail closed like every sibling step: a connection error mid-dry-run
        # must end in a FAILED report with the binding released, not a raw
        # traceback with the write slot still held.
        deps.gate.unbind(binding)
        return _fail(run, clock, f"server-side dry-run failed: {exc} (failing closed)")
    if dry_run_only:
        deps.gate.unbind(binding)
        _enter(run, CapacityState.DONE, clock)
        run.completed_at = clock.now()
        return run

    # -- BASELINE: the steady state must hold before we may change anything ----
    _enter(run, CapacityState.BASELINE, clock)
    try:
        baseline = observe_until(
            spec.hypotheses,
            deps.metrics,
            clock,
            deadline=clock.now() + spec.baseline_seconds,
            interval_seconds=spec.observe_interval_seconds,
        )
    except Exception as exc:
        deps.gate.unbind(binding)
        return _fail(run, clock, f"baseline observation failed: {exc}")
    run.baseline_results = list(baseline.results)
    if baseline.breached and baseline.breach is not None:
        deps.gate.unbind(binding)
        return _fail(
            run,
            clock,
            "steady state not met; refusing to scale "
            f"({_describe_breach(spec, baseline.breach)})",
        )

    # -- APPLY ------------------------------------------------------------------
    _enter(run, CapacityState.APPLY, clock)
    try:
        applied = deps.executor.apply(
            spec.workload, spec.namespace, spec.desired_replicas, binding
        )
    except Exception as exc:
        deps.gate.unbind(binding)
        return _fail(run, clock, f"scale apply failed: {exc}")
    run.applied_at = applied.applied_at

    # -- OBSERVE: the steady state must survive the settle window ---------------
    _enter(run, CapacityState.OBSERVE, clock)
    try:
        outcome = observe_until(
            spec.hypotheses,
            deps.metrics,
            clock,
            deadline=run.applied_at + spec.settle_seconds,
            interval_seconds=spec.observe_interval_seconds,
        )
    except Exception as exc:
        # Blind with a fresh change applied is not a state we stay in: move
        # back to the recorded known-good count, then fail.
        _safe_revert(deps, applied, run)
        deps.gate.unbind(binding)
        return _fail(
            run,
            clock,
            f"observation failed after the change was applied; reverted to "
            f"{applied.previous} replicas: {exc}",
        )
    run.settle_results = list(outcome.results)

    if outcome.breached and outcome.breach is not None:
        # AUTO-REVERT: the patch back to the previous count happens on the
        # breaching tick, before any sleep or bookkeeping. This path is
        # deterministic and beneath the LLM — the capacity abort.
        _safe_revert(deps, applied, run)
        run.breach_detected_at = outcome.breach.at
        run.reverted_at = clock.now()
        run.revert_reason = _describe_breach(spec, outcome.breach)
        _enter(run, CapacityState.REVERT, clock)
    else:
        # Success keeps the change — a verified right-size is the deliverable.
        _enter(run, CapacityState.VERIFY, clock)

    deps.gate.unbind(binding)
    _enter(run, CapacityState.REPORT, clock)
    _enter(run, CapacityState.DONE, clock)
    run.completed_at = clock.now()
    return run
