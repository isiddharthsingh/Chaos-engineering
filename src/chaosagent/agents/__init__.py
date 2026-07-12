"""The agent brain: MCP wiring, the permission gate, and the SDK harness.

Phase 0 ships this in read-only (OBSERVE) mode: the agent can list cluster state
and query metrics, but the permission gate refuses every state-changing tool
call. The gate is the ``canUseTool`` enforcement point the plan calls out as the
natural home for "no prod without approval"; in Phase 1 it gains the
PolicyEngine-bound path that lets an approved experiment through.
"""

from chaosagent.agents.permission import PermissionGate, PermissionResult, RunMode, is_read_only

__all__ = ["PermissionGate", "PermissionResult", "RunMode", "is_read_only"]
