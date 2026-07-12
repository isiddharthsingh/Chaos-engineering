"""resolve_action binds an action's env/kind/scope from the registered target."""

from __future__ import annotations

import pytest

from chaosagent.domain.actions import FaultSpec, ProposedAction
from chaosagent.domain.enums import ActionType, EnvironmentTier, FaultType, TargetKind
from chaosagent.domain.targets import CredentialRef, Target
from chaosagent.resolve import resolve_action

PROD_TARGET = Target(
    id="prod-cluster",
    name="Prod",
    kind=TargetKind.KUBERNETES,
    environment=EnvironmentTier.PROD,
    allowed_namespaces=["payments", "checkout"],
    credential=CredentialRef(service_account="agent-observer"),
)


def _spoofed_dev_action() -> ProposedAction:
    return ProposedAction(
        action_type=ActionType.INJECT_FAULT,
        target_id="prod-cluster",
        environment=EnvironmentTier.DEV,  # a lie
        namespace="payments",
        namespace_chaos_enabled=True,
        fault=FaultSpec(fault_type=FaultType.POD_KILL, duration_seconds=60),
        ttl_seconds=300,
    )


def test_environment_is_forced_from_target() -> None:
    resolved = resolve_action(_spoofed_dev_action(), PROD_TARGET)
    assert resolved.environment is EnvironmentTier.PROD


def test_kind_and_scope_are_populated() -> None:
    resolved = resolve_action(_spoofed_dev_action(), PROD_TARGET)
    assert resolved.target_kind is TargetKind.KUBERNETES
    assert resolved.target_allowed_namespaces == ("payments", "checkout")


def test_mismatched_target_id_raises() -> None:
    action = _spoofed_dev_action().model_copy(update={"target_id": "other"})
    with pytest.raises(ValueError, match="does not match target"):
        resolve_action(action, PROD_TARGET)


def test_resolution_does_not_mutate_input() -> None:
    action = _spoofed_dev_action()
    resolve_action(action, PROD_TARGET)
    assert action.environment is EnvironmentTier.DEV  # original untouched
