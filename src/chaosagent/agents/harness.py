"""The Claude Agent SDK harness — Phase 0 read-only observer.

Wires the MCP "hands" (Kubernetes / Prometheus / Grafana) to the LLM with the
:class:`PermissionGate` installed as the SDK ``can_use_tool`` callback, so every
tool call the model attempts is judged before it runs. In Phase 0 the gate is in
OBSERVE mode: the agent can read cluster state and query metrics and nothing else.

The ``claude_agent_sdk`` dependency is optional (the ``agent`` extra); it is
imported lazily so the deterministic core installs and tests without it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, cast

from chaosagent.agents.mcp_config import McpEndpoints, build_mcp_servers
from chaosagent.agents.permission import PermissionGate, RunMode

if TYPE_CHECKING:
    from claude_agent_sdk import (
        ClaudeAgentOptions,
        PermissionResultAllow,
        PermissionResultDeny,
        ToolPermissionContext,
    )

OBSERVER_SYSTEM_PROMPT = """\
You are the Observer agent of a chaos-engineering platform, running in READ-ONLY
mode. You may ONLY inspect infrastructure and metrics — never change state.

Your tools:
  * Kubernetes MCP — list/get workloads, pods, nodes, events (read-only server).
  * Prometheus MCP — run PromQL to read the current and historical steady state.
  * Grafana MCP — read dashboards, panels, and firing alerts.

When asked about a target, report: what is running, its current steady-state
metrics (latency, error rate, saturation), any firing alerts, and whether the
target looks healthy enough to be a candidate for a chaos experiment. Do not
propose or attempt any mutation; that is a later phase gated by policy. If a tool
call is refused, note it and continue with what you can read.
"""

# Built-in tools the observer never needs; the gate would refuse them anyway,
# but denying them up front keeps the model from trying.
_DISALLOWED_BUILTINS = ["Bash", "Write", "Edit", "NotebookEdit", "WebFetch"]


def build_can_use_tool(gate: PermissionGate) -> Any:
    """Adapt a :class:`PermissionGate` into an SDK ``can_use_tool`` callback."""
    from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

    async def _can_use_tool(
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        decision = gate.check(tool_name, tool_input)
        if decision.allowed:
            return PermissionResultAllow()
        return PermissionResultDeny(message=decision.reason, interrupt=False)

    return _can_use_tool


class ObserverHarness:
    """Phase 0 read-only agent over a single target's cluster and metrics."""

    def __init__(
        self,
        endpoints: McpEndpoints | None = None,
        *,
        model: str = "claude-opus-4-8",
    ) -> None:
        self.endpoints = endpoints or McpEndpoints.from_env()
        self.model = model
        self.gate = PermissionGate(mode=RunMode.OBSERVE)

    def build_options(self) -> ClaudeAgentOptions:
        """Construct the SDK options. Requires the ``agent`` extra installed."""
        from claude_agent_sdk import ClaudeAgentOptions

        # build_mcp_servers stays SDK-free (optional dep), so its dict is cast to
        # the SDK's McpServerConfig mapping here at the boundary.
        mcp_servers = cast(Any, build_mcp_servers(self.endpoints, read_only=True))
        return ClaudeAgentOptions(
            model=self.model,
            system_prompt=OBSERVER_SYSTEM_PROMPT,
            mcp_servers=mcp_servers,
            can_use_tool=build_can_use_tool(self.gate),
            disallowed_tools=_DISALLOWED_BUILTINS,
            permission_mode="default",
        )

    async def observe(self, prompt: str) -> str:
        """Run one read-only observation turn and return the agent's text.

        The prompt is delivered as a single-message async stream: the SDK requires
        streaming mode whenever a ``can_use_tool`` callback is set (as ours is), so
        a plain string prompt would be rejected.
        """
        from claude_agent_sdk import AssistantMessage, TextBlock, query

        async def _stream() -> AsyncIterator[dict[str, Any]]:
            yield {"type": "user", "message": {"role": "user", "content": prompt}}

        chunks: list[str] = []
        async for message in query(prompt=_stream(), options=self.build_options()):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        chunks.append(block.text)
        return "\n".join(chunks)
