"""Build the capacity report: outcome, per-phase stats, rationale, and fixes.

Pure given the run record (and the optional recommendation that motivated it):
no cluster, no metrics store, and deliberately no LLM. The suggestion table is
deterministic — a fix appears iff its trigger condition is present in the data,
so reports are reproducible and comparable. Cost is rendered as an advisory
signal only.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from chaosagent.analyze.report import PhaseStats, Suggestion, _phase_stats
from chaosagent.capacity.lifecycle import CapacityRun, CapacityState, CapacityTransition
from chaosagent.capacity.recommend import Recommendation


class CapacityOutcome(StrEnum):
    """What happened to the replica change itself."""

    KEPT = "kept"  # verified through the settle window; the change stands
    REVERTED = "reverted"  # moved back to the recorded known-good count
    NOT_APPLIED = "not_applied"  # refused or failed before any write


class CapacityHypothesisVerdict(BaseModel):
    """How one hypothesis fared across the baseline and settle windows."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    baseline: PhaseStats
    settle: PhaseStats
    baseline_ok: bool
    held_through_settle: bool


class CapacityReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    title: str
    target_id: str
    namespace: str
    workload: str
    state: CapacityState
    outcome: CapacityOutcome
    previous_replicas: int | None
    desired_replicas: int | None
    #: Where the workload ended up: desired when kept, previous when reverted,
    #: None when a failed revert left the actual count unconfirmed.
    final_replicas: int | None
    reverted: bool
    revert_reason: str | None
    time_to_revert_seconds: float | None
    failure_reason: str | None
    #: Set when the revert patch could not be confirmed — the workload may
    #: still be at the new count. Never silently swallowed.
    revert_error: str | None
    hypotheses: tuple[CapacityHypothesisVerdict, ...]
    #: The recommendation rationale that motivated the change, verbatim.
    recommendation_rationale: tuple[str, ...]
    #: Advisory monthly cost delta from OpenCost; never an authority.
    estimated_monthly_delta: float | None
    suggestions: tuple[Suggestion, ...]
    transitions: tuple[CapacityTransition, ...]


def _outcome(run: CapacityRun) -> CapacityOutcome:
    if run.applied_at is None:
        return CapacityOutcome.NOT_APPLIED
    if run.state is CapacityState.DONE and not run.reverted:
        return CapacityOutcome.KEPT
    # Every applied-but-not-kept path (breach, or failure with the change
    # live) moves back toward the previous count; revert_error flags doubt.
    return CapacityOutcome.REVERTED


def _suggest(
    run: CapacityRun, outcome: CapacityOutcome, recommendation: Recommendation | None
) -> tuple[Suggestion, ...]:
    suggestions: list[Suggestion] = []
    if outcome is CapacityOutcome.REVERTED and run.reverted:
        suggestions.append(
            Suggestion(
                id="investigate-settle-breach",
                text=(
                    "The steady state broke inside the settle window and the change was "
                    "reverted; investigate what the new replica count starved before "
                    "retrying."
                ),
            )
        )
    kept_a_change = (
        outcome is CapacityOutcome.KEPT
        and run.previous_replicas is not None
        and run.desired_replicas is not None
        and run.previous_replicas != run.desired_replicas
    )
    if kept_a_change:
        suggestions.append(
            Suggestion(
                id="set-hpa-bounds",
                text=(
                    "Encode the verified size as HPA bounds (minReplicas at the kept "
                    "count) so the autoscaler holds the right-size instead of drifting "
                    "back."
                ),
            )
        )
    if recommendation is not None:
        observed = recommendation.observed_utilization
        if observed is not None and observed > 1.0:
            suggestions.append(
                Suggestion(
                    id="raise-requests",
                    text=(
                        "Observed utilization exceeds the declared resource requests; "
                        "raise the requests so scheduling and autoscaling see real demand."
                    ),
                )
            )
        if "revert-admissible" in recommendation.clamps:
            suggestions.append(
                Suggestion(
                    id="lower-requests",
                    text=(
                        "The proportional size was clamped at the revert-admissible "
                        "floor; the workload stays over-provisioned — lower its requests "
                        "or repeat bounded downscales."
                    ),
                )
            )
    return tuple(suggestions)


