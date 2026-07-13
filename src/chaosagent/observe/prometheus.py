"""Synchronous Prometheus HTTP API client.

Deliberately sync (httpx.Client): the observe loop sits on the safety path and
must be deterministic under test with an injectable clock — no event-loop
hazards. A custom transport is injectable for offline tests.
"""

from __future__ import annotations

from typing import Any

import httpx


class PrometheusError(RuntimeError):
    """Raised when a query cannot be executed or Prometheus reports failure."""


class PrometheusClient:
    """Thin client over ``/api/v1/query`` and ``/api/v1/query_range``."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"), timeout=timeout_seconds, transport=transport
        )

    def instant(self, query: str, *, at: float | None = None) -> list[dict[str, Any]]:
        """Run an instant query; return the ``data.result`` sample list."""
        params: dict[str, str] = {"query": query}
        if at is not None:
            params["time"] = str(at)
        return self._call("/api/v1/query", params)

    def range(
        self, query: str, *, start: float, end: float, step_seconds: float
    ) -> list[dict[str, Any]]:
        """Run a range query over [start, end] at the given resolution."""
        params = {
            "query": query,
            "start": str(start),
            "end": str(end),
            "step": str(step_seconds),
        }
        return self._call("/api/v1/query_range", params)

    def scalar(self, query: str) -> float | None:
        """Instant-query and reduce to one float; None when the result is empty.

        None is a distinct outcome — the hypothesis layer decides whether "no
        data" breaches (fail closed) or satisfies (empty error-rate queries).
        """
        result = self.instant(query)
        if not result:
            return None
        if len(result) > 1:
            # A hypothesis must judge one series. An unaggregated query that
            # matches several would otherwise be reduced to an arbitrary one,
            # so a breach in the series actually under fault could go unseen.
            raise PrometheusError(
                f"query {query!r} returned {len(result)} series; a steady-state "
                "hypothesis must resolve to a single series (add labels or an aggregation)"
            )
        try:
            return float(result[0]["value"][1])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise PrometheusError(f"malformed sample for query {query!r}: {exc}") from exc

    def close(self) -> None:
        self._http.close()

    def _call(self, path: str, params: dict[str, str]) -> list[dict[str, Any]]:
        try:
            response = self._http.get(path, params=params)
        except httpx.HTTPError as exc:
            raise PrometheusError(f"prometheus request failed: {exc}") from exc
        if response.status_code != 200:
            raise PrometheusError(
                f"prometheus returned HTTP {response.status_code}: {response.text[:200]}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise PrometheusError(f"prometheus returned a non-JSON body: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("status") != "success":
            detail = payload.get("error", "unknown error") if isinstance(payload, dict) else payload
            raise PrometheusError(f"prometheus query failed: {detail}")
        data = payload.get("data") or {}
        return list(data.get("result", []))
