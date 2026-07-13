"""The permission gate — the deterministic write gatekeeper for both paths.

Every MCP tool call the LLM wants to make passes through :meth:`check` (the SDK
``canUseTool`` callback); the direct-client executor path goes through
:meth:`authorize_write`. Both admit a write only while a policy-approved action
is bound to the gate's single, TTL-expiring slot. Classification is
default-deny: a tool is treated as state-changing unless it clearly reads.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from enum import StrEnum

from chaosagent.clock import Clock, SystemClock
from chaosagent.domain.actions import ProposedAction
from chaosagent.domain.policy import PolicyDecision

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


class BindingError(RuntimeError):
    """Raised when an action cannot be bound to the gate's write slot."""


@dataclass(frozen=True)
class ActionBinding:
    """A policy-approved action bound to the gate for its TTL. Possessing the
    receipt is not enough — the gate only honours its *active* binding."""

    token: str
    action: ProposedAction
    decision: PolicyDecision
    expires_at: float


class PermissionGate:
    """Decides whether a proposed write may run — one gatekeeper for both the
    MCP path (:meth:`check`) and the direct-client executor path
    (:meth:`authorize_write`).

    The gate holds at most one binding at a time (mirrors the
    ``single-experiment`` policy) and every binding expires with its action's
    TTL, so an abandoned approval cannot authorise writes indefinitely.
    """

    def __init__(self, mode: RunMode = RunMode.OBSERVE, *, clock: Clock | None = None) -> None:
        self.mode = mode
        self._clock: Clock = clock or SystemClock()
        self._binding: ActionBinding | None = None

    def bind(self, action: ProposedAction, decision: PolicyDecision) -> ActionBinding:
        """Bind a policy-approved, state-changing action to the write slot."""
        if self.mode is not RunMode.EXPERIMENT:
            raise BindingError("actions can only be bound in EXPERIMENT mode")
        if not decision.allowed:
            raise BindingError(f"cannot bind a denied action: {decision.reason()}")
        if not action.action_type.is_state_changing:
            raise BindingError("only state-changing actions need a binding")
        if action.ttl_seconds is None:
            raise BindingError("a bound action must declare ttl_seconds")
        if self.active_binding() is not None:
            raise BindingError("an unexpired binding is already active; unbind it first")
        binding = ActionBinding(
            token=secrets.token_hex(8),
            action=action,
            decision=decision,
            expires_at=self._clock.now() + action.ttl_seconds,
        )
        self._binding = binding
        return binding

    def unbind(self, binding: ActionBinding) -> None:
        """Release the slot. Idempotent — the rollback path must never fail here."""
        if self._binding is not None and self._binding.token == binding.token:
            self._binding = None

    def active_binding(self) -> ActionBinding | None:
        """The current binding, or None once it has expired."""
        binding = self._binding
        if binding is None:
            return None
        if self._clock.now() >= binding.expires_at:
            self._binding = None
            return None
        return binding

    def authorize_write(self, *, namespace: str | None) -> PermissionResult:
        """Judge a write against the active binding: there must be one, and the
        write must land in the bound action's namespace."""
        binding = self.active_binding()
        if binding is None:
            return PermissionResult.deny(
                "write requires a policy-approved action; none is bound"
            )
        if namespace is None:
            return PermissionResult.deny(
                "write does not declare a namespace; bound writes must be namespaced"
            )
        if namespace != binding.action.namespace:
            return PermissionResult.deny(
                f"write targets namespace {namespace!r} but the bound action is scoped to "
                f"{binding.action.namespace!r}"
            )
        return PermissionResult.allow(f"write authorized by action binding {binding.token}")

    def check(
        self, tool_name: str, tool_input: dict[str, object] | None = None
    ) -> PermissionResult:
        if is_read_only(tool_name):
            return PermissionResult.allow()
        if self.mode is RunMode.OBSERVE:
            return PermissionResult.deny(
                f"tool {tool_name!r} is state-changing; harness is in OBSERVE (read-only) mode"
            )
        namespace = tool_input.get("namespace") if tool_input else None
        result = self.authorize_write(
            namespace=namespace if isinstance(namespace, str) else None
        )
        if result.allowed:
            return result
        return PermissionResult.deny(f"tool {tool_name!r}: {result.reason}")
