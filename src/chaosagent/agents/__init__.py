"""The agent brain: MCP wiring, the permission gate, and the SDK harnesses.

The permission gate is the ``canUseTool`` enforcement point AND the direct-
client write gatekeeper: in OBSERVE mode every state-changing call is refused;
in EXPERIMENT mode a write passes only while the executor has bound a
policy-approved ``ProposedAction`` to the gate's single, TTL-expiring slot.
"""

from chaosagent.agents.permission import (
    ActionBinding,
    BindingError,
    PermissionGate,
    PermissionResult,
    RunMode,
    is_read_only,
)

__all__ = [
    "ActionBinding",
    "BindingError",
    "PermissionGate",
    "PermissionResult",
    "RunMode",
    "is_read_only",
]
