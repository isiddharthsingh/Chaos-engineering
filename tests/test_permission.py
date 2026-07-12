"""Permission-gate classification and mode behaviour."""

from __future__ import annotations

import pytest

from chaosagent.agents.permission import PermissionGate, RunMode, is_read_only

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
