"""Permission-gate classification, mode behaviour, and action binding."""

from __future__ import annotations

import pytest

from chaosagent.agents.permission import BindingError, PermissionGate, RunMode, is_read_only
from chaosagent.domain.actions import FaultSpec, ProposedAction
from chaosagent.domain.enums import ActionType, EnvironmentTier, FaultType
from chaosagent.domain.policy import PolicyDecision, Violation
from fakes import FakeClock

READ_ONLY_TOOLS = [
    "mcp__kubernetes__resources_list",
    "mcp__kubernetes__resources_get",
    "mcp__kubernetes__pods_get",
    "mcp__kubernetes__pods_log",
    "mcp__kubernetes__events_list",
    "mcp__kubernetes__pods_top",
    "mcp__prometheus__execute_query",
    "mcp__grafana__query_prometheus",
    "mcp__grafana__list_datasources",
    "mcp__grafana__get_alert_rules",
    "mcp__kubernetes__namespaces_list",
    # camelCase names must tokenize.
    "listPods",
    "getNamespaces",
    # label / annotation enumeration are read-only metrics tools ("label" must
    # no longer force a write verdict).
    "mcp__grafana__list_prometheus_label_values",
    "mcp__grafana__list_prometheus_label_names",
    "mcp__grafana__list_annotations",
    "Read",
]

STATE_CHANGING_TOOLS = [
    "mcp__kubernetes__resources_create_or_update",
    "mcp__kubernetes__resources_delete",
    "mcp__kubernetes__resources_scale",
    "mcp__kubernetes__pods_exec",
    "mcp__kubernetes__pods_run",
    "mcp__k6__run_test",
    "mcp__chaos__inject_podchaos",
    "Bash",
    "Write",
    "Edit",
    # Unknown tool: default-deny in observe mode.
    "mcp__mystery__frobnicate",
]


@pytest.mark.parametrize("name", READ_ONLY_TOOLS)
def test_read_only_tools_classified_read(name: str) -> None:
    assert is_read_only(name) is True


@pytest.mark.parametrize("name", STATE_CHANGING_TOOLS)
def test_state_changing_tools_classified_write(name: str) -> None:
    assert is_read_only(name) is False


def test_write_marker_wins_over_read() -> None:
    # A tool that both reads and writes is treated as a write (safe default).
    assert is_read_only("mcp__x__get_or_create") is False


def test_camelcase_write_still_denied() -> None:
    assert is_read_only("deletePod") is False
    assert is_read_only("scaleDeployment") is False


def test_ambiguous_scale_noun_stays_denied() -> None:
    # get_deployment_scale reads, but "scale" is a write verb; over-denial is the
    # safe direction, so this remains classified as not-read-only.
    assert is_read_only("mcp__kubernetes__get_deployment_scale") is False


def test_observe_mode_allows_reads_denies_writes() -> None:
    gate = PermissionGate(mode=RunMode.OBSERVE)
    assert gate.check("mcp__kubernetes__resources_list").allowed is True
    denied = gate.check("mcp__kubernetes__resources_delete")
    assert denied.allowed is False
    assert "OBSERVE" in denied.reason


def test_experiment_mode_denies_unbound_write() -> None:
    gate = PermissionGate(mode=RunMode.EXPERIMENT)
    assert gate.check("mcp__prometheus__execute_query").allowed is True
    denied = gate.check("mcp__kubernetes__resources_scale")
    assert denied.allowed is False
    assert "policy-approved action" in denied.reason


# -- action binding (the EXPERIMENT-mode write path) -----------------------------


def _approved_action(**overrides: object) -> ProposedAction:
    base: dict[str, object] = {
        "action_type": ActionType.INJECT_FAULT,
        "target_id": "kind-local",
        "environment": EnvironmentTier.DEV,
        "namespace": "boutique",
        "namespace_chaos_enabled": True,
        "fault": FaultSpec(fault_type=FaultType.POD_KILL, ratio=0.34, duration_seconds=60),
        "ttl_seconds": 300,
    }
    base.update(overrides)
    return ProposedAction.model_validate(base)


