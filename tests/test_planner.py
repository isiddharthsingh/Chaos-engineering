"""Planner: prompt contract and spec extraction (SDK-free), harness wiring (gated)."""

from __future__ import annotations

import json

import pytest

from chaosagent.agents.planner import (
    PLANNER_SYSTEM_PROMPT,
    PlannerError,
    extract_experiment_spec,
)

_SPEC = {
    "title": "cartservice survives a one-third pod kill",
    "target_id": "kind-local",
    "namespace": "boutique",
    "fault": {
        "fault_type": "pod_kill",
        "selector": {"app": "cartservice"},
        "ratio": 0.34,
        "duration_seconds": 60,
    },
    "hypotheses": [
        {
            "name": "replicas",
            "query": 'kube_deployment_status_replicas_available{deployment="cartservice"}',
            "comparator": ">=",
            "threshold": 1.0,
        }
    ],
    "ttl_seconds": 300,
}


def _fenced(payload: object, tag: str = "json") -> str:
    return f"```{tag}\n{json.dumps(payload, indent=2)}\n```"


def test_extract_parses_a_fenced_json_block() -> None:
    text = f"Here is the experiment I propose:\n\n{_fenced(_SPEC)}\n\nRationale: ..."
    spec = extract_experiment_spec(text)
    assert spec.target_id == "kind-local"
    assert spec.fault.fault_type.value == "pod_kill"
    assert spec.hypotheses[0].comparator.value == ">="


def test_extract_accepts_untagged_fences() -> None:
    spec = extract_experiment_spec(_fenced(_SPEC, tag=""))
    assert spec.namespace == "boutique"


def test_extract_uses_the_final_block_as_the_answer() -> None:
    draft = dict(_SPEC, title="draft attempt")
    text = f"{_fenced(draft)}\n\nOn reflection, corrected:\n{_fenced(_SPEC)}"
    assert extract_experiment_spec(text).title == _SPEC["title"]


def test_extract_raises_when_the_final_block_is_invalid() -> None:
    # A retracted-but-valid earlier draft must NOT be executed when the model's
    # final block is malformed; raising lets the repair turn fire instead.
    text = f"{_fenced(_SPEC)}\n\nOn reflection:\n```json\n{{\"note\": \"not a spec\"}}\n```"
    with pytest.raises(PlannerError, match="final fenced block"):
        extract_experiment_spec(text)


def test_extract_without_a_block_raises() -> None:
    with pytest.raises(PlannerError, match="fenced"):
        extract_experiment_spec("I could not design an experiment.")


def test_extract_with_only_invalid_specs_raises_with_the_validation_error() -> None:
    bad = dict(_SPEC, ttl_seconds=-1)
    with pytest.raises(PlannerError, match="ttl_seconds"):
        extract_experiment_spec(_fenced(bad))


def test_system_prompt_embeds_schema_and_caps() -> None:
    # The schema, the numeric caps, and the pod-fault restriction all appear so
    # the model plans inside the same bounds the engine enforces.
    assert "ExperimentSpec" in PLANNER_SYSTEM_PROMPT
    assert '"properties"' in PLANNER_SYSTEM_PROMPT
    for cap in ("0.5", "900", "3600"):
        assert cap in PLANNER_SYSTEM_PROMPT
    assert "pod_kill" in PLANNER_SYSTEM_PROMPT
    assert "read-only" in PLANNER_SYSTEM_PROMPT.lower()


# -- SDK-dependent wiring (skipped without the `agent` extra) --------------------


def test_planner_harness_is_read_only() -> None:
    pytest.importorskip("claude_agent_sdk")
    from chaosagent.agents.permission import RunMode
    from chaosagent.agents.planner import PlannerHarness

    harness = PlannerHarness()
    assert harness.gate.mode is RunMode.OBSERVE
    options = harness.build_options()
    assert set(options.mcp_servers) == {"kubernetes", "prometheus", "grafana"}
    assert options.can_use_tool is not None
    assert "Bash" in (options.disallowed_tools or [])
