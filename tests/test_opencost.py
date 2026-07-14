"""OpenCostClient over the allocation API — advisory cost, never an authority."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from chaosagent.capacity import WorkloadRef
from chaosagent.capacity.opencost import (
    MINUTES_PER_MONTH,
    OpenCostClient,
    estimate_monthly_delta,
)

_REF = WorkloadRef(kind="deployment", name="cartservice")


def _allocation(entries: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"code": 200, "data": [entries]})


def _client(handler: Any) -> OpenCostClient:
    return OpenCostClient(
        "http://opencost.local:9003", transport=httpx.MockTransport(handler)
    )


def test_workload_monthly_cost_normalizes_the_window_to_a_month() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        # 10080 minutes = 7 days at 5.0 total -> monthly ~ 5.0/10080*43800
        return _allocation({"cartservice": {"minutes": 10080.0, "totalCost": 5.0}})

    client = _client(handler)
    cost = client.workload_monthly_cost("boutique", _REF)
    assert cost is not None
    assert cost == 5.0 / 10080.0 * MINUTES_PER_MONTH
    assert seen["path"] == "/allocation"
    assert seen["params"]["aggregate"] == "controller"
    assert seen["params"]["filterNamespaces"] == "boutique"
    assert seen["params"]["filterControllers"] == "cartservice"
    client.close()


def test_missing_workload_returns_none() -> None:
    client = _client(lambda request: _allocation({"other": {"minutes": 1.0, "totalCost": 1.0}}))
    assert client.workload_monthly_cost("boutique", _REF) is None


@pytest.mark.parametrize(
    "key", ["cartservice", "deployment:cartservice", "boutique/deployment/cartservice"]
)
def test_aggregate_key_shapes_are_all_matched(key: str) -> None:
    # OpenCost keys controller aggregates differently across versions/options;
    # every shape whose final segment is the controller name must match.
    client = _client(lambda request: _allocation({key: {"minutes": 10080.0, "totalCost": 5.0}}))
    assert client.workload_monthly_cost("boutique", _REF) is not None


def test_a_suffix_named_sibling_is_not_matched() -> None:
    client = _client(
        lambda request: _allocation(
            {"deployment:other-cartservice": {"minutes": 10080.0, "totalCost": 5.0}}
        )
    )
    assert client.workload_monthly_cost("boutique", _REF) is None


def test_http_error_returns_none_never_raises() -> None:
    client = _client(lambda request: httpx.Response(500, text="boom"))
    assert client.workload_monthly_cost("boutique", _REF) is None


def test_network_error_returns_none_never_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    assert _client(handler).workload_monthly_cost("boutique", _REF) is None


def test_malformed_body_returns_none_never_raises() -> None:
    client = _client(lambda request: httpx.Response(200, text="{not json"))
    assert client.workload_monthly_cost("boutique", _REF) is None
    client = _client(lambda request: httpx.Response(200, json={"code": 200, "data": "?"}))
    assert client.workload_monthly_cost("boutique", _REF) is None


def test_zero_minutes_returns_none() -> None:
    client = _client(lambda request: _allocation({"cartservice": {"minutes": 0, "totalCost": 0}}))
    assert client.workload_monthly_cost("boutique", _REF) is None


def test_estimate_monthly_delta_scales_linearly_per_replica() -> None:
    assert estimate_monthly_delta(30.0, current_replicas=4, desired_replicas=6) == 15.0
    assert estimate_monthly_delta(30.0, current_replicas=4, desired_replicas=3) == -7.5
    assert estimate_monthly_delta(30.0, current_replicas=4, desired_replicas=4) == 0.0
    assert estimate_monthly_delta(30.0, current_replicas=0, desired_replicas=1) is None
