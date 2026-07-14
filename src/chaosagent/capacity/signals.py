"""Utilization signals — PromQL builders and the workload usage snapshot.

Query shapes target the kube-prometheus-stack defaults (cAdvisor +
kube-state-metrics). Everything is fetched through the ``ScalarSource``
protocol, so tests script the signals offline and the recommender stays pure.

The VPA reader is recommend-only by construction: it reads with observer
credentials and its output feeds the recommendation *rationale*, never the
replica math. KEDA and Karpenter signals are the same shape; their writes
(like VPA's) are Phase-4 decisions.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from chaosagent.capacity.spec import WorkloadRef
from chaosagent.execute.kubernetes import _api_status
from chaosagent.observe import ScalarSource


class SignalError(RuntimeError):
    """A signal the recommendation cannot proceed without could not be resolved."""


#: kube-state-metrics series carrying the declared replica count, per kind.
_REPLICA_SERIES: dict[str, tuple[str, str]] = {
    "deployment": ("kube_deployment_spec_replicas", "deployment"),
    "statefulset": ("kube_statefulset_replicas", "statefulset"),
}


def replicas_query(namespace: str, workload: WorkloadRef) -> str:
    # max() collapses duplicate series (HA kube-state-metrics pairs, doubled
    # scrape jobs) into the one unambiguous count a scalar read requires.
    series, label = _REPLICA_SERIES[workload.kind]
    return f'max({series}{{namespace="{namespace}",{label}="{workload.name}"}})'


def _pod_re(workload: WorkloadRef) -> str:
    # Deployment pods are "<name>-<rs-hash>-<pod-hash>", statefulset pods
    # "<name>-<ordinal>". The suffix groups must not admit a '-', or the
    # pattern for `frontend` would also swallow `frontend-v2`'s pods and
    # contaminate the utilization ratio with a sibling workload.
    if workload.kind == "statefulset":
        return f"{workload.name}-[0-9]+"
    return f"{workload.name}-[a-z0-9]+-[a-z0-9]+"


def _cpu_ratio(namespace: str, workload: WorkloadRef, window: str) -> str:
    pods = _pod_re(workload)
    return (
        f'sum(rate(container_cpu_usage_seconds_total{{namespace="{namespace}",'
        f'pod=~"{pods}",container!=""}}[{window}]))'
        " / "
        f'sum(kube_pod_container_resource_requests{{namespace="{namespace}",'
        f'pod=~"{pods}",resource="cpu"}})'
    )


def _memory_ratio(namespace: str, workload: WorkloadRef) -> str:
    pods = _pod_re(workload)
    return (
        f'sum(container_memory_working_set_bytes{{namespace="{namespace}",'
        f'pod=~"{pods}",container!=""}})'
        " / "
        f'sum(kube_pod_container_resource_requests{{namespace="{namespace}",'
        f'pod=~"{pods}",resource="memory"}})'
    )


def cpu_avg_utilization_query(
    namespace: str, workload: WorkloadRef, *, lookback_minutes: int
) -> str:
    """Average CPU used vs requested over the lookback (a rate over the whole
    window IS the window average)."""
    return _cpu_ratio(namespace, workload, f"{lookback_minutes}m")


def cpu_p95_utilization_query(
    namespace: str, workload: WorkloadRef, *, lookback_minutes: int
) -> str:
    return (
        f"quantile_over_time(0.95, ({_cpu_ratio(namespace, workload, '5m')})"
        f"[{lookback_minutes}m:1m])"
    )


def memory_avg_utilization_query(
    namespace: str, workload: WorkloadRef, *, lookback_minutes: int
) -> str:
    return f"avg_over_time(({_memory_ratio(namespace, workload)})[{lookback_minutes}m:1m])"


def memory_p95_utilization_query(
    namespace: str, workload: WorkloadRef, *, lookback_minutes: int
) -> str:
    return (
        f"quantile_over_time(0.95, ({_memory_ratio(namespace, workload)})"
        f"[{lookback_minutes}m:1m])"
    )


VPA_GROUP = "autoscaling.k8s.io"
VPA_VERSION = "v1"
VPA_PLURAL = "verticalpodautoscalers"

#: WorkloadRef.kind -> the Kind string a VPA targetRef uses.
_VPA_TARGET_KINDS = {"deployment": "Deployment", "statefulset": "StatefulSet"}


class VpaRecommendation(BaseModel):
    """One container's target requests as recommended by a VPA (read-only)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    container: str
    cpu: str | None = None
    memory: str | None = None


