"""ChaosMeshExecutor: gate-checked apply, ungated delete, Kyverno-carrying denials."""

from __future__ import annotations

import json

import pytest

from chaosagent.agents.permission import ActionBinding, PermissionGate, RunMode
from chaosagent.domain.actions import FaultSpec, ProposedAction
from chaosagent.domain.enums import ActionType, EnvironmentTier, FaultType
from chaosagent.domain.policy import PolicyDecision
from chaosagent.execute import AppliedExperiment, ChaosMeshExecutor, ExecutionDenied
from chaosagent.faults import compose_podchaos
from fakes import FakeApiException, FakeClock, FakeCustomObjectsApi

_FAULT = FaultSpec(
    fault_type=FaultType.POD_KILL,
    selector={"app": "cartservice"},
    ratio=0.34,
    duration_seconds=60,
)


def _action(namespace: str = "boutique") -> ProposedAction:
    return ProposedAction(
        action_type=ActionType.INJECT_FAULT,
        target_id="kind-local",
        environment=EnvironmentTier.DEV,
        namespace=namespace,
        namespace_chaos_enabled=True,
        fault=_FAULT,
        ttl_seconds=300,
    )


def _rig(
    namespace: str = "boutique",
) -> tuple[FakeCustomObjectsApi, ChaosMeshExecutor, ActionBinding]:
    clock = FakeClock(start=1000.0)
    gate = PermissionGate(mode=RunMode.EXPERIMENT, clock=clock)
    binding = gate.bind(_action(namespace), PolicyDecision.allow())
    api = FakeCustomObjectsApi()
    return api, ChaosMeshExecutor(api, gate, clock=clock), binding


def _cr(namespace: str = "boutique", name: str = "probe") -> dict[str, object]:
    return compose_podchaos(_FAULT, namespace=namespace, name=name)


def test_apply_dry_runs_then_creates_and_returns_receipt() -> None:
    api, executor, binding = _rig()
    applied = executor.apply(_cr(), binding)
    assert api.calls == [
        ("create", "boutique", "podchaos", "probe", "All"),  # server-side dry-run first
        ("create", "boutique", "podchaos", "probe", ""),
    ]
    assert applied == AppliedExperiment(
        kind="PodChaos", name="probe", namespace="boutique", applied_at=1000.0
    )


def test_dry_run_alone_never_creates() -> None:
    api, executor, binding = _rig()
    executor.dry_run(_cr(), binding)
    assert api.calls == [("create", "boutique", "podchaos", "probe", "All")]


def test_unbound_gate_denies_before_any_api_call() -> None:
    gate = PermissionGate(mode=RunMode.EXPERIMENT, clock=FakeClock())
    api = FakeCustomObjectsApi()
    executor = ChaosMeshExecutor(api, gate, clock=FakeClock())
    forged = ActionBinding(
        token="forged", action=_action(), decision=PolicyDecision.allow(), expires_at=9e9
    )
    with pytest.raises(ExecutionDenied, match="policy-approved action"):
        executor.apply(_cr(), forged)
    assert api.calls == []


def test_cr_namespace_must_match_the_binding() -> None:
    api, executor, binding = _rig(namespace="boutique")
    with pytest.raises(ExecutionDenied, match="namespace"):
        executor.apply(_cr(namespace="default"), binding)
    assert api.calls == []


def test_unknown_kind_is_refused() -> None:
    api, executor, binding = _rig()
    cr = _cr()
    cr["kind"] = "NetworkChaos"
    with pytest.raises(ExecutionDenied, match="NetworkChaos"):
        executor.apply(cr, binding)
    assert api.calls == []


def test_admission_denial_surfaces_the_kyverno_rule() -> None:
    api, executor, binding = _rig()
    api.create_error = FakeApiException(
        400,
        body=json.dumps(
            {
                "kind": "Status",
                "message": (
                    "admission webhook denied the request: [require-chaos-namespace] "
                    "namespace 'boutique' is not labelled chaos-enabled=true"
                ),
            }
        ),
    )
    with pytest.raises(ExecutionDenied, match="require-chaos-namespace"):
        executor.apply(_cr(), binding)
    # Denied at the dry-run step: the real create was never attempted.
    assert api.calls == [("create", "boutique", "podchaos", "probe", "All")]


def test_unexpected_api_error_is_not_swallowed() -> None:
    api, executor, binding = _rig()
    api.create_error = RuntimeError("connection reset")
    with pytest.raises(RuntimeError, match="connection reset"):
        executor.apply(_cr(), binding)


def test_delete_is_idempotent_and_never_gated() -> None:
    api, executor, binding = _rig()
    applied = executor.apply(_cr(), binding)
    # An expired/absent binding must not block moving toward safety.
    ungated = ChaosMeshExecutor(
        api, PermissionGate(mode=RunMode.EXPERIMENT, clock=FakeClock()), clock=FakeClock()
    )
    ungated.delete(applied)
    ungated.delete(applied)  # second delete: 404 swallowed
    deletes = [call for call in api.calls if call[0] == "delete"]
    assert len(deletes) == 2


def test_count_running_uses_the_managed_by_selector() -> None:
    api, executor, binding = _rig()
    assert executor.count_running("boutique") == 0
    executor.apply(_cr(), binding)
    assert executor.count_running("boutique") == 1
    assert executor.count_running("other") == 0
    list_calls = [call for call in api.calls if call[0] == "list"]
    assert list_calls[0][3] == "app.kubernetes.io/managed-by=chaosagent"


def test_module_imports_without_the_kubernetes_extra() -> None:
    # The module must not import `kubernetes` at import time; presence of the
    # symbols proves the lazy layout (the real proof is the no-extra CI run).
    from chaosagent.execute import build_experimenter_api, read_namespace_chaos_enabled

    assert callable(build_experimenter_api)
    assert callable(read_namespace_chaos_enabled)
