"""ScaleExecutor: gate-checked apply, ungated bounded revert, Kyverno denials."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from chaosagent.agents.permission import ActionBinding, PermissionGate, RunMode
from chaosagent.domain.actions import ProposedAction, ReplicaChange, WorkloadRef
from chaosagent.domain.enums import ActionType, EnvironmentTier
from chaosagent.domain.policy import PolicyDecision
from chaosagent.execute import AppliedScale, ExecutionDenied, ScaleExecutor
from fakes import FakeApiException, FakeClock, FakeScaleApi

_REF = WorkloadRef(kind="deployment", name="cartservice")


def _action(
    namespace: str = "boutique",
    current: int = 4,
    desired: int = 6,
    workload: WorkloadRef = _REF,
) -> ProposedAction:
    return ProposedAction(
        action_type=ActionType.SCALE_WORKLOAD,
        target_id="kind-local",
        environment=EnvironmentTier.DEV,
        namespace=namespace,
        workload=workload,
        replica_change=ReplicaChange(current=current, desired=desired),
        ttl_seconds=300,
    )


def _rig(
    current: int = 4, desired: int = 6, namespace: str = "boutique"
) -> tuple[FakeScaleApi, ScaleExecutor, ActionBinding, FakeClock]:
    clock = FakeClock(start=1000.0)
    gate = PermissionGate(mode=RunMode.EXPERIMENT, clock=clock)
    binding = gate.bind(_action(namespace, current, desired), PolicyDecision.allow())
    api = FakeScaleApi(current=current)
    return api, ScaleExecutor(api, gate, clock=clock), binding, clock


def test_apply_dry_runs_then_patches_and_returns_receipt() -> None:
    api, executor, binding, _ = _rig()
    applied = executor.apply(_REF, "boutique", 6, binding)
    assert api.patches == [
        ("deployment", "cartservice", "boutique", 6, "All"),  # server-side dry-run first
        ("deployment", "cartservice", "boutique", 6, ""),
    ]
    assert applied == AppliedScale(
        kind="deployment",
        name="cartservice",
        namespace="boutique",
        previous=4,
        desired=6,
        applied_at=1000.0,
    )
    assert api.current == 6


def test_dry_run_alone_never_patches_for_real() -> None:
    api, executor, binding, _ = _rig()
    executor.dry_run(_REF, "boutique", 6, binding)
    assert api.patches == [("deployment", "cartservice", "boutique", 6, "All")]
    assert api.current == 4


def test_read_replicas_returns_the_live_count() -> None:
    api, executor, _, _ = _rig(current=7)
    assert executor.read_replicas(_REF, "boutique") == 7
    assert api.reads == [("deployment", "cartservice", "boutique")]


def test_unbound_gate_denies_before_any_api_call() -> None:
    clock = FakeClock()
    gate = PermissionGate(mode=RunMode.EXPERIMENT, clock=clock)
    api = FakeScaleApi()
    executor = ScaleExecutor(api, gate, clock=clock)
    forged = ActionBinding(
        token="forged", action=_action(), decision=PolicyDecision.allow(), expires_at=9e9
    )
    with pytest.raises(ExecutionDenied, match="policy-approved action"):
        executor.apply(_REF, "boutique", 6, forged)
    with pytest.raises(ExecutionDenied, match="policy-approved action"):
        executor.dry_run(_REF, "boutique", 6, forged)
    assert api.patches == [] and api.journal == []


def test_namespace_must_match_the_binding() -> None:
    api, executor, binding, _ = _rig(namespace="boutique")
    with pytest.raises(ExecutionDenied, match="namespace"):
        executor.apply(_REF, "default", 6, binding)
    assert api.patches == [] and api.journal == []


def test_workload_must_match_the_bound_action() -> None:
    # A binding approved for cartservice must not authorize scaling a sibling
    # workload in the same namespace to the approved count.
    api, executor, binding, _ = _rig()
    other = WorkloadRef(kind="deployment", name="frontend")
    with pytest.raises(ExecutionDenied, match="frontend"):
        executor.apply(other, "boutique", 6, binding)
    assert api.patches == [] and api.journal == []


def test_stale_receipt_is_refused_while_another_binding_is_active() -> None:
    # Approval A (4->6) is unbound and B (4->5) bound: a caller holding
    # receipt A must not be able to write A's count under B's slot.
    clock = FakeClock(start=1000.0)
    gate = PermissionGate(mode=RunMode.EXPERIMENT, clock=clock)
    api = FakeScaleApi(current=4)
    executor = ScaleExecutor(api, gate, clock=clock)
    stale = gate.bind(_action(current=4, desired=6), PolicyDecision.allow())
    gate.unbind(stale)
    gate.bind(_action(current=4, desired=5), PolicyDecision.allow())
    with pytest.raises(ExecutionDenied, match="active binding"):
        executor.apply(_REF, "boutique", 6, stale)
    assert api.patches == [] and api.journal == []


def test_replica_count_must_match_the_bound_action() -> None:
    # The binding approved 4->6; patching to any other count under it would
    # evade the cap the policy engine judged.
    api, executor, binding, _ = _rig(current=4, desired=6)
    with pytest.raises(ExecutionDenied, match="approved"):
        executor.apply(_REF, "boutique", 12, binding)
    assert api.patches == [] and api.journal == []


def test_admission_denial_surfaces_the_kyverno_rule() -> None:
    api, executor, binding, _ = _rig()
    api.patch_error = FakeApiException(
        400,
        body=json.dumps(
            {
                "kind": "Status",
                "message": (
                    "admission webhook denied the request: [cap-replica-change] "
                    "replica change exceeds +/-50%"
                ),
            }
        ),
    )
    with pytest.raises(ExecutionDenied, match="cap-replica-change"):
        executor.apply(_REF, "boutique", 6, binding)
    # Denied at the dry-run step: the real patch was never attempted.
    assert api.journal == ["patch_scale:6:dry"]


def test_unexpected_api_error_is_not_swallowed() -> None:
    api, executor, binding, _ = _rig()
    api.patch_error = RuntimeError("connection reset")
    with pytest.raises(RuntimeError, match="connection reset"):
        executor.apply(_REF, "boutique", 6, binding)


def test_revert_works_with_an_expired_binding() -> None:
    # The capacity analogue of the abort delete: moving back to the recorded
    # known-good count must not be blockable by an expired binding.
    api, executor, binding, clock = _rig()
    applied = executor.apply(_REF, "boutique", 6, binding)
    clock.advance(1000)  # well past the 300s TTL
    executor.revert(applied)
    assert api.current == 4
    assert api.patches[-1] == ("deployment", "cartservice", "boutique", 4, "")


def test_revert_only_ever_writes_previous_and_is_idempotent() -> None:
    api, executor, binding, _ = _rig()
    applied = executor.apply(_REF, "boutique", 6, binding)
    executor.revert(applied)
    executor.revert(applied)  # second revert: same target count, still fine
    reverts = api.patches[2:]
    assert all(patch[3] == applied.previous for patch in reverts)
    assert api.current == 4


class CappedScaleApi(FakeScaleApi):
    """FakeScaleApi that mimics the Kyverno cap-replica-change admission policy:
    a patch moving the LIVE count by more than +/-50% is denied, as is any
    scale from zero (both rules judge against the live count)."""

    def patch_scale(
        self, kind: str, name: str, namespace: str, replicas: int, *, dry_run: str | None = None
    ) -> None:
        out_of_cap = self.current > 0 and abs(replicas - self.current) / self.current > 0.5
        from_zero = self.current == 0 and replicas > 0
        if out_of_cap or from_zero:
            self.journal.append(f"patch_denied:{replicas}")
            raise FakeApiException(
                400, body='{"message": "[replica-cap] change exceeds the +/-50% cap"}'
            )
        super().patch_scale(kind, name, namespace, replicas, dry_run=dry_run)


def test_revert_steps_through_downward_drift() -> None:
    # Approved 4->6; an HPA drifts the workload to 9 during the settle window.
    # A direct revert 9->4 (-56%) is denied at admission, so the revert must
    # converge in cap-compliant steps: 9 -> 5 -> 4.
    clock = FakeClock(start=1000.0)
    gate = PermissionGate(mode=RunMode.EXPERIMENT, clock=clock)
    binding = gate.bind(_action(current=4, desired=6), PolicyDecision.allow())
    api = CappedScaleApi(current=4)
    executor = ScaleExecutor(api, gate, clock=clock)
    applied = executor.apply(_REF, "boutique", 6, binding)
    api.current = 9  # live drift after apply
    executor.revert(applied)
    assert api.current == 4
    reverts = [patch[3] for patch in api.patches[2:]]
    assert reverts == [5, 4]  # each step within the admission cap


def test_revert_steps_through_upward_drift() -> None:
    # Approved 6->4 (previous=6); someone scales the workload down to 3.
    # A direct revert 3->6 (+100%) is denied, so it steps 3 -> 4 -> 6.
    clock = FakeClock(start=1000.0)
    gate = PermissionGate(mode=RunMode.EXPERIMENT, clock=clock)
    binding = gate.bind(_action(current=6, desired=4), PolicyDecision.allow())
    api = CappedScaleApi(current=6)
    executor = ScaleExecutor(api, gate, clock=clock)
    applied = executor.apply(_REF, "boutique", 4, binding)
    api.current = 3  # live drift after apply
    executor.revert(applied)
    assert api.current == 6
    reverts = [patch[3] for patch in api.patches[2:]]
    assert reverts == [4, 6]


def test_revert_refuses_to_wake_a_scaled_to_zero_workload() -> None:
    clock = FakeClock(start=1000.0)
    gate = PermissionGate(mode=RunMode.EXPERIMENT, clock=clock)
    binding = gate.bind(_action(current=4, desired=6), PolicyDecision.allow())
    api = CappedScaleApi(current=4)
    executor = ScaleExecutor(api, gate, clock=clock)
    applied = executor.apply(_REF, "boutique", 6, binding)
    api.current = 0  # something scaled it to zero
    with pytest.raises(ExecutionDenied, match="scale-from-zero"):
        executor.revert(applied)


def test_revert_swallows_a_missing_workload() -> None:
    # A workload deleted out from under us leaves nothing to revert; like the
    # abort delete, a 404 must not turn the safety path into a crash.
    api, executor, binding, _ = _rig()
    applied = executor.apply(_REF, "boutique", 6, binding)
    api.patch_error = FakeApiException(404, reason="Not Found")
    executor.revert(applied)  # does not raise


def test_module_imports_without_the_kubernetes_extra() -> None:
    from chaosagent.execute import build_scale_api

    assert callable(build_scale_api)


def test_execute_package_imports_in_a_fresh_process() -> None:
    # Regression: execute -> scale -> capacity -> lifecycle -> execute was a
    # circular import that only in-process test ordering hid. A fresh
    # interpreter importing chaosagent.execute first must succeed.
    src = Path(__file__).resolve().parents[1] / "src"
    result = subprocess.run(
        [sys.executable, "-c", "import chaosagent.execute; import chaosagent.experiment"],
        env={"PYTHONPATH": str(src)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
