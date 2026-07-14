"""OpenCost signal — advisory monthly workload cost over plain HTTP.

Mirrors the Prometheus client (sync httpx, injectable transport for offline
tests), with one deliberate difference: cost is a signal, never an authority,
so a missing workload, a failing OpenCost, or a malformed body all yield None
— they must never fail a recommendation or a run, and a cost number can never
raise a cap.
"""

from __future__ import annotations

from typing import Any

import httpx

from chaosagent.capacity.spec import WorkloadRef

#: 30.42 days — the normalization convention kubectl-cost uses.
MINUTES_PER_MONTH = 43800.0

#: Allocation window queried; the observed cost rate is normalized to a month.
_WINDOW = "7d"


class OpenCostClient:
    """Thin client over the OpenCost ``/allocation`` API."""

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

    def workload_monthly_cost(self, namespace: str, workload: WorkloadRef) -> float | None:
        """The workload's observed cost normalized to a month, or None when
        unknown. Never raises — cost is advisory."""
        try:
            response = self._http.get(
                "/allocation",
                params={
                    "window": _WINDOW,
                    "aggregate": "controller",
                    "filterNamespaces": namespace,
                    "filterControllers": workload.name,
                },
            )
            payload: Any = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        if response.status_code != 200 or not isinstance(payload, dict):
            return None
        data = payload.get("data")
        if not isinstance(data, list):
            return None
        for step in data:
            if not isinstance(step, dict):
                continue
            entry = _entry_for(step, workload.name)
            if entry is None:
                continue
            total = entry.get("totalCost")
            minutes = entry.get("minutes")
            if (
                isinstance(total, int | float)
                and isinstance(minutes, int | float)
                and minutes > 0
            ):
                return float(total) / float(minutes) * MINUTES_PER_MONTH
        return None

    def close(self) -> None:
        self._http.close()


def _entry_for(step: dict[str, Any], name: str) -> dict[str, Any] | None:
    """The allocation entry for a controller. OpenCost keys aggregate results
    as a bare name, 'kind:name', or 'namespace/kind/name' depending on version
    and options — accept any key whose final segment is the controller name."""
    for key, entry in step.items():
        if not isinstance(entry, dict):
            continue
        if key == name or key.endswith(f":{name}") or key.endswith(f"/{name}"):
            return entry
    return None


def estimate_monthly_delta(
    monthly_cost: float, *, current_replicas: int, desired_replicas: int
) -> float | None:
    """Signed monthly cost delta of a replica change, assuming cost scales
    linearly with replicas. Advisory, like everything cost."""
    if current_replicas <= 0:
        return None
    return monthly_cost * (desired_replicas - current_replicas) / current_replicas
