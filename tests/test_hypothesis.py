"""SteadyStateHypothesis comparison and evaluation semantics."""

from __future__ import annotations

import pytest

from chaosagent.observe import Comparator, NoDataPolicy, SteadyStateHypothesis


class _OneShotSource:
    """ScalarSource stand-in returning a fixed value and recording the query."""

    def __init__(self, value: float | None) -> None:
        self.value = value
        self.queries: list[str] = []

    def scalar(self, query: str) -> float | None:
        self.queries.append(query)
        return self.value


def _hypothesis(**overrides: object) -> SteadyStateHypothesis:
    base: dict[str, object] = {
        "name": "replicas-available",
        "query": 'kube_deployment_status_replicas_available{deployment="cartservice"}',
        "comparator": ">=",
        "threshold": 1.0,
    }
    base.update(overrides)
    return SteadyStateHypothesis.model_validate(base)


@pytest.mark.parametrize(
    "comparator,value,expected",
    [
        (">=", 1.0, True),
        (">=", 0.0, False),
        (">", 1.0, False),
        (">", 1.5, True),
        ("<", 0.5, True),
        ("<", 1.0, False),
        ("<=", 1.0, True),
        ("<=", 1.1, False),
    ],
)
def test_compare_semantics(comparator: str, value: float, expected: bool) -> None:
    hypothesis = _hypothesis(comparator=comparator)
    assert hypothesis.compare(value) is expected


def test_equality_comparator_is_rejected() -> None:
    with pytest.raises(ValueError):
        _hypothesis(comparator="==")


def test_evaluate_satisfied_result_carries_context() -> None:
    source = _OneShotSource(3.0)
    hypothesis = _hypothesis()
    result = hypothesis.evaluate(source, at=1720000000.0)
    assert source.queries == [hypothesis.query]
    assert result.hypothesis_name == "replicas-available"
    assert result.at == 1720000000.0
    assert result.value == 3.0
    assert result.satisfied is True


def test_evaluate_breach() -> None:
    result = _hypothesis().evaluate(_OneShotSource(0.0), at=5.0)
    assert result.satisfied is False


def test_no_data_breaches_by_default() -> None:
    result = _hypothesis().evaluate(_OneShotSource(None), at=5.0)
    assert result.value is None
    assert result.satisfied is False


def test_nan_follows_the_no_data_policy() -> None:
    # A 0/0 ratio query with no traffic returns NaN, not None; NaN must obey
    # the operator's NoDataPolicy rather than silently reading as a breach.
    nan = float("nan")
    breach = _hypothesis(comparator="<", threshold=0.05)
    assert breach.evaluate(_OneShotSource(nan), at=1.0).satisfied is False
    satisfy = _hypothesis(comparator="<", threshold=0.05, on_no_data=NoDataPolicy.SATISFY)
    assert satisfy.evaluate(_OneShotSource(nan), at=1.0).satisfied is True


def test_no_data_can_satisfy_for_empty_error_rate_queries() -> None:
    hypothesis = _hypothesis(
        name="error-rate",
        comparator="<",
        threshold=0.05,
        on_no_data=NoDataPolicy.SATISFY,
    )
    result = hypothesis.evaluate(_OneShotSource(None), at=5.0)
    assert result.satisfied is True


def test_hypothesis_is_frozen_and_validated() -> None:
    hypothesis = _hypothesis(consecutive_breaches=3)
    assert hypothesis.consecutive_breaches == 3
    assert hypothesis.comparator is Comparator.GE
    with pytest.raises(ValueError):
        _hypothesis(consecutive_breaches=0)