def read_vpa_recommendations(
    api: Any, namespace: str, workload: WorkloadRef
) -> tuple[VpaRecommendation, ...]:
    """Target requests from any VPA pointing at the workload; () when the CRD
    is not installed or no VPA matches. An observer read — this phase never
    writes VPAs (vertical writes are a Phase-4 decision)."""
    try:
        listed = api.list_namespaced_custom_object(VPA_GROUP, VPA_VERSION, namespace, VPA_PLURAL)
    except Exception as exc:
        if _api_status(exc) == 404:
            return ()
        raise
    target_kind = _VPA_TARGET_KINDS[workload.kind]
    recommendations: list[VpaRecommendation] = []
    for item in listed.get("items", []):
        target = (item.get("spec") or {}).get("targetRef") or {}
        if target.get("kind") != target_kind or target.get("name") != workload.name:
            continue
        status = (item.get("status") or {}).get("recommendation") or {}
        for container in status.get("containerRecommendations") or []:
            requested = container.get("target") or {}
            recommendations.append(
                VpaRecommendation(
                    container=str(container.get("containerName", "")),
                    cpu=str(requested["cpu"]) if "cpu" in requested else None,
                    memory=str(requested["memory"]) if "memory" in requested else None,
                )
            )
    return tuple(recommendations)


def read_live_vpa_recommendations(
    namespace: str,
    workload: WorkloadRef,
    *,
    kubeconfig: str | None = None,
    context: str | None = None,
) -> tuple[VpaRecommendation, ...]:
    """Live variant over a non-impersonated CustomObjectsApi (an observer
    concern, like the namespace-label probe). Returns () without the
    ``kubernetes`` extra — the VPA signal is advisory."""
    try:
        from kubernetes import client, config
    except ImportError:
        return ()
    configuration = client.Configuration()
    config.load_kube_config(
        config_file=kubeconfig, context=context, client_configuration=configuration
    )
    api = client.CustomObjectsApi(client.ApiClient(configuration))
    return read_vpa_recommendations(api, namespace, workload)


class WorkloadUsage(BaseModel):
    """Point-in-time utilization snapshot for one workload.

    Utilization values are fractions of the *requested* resources (1.0 = using
    exactly what is requested); None means the signal was unavailable.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    namespace: str
    workload: WorkloadRef
    current_replicas: int = Field(ge=0)
    cpu_avg: float | None = None
    cpu_p95: float | None = None
    memory_avg: float | None = None
    memory_p95: float | None = None
    lookback_minutes: int = Field(gt=0)
    #: VPA target requests, folded into the rationale (recommend-only).
    vpa: tuple[VpaRecommendation, ...] = ()


def fetch_usage(
    client: ScalarSource,
    namespace: str,
    workload: WorkloadRef,
    *,
    lookback_minutes: int = 60,
    vpa: tuple[VpaRecommendation, ...] = (),
) -> WorkloadUsage:
    """Assemble the snapshot from live signals. The replica count is required
    (SignalError when absent — there is nothing to size without it); the
    utilization signals are advisory and stay None when their series are absent.
    """
    replicas = client.scalar(replicas_query(namespace, workload))
    if replicas is None:
        raise SignalError(
            f"no replica count in Prometheus for {workload.kind}/{workload.name} in "
            f"namespace {namespace!r} (is kube-state-metrics scraping it?)"
        )
    return WorkloadUsage(
        namespace=namespace,
        workload=workload,
        current_replicas=int(replicas),
        cpu_avg=client.scalar(
            cpu_avg_utilization_query(namespace, workload, lookback_minutes=lookback_minutes)
        ),
        cpu_p95=client.scalar(
            cpu_p95_utilization_query(namespace, workload, lookback_minutes=lookback_minutes)
        ),
        memory_avg=client.scalar(
            memory_avg_utilization_query(namespace, workload, lookback_minutes=lookback_minutes)
        ),
        memory_p95=client.scalar(
            memory_p95_utilization_query(namespace, workload, lookback_minutes=lookback_minutes)
        ),
        lookback_minutes=lookback_minutes,
        vpa=vpa,
    )
