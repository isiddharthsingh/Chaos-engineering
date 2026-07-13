"""Observability — the steady-state eye of the platform.

A synchronous Prometheus client, typed steady-state hypotheses, and the observe
loop whose breach detection drives the deterministic auto-abort. Everything here
is beneath the LLM: no model in the loop, injectable clock, testable offline.
"""

from chaosagent.observe.hypothesis import (
    Comparator,
    HypothesisResult,
    NoDataPolicy,
    ScalarSource,
    SteadyStateHypothesis,
)
from chaosagent.observe.loop import ObserveOutcome, observe_until, sample_window
from chaosagent.observe.prometheus import PrometheusClient, PrometheusError

__all__ = [
    "Comparator",
    "HypothesisResult",
    "NoDataPolicy",
    "ObserveOutcome",
    "PrometheusClient",
    "PrometheusError",
    "ScalarSource",
    "SteadyStateHypothesis",
    "observe_until",
    "sample_window",
]
