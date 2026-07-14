"""Workload ``/scale`` executor — the capacity twin of the chaos executor.

The Kyverno ``cap-replica-change`` policy matches the ``/scale`` subresource,
so the server-side dry-run of a scale patch IS the live policy self-check —
the same mechanism as chaos CRs. Apply is gate-checked; revert deliberately is
not (the capacity analogue of the abort delete): moving back to the recorded
known-good count must never be blockable by an expired binding, and it only
ever moves toward ``applied.previous`` — directly, or in cap-compliant steps
when the live count drifted out of the admission cap's reach. Kyverno still
sees every revert patch — belt and suspenders, not a bypass.

The ``kubernetes`` client lives in the optional ``agent`` extra and is imported
lazily; this module imports (and the executor tests run) without it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Protocol

from chaosagent.agents.permission import ActionBinding, PermissionGate
from chaosagent.clock import Clock, SystemClock
from chaosagent.domain.actions import ReplicaChange, WorkloadRef
from chaosagent.execute.kubernetes import (
    EXPERIMENTER_USER,
    ExecutionDenied,
    _api_status,
    _denial_message,
)


class ScaleApiProtocol(Protocol):
    """The slice of the apps/v1 scale API the executor uses."""

    def read_scale(self, kind: str, name: str, namespace: str) -> int: ...

    def patch_scale(
        self, kind: str, name: str, namespace: str, replicas: int, *, dry_run: str | None = None
    ) -> None: ...


@dataclass(frozen=True)
class AppliedScale:
    """Receipt for a replica change this executor made — everything revert needs."""

    kind: str
    name: str
    namespace: str
    previous: int
    desired: int
    applied_at: float


#: Cap-compliant revert steps shrink the live/target ratio by >= (1 + cap) per
#: patch, so 16 steps cover any drift the admission layer could have admitted.
_MAX_REVERT_STEPS = 16


class ScaleExecutor:
    """Applies and reverts bounded replica changes under the permission gate.

    ``revert_step_pct`` is the admission layer's per-patch cap (Kyverno
    ``cap-replica-change``); the stepped revert stays inside it so a revert can
    always make admissible progress even after live drift.
    """

    def __init__(
        self,
        api: ScaleApiProtocol,
        gate: PermissionGate,
        *,
        clock: Clock | None = None,
        revert_step_pct: float = 0.5,
    ) -> None:
        self._api = api
        self._gate = gate
        self._clock: Clock = clock or SystemClock()
        self._revert_step_pct = revert_step_pct

    def read_replicas(self, ref: WorkloadRef, namespace: str) -> int:
        """The workload's current replica count (a read; never gated)."""
        return self._api.read_scale(ref.kind, ref.name, namespace)

    def dry_run(
        self, ref: WorkloadRef, namespace: str, replicas: int, binding: ActionBinding
    ) -> None:
        """Run the full admission chain server-side without persisting the patch."""
        self._admit(ref, namespace, replicas, binding)
        self._patch(ref.kind, ref.name, namespace, replicas, dry_run=True)

    def apply(
        self, ref: WorkloadRef, namespace: str, replicas: int, binding: ActionBinding
    ) -> AppliedScale:
        """Gate -> shape checks -> server-side dry-run -> real patch. Each step
        is fatal: nothing is changed unless every layer said yes."""
        change = self._admit(ref, namespace, replicas, binding)
        self._patch(ref.kind, ref.name, namespace, replicas, dry_run=True)
        self._patch(ref.kind, ref.name, namespace, replicas, dry_run=False)
        return AppliedScale(
            kind=ref.kind,
            name=ref.name,
            namespace=namespace,
            previous=change.current,
            desired=replicas,
            applied_at=self._clock.now(),
        )

    def revert(self, applied: AppliedScale) -> None:
        """Return the workload to its recorded previous count. Never gated,
        idempotent (patching to the same count twice is a no-op, and a deleted
        workload's 404 is swallowed) — this is the auto-revert path and must
        always be able to move toward the known-good state.

        The direct patch can be denied by the admission cap if the live count
        drifted after apply (an HPA, another operator): the cap judges against
        the LIVE count, not the one we recorded. In that case the revert walks
        toward the previous count in cap-compliant steps instead of giving up —
        moving toward safety must not be blockable by our own guardrail."""
        try:
            self._patch(
                applied.kind, applied.name, applied.namespace, applied.previous, dry_run=False
            )
            return
        except ExecutionDenied as exc:
            if self._is_missing(exc):
                return
        self._stepped_revert(applied)

    def _stepped_revert(self, applied: AppliedScale) -> None:
        target = applied.previous
        for _ in range(_MAX_REVERT_STEPS):
            try:
                live = self._api.read_scale(applied.kind, applied.name, applied.namespace)
            except Exception as exc:
                if _api_status(exc) == 404:
                    return  # workload gone: nothing left to revert
                raise
            if live == target:
                return
            if live <= 0:
                # Scaling from zero is unbounded and refused everywhere;
                # waking a scaled-to-zero workload needs a human.
                raise ExecutionDenied(
                    f"cannot revert {applied.kind} {applied.name!r} to {target}: the "
                    "live count is 0 and scale-from-zero requires a human"
                )
            cap = self._revert_step_pct
            if target > live:
                step = min(target, math.floor(round(live * (1 + cap), 9)))
            else:
                step = max(target, math.ceil(round(live * (1 - cap), 9)))
            if step == live:
                raise ExecutionDenied(
                    f"revert of {applied.kind} {applied.name!r} to {target} cannot make "
                    f"an admissible step from {live} under the +/-{cap:.0%} cap"
                )
            try:
                self._patch(applied.kind, applied.name, applied.namespace, step, dry_run=False)
            except ExecutionDenied as exc:
                if self._is_missing(exc):
                    return
                raise
        raise ExecutionDenied(
            f"revert of {applied.kind} {applied.name!r} to {target} did not converge "
            f"within {_MAX_REVERT_STEPS} steps"
        )

    @staticmethod
    def _is_missing(exc: ExecutionDenied) -> bool:
        cause = exc.__cause__
        return cause is not None and _api_status(cause) == 404

    def _admit(
        self, ref: WorkloadRef, namespace: str, replicas: int, binding: ActionBinding
    ) -> ReplicaChange:
        result = self._gate.authorize_write(namespace=namespace)
        if not result.allowed:
            raise ExecutionDenied(result.reason)
        # The receipt must BE the gate's active binding — a retained receipt
        # from an earlier (unbound) approval must not pass its own content
        # checks while a different approval holds the slot.
        active = self._gate.active_binding()
        if active is None or active.token != binding.token:
            raise ExecutionDenied(
                "the supplied binding is not the gate's active binding; "
                "writes are only honoured under the currently bound approval"
            )
        if namespace != binding.action.namespace:
            raise ExecutionDenied(
                f"scale targets namespace {namespace!r} but the bound action is scoped "
                f"to {binding.action.namespace!r}"
            )
        bound_workload = binding.action.workload
        if (
            bound_workload is None
            or bound_workload.kind != ref.kind
            or bound_workload.name != ref.name
        ):
            approved = (
                "no workload"
                if bound_workload is None
                else f"{bound_workload.kind}/{bound_workload.name}"
            )
            raise ExecutionDenied(
                f"scale targets {ref.kind}/{ref.name} but the bound action approved "
                f"{approved}; a binding authorizes exactly the workload the policy "
                "engine judged"
            )
        change = binding.action.replica_change
        if change is None or replicas != change.desired:
            approved = "none" if change is None else f"{change.current}->{change.desired}"
            raise ExecutionDenied(
                f"scale to {replicas} does not match the bound action's approved "
                f"replica change ({approved})"
            )
        return change

    def _patch(
        self, kind: str, name: str, namespace: str, replicas: int, *, dry_run: bool
    ) -> None:
        try:
            if dry_run:
                self._api.patch_scale(kind, name, namespace, replicas, dry_run="All")
            else:
                self._api.patch_scale(kind, name, namespace, replicas)
        except Exception as exc:
            if _api_status(exc) is None:
                raise
            stage = "server-side dry-run" if dry_run else "patch"
            raise ExecutionDenied(
                f"scale of {kind} {name!r} to {replicas} rejected at {stage}: "
                f"{_denial_message(exc)}"
            ) from exc