def build_capacity_report(
    run: CapacityRun, *, recommendation: Recommendation | None = None
) -> CapacityReport:
    """Score a finished capacity run. Pure: same inputs, same report."""
    outcome = _outcome(run)
    verdicts = []
    for hypothesis in run.spec.hypotheses:
        baseline = _phase_stats(hypothesis.name, run.baseline_results)
        settle = _phase_stats(hypothesis.name, run.settle_results)
        verdicts.append(
            CapacityHypothesisVerdict(
                name=hypothesis.name,
                baseline=baseline,
                settle=settle,
                baseline_ok=baseline.samples > 0 and baseline.fraction == 1.0,
                held_through_settle=settle.samples > 0 and settle.fraction == 1.0,
            )
        )
    time_to_revert = None
    if run.reverted_at is not None and run.breach_detected_at is not None:
        time_to_revert = run.reverted_at - run.breach_detected_at
    if outcome is CapacityOutcome.KEPT:
        final = run.desired_replicas
    elif outcome is CapacityOutcome.REVERTED and run.revert_error is not None:
        # The revert patch could not be confirmed: the workload's actual count
        # is unknown, and reporting `previous` here would tell automation the
        # workload is safely back when it may not be.
        final = None
    else:
        final = run.previous_replicas
    return CapacityReport(
        run_id=run.run_id,
        title=run.spec.title,
        target_id=run.spec.target_id,
        namespace=run.spec.namespace,
        workload=f"{run.spec.workload.kind}/{run.spec.workload.name}",
        state=run.state,
        outcome=outcome,
        previous_replicas=run.previous_replicas,
        desired_replicas=run.desired_replicas,
        final_replicas=final,
        reverted=run.reverted,
        revert_reason=run.revert_reason,
        time_to_revert_seconds=time_to_revert,
        failure_reason=run.failure_reason,
        revert_error=run.revert_error,
        hypotheses=tuple(verdicts),
        recommendation_rationale=(
            recommendation.rationale if recommendation is not None else ()
        ),
        estimated_monthly_delta=(
            recommendation.estimated_monthly_delta if recommendation is not None else None
        ),
        suggestions=_suggest(run, outcome, recommendation),
        transitions=tuple(run.transitions),
    )


def render_capacity_text(report: CapacityReport) -> str:
    """Human-readable summary for the CLI, in the run-report style."""
    lines = [
        f"capacity   : {report.title}",
        f"run        : {report.run_id}  target={report.target_id}  ns={report.namespace}",
        f"workload   : {report.workload}",
        f"state      : {report.state.value.upper()}"
        + (" (REVERTED)" if report.reverted else ""),
    ]
    previous, desired = report.previous_replicas, report.desired_replicas
    if report.outcome is CapacityOutcome.KEPT and previous is not None:
        lines.append(f"replicas   : {previous} -> {desired} (change kept)")
    elif report.outcome is CapacityOutcome.REVERTED and previous is not None:
        final = "unconfirmed" if report.final_replicas is None else str(report.final_replicas)
        lines.append(f"replicas   : {previous} -> {desired} -> {final} (auto-reverted)")
    elif previous is not None:
        lines.append(f"replicas   : {previous} (nothing applied)")
    if report.reverted:
        lines.append(f"reverted   : {report.revert_reason}")
        if report.time_to_revert_seconds is not None:
            lines.append(
                f"             detected->reverted in {report.time_to_revert_seconds:.1f}s"
            )
    if report.failure_reason:
        lines.append(f"failure    : {report.failure_reason}")
    if report.revert_error:
        lines.append(f"revert     : UNCONFIRMED — {report.revert_error}")
    lines.append("hypotheses :")
    for verdict in report.hypotheses:
        if (verdict.baseline.samples, verdict.settle.samples) == (0, 0):
            lines.append(f"  - {verdict.name}: not exercised (no samples taken)")
            continue
        lines.append(
            f"  - {verdict.name}: baseline {verdict.baseline.satisfied}/"
            f"{verdict.baseline.samples}, settle {verdict.settle.satisfied}/"
            f"{verdict.settle.samples}"
            f" ({'held through settle' if verdict.held_through_settle else 'BREACHED in settle'})"
        )
    if report.recommendation_rationale:
        lines.append("rationale  :")
        for line in report.recommendation_rationale:
            lines.append(f"  - {line}")
    if report.estimated_monthly_delta is not None:
        lines.append(
            f"cost       : {report.estimated_monthly_delta:+.2f}/month (estimated, advisory)"
        )
    if report.suggestions:
        lines.append("suggested fixes:")
        for suggestion in report.suggestions:
            lines.append(f"  - [{suggestion.id}] {suggestion.text}")
    return "\n".join(lines)
