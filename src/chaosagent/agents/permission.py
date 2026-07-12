"""The permission gate — the deterministic ``canUseTool`` enforcement point.

Every MCP tool call the LLM wants to make passes through here first. In Phase 0
the gate runs in OBSERVE mode and admits only read-only tools; anything that
could change state is refused before the SDK ever dispatches it. Classification
is default-deny: a tool is treated as state-changing unless it clearly reads.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

# Verb tokens that mark a tool as state-changing. Matched against whole tokens
# (not substrings) so "execute_query" is not mistaken for an "exec".
#
# Deliberately NOT here: "label"/"annotate" — they collide with read-only metrics
# tools the observer relies on (Prometheus `label_names`/`label_values`, Grafana
# `list_annotations`). Real K8s label/annotate writes surface as create/update/
# patch on the Kubernetes MCP, which are covered below.
_WRITE_TOKENS: frozenset[str] = frozenset(
    {
        "create",
        "update",
        "delete",
        "remove",
        "apply",
        "patch",
        "scale",
        "exec",
        "cordon",
        "drain",
        "rollout",
        "restart",
        "start",
        "stop",
        "inject",
        "kill",
        "evict",
        "write",
        "set",
        "run",
        "put",
        "post",
        "add",
        "edit",
        "destroy",
        "terminate",
        "disrupt",
    }
)

# Verb tokens that positively mark a tool as read-only. A tool must carry one of
# these and no write token to be admitted in OBSERVE mode.
_READ_TOKENS: frozenset[str] = frozenset(
    {
        "list",
        "get",
        "read",
        "describe",
        "query",
        "search",
        "log",
        "logs",
        "top",
        "watch",
        "events",
        "event",
        "view",
        "status",
        "info",
        "explain",
        "metric",
        "metrics",
        "context",
        "contexts",
        "show",
        "count",
        "ping",
    }
)

# Insert a boundary at lower->upper transitions so camelCase names tokenize:
# "listPods" -> "list Pods". Applied before lowercasing.
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_TOKEN_RE = re.compile(r"[^a-z0-9]+")


def _tokens(tool_name: str) -> set[str]:
    split = _CAMEL_RE.sub(" ", tool_name).lower()
    return {tok for tok in _TOKEN_RE.split(split) if tok}


def is_read_only(tool_name: str) -> bool:
    """Safety-biased classification of an MCP tool by its name tokens.

    A write token anywhere forces a write verdict; otherwise the tool must carry
    a read token to be admitted. Anything unrecognised returns False, so OBSERVE
    mode denies whatever it cannot positively recognise as a read.

    Bias is toward over-denial: an ambiguous name like ``get_deployment_scale``
    (a read whose name embeds the write noun "scale") stays denied. Refusing a
    read in OBSERVE mode is harmless; admitting a write would not be.
    """
    tokens = _tokens(tool_name)
    if tokens & _WRITE_TOKENS:
        return False
    return bool(tokens & _READ_TOKENS)


class RunMode(StrEnum):
    """How much the gate lets through.

    OBSERVE — Phase 0. Read-only tools only; every write is refused.
    EXPERIMENT — Phase 1+. Reads always pass; a write passes only when the
    executor has bound it to a policy-approved action (wired in Phase 1).
    """

    OBSERVE = "observe"
    EXPERIMENT = "experiment"


@dataclass(frozen=True)
class PermissionResult:
    """The gate's verdict for one tool call."""

    allowed: bool
    reason: str

    @classmethod
    def allow(cls, reason: str = "read-only tool") -> PermissionResult:
        return cls(True, reason)

    @classmethod
    def deny(cls, reason: str) -> PermissionResult:
        return cls(False, reason)


class PermissionGate:
    """Decides whether a proposed MCP tool call may run.

    In Phase 0 this is intentionally simple and total: it admits reads and
    refuses writes. It exists as a distinct object so Phase 1 can inject the
    PolicyEngine-bound write path without touching the harness wiring.
    """

    def __init__(self, mode: RunMode = RunMode.OBSERVE) -> None:
        self.mode = mode

    def check(
        self, tool_name: str, tool_input: dict[str, object] | None = None
    ) -> PermissionResult:
        if is_read_only(tool_name):
            return PermissionResult.allow()
        if self.mode is RunMode.OBSERVE:
            return PermissionResult.deny(
                f"tool {tool_name!r} is state-changing; harness is in OBSERVE (read-only) mode"
            )
        # EXPERIMENT mode: raw writes still require a policy-approved action bound
        # by the executor. Until that binding exists (Phase 1), refuse by default.
        return PermissionResult.deny(
            f"tool {tool_name!r} requires a policy-approved action; none is bound"
        )
