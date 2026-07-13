"""observe_until / sample_window against a scripted source and a fake clock."""

from __future__ import annotations

from chaosagent.observe import SteadyStateHypothesis, observe_until, sample_window
from fakes import FakeClock, ScriptedPrometheus

_QUERY = "replicas_available"
_QUERY_B = "error_rate"


def _hypothesis(**overrides: object) -> SteadyStateHypothesis:
    base: dict[str, object] = {
        "name": "replicas",
        "query": _QUERY,
        "comparator": ">=",
        "threshold": 1.0,
    }
    base.update(overrides)
    return SteadyStateHypothesis.model_validate(base)


def test_healthy_run_polls_to_the_deadline() -> None:
    journal: list[str] = []
    clock = FakeClock(start=100.0, journal=journal)
    client = ScriptedPrometheus({_QUERY: [2.0]}, journal=journal)
    outcome = observe_until(
        [_hypothesis()], client, clock, deadline=110.0, interval_seconds=5.0
    )
    assert outcome.breached is False
    assert outcome.breach is None
    # Ticks at t=100, 105, 110 -> three samples, two sleeps.
    assert [r.at for r in outcome.results] == [100.0, 105.0, 110.0]
    assert clock.sleeps == [5.0, 5.0]
    assert all(r.satisfied for r in outcome.results)


def test_breach_returns_on_the_breaching_tick_with_no_sleep_after() -> None:
    journal: list[str] = []
    clock = FakeClock(start=0.0, journal=journal)
    client = ScriptedPrometheus({_QUERY: [2.0, 2.0, 0.0]}, journal=journal)
    outcome = observe_until(
        [_hypothesis()], client, clock, deadline=1000.0, interval_seconds=5.0
    )
    assert outcome.breached is True
    assert outcome.breach is not None
    assert outcome.breach.at == 10.0
    assert outcome.breach.value == 0.0
    # The loop must return immediately on detection: the journalled breach
    # sample is the final event — no sleep after it.
    assert journal[-1] == f"scalar:{_QUERY}=0.0"


def test_consecutive_breaches_rides_out_a_single_blip() -> None:
    clock = FakeClock()
    client = ScriptedPrometheus({_QUERY: [2.0, 0.0, 2.0, 0.0, 0.0]})
    outcome = observe_until(
        [_hypothesis(consecutive_breaches=2)],
        client,
        clock,
        deadline=1000.0,
        interval_seconds=5.0,
    )
    assert outcome.breached is True
    # Breach confirmed only on the second consecutive failure (5th tick, t=20).
    assert outcome.breach is not None
    assert outcome.breach.at == 20.0
    assert len(outcome.results) == 5


def test_all_hypotheses_are_evaluated_each_tick() -> None:
    clock = FakeClock()
    client = ScriptedPrometheus({_QUERY: [2.0], _QUERY_B: [0.01]})
    errors = _hypothesis(name="errors", query=_QUERY_B, comparator="<", threshold=0.05)
    outcome = observe_until(
        [_hypothesis(), errors], client, clock, deadline=5.0, interval_seconds=5.0
    )
    names = [r.hypothesis_name for r in outcome.results]
    assert names == ["replicas", "errors", "replicas", "errors"]


def test_breach_identifies_the_failing_hypothesis() -> None:
    clock = FakeClock()
    client = ScriptedPrometheus({_QUERY: [2.0], _QUERY_B: [0.5]})
    errors = _hypothesis(name="errors", query=_QUERY_B, comparator="<", threshold=0.05)
    outcome = observe_until(
        [_hypothesis(), errors], client, clock, deadline=1000.0, interval_seconds=5.0
    )
    assert outcome.breached is True
    assert outcome.breach is not None
    assert outcome.breach.hypothesis_name == "errors"


def test_past_deadline_still_samples_once() -> None:
    clock = FakeClock(start=50.0)
    client = ScriptedPrometheus({_QUERY: [2.0]})
    outcome = observe_until(
        [_hypothesis()], client, clock, deadline=50.0, interval_seconds=5.0
    )
    assert len(outcome.results) == 1
    assert clock.sleeps == []


def test_duplicate_hypothesis_names_do_not_share_a_streak() -> None:
    # Two hypotheses named identically must not share a breach counter: a
    # passing duplicate would otherwise reset a failing one and hide the breach.
    clock = FakeClock()
    client = ScriptedPrometheus({_QUERY: [2.0], _QUERY_B: [0.0]})
    failing = _hypothesis(name="slo", query=_QUERY_B, comparator=">=", threshold=1.0)
    passing = _hypothesis(name="slo", query=_QUERY, comparator=">=", threshold=1.0)
    outcome = observe_until(
        [passing, failing], client, clock, deadline=1000.0, interval_seconds=5.0
    )
    assert outcome.breached is True
    assert outcome.breach is not None
    assert outcome.breach.value == 0.0


def test_sample_window_never_exits_early_on_breach() -> None:
    clock = FakeClock(start=0.0)
    client = ScriptedPrometheus({_QUERY: [0.0, 0.0, 2.0]})
    results = sample_window(
        [_hypothesis()], client, clock, deadline=10.0, interval_seconds=5.0
    )
    # Breaching samples are collected, not acted on — recovery windows need the
    # full series to score how fast the steady state came back.
    assert [r.satisfied for r in results] == [False, False, True]