class _AppsScaleApi:
    """Adapts AppsV1Api's per-kind ``/scale`` methods to :class:`ScaleApiProtocol`."""

    def __init__(self, apps: Any) -> None:
        self._apps = apps

    def read_scale(self, kind: str, name: str, namespace: str) -> int:
        if kind == "deployment":
            scale = self._apps.read_namespaced_deployment_scale(name, namespace)
        elif kind == "statefulset":
            scale = self._apps.read_namespaced_stateful_set_scale(name, namespace)
        else:
            raise KeyError(kind)
        return int(scale.spec.replicas or 0)

    def patch_scale(
        self, kind: str, name: str, namespace: str, replicas: int, *, dry_run: str | None = None
    ) -> None:
        body = {"spec": {"replicas": replicas}}
        kwargs: dict[str, str] = {"dry_run": dry_run} if dry_run else {}
        if kind == "deployment":
            self._apps.patch_namespaced_deployment_scale(name, namespace, body, **kwargs)
        elif kind == "statefulset":
            self._apps.patch_namespaced_stateful_set_scale(name, namespace, body, **kwargs)
        else:
            raise KeyError(kind)


def build_scale_api(
    *,
    kubeconfig: str | None = None,
    context: str | None = None,
    impersonate: str | None = EXPERIMENTER_USER,
) -> Any:
    """Scale API that impersonates the experimenter ServiceAccount, so the
    namespaced RBAC genuinely bounds every scale write this process makes —
    the twin of ``build_experimenter_api``."""
    from kubernetes import client, config

    configuration = client.Configuration()
    config.load_kube_config(
        config_file=kubeconfig, context=context, client_configuration=configuration
    )
    api_client = client.ApiClient(configuration)
    if impersonate:
        api_client.set_default_header("Impersonate-User", impersonate)
    return _AppsScaleApi(client.AppsV1Api(api_client))
