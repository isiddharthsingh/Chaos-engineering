"""PrometheusClient over the HTTP API, exercised through httpx.MockTransport."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from chaosagent.observe import PrometheusClient, PrometheusError


def _success(result: list[dict[str, Any]], result_type: str = "vector") -> httpx.Response:
    payload = {"status": "success", "data": {"resultType": result_type, "result": result}}
    return httpx.Response(200, json=payload)


def _client(handler: Any) -> PrometheusClient:
    return PrometheusClient(
        "http://prometheus.local:9090", transport=httpx.MockTransport(handler)
    )


def test_instant_sends_query_and_returns_result() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return _success([{"metric": {"pod": "cart-1"}, "value": [1720000000.0, "1"]}])

    client = _client(handler)
    result = client.instant("up{job='cartservice'}")
    assert seen["path"] == "/api/v1/query"
    assert seen["params"] == {"query": "up{job='cartservice'}"}
    assert result[0]["metric"] == {"pod": "cart-1"}
    client.close()


def test_instant_passes_evaluation_time() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return _success([])

    _client(handler).instant("up", at=1720000123.5)
    assert seen["params"]["time"] == "1720000123.5"


def test_range_sends_window_params() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return _success(
            [{"metric": {}, "values": [[1720000000.0, "1"], [1720000005.0, "0"]]}],
            result_type="matrix",
        )

    result = _client(handler).range("up", start=1720000000.0, end=1720000060.0, step_seconds=5)
    assert seen["path"] == "/api/v1/query_range"
    assert seen["params"] == {
        "query": "up",
        "start": "1720000000.0",
        "end": "1720000060.0",
        "step": "5",
    }
    assert len(result[0]["values"]) == 2


def test_scalar_returns_first_sample_as_float() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _success([{"metric": {}, "value": [1720000000.0, "3.5"]}])

    assert _client(handler).scalar("replicas") == 3.5


def test_scalar_returns_none_on_empty_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _success([])

    assert _client(handler).scalar("absent_metric") is None


def test_scalar_rejects_multi_series_results() -> None:
    # An unaggregated query that matches several series is ambiguous: reducing
    # to an arbitrary one could hide a breach in the series under fault.
    def handler(request: httpx.Request) -> httpx.Response:
        return _success(
            [
                {"metric": {"deployment": "a"}, "value": [1720000000.0, "1"]},
                {"metric": {"deployment": "b"}, "value": [1720000000.0, "0"]},
            ]
        )

    with pytest.raises(PrometheusError, match="2 series"):
        _client(handler).scalar("kube_deployment_status_replicas_available")


def test_http_error_status_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="overloaded")

    with pytest.raises(PrometheusError, match="503"):
        _client(handler).instant("up")


def test_prometheus_error_status_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"status": "error", "errorType": "bad_data", "error": "parse error"}
        )

    with pytest.raises(PrometheusError, match="parse error"):
        _client(handler).instant("up{")


def test_transport_failure_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(PrometheusError, match="connection refused"):
        _client(handler).instant("up")


def test_malformed_body_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    with pytest.raises(PrometheusError):
        _client(handler).instant("up")


def test_malformed_sample_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=json.dumps({"status": "success", "data": {"result": [{"metric": {}}]}})
        )

    with pytest.raises(PrometheusError):
        _client(handler).scalar("up")
