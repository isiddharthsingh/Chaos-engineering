"""MCP server wiring — the agent's "hands".

Produces plain stdio-server config dicts (the shape the Claude Agent SDK's
``mcp_servers`` option expects) for the three Phase 0 servers:

  * Kubernetes  — containers/kubernetes-mcp-server, started with --read-only so
    the server itself refuses destructive calls (belt to the gate's suspenders).
  * Prometheus  — read-only PromQL access for the observe loop.
  * Grafana     — dashboards / alerts / LogQL, read-only.

Endpoints default to the local kind rig and are overridable by environment
variable so the same code points at a real cluster in later phases.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# The SDK's McpStdioServerConfig is a TypedDict of exactly these keys.
StdioServerConfig = dict[str, object]


@dataclass(frozen=True)
class McpEndpoints:
    """Resolved connection settings for the observe-time MCP servers."""

    prometheus_url: str = "http://localhost:9090"
    grafana_url: str = "http://localhost:3000"
    grafana_api_key: str = ""
    kubeconfig: str | None = None
    #: Extra env passed to every server (e.g. PATH tweaks in CI).
    extra_env: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> McpEndpoints:
        return cls(
            prometheus_url=os.environ.get("CHAOSAGENT_PROMETHEUS_URL", cls.prometheus_url),
            grafana_url=os.environ.get("CHAOSAGENT_GRAFANA_URL", cls.grafana_url),
            grafana_api_key=os.environ.get("CHAOSAGENT_GRAFANA_API_KEY", ""),
            kubeconfig=os.environ.get("KUBECONFIG"),
        )


def build_mcp_servers(
    endpoints: McpEndpoints | None = None,
    *,
    read_only: bool = True,
) -> dict[str, StdioServerConfig]:
    """Assemble the stdio MCP server configs for the agent.

    ``read_only=True`` (the Phase 0 default) starts the Kubernetes server in its
    read-only mode. The metrics servers are read-only by nature.
    """
    ep = endpoints or McpEndpoints.from_env()

    k8s_args = ["-y", "kubernetes-mcp-server@latest"]
    if read_only:
        k8s_args.append("--read-only")
    k8s_env: dict[str, str] = dict(ep.extra_env)
    if ep.kubeconfig:
        k8s_env["KUBECONFIG"] = ep.kubeconfig

    prometheus_env = {"PROMETHEUS_URL": ep.prometheus_url, **ep.extra_env}

    grafana_env = {"GRAFANA_URL": ep.grafana_url, **ep.extra_env}
    if ep.grafana_api_key:
        grafana_env["GRAFANA_API_KEY"] = ep.grafana_api_key

    return {
        "kubernetes": {
            "type": "stdio",
            "command": "npx",
            "args": k8s_args,
            "env": k8s_env,
        },
        "prometheus": {
            "type": "stdio",
            "command": "uvx",
            "args": ["prometheus-mcp-server"],
            "env": prometheus_env,
        },
        "grafana": {
            "type": "stdio",
            "command": "mcp-grafana",
            "args": ["--transport", "stdio"],
            "env": grafana_env,
        },
    }
