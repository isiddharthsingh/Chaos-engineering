"""Chaos Mesh CR executor over the Kubernetes CustomObjects API.

The apply path is belt-and-suspenders by construction: gate authorization ->
shape checks -> server-side dry-run (Kyverno admission runs on dry-run, so this
IS the live policy self-check) -> real create. The delete path is never gated —
abort/rollback must not be blockable by an expired binding.

The ``kubernetes`` client lives in the optional ``agent`` extra and is imported
lazily; this module imports (and the executor tests run) without it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from chaosagent.agents.permission import ActionBinding, PermissionGate
from chaosagent.clock import Clock, SystemClock
from chaosagent.faults.chaosmesh import MANAGED_BY_LABEL, MANAGED_BY_VALUE

GROUP = "chaos-mesh.org"
VERSION = "v1alpha1"
#: k6-operator load CRs live in their own API group.
K6_GROUP = "k6.io"
K6_VERSION = "v1alpha1"

#: CR kinds this executor may touch, and their API plurals — kept in lockstep
#: with the composer dispatcher so every kind it emits is deletable.
PLURALS: dict[str, str] = {
    "PodChaos": "podchaos",
    "NetworkChaos": "networkchaos",
    "StressChaos": "stresschaos",
    "IOChaos": "iochaos",
    "DNSChaos": "dnschaos",
    "TimeChaos": "timechaos",
}
K6_PLURALS: dict[str, str] = {"TestRun": "testruns"}


def _route(kind: str) -> tuple[str, str, str] | None:
    """kind -> (group, version, plural) across every API family we may touch."""
    if kind in PLURALS:
        return GROUP, VERSION, PLURALS[kind]
    if kind in K6_PLURALS:
        return K6_GROUP, K6_VERSION, K6_PLURALS[kind]
    return None

#: The namespaced write identity; impersonating it makes the tiered RBAC apply
#: for real (same mechanism as `kubectl --as` in scripts/verify-guardrails.sh).
EXPERIMENTER_USER = "system:serviceaccount:chaos-agent-system:agent-experimenter"


class ExecutionDenied(RuntimeError):
    """A write was refused — by the gate, a shape check, or the admission layer."""


class CustomObjectsApiProtocol(Protocol):
    """The slice of kubernetes.client.CustomObjectsApi the executor uses."""

    def create_namespaced_custom_object(
        self,
        group: str,
        version: str,
        namespace: str,
        plural: str,
        body: dict[str, Any],
        *,
        dry_run: str | None = None,
    ) -> object: ...

    def delete_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, name: str
    ) -> object: ...

    def list_namespaced_custom_object(
        self,
        group: str,
        version: str,
        namespace: str,
        plural: str,
        *,
        label_selector: str | None = None,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class AppliedExperiment:
    """Receipt for a CR this executor created — everything delete needs."""

    kind: str
    name: str
    namespace: str
    applied_at: float


def _api_status(exc: BaseException) -> int | None:
    """Duck-typed ApiException detection (kubernetes may not be installed)."""
    status = getattr(exc, "status", None)
    return status if isinstance(status, int) else None


def _denial_message(exc: BaseException) -> str:
    """Pull the admission webhook message (it carries the Kyverno rule id)."""
    body = getattr(exc, "body", None)
    if isinstance(body, str) and body:
        try:
            parsed = json.loads(body)
        except ValueError:
            return body
        if isinstance(parsed, dict) and parsed.get("message"):
            return str(parsed["message"])
        return body
    return str(exc)


class ChaosMeshExecutor:
    """Applies and deletes Chaos Mesh CRs under the permission gate."""

    def __init__(
        self,
        api: CustomObjectsApiProtocol,
        gate: PermissionGate,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._api = api
        self._gate = gate
        self._clock: Clock = clock or SystemClock()

    def dry_run(self, cr: dict[str, Any], binding: ActionBinding) -> None:
        """Run the full admission chain server-side without persisting the CR."""
        namespace, route = self._admit(cr, binding)
        self._create(cr, namespace, route, dry_run=True)

    def apply(self, cr: dict[str, Any], binding: ActionBinding) -> AppliedExperiment:
        """Gate -> shape checks -> server-side dry-run -> real create. Each step
        is fatal: nothing is created unless every layer said yes."""
        namespace, route = self._admit(cr, binding)
        self._create(cr, namespace, route, dry_run=True)
        self._create(cr, namespace, route, dry_run=False)
        return AppliedExperiment(
            kind=str(cr["kind"]),
            name=str(cr["metadata"]["name"]),
            namespace=namespace,
            applied_at=self._clock.now(),
        )

    def delete(self, applied: AppliedExperiment) -> None:
        """Delete the CR. Never gated, idempotent (404 is swallowed) — this is
        the abort path and must always be able to move toward safety."""
        route = _route(applied.kind)
        if route is None:
            raise KeyError(applied.kind)
        group, version, plural = route
        try:
            self._api.delete_namespaced_custom_object(
                group, version, applied.namespace, plural, applied.name
            )
        except Exception as exc:
            if _api_status(exc) == 404:
                return
            raise

    def count_running(self, namespace: str) -> int:
        """How many chaosagent-managed resources exist in the namespace —
        the probe behind the ``single-experiment`` policy rule. A 404 means the
        kind's CRD is not installed, i.e. zero such resources exist."""
        selector = f"{MANAGED_BY_LABEL}={MANAGED_BY_VALUE}"
        routes = [(GROUP, VERSION, plural) for plural in PLURALS.values()]
        routes += [(K6_GROUP, K6_VERSION, plural) for plural in K6_PLURALS.values()]
        total = 0
        for group, version, plural in routes:
            try:
                listed = self._api.list_namespaced_custom_object(
                    group, version, namespace, plural, label_selector=selector
                )
            except Exception as exc:
                if _api_status(exc) == 404:
                    continue
                raise
            total += len(listed.get("items", []))
        return total

    def _admit(
        self, cr: dict[str, Any], binding: ActionBinding
    ) -> tuple[str, tuple[str, str, str]]:
        metadata = cr.get("metadata") or {}
        namespace = metadata.get("namespace")
        result = self._gate.authorize_write(
            namespace=namespace if isinstance(namespace, str) else None
        )
        if not result.allowed:
            raise ExecutionDenied(result.reason)
        kind = cr.get("kind")
        route = _route(str(kind))
        if route is None:
            raise ExecutionDenied(f"CR kind {kind!r} is not executable")
        if namespace != binding.action.namespace:
            raise ExecutionDenied(
                f"CR namespace {namespace!r} does not match the bound action's "
                f"namespace {binding.action.namespace!r}"
            )
        return str(namespace), route

    def _create(
        self, cr: dict[str, Any], namespace: str, route: tuple[str, str, str], *, dry_run: bool
    ) -> None:
        group, version, plural = route
        try:
            if dry_run:
                self._api.create_namespaced_custom_object(
                    group, version, namespace, plural, cr, dry_run="All"
                )
            else:
                self._api.create_namespaced_custom_object(group, version, namespace, plural, cr)
        except Exception as exc:
            if _api_status(exc) is None:
                raise
            stage = "server-side dry-run" if dry_run else "create"
            raise ExecutionDenied(
                f"{cr['kind']} {cr['metadata']['name']!r} rejected at {stage}: "
                f"{_denial_message(exc)}"
            ) from exc


