"""Harness wiring — runs only when the optional `agent` extra is installed.

Verifies the PermissionGate -> SDK can_use_tool adapter maps verdicts correctly
and that build_options() assembles without error. No network / model calls.
"""

from __future__ import annotations

import pytest

pytest.importorskip("claude_agent_sdk")

from chaosagent.agents.harness import ObserverHarness, build_can_use_tool  # noqa: E402
from chaosagent.agents.permission import PermissionGate, RunMode  # noqa: E402


class _Ctx:
    """Stand-in for ToolPermissionContext (only attribute access is used)."""


async def test_adapter_allows_reads() -> None:
    cb = build_can_use_tool(PermissionGate(RunMode.OBSERVE))
    result = await cb("mcp__kubernetes__resources_list", {}, _Ctx())
    assert result.behavior == "allow"


async def test_adapter_denies_writes_with_reason() -> None:
    cb = build_can_use_tool(PermissionGate(RunMode.OBSERVE))
    result = await cb("mcp__kubernetes__resources_delete", {}, _Ctx())
    assert result.behavior == "deny"
    assert "OBSERVE" in result.message


def test_build_options_assembles() -> None:
    options = ObserverHarness().build_options()
    assert set(options.mcp_servers) == {"kubernetes", "prometheus", "grafana"}
    assert options.can_use_tool is not None
    assert "Bash" in (options.disallowed_tools or [])
