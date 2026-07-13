"""Steady-state hypotheses — typed SLO thresholds over PromQL scalars.

Pure comparison logic; breach *counting* (consecutive_breaches) lives in the
observe loop so a hypothesis stays a frozen value object.
"""

from __future__ import annotations

import math
import operator
from collections.abc import Callable
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class ScalarSource(Protocol):
    """Anything that can answer a PromQL query with one float (or no data)."""

    def scalar(self, query: str) -> float | None: ...


class Comparator(StrEnum):
    """Threshold comparators. No ``==`` — float equality is a footgun."""

    LT = "<"
    LE = "<="
    GT = ">"
    GE = ">="


_OPS: dict[Comparator, Callable[[float, float], bool]] = {
    Comparator.LT: operator.lt,
    Comparator.LE: operator.le,
    Comparator.GT: operator.gt,
    Comparator.GE: operator.ge,
}


class NoDataPolicy(StrEnum):
    """What an empty query result means. BREACH fails closed (a vanished metric
    is treated as an SLO breach); SATISFY suits legitimately-empty results such
    as an error-rate query when no errors have occurred."""

    BREACH = "breach"
    SATISFY = "satisfy"


class HypothesisResult(BaseModel):
    """One evaluation of one hypothesis at one point in time."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hypothesis_name: str
    at: float
    value: float | None
    satisfied: bool


class SteadyStateHypothesis(BaseModel):
    """The steady state holds while ``value <comparator> threshold`` is true."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    query: str = Field(min_length=1)
    comparator: Comparator
    threshold: float
    #: The observe loop only declares a breach after this many unsatisfied
    #: evaluations in a row (rides out single-scrape blips).
    consecutive_breaches: int = Field(default=1, ge=1)
    on_no_data: NoDataPolicy = NoDataPolicy.BREACH

    def compare(self, value: float) -> bool:
        """Pure threshold check."""
        return _OPS[self.comparator](value, self.threshold)

    def evaluate(self, client: ScalarSource, *, at: float) -> HypothesisResult:
        """Query the source once and judge the sample."""
        value = client.scalar(self.query)
        # NaN (e.g. a 0/0 ratio query with no traffic) is "no data" too: every
        # comparison with NaN is False, which would silently read as a breach
        # regardless of the operator's chosen NoDataPolicy.
        if value is None or math.isnan(value):
            satisfied = self.on_no_data is NoDataPolicy.SATISFY
        else:
            satisfied = self.compare(value)
        return HypothesisResult(
            hypothesis_name=self.name, at=at, value=value, satisfied=satisfied
        )
