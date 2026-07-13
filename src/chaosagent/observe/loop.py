"""The observe loop — the deterministic primitive beneath the auto-abort.

``observe_until`` polls every hypothesis on a fixed interval and returns on the
breaching tick with **no sleep after detection**; the caller (the lifecycle)
deletes the fault CR before anything else happens. ``sample_window`` is the
non-aborting variant for baseline/recovery windows, where breaching samples are
data to score, not a reason to stop collecting.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from chaosagent.clock import Clock
from chaosagent.observe.hypothesis import HypothesisResult, ScalarSource, SteadyStateHypothesis


@dataclass(frozen=True)
class ObserveOutcome:
    """What the loop saw: every sample taken, plus the confirming breach if any."""

    breached: bool
    breach: HypothesisResult | None
    results: tuple[HypothesisResult, ...]


def observe_until(
    hypotheses: Sequence[SteadyStateHypothesis],
    client: ScalarSource,
    clock: Clock,
    *,
    deadline: float,
    interval_seconds: float,
) -> ObserveOutcome:
    """Poll until the deadline, returning immediately when any hypothesis has
    been unsatisfied ``consecutive_breaches`` ticks in a row. Always samples at
    least once, even if the deadline has already passed."""
    return _tick_loop(
        hypotheses,
        client,
        clock,
        deadline=deadline,
        interval_seconds=interval_seconds,
        stop_on_breach=True,
    )


def sample_window(
    hypotheses: Sequence[SteadyStateHypothesis],
    client: ScalarSource,
    clock: Clock,
    *,
    deadline: float,
    interval_seconds: float,
) -> tuple[HypothesisResult, ...]:
    """Sample every hypothesis until the deadline with no early exit."""
    return _tick_loop(
        hypotheses,
        client,
        clock,
        deadline=deadline,
        interval_seconds=interval_seconds,
        stop_on_breach=False,
    ).results


def _tick_loop(
    hypotheses: Sequence[SteadyStateHypothesis],
    client: ScalarSource,
    clock: Clock,
    *,
    deadline: float,
    interval_seconds: float,
    stop_on_breach: bool,
) -> ObserveOutcome:
    results: list[HypothesisResult] = []
    # Keyed by position, not name: two hypotheses sharing a name must not share
    # a streak counter (a passing duplicate would reset a failing one, hiding
    # the breach and defeating the auto-abort).
    streaks: list[int] = [0] * len(hypotheses)
    while True:
        at = clock.now()
        for index, hypothesis in enumerate(hypotheses):
            result = hypothesis.evaluate(client, at=at)
            results.append(result)
            if result.satisfied:
                streaks[index] = 0
                continue
            streaks[index] += 1
            if stop_on_breach and streaks[index] >= hypothesis.consecutive_breaches:
                return ObserveOutcome(breached=True, breach=result, results=tuple(results))
        if clock.now() >= deadline:
            return ObserveOutcome(breached=False, breach=None, results=tuple(results))
        clock.sleep(interval_seconds)
