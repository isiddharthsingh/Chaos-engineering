"""CLI smoke tests exercising register/list/check end to end."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chaosagent.cli import main

TARGET = {
    "id": "kind-local",
    "name": "Local kind cluster",
    "kind": "kubernetes",
    "environment": "dev",
    "provider": "kind",
    "allowed_namespaces": ["payments"],
    "credential": {"service_account": "agent-experimenter"},
}

PROD_TARGET = {
    "id": "prod-cluster",
    "name": "Prod",
    "kind": "kubernetes",
    "environment": "prod",
    "provider": "eks",
    "allowed_namespaces": ["payments"],
    "credential": {"service_account": "agent-observer"},
}


def _write(path: Path, payload: dict[str, object]) -> Path:
    path.write_text(json.dumps(payload))
    return path


def _register(store: Path, tmp_path: Path, target: dict[str, object]) -> None:
    target_file = _write(tmp_path / f"{target['id']}.json", target)
    assert main(["--store", str(store), "register", str(target_file)]) == 0


def test_register_then_list(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    store = tmp_path / "targets.json"
    _register(store, tmp_path, TARGET)
    assert main(["--store", str(store), "list"]) == 0
    out = capsys.readouterr().out
    assert "kind-local" in out and "chaos-capable" in out


def test_check_unregistered_target_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = tmp_path / "targets.json"
    action = {
        "action_type": "inject_fault",
        "target_id": "ghost",
        "environment": "dev",
        "namespace": "payments",
        "namespace_chaos_enabled": True,
        "fault": {"fault_type": "pod_kill", "ratio": 0.3, "duration_seconds": 60},
        "ttl_seconds": 300,
    }
    action_file = _write(tmp_path / "a.json", action)
    assert main(["--store", str(store), "check", str(action_file)]) == 2
    assert "not registered" in capsys.readouterr().out


def test_check_denies_unlabelled_namespace(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = tmp_path / "targets.json"
    _register(store, tmp_path, TARGET)
    action = {
        "action_type": "inject_fault",
        "target_id": "kind-local",
        "environment": "dev",
        "namespace": "payments",
        "namespace_chaos_enabled": False,
        "fault": {"fault_type": "pod_kill", "ratio": 0.3, "duration_seconds": 60},
        "ttl_seconds": 300,
    }
    action_file = _write(tmp_path / "a.json", action)
    assert main(["--store", str(store), "check", str(action_file)]) == 2
    assert "require-chaos-namespace" in capsys.readouterr().out


def test_check_denies_spoofed_prod_environment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The action LIES about being dev; the registered target is prod. Resolution
    # from the registry must win and env-scope must deny.
    store = tmp_path / "targets.json"
    _register(store, tmp_path, PROD_TARGET)
    action = {
        "action_type": "inject_fault",
        "target_id": "prod-cluster",
        "environment": "dev",  # spoofed
        "namespace": "payments",
        "namespace_chaos_enabled": True,
        "fault": {"fault_type": "pod_kill", "ratio": 0.3, "duration_seconds": 60},
        "ttl_seconds": 300,
    }
    action_file = _write(tmp_path / "a.json", action)
    assert main(["--store", str(store), "check", str(action_file)]) == 2
    assert "env-scope" in capsys.readouterr().out


def test_check_denies_out_of_scope_namespace(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = tmp_path / "targets.json"
    _register(store, tmp_path, TARGET)  # scope is ["payments"]
    action = {
        "action_type": "inject_fault",
        "target_id": "kind-local",
        "environment": "dev",
        "namespace": "boutique",  # not in the target's allowed_namespaces
        "namespace_chaos_enabled": True,
        "fault": {"fault_type": "pod_kill", "ratio": 0.3, "duration_seconds": 60},
        "ttl_seconds": 300,
    }
    action_file = _write(tmp_path / "a.json", action)
    assert main(["--store", str(store), "check", str(action_file)]) == 2
    assert "namespace-scope" in capsys.readouterr().out


def test_check_allows_valid_action(tmp_path: Path) -> None:
    store = tmp_path / "targets.json"
    _register(store, tmp_path, TARGET)
    action = {
        "action_type": "inject_fault",
        "target_id": "kind-local",
        "environment": "dev",
        "namespace": "payments",
        "namespace_chaos_enabled": True,
        "fault": {"fault_type": "pod_kill", "ratio": 0.3, "duration_seconds": 60},
        "ttl_seconds": 300,
    }
    action_file = _write(tmp_path / "a.json", action)
    assert main(["--store", str(store), "check", str(action_file)]) == 0