def build_experimenter_api(
    *,
    kubeconfig: str | None = None,
    context: str | None = None,
    impersonate: str | None = EXPERIMENTER_USER,
) -> Any:
    """CustomObjectsApi that impersonates the experimenter ServiceAccount, so
    the namespaced RBAC genuinely bounds every write this process makes."""
    from kubernetes import client, config

    configuration = client.Configuration()
    config.load_kube_config(
        config_file=kubeconfig, context=context, client_configuration=configuration
    )
    api_client = client.ApiClient(configuration)
    if impersonate:
        api_client.set_default_header("Impersonate-User", impersonate)
    return client.CustomObjectsApi(api_client)


def read_namespace_chaos_enabled(
    namespace: str, *, kubeconfig: str | None = None, context: str | None = None
) -> bool:
    """Whether the namespace carries ``chaos-enabled=true`` — resolved from the
    live cluster (not impersonated: reading namespace labels is an observer
    concern, and the experimenter role deliberately cannot)."""
    from kubernetes import client, config

    configuration = client.Configuration()
    config.load_kube_config(
        config_file=kubeconfig, context=context, client_configuration=configuration
    )
    core = client.CoreV1Api(client.ApiClient(configuration))
    labels = core.read_namespace(namespace).metadata.labels or {}
    return bool(labels.get("chaos-enabled") == "true")


def read_configmap_exists(
    namespace: str, name: str, *, kubeconfig: str | None = None, context: str | None = None
) -> bool:
    """Whether a ConfigMap exists — the PREFLIGHT probe behind k6 load specs (a
    TestRun referencing a missing script ConfigMap is admitted but never starts
    any load). Read like the namespace probe: an observer concern, not
    impersonated (the experimenter role deliberately cannot read ConfigMaps)."""
    from kubernetes import client, config

    configuration = client.Configuration()
    config.load_kube_config(
        config_file=kubeconfig, context=context, client_configuration=configuration
    )
    core = client.CoreV1Api(client.ApiClient(configuration))
    try:
        core.read_namespaced_config_map(name, namespace)
    except Exception as exc:
        if _api_status(exc) == 404:
            return False
        raise
    return True
