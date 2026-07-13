"""Build the experiment report: per-phase stats, verdicts, score, and fixes.

The resilience score is a weighted probe rubric (probe kinds borrowed from the
LitmusChaos model — ``start`` one-shot before the fault, ``continuous`` sampling
of a window, ``end`` one-shot after recovery):

    per hypothesis: 100 * ( start_weight    * start_ok
                          + during_weight   * during_fraction
                          + recovery_weight * recovery_fraction
                          + end_weight      * recovered )
    overall:        min across hypotheses, capped at 30.0 if the run aborted

The DEFAULT weights (0 / 0.6 / 0.4 / 0) pin the Phase-1 formula
``100 * (0.6 * during_fraction + 0.4 * recovery_fraction)`` exactly, so scores
stay reproducible and comparable across phases; any other rubric must be passed
explicitly (``build_report(run, weights=...)``) and weights must sum to 1.

The suggestion table is deterministic (no LLM): a fix appears iff its trigger
condition is present in the data, so reports are reproducible and comparable.
"""

from __future__ import annotations

import math
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from chaosagent.experiment.lifecycle import ExperimentRun, ExperimentState, StateTransition
from chaosagent.observe.hypothesis import HypothesisResult

_ABORT_SCORE_CAP = 30.0

_POD_FAULT_ACTIONS = ("pod_kill", "pod_failure", "container_kill")


class ProbeKind(StrEnum):
    """Litmus-style probe kinds: when (and how often) a hypothesis is checked."""

    START = "start"  # one-shot: the last sample before injection
    CONTINUOUS = "continuous"  # every sample across a window
    END = "end"  # one-shot: the last sample of the recovery window


class Window(StrEnum):
    BASELINE = "baseline"
    DURING = "during"
    RECOVERY = "recovery"


class ProbeWeights(BaseModel):
    """The scoring rubric: how much each probe contributes to a hypothesis score.

    Defaults pin the Phase-1 formula (0.6 during + 0.4 recovery, one-shots
    ignored). Weights must sum to 1 so scores stay on the same 0-100 scale.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: start probe (one-shot before the fault). Note: any run this lifecycle
    #: actually injected passed its baseline gate, so the start probe holds by
    #: construction there — a nonzero weight mostly rewards having injected at
    #: all and only discriminates for runs scored from other sources.
    start: float = Field(default=0.0, ge=0.0, le=1.0)
    #: continuous probe over the fault window.
    during: float = Field(default=0.6, ge=0.0, le=1.0)
    #: continuous probe over the recovery window.
    recovery: float = Field(default=0.4, ge=0.0, le=1.0)
    #: end probe (one-shot after recovery).
    end: float = Field(default=0.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _sums_to_one(self) -> ProbeWeights:
        total = self.start + self.during + self.recovery + self.end
        if not math.isclose(total, 1.0, abs_tol=1e-9):
            raise ValueError(f"probe weights must sum to 1 (got {total})")
        return self


class ProbeResult(BaseModel):
    """One probe's outcome for one hypothesis, tagged with kind and window."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ProbeKind
    window: Window
    samples: int
    fraction: float


