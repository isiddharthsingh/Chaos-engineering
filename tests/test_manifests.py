"""Structural + safety validation of the shipped K8s manifests.

No cluster required: we parse every YAML doc under config/ and assert the
guardrail invariants hold statically. This is what keeps the in-cluster
enforcement layer honest without a live Kyverno install.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from chaosagent.config import load_policy_config

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
RBAC_DIR = CONFIG_DIR / "rbac"
KYVERNO_DIR = CONFIG_DIR / "policies" / "kyverno"


def _docs(path: Path) -> list[dict]:
    return [d for d in yaml.safe_load_all(path.read_text()) if d]


def _all_manifests() -> list[tuple[Path, dict]]:
    out: list[tuple[Path, dict]] = []
    for path in sorted(CONFIG_DIR.rglob("*.yaml")):
        if path.name == "engine.yaml":  # policy config, not a manifest
            continue
        for doc in _docs(path):
            out.append((path, doc))
    return out


def test_every_manifest_has_apiversion_and_kind() -> None:
    manifests = _all_manifests()
    assert manifests, "expected manifests under config/"
    for path, doc in manifests:
        assert "apiVersion" in doc, f"{path}: missing apiVersion"
        assert "kind" in doc, f"{path}: missing kind"
        assert doc.get("metadata", {}).get("name"), f"{path}: missing metadata.name"


def test_observer_role_is_read_only() -> None:
    # The observer tier must never hold a write verb — that is its whole point.
    write_verbs = {"create", "update", "patch", "delete", "deletecollection", "*"}
    checked = 0
    for path, doc in _all_manifests():
        if doc.get("kind") == "ClusterRole" and "observer" in doc["metadata"]["name"]:
            checked += 1
            for rule in doc.get("rules", []):
                verbs = {v.lower() for v in rule.get("verbs", [])}
                assert not (verbs & write_verbs), f"{path}: observer has write verb {verbs}"
    assert checked, "no observer ClusterRole found — test would pass vacuously"


def test_no_cluster_admin_binding() -> None:
    for path, doc in _all_manifests():
        if doc.get("kind") in ("ClusterRoleBinding", "RoleBinding"):
            role = doc.get("roleRef", {}).get("name", "")
            assert role != "cluster-admin", f"{path}: binds cluster-admin"


def test_experimenter_write_is_namespaced_only() -> None:
    # The experimenter's write grants must come from a namespaced Role bound by a
    # RoleBinding — never a ClusterRoleBinding. That is the namespace-scope boundary.
    saw_role_binding = False
    for path, doc in _all_manifests():
        if doc.get("kind") == "ClusterRoleBinding":
            names = {s.get("name") for s in doc.get("subjects", [])}
            assert "agent-experimenter" not in names, (
                f"{path}: experimenter must not have a cluster-wide binding"
            )
        if doc.get("kind") == "RoleBinding":
            names = {s.get("name") for s in doc.get("subjects", [])}
            if "agent-experimenter" in names:
                saw_role_binding = True
    assert saw_role_binding, "expected a namespaced RoleBinding for agent-experimenter"


def test_replica_cap_matches_engine_config() -> None:
    # Anti-drift: the Kyverno deny must actually block changes ABOVE the engine's
    # max_replica_pct_change. Assert on the parsed deny condition (operator + value),
    # not a substring — a substring check would survive an operator flip to LessThan.
    cap = load_policy_config().max_replica_pct_change
    docs = _docs(KYVERNO_DIR / "cap-replica-change.yaml")
    policy = next(d for d in docs if d.get("kind") == "ClusterPolicy")
    rule = next(r for r in policy["spec"]["rules"] if r["name"] == "bound-replica-percentage")
    conditions = rule["validate"]["deny"]["conditions"]["any"]
    match = [c for c in conditions if c.get("operator") == "GreaterThan" and c.get("value") == cap]
    assert match, (
        f"cap-replica-change must deny when the ratio is GreaterThan {cap} "
        f"(engine max_replica_pct_change); found conditions: {conditions}"
    )


def test_kyverno_policies_enforce_not_audit() -> None:
    checked = 0
    for path in KYVERNO_DIR.rglob("*.yaml"):
        for doc in _docs(path):
            if doc.get("kind") == "ClusterPolicy":
                checked += 1
                assert doc["spec"].get("validationFailureAction") == "Enforce", (
                    f"{path}: policy must Enforce, not Audit"
                )
    assert checked, "no Kyverno ClusterPolicy found — test would pass vacuously"