def _gate(clock: FakeClock | None = None) -> PermissionGate:
    return PermissionGate(mode=RunMode.EXPERIMENT, clock=clock or FakeClock())


def test_bind_then_authorize_write_in_bound_namespace() -> None:
    gate = _gate()
    binding = gate.bind(_approved_action(), PolicyDecision.allow())
    assert gate.active_binding() is binding
    assert gate.authorize_write(namespace="boutique").allowed is True


def test_authorize_write_denies_other_namespace_and_missing_namespace() -> None:
    gate = _gate()
    gate.bind(_approved_action(), PolicyDecision.allow())
    assert gate.authorize_write(namespace="default").allowed is False
    assert gate.authorize_write(namespace=None).allowed is False


def test_authorize_write_denies_without_binding() -> None:
    denied = _gate().authorize_write(namespace="boutique")
    assert denied.allowed is False
    assert "policy-approved action" in denied.reason


def test_binding_expires_with_the_action_ttl() -> None:
    clock = FakeClock(start=1000.0)
    gate = _gate(clock)
    gate.bind(_approved_action(ttl_seconds=300), PolicyDecision.allow())
    clock.advance(299.0)
    assert gate.authorize_write(namespace="boutique").allowed is True
    clock.advance(2.0)
    assert gate.authorize_write(namespace="boutique").allowed is False
    assert gate.active_binding() is None


def test_unbind_clears_the_slot_and_is_idempotent() -> None:
    gate = _gate()
    binding = gate.bind(_approved_action(), PolicyDecision.allow())
    gate.unbind(binding)
    assert gate.active_binding() is None
    gate.unbind(binding)  # second unbind is a safe no-op (rollback path)
    assert gate.authorize_write(namespace="boutique").allowed is False


def test_bind_refused_outside_experiment_mode() -> None:
    gate = PermissionGate(mode=RunMode.OBSERVE, clock=FakeClock())
    with pytest.raises(BindingError, match="EXPERIMENT"):
        gate.bind(_approved_action(), PolicyDecision.allow())


def test_bind_refused_for_denied_decision() -> None:
    denied = PolicyDecision.deny([Violation(rule="env-scope", message="prod")])
    with pytest.raises(BindingError, match="denied"):
        _gate().bind(_approved_action(), denied)


def test_bind_refused_for_read_only_action() -> None:
    observe = _approved_action(action_type=ActionType.OBSERVE, fault=None)
    with pytest.raises(BindingError, match="state-changing"):
        _gate().bind(observe, PolicyDecision.allow())


def test_bind_refused_without_ttl() -> None:
    with pytest.raises(BindingError, match="ttl"):
        _gate().bind(_approved_action(ttl_seconds=None), PolicyDecision.allow())


def test_single_slot_refuses_a_second_bind_until_unbound_or_expired() -> None:
    clock = FakeClock()
    gate = _gate(clock)
    binding = gate.bind(_approved_action(), PolicyDecision.allow())
    with pytest.raises(BindingError, match="already"):
        gate.bind(_approved_action(), PolicyDecision.allow())
    gate.unbind(binding)
    gate.bind(_approved_action(), PolicyDecision.allow())  # slot is free again
    clock.advance(301.0)  # expiry also frees the slot
    gate.bind(_approved_action(), PolicyDecision.allow())


def test_check_admits_bound_write_in_bound_namespace_only() -> None:
    gate = _gate()
    gate.bind(_approved_action(), PolicyDecision.allow())
    allowed = gate.check("mcp__kubernetes__resources_create_or_update", {"namespace": "boutique"})
    assert allowed.allowed is True
    denied = gate.check("mcp__kubernetes__resources_create_or_update", {"namespace": "default"})
    assert denied.allowed is False
    no_namespace = gate.check("mcp__kubernetes__resources_create_or_update", {})
    assert no_namespace.allowed is False


def test_check_reads_stay_open_regardless_of_binding() -> None:
    gate = _gate()
    assert gate.check("mcp__prometheus__execute_query").allowed is True
    gate.bind(_approved_action(), PolicyDecision.allow())
    assert gate.check("mcp__prometheus__execute_query").allowed is True