class PhaseStats(BaseModel):
    """How one hypothesis fared across one phase's samples."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    samples: int
    satisfied: int
    fraction: float


class HypothesisVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    baseline: PhaseStats
    during: PhaseStats
    recovery: PhaseStats
    baseline_ok: bool
    #: The steady state held on the last baseline sample (the start probe).
    start_ok: bool
    held_during_fault: bool
    #: The steady state was back by the *end* of the recovery window.
    recovered: bool
    probes: tuple[ProbeResult, ...]
    score: float


class Suggestion(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    text: str


class ExperimentReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    title: str
    target_id: str
    namespace: str
    state: ExperimentState
    aborted: bool
    abort_reason: str | None
    time_to_abort_seconds: float | None
    failure_reason: str | None
    #: Set when a teardown delete could not be confirmed — the CR may still
    #: exist even though the run completed. Never silently swallowed.
    rollback_error: str | None
    resilience_score: float
    hypotheses: tuple[HypothesisVerdict, ...]
    suggestions: tuple[Suggestion, ...]
    transitions: tuple[StateTransition, ...]


def _phase_stats(name: str, results: list[HypothesisResult]) -> PhaseStats:
    samples = [result for result in results if result.hypothesis_name == name]
    satisfied = sum(1 for result in samples if result.satisfied)
    fraction = satisfied / len(samples) if samples else 0.0
    return PhaseStats(samples=len(samples), satisfied=satisfied, fraction=fraction)


def _last_sample_ok(name: str, results: list[HypothesisResult]) -> bool:
    samples = [result for result in results if result.hypothesis_name == name]
    return bool(samples) and samples[-1].satisfied


def _one_shot(kind: ProbeKind, window: Window, ok: bool, taken: bool) -> ProbeResult:
    # samples=0 when the window never produced a sample — a probe that did not
    # run must not be reported as an observation that failed.
    return ProbeResult(
        kind=kind, window=window, samples=1 if taken else 0, fraction=1.0 if ok else 0.0
    )


def _verdict(run: ExperimentRun, name: str, weights: ProbeWeights) -> HypothesisVerdict:
    baseline = _phase_stats(name, run.baseline_results)
    during = _phase_stats(name, run.during_results)
    recovery = _phase_stats(name, run.recovery_results)
    start_ok = _last_sample_ok(name, run.baseline_results)
    recovered = _last_sample_ok(name, run.recovery_results)
    probes = (
        _one_shot(ProbeKind.START, Window.BASELINE, start_ok, baseline.samples > 0),
        ProbeResult(
            kind=ProbeKind.CONTINUOUS,
            window=Window.DURING,
            samples=during.samples,
            fraction=during.fraction,
        ),
        ProbeResult(
            kind=ProbeKind.CONTINUOUS,
            window=Window.RECOVERY,
            samples=recovery.samples,
            fraction=recovery.fraction,
        ),
        _one_shot(ProbeKind.END, Window.RECOVERY, recovered, recovery.samples > 0),
    )
    score = 100.0 * (
        weights.start * float(start_ok)
        + weights.during * during.fraction
        + weights.recovery * recovery.fraction
        + weights.end * float(recovered)
    )
    return HypothesisVerdict(
        name=name,
        baseline=baseline,
        during=during,
        recovery=recovery,
        baseline_ok=baseline.samples > 0 and baseline.fraction == 1.0,
        start_ok=start_ok,
        held_during_fault=during.samples > 0 and during.fraction == 1.0,
        recovered=recovered,
        probes=probes,
        score=round(score, 1),
    )


def _suggest(run: ExperimentRun, verdicts: tuple[HypothesisVerdict, ...]) -> tuple[Suggestion, ...]:
    if run.injected_at is None:
        return ()  # the fault never ran; prescribing fixes would be noise
    suggestions: list[Suggestion] = []
    pod_fault = run.spec.fault.fault_type.value in _POD_FAULT_ACTIONS
    any_breach = any(not verdict.held_during_fault for verdict in verdicts)
    if pod_fault and any_breach:
        suggestions.append(
            Suggestion(
                id="add-pdb",
                text=(
                    "Add a PodDisruptionBudget so voluntary disruptions cannot take the "
                    "workload below its serving minimum."
                ),
            )
        )
        suggestions.append(
            Suggestion(
                id="raise-min-replicas",
                text=(
                    "Raise the deployment's replica floor (or HPA minReplicas) so losing "
                    f"{run.spec.fault.ratio:.0%} of pods leaves enough capacity to serve."
                ),
            )
        )
    if any(not verdict.recovered for verdict in verdicts):
        suggestions.append(
            Suggestion(
                id="investigate-recovery",
                text=(
                    "The steady state had not returned by the end of the recovery window; "
                    "investigate readiness probes, startup time, and pending pods."
                ),
            )
        )
    if any(0.0 < verdict.during.fraction < 1.0 for verdict in verdicts):
        suggestions.append(
            Suggestion(
                id="add-retries-timeouts",
                text=(
                    "The SLO held only part of the time under fault; add client retries "
                    "with timeouts/budgets so brief pod loss does not surface to callers."
                ),
            )
        )
    return tuple(suggestions)


def build_report(run: ExperimentRun, *, weights: ProbeWeights | None = None) -> ExperimentReport:
    """Score a finished run. Pure: same run record (and rubric), same report."""
    weights = weights or ProbeWeights()
    verdicts = tuple(
        _verdict(run, hypothesis.name, weights) for hypothesis in run.spec.hypotheses
    )
    score = min(verdict.score for verdict in verdicts)
    if run.aborted:
        score = min(score, _ABORT_SCORE_CAP)
    time_to_abort = None
    if run.aborted_at is not None and run.breach_detected_at is not None:
        time_to_abort = run.aborted_at - run.breach_detected_at
    return ExperimentReport(
        run_id=run.run_id,
        title=run.spec.title,
        target_id=run.spec.target_id,
        namespace=run.spec.namespace,
        state=run.state,
        aborted=run.aborted,
        abort_reason=run.abort_reason,
        time_to_abort_seconds=time_to_abort,
        failure_reason=run.failure_reason,
        rollback_error=run.rollback_error,
        resilience_score=score,
        hypotheses=verdicts,
        suggestions=_suggest(run, verdicts),
        transitions=tuple(run.transitions),
    )


def render_text(report: ExperimentReport) -> str:
    """Human-readable summary for the CLI."""
    lines = [
        f"experiment : {report.title}",
        f"run        : {report.run_id}  target={report.target_id}  ns={report.namespace}",
        f"state      : {report.state.value.upper()}"
        + (" (ABORTED)" if report.aborted else ""),
        f"resilience : {report.resilience_score:.1f}/100",
    ]
    if report.aborted:
        lines.append(f"abort      : {report.abort_reason}")
        if report.time_to_abort_seconds is not None:
            lines.append(f"             detected->deleted in {report.time_to_abort_seconds:.1f}s")
    if report.failure_reason:
        lines.append(f"failure    : {report.failure_reason}")
    if report.rollback_error:
        lines.append(f"rollback   : UNCONFIRMED — {report.rollback_error}")
    lines.append("hypotheses :")
    for verdict in report.hypotheses:
        if (verdict.baseline.samples, verdict.during.samples, verdict.recovery.samples) == (
            0,
            0,
            0,
        ):
            lines.append(f"  - {verdict.name}: not exercised (no samples taken)")
            continue
        lines.append(
            f"  - {verdict.name}: baseline {verdict.baseline.satisfied}/{verdict.baseline.samples}"
            f", during {verdict.during.satisfied}/{verdict.during.samples}"
            f", recovery {verdict.recovery.satisfied}/{verdict.recovery.samples}"
            f" -> score {verdict.score:.1f}"
            f" ({'held' if verdict.held_during_fault else 'breached'} during fault, "
            f"{'recovered' if verdict.recovered else 'NOT recovered'})"
        )
    if report.suggestions:
        lines.append("suggested fixes:")
        for suggestion in report.suggestions:
            lines.append(f"  - [{suggestion.id}] {suggestion.text}")
    return "\n".join(lines)
