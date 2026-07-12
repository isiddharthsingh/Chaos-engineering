"""Bind a proposed action to its registered target.

A proposed action *declares* an environment, and may declare a namespace. Those
declarations must never be trusted — an LLM or a hand-written JSON file could
claim ``environment: dev`` for a production target. This resolver overrides the
environment, target kind, and namespace scope from the authoritative registry
entry, producing the action the policy engine actually judges.
"""

from __future__ import annotations

from chaosagent.domain.actions import ProposedAction
from chaosagent.domain.targets import Target


def resolve_action(action: ProposedAction, target: Target) -> ProposedAction:
    """Return a copy of ``action`` with target-derived fields forced from the
    registry entry. Raises if the action names a different target."""
    if action.target_id != target.id:
        raise ValueError(
            f"action target_id {action.target_id!r} does not match target {target.id!r}"
        )
    return action.model_copy(
        update={
            "environment": target.environment,
            "target_kind": target.kind,
            "target_allowed_namespaces": tuple(target.allowed_namespaces),
        }
    )
