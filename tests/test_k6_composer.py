"""Composer: LoadSpec -> k6-operator TestRun CR (+ the k6 admission gate policy)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from chaosagent.load import LoadSpec, compose_testrun

_NAME_RE = re.compile(r"\Achaosagent-load-[0-9a-f]{8}\Z")

_K6_POLICY = (
    Path(__file__).resolve().parents[1]
    / "config"
    / "policies"
    / "kyverno"
    / "load"
    / "require-chaos-namespace-k6.yaml"
)


def _load(**overrides: object) -> LoadSpec:
    base: dict[str, object] = {
        "script_configmap": "checkout-load",
        "duration_seconds": 120,
        "ttl_seconds": 300,
    }
    base.update(overrides)
    return LoadSpec.model_validate(base)


def test_testrun_composes_full_cr() -> None:
    cr = compose_testrun(_load(), namespace="boutique")
    assert cr["apiVersion"] == "k6.io/v1alpha1"
    assert cr["kind"] == "TestRun"
    assert cr["metadata"]["namespace"] == "boutique"
    assert cr["metadata"]["labels"] == {"app.kubernetes.io/managed-by": "chaosagent"}
    assert cr["spec"]["parallelism"] == 1
    assert cr["spec"]["script"] == {
        "configMap": {"name": "checkout-load", "file": "script.js"}
    }


def test_testrun_is_self_bounding_like_a_fault_cr() -> None:
    # --duration overrides the script and cleanup reaps the runner jobs, so an
    # orphaned TestRun (agent died after apply) still stops by itself.
    cr = compose_testrun(_load(duration_seconds=120), namespace="boutique")
    assert cr["spec"]["arguments"] == "--duration 120s"
    assert cr["spec"]["cleanup"] == "post"


def test_script_file_and_parallelism_are_configurable() -> None:
    cr = compose_testrun(
        _load(script_file="checkout.js", parallelism=4), namespace="boutique"
    )
    assert cr["spec"]["parallelism"] == 4
    assert cr["spec"]["script"]["configMap"]["file"] == "checkout.js"


def test_generated_name_is_dns_label_safe_and_explicit_name_wins() -> None:
    generated = compose_testrun(_load(), namespace="boutique")["metadata"]["name"]
    assert _NAME_RE.match(generated), generated
    assert len(generated) <= 63
    named = compose_testrun(_load(), namespace="boutique", name="load-ok")
    assert named["metadata"]["name"] == "load-ok"


def test_load_spec_is_frozen_and_bounded() -> None:
    with pytest.raises(ValidationError):
        LoadSpec.model_validate({"script_configmap": "x", "duration_seconds": 0, "ttl_seconds": 60})
    with pytest.raises(ValidationError):
        LoadSpec.model_validate(
            {"script_configmap": "", "duration_seconds": 60, "ttl_seconds": 60}
        )
    with pytest.raises(ValidationError):
        LoadSpec.model_validate(
            {"script_configmap": "x", "duration_seconds": 60, "ttl_seconds": 60, "bogus": 1}
        )
    spec = _load()
    with pytest.raises(ValidationError):
        spec.parallelism = 2
    # Blast-radius cap: the policy engine never sees the load, so the bound
    # lives on the model itself.
    with pytest.raises(ValidationError):
        _load(parallelism=11)
    # The declared lifetime must cover the run duration.
    with pytest.raises(ValidationError):
        _load(duration_seconds=600, ttl_seconds=300)


def test_k6_gate_policy_names_the_testrun_kind() -> None:
    # The chaos Kyverno policies match Chaos Mesh kinds only; without this twin
    # a TestRun in an unlabelled namespace would not be refused.
    docs = [d for d in yaml.safe_load_all(_K6_POLICY.read_text()) if d]
    policy = next(d for d in docs if d.get("kind") == "ClusterPolicy")
    assert policy["spec"]["validationFailureAction"] == "Enforce"
    kinds = policy["spec"]["rules"][0]["match"]["any"][0]["resources"]["kinds"]
    assert "TestRun" in kinds
    message = policy["spec"]["rules"][0]["validate"]["message"]
    assert "require-chaos-namespace" in message
