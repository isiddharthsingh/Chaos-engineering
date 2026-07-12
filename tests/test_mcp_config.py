"""MCP server config assembly."""

from __future__ import annotations

from chaosagent.agents.mcp_config import McpEndpoints, build_mcp_servers


def test_read_only_adds_flag() -> None:
    servers = build_mcp_servers(McpEndpoints(), read_only=True)
    assert "--read-only" in servers["kubernetes"]["args"]


def test_write_mode_omits_flag() -> None:
    servers = build_mcp_servers(McpEndpoints(), read_only=False)
    assert "--read-only" not in servers["kubernetes"]["args"]


def test_all_servers_are_stdio() -> None:
    servers = build_mcp_servers(McpEndpoints())
    assert set(servers) == {"kubernetes", "prometheus", "grafana"}
    for cfg in servers.values():
        assert cfg["type"] == "stdio"
        assert isinstance(cfg["command"], str)


def test_endpoints_flow_into_env() -> None:
    ep = McpEndpoints(
        prometheus_url="http://prom:9090",
        grafana_url="http://graf:3000",
        grafana_api_key="secret",
        kubeconfig="/tmp/kubeconfig",
    )
    servers = build_mcp_servers(ep)
    assert servers["prometheus"]["env"]["PROMETHEUS_URL"] == "http://prom:9090"
    assert servers["grafana"]["env"]["GRAFANA_URL"] == "http://graf:3000"
    assert servers["grafana"]["env"]["GRAFANA_API_KEY"] == "secret"
    assert servers["kubernetes"]["env"]["KUBECONFIG"] == "/tmp/kubeconfig"


def test_grafana_key_omitted_when_absent() -> None:
    servers = build_mcp_servers(McpEndpoints(grafana_api_key=""))
    assert "GRAFANA_API_KEY" not in servers["grafana"]["env"]


def test_from_env_override(monkeypatch) -> None:
    monkeypatch.setenv("CHAOSAGENT_PROMETHEUS_URL", "http://env-prom:9090")
    ep = McpEndpoints.from_env()
    assert ep.prometheus_url == "http://env-prom:9090"
