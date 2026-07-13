"""Analyst: phase stats, verdicts, the pinned resilience score, and suggestions."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from chaosagent.analyze import ProbeWeights, build_report, render_text
from chaosagent.experiment import ExperimentRun, ExperimentSpec, ExperimentState, StateTransition
from chaosagent.observe import HypothesisResult

_QUERY = "replicas_available"


def _spec(hypothesis_names: list[str] | None = None) -> ExperimentSpec:
    names = hypothesis_names or ["replicas"]
    return ExperimentSpec.model_validate(
        {
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
                {"name": name, "query": _QUERY, "comparator": ">=", "threshold": 1.0}
                for name in names
            ],
            "ttl_seconds": 300,
        }
    )


def _samples(name: str, satisfied_flags: list[bool], at0: float = 0.0) -> list[HypothesisResult]:
    return [
        HypothesisResult(
            hypothesis_name=name,
            at=at0 + i * 5.0,
            value=2.0 if satisfied else 0.0,
            satisfied=satisfied,
        )
        for i, satisfied in enumerate(satisfied_flags)
    ]


def _run(
    *,
    hypothesis_names: list[str] | None = None,
    baseline: list[HypothesisResult],
    during: list[HypothesisResult],
    recovery: list[HypothesisResult],
    aborted_at: float | None = None,
    breach_detected_at: float | None = None,
    abort_reason: str | None = None,
) -> ExperimentRun:
    return ExperimentRun(
        run_id="abc123",
        spec=_spec(hypothesis_names),
        state=ExperimentState.DONE,
        transitions=[StateTransition(state=ExperimentState.DONE, at=100.0)],
        baseline_results=baseline,
        during_results=during,
        recovery_results=recovery,
        injected_at=10.0,
        breach_detected_at=breach_detected_at,
        aborted_at=aborted_at,
        abort_reason=abort_reason,
        completed_at=100.0,
    )


def test_perfect_run_scores_100() -> None:
    run = _run(
        baseline=_samples("replicas", [True] * 3),
        during=_samples("replicas", [True] * 4),
        recovery=_samples("replicas", [True] * 3),
    )
    report = build_report(run)
    assert report.resilience_score == 100.0
    verdict = report.hypotheses[0]
    assert verdict.baseline_ok is True
    assert verdict.held_during_fault is True
    assert verdict.recovered is True
    assert verdict.during.fraction == 1.0
    assert report.aborted is False
    assert report.time_to_abort_seconds is None
    assert report.suggestions == ()


def test_partial_during_full_recovery_scores_by_the_pinned_formula() -> None:
    run = _run(
        baseline=_samples("replicas", [True] * 3),
        during=_samples("replicas", [True, False, True, False]),  # 0.5 held
        recovery=_samples("replicas", [True] * 4),
    )
    report = build_report(run)
    # 100 * (0.6 * 0.5 + 0.4 * 1.0) = 70.0
    assert report.resilience_score == 70.0
    verdict = report.hypotheses[0]
    assert verdict.held_during_fault is False
    assert verdict.recovered is True


def test_overall_score_is_the_minimum_across_hypotheses() -> None:
    run = _run(
        hypothesis_names=["replicas", "errors"],
        baseline=_samples("replicas", [True] * 2) + _samples("errors", [True] * 2),
        during=_samples("replicas", [True] * 4) + _samples("errors", [False] * 4),
        recovery=_samples("replicas", [True] * 2) + _samples("errors", [True] * 2),
    )
    report = build_report(run)
    scores = {verdict.name: verdict.score for verdict in report.hypotheses}
    assert scores["replicas"] == 100.0
    assert scores["errors"] == 40.0  # 100 * (0.6*0 + 0.4*1)
    assert report.resilience_score == 40.0


def test_aborted_run_is_capped_at_30() -> None:
    run = _run(
        baseline=_samples("replicas", [True] * 3),
        during=_samples("replicas", [True, True, True, False]),  # 0.75 held
        recovery=_samples("replicas", [True] * 3),
        aborted_at=26.0,
        breach_detected_at=25.0,
        abort_reason="steady-state hypothesis 'replicas' breached",
    )
    report = build_report(run)
    assert report.aborted is True
    assert report.time_to_abort_seconds == 1.0
    assert report.resilience_score == 30.0  # 85 raw, capped by the abort


def test_breached_pod_fault_suggests_pdb_and_min_replicas() -> None:
    run = _run(
        baseline=_samples("replicas", [True] * 3),
        during=_samples("replicas", [True, False, False, False]),
        recovery=_samples("replicas", [False, False, True]),
        aborted_at=20.0,
        breach_detected_at=20.0,
    )
    report = build_report(run)
    ids = [suggestion.id for suggestion in report.suggestions]
    assert "add-pdb" in ids
    assert "raise-min-replicas" in ids
    # Partial (non-zero, non-total) hold during the fault -> resilience gap.
    assert "add-retries-timeouts" in ids


def test_unrecovered_run_suggests_investigation() -> None:
    run = _run(
        baseline=_samples("replicas", [True] * 3),
        during=_samples("replicas", [False] * 4),
        recovery=_samples("replicas", [False, False, False]),
    )
    report = build_report(run)
    assert report.hypotheses[0].recovered is False
    assert "investigate-recovery" in [suggestion.id for suggestion in report.suggestions]


def test_failed_preflight_run_reports_without_samples() -> None:
    run = ExperimentRun(
        run_id="abc123",
        spec=_spec(),
        state=ExperimentState.FAILED,
        transitions=[
            StateTransition(state=ExperimentState.PLAN, at=0.0),
            StateTransition(state=ExperimentState.PREFLIGHT, at=0.0),
            StateTransition(state=ExperimentState.FAILED, at=0.0),
        ],
        failure_reason="policy pre-flight denied: [require-chaos-namespace] ...",
    )
    report = build_report(run)
    assert report.resilience_score == 0.0
    assert report.hypotheses[0].during.samples == 0
    # The fault never ran; prescribing fixes would be noise.
    assert report.suggestions == ()
    assert report.failure_reason is not None
    # And the text must not claim a breach that never had a chance to happen.
    text = render_text(report)
    assert "not exercised" in text
    assert "NOT recovered" not in text


def test_render_text_carries_the_essentials() -> None:
    run = _run(
        baseline=_samples("replicas", [True] * 3),
        during=_samples("replicas", [True, False, True, False]),
        recovery=_samples("replicas", [True] * 3),
        aborted_at=26.0,
        breach_detected_at=25.0,
        abort_reason="steady-state hypothesis 'replicas' breached",
    )
    text = render_text(build_report(run))
    assert "cartservice survives a one-third pod kill" in text
    assert "abc123" in text
    assert "30.0" in text  # capped score
    assert "replicas" in text
    assert "ABORTED" in text
    assert "add-pdb" in text


# -- Probe kinds + the weighted rubric (Phase 2) ---------------------------------


def test_probe_results_tag_kind_and_window() -> None:
    run = _run(
        baseline=_samples("replicas", [True] * 3),
        during=_samples("replicas", [True] * 4),
        recovery=_samples("replicas", [True] * 3),
    )
    verdict = build_report(run).hypotheses[0]
    assert verdict.start_ok is True
    probes = {(p.kind.value, p.window.value): p for p in verdict.probes}
    assert set(probes) == {
        ("start", "baseline"),
        ("continuous", "during"),
        ("continuous", "recovery"),
        ("end", "recovery"),
    }
    assert probes[("start", "baseline")].samples == 1  # one-shot
    assert probes[("continuous", "during")].samples == 4
    assert probes[("continuous", "recovery")].samples == 3
    assert probes[("end", "recovery")].samples == 1  # one-shot
    assert all(p.fraction == 1.0 for p in verdict.probes)


def test_start_probe_is_the_last_baseline_sample() -> None:
    run = _run(
        baseline=_samples("replicas", [True, True, False]),  # breached at inject time
        during=_samples("replicas", [True] * 4),
        recovery=_samples("replicas", [True] * 3),
    )
    verdict = build_report(run).hypotheses[0]
    assert verdict.start_ok is False
    probes = {(p.kind.value, p.window.value): p.fraction for p in verdict.probes}
    assert probes[("start", "baseline")] == 0.0


def test_default_weights_pin_the_phase_1_formula() -> None:
    run = _run(
        baseline=_samples("replicas", [True] * 3),
        during=_samples("replicas", [True, False, True, False]),  # 0.5 held
        recovery=_samples("replicas", [True] * 4),
    )
    pinned = build_report(run)
    explicit = build_report(run, weights=ProbeWeights())
    assert pinned.resilience_score == explicit.resilience_score == 70.0


def test_custom_probe_weights_contribute_deterministically() -> None:
    run = _run(
        baseline=_samples("replicas", [True] * 3),
        during=_samples("replicas", [True, False, True, False]),  # 0.5 held
        recovery=_samples("replicas", [True] * 4),  # recovered, end ok
    )
    weights = ProbeWeights(start=0.2, during=0.4, recovery=0.2, end=0.2)
    report = build_report(run, weights=weights)
    # 100 * (0.2*1.0 + 0.4*0.5 + 0.2*1.0 + 0.2*1.0) = 80.0
    assert report.resilience_score == 80.0


def test_probe_weights_must_sum_to_one() -> None:
    with pytest.raises(ValidationError):
        ProbeWeights(start=0.5, during=0.5, recovery=0.5, end=0.5)
    with pytest.raises(ValidationError):
        ProbeWeights(start=-0.2, during=0.6, recovery=0.4, end=0.2)


def test_one_shot_probes_report_zero_samples_when_never_taken() -> None:
    # A preflight-failed run sampled nothing; the report must not fabricate
    # start/end probe observations that never happened.
    run = ExperimentRun(
        run_id="abc123",
        spec=_spec(),
        state=ExperimentState.FAILED,
        transitions=[StateTransition(state=ExperimentState.FAILED, at=0.0)],
        failure_reason="policy pre-flight denied",
    )
    verdict = build_report(run).hypotheses[0]
    assert all(probe.samples == 0 for probe in verdict.probes)


def test_unconfirmed_rollback_is_surfaced_in_the_report() -> None:
    run = _run(
        baseline=_samples("replicas", [True] * 3),
        during=_samples("replicas", [True] * 4),
        recovery=_samples("replicas", [True] * 3),
    )
    run.rollback_error = "delete of 'chaosagent-load-abc' failed: boom"
    report = build_report(run)
    assert report.rollback_error == "delete of 'chaosagent-load-abc' failed: boom"
    text = render_text(report)
    assert "UNCONFIRMED" in text and "chaosagent-load-abc" in text
