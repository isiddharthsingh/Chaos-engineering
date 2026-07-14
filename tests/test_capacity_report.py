"""Capacity report: kept/reverted outcomes, phase stats, and pinned suggestions."""

from __future__ import annotations

from chaosagent.capacity import (
    CapacityRun,
    CapacitySpec,
    CapacityState,
    CapacityTransition,
    Recommendation,
    WorkloadRef,
)
from chaosagent.capacity.report import (
    CapacityOutcome,
    build_capacity_report,
    render_capacity_text,
)
from chaosagent.observe import HypothesisResult

_QUERY = "latency_p95"
_REF = WorkloadRef(kind="deployment", name="cartservice")


def _spec(desired: int = 3) -> CapacitySpec:
    return CapacitySpec.model_validate(
        {
            "title": "right-size cartservice to observed load",
            "target_id": "kind-local",
            "namespace": "boutique",
            "workload": {"kind": "deployment", "name": "cartservice"},
            "desired_replicas": desired,
            "hypotheses": [
                {"name": "latency", "query": _QUERY, "comparator": "<", "threshold": 200.0}
            ],
            "ttl_seconds": 300,
            "baseline_seconds": 10,
            "settle_seconds": 30,
        }
    )


def _samples(flags: list[bool], at0: float = 0.0) -> list[HypothesisResult]:
    return [
        HypothesisResult(
            hypothesis_name="latency",
            at=at0 + i * 5.0,
            value=100.0 if satisfied else 500.0,
            satisfied=satisfied,
        )
        for i, satisfied in enumerate(flags)
    ]


def _run(**overrides: object) -> CapacityRun:
    base: dict[str, object] = {
        "run_id": "cap123",
        "spec": _spec(),
        "state": CapacityState.DONE,
        "transitions": [CapacityTransition(state=CapacityState.DONE, at=40.0)],
        "baseline_results": _samples([True] * 3),
        "settle_results": _samples([True] * 7, at0=10.0),
        "previous_replicas": 4,
        "desired_replicas": 3,
        "applied_at": 10.0,
        "completed_at": 40.0,
    }
    base.update(overrides)
    return CapacityRun.model_validate(base)


def _recommendation(**overrides: object) -> Recommendation:
    base: dict[str, object] = {
        "namespace": "boutique",
        "workload": _REF,
        "current_replicas": 4,
        "desired_replicas": 3,
        "target_utilization": 0.6,
        "observed_utilization": 0.4,
        "clamps": (),
        "rationale": ("signals over 60m: cpu avg 40% p95 55%, memory avg 30% p95 35%",),
    }
    base.update(overrides)
    return Recommendation.model_validate(base)


def test_kept_change_reports_kept_outcome_and_final_count() -> None:
    report = build_capacity_report(_run())
    assert report.outcome is CapacityOutcome.KEPT
    assert (report.previous_replicas, report.desired_replicas) == (4, 3)
    assert report.final_replicas == 3
    assert report.reverted is False
    verdict = report.hypotheses[0]
    assert verdict.baseline.samples == 3 and verdict.baseline.fraction == 1.0
    assert verdict.settle.samples == 7 and verdict.held_through_settle is True
    # A kept change that moved the count suggests pinning it via HPA bounds —
    # and nothing else without its trigger.
    assert [s.id for s in report.suggestions] == ["set-hpa-bounds"]
    text = render_capacity_text(report)
    assert "replicas   : 4 -> 3 (change kept)" in text
    assert "held through settle" in text


def test_reverted_change_reports_revert_and_time_to_revert() -> None:
    run = _run(
        settle_results=_samples([True, False], at0=10.0),
        breach_detected_at=15.0,
        reverted_at=15.0,
        revert_reason="steady-state hypothesis 'latency' breached: value 500.0",
    )
    report = build_capacity_report(run)
    assert report.outcome is CapacityOutcome.REVERTED
    assert report.final_replicas == 4  # back at the known-good count
    assert report.time_to_revert_seconds == 0.0
    verdict = report.hypotheses[0]
    assert verdict.held_through_settle is False
    assert [s.id for s in report.suggestions] == ["investigate-settle-breach"]
    text = render_capacity_text(report)
    assert "replicas   : 4 -> 3 -> 4 (auto-reverted)" in text
    assert "latency" in text


def test_noop_kept_change_does_not_suggest_hpa_bounds() -> None:
    report = build_capacity_report(_run(spec=_spec(desired=4), desired_replicas=4))
    assert report.outcome is CapacityOutcome.KEPT
    assert report.suggestions == ()


def test_failed_preflight_reports_nothing_applied() -> None:
    run = _run(
        state=CapacityState.FAILED,
        transitions=[
            CapacityTransition(state=CapacityState.PREFLIGHT, at=0.0),
            CapacityTransition(state=CapacityState.FAILED, at=0.0),
        ],
        baseline_results=[],
        settle_results=[],
        applied_at=None,
        failure_reason="policy pre-flight denied: [revert-admissible] ...",
    )
    report = build_capacity_report(run)
    assert report.outcome is CapacityOutcome.NOT_APPLIED
    assert report.final_replicas == 4
    assert report.suggestions == ()
    text = render_capacity_text(report)
    assert "nothing applied" in text
    assert "revert-admissible" in text
    assert "not exercised" in text  # no samples were taken


def test_revert_error_is_surfaced_never_swallowed() -> None:
    run = _run(
        settle_results=_samples([True, False], at0=10.0),
        breach_detected_at=15.0,
        reverted_at=15.0,
        revert_reason="breached",
        revert_error="revert of deployment/cartservice to 4 failed: apiserver 500",
    )
    report = build_capacity_report(run)
    assert report.revert_error is not None
    # A failed revert must not claim the workload is back at the previous
    # count — the actual count is unknown.
    assert report.final_replicas is None
    text = render_capacity_text(report)
    assert "UNCONFIRMED" in text and "apiserver 500" in text
    assert "-> unconfirmed" in text


def test_recommendation_rationale_and_cost_render() -> None:
    rec = _recommendation(estimated_monthly_delta=-12.4)
    report = build_capacity_report(_run(), recommendation=rec)
    assert report.recommendation_rationale == rec.rationale
    assert report.estimated_monthly_delta == -12.4
    text = render_capacity_text(report)
    assert "signals over 60m" in text
    assert "-12.40" in text


def test_over_request_utilization_suggests_raising_requests() -> None:
    rec = _recommendation(observed_utilization=1.3, desired_replicas=6, clamps=("replica-cap",))
    report = build_capacity_report(_run(), recommendation=rec)
    assert "raise-requests" in [s.id for s in report.suggestions]


def test_floor_clamped_downscale_suggests_lowering_requests() -> None:
    rec = _recommendation(observed_utilization=0.1, clamps=("revert-admissible",))
    report = build_capacity_report(_run(), recommendation=rec)
    assert "lower-requests" in [s.id for s in report.suggestions]


def test_no_recommendation_yields_no_signal_suggestions() -> None:
    report = build_capacity_report(_run())
    ids = [s.id for s in report.suggestions]
    assert "raise-requests" not in ids and "lower-requests" not in ids


def test_report_is_deterministic() -> None:
    first = build_capacity_report(_run())
    for _ in range(5):
        assert build_capacity_report(_run()) == first
