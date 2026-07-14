"""HPA bound composition — the second capacity write family, gated before granted.

Phase 3 ships the admission cap (``cap-hpa-bounds``), the experimenter's
autoscaling grant, and this pure patch builder. Any apply path rides the same
permission gate + server-side dry-run as ``/scale``; autonomous HPA writes are
a Phase 4 decision — this phase recommends bounds (``set-hpa-bounds``), it
does not move them.
"""

from __future__ import annotations

from typing import Any

from chaosagent.domain.targets import _SLUG_RE


def compose_hpa_patch(
    name: str, namespace: str, *, min_replicas: int, max_replicas: int
) -> dict[str, Any]:
    """Patch body bounding an ``autoscaling/v2`` HorizontalPodAutoscaler. Pure:
    validates shape only — admissibility is the engine's and Kyverno's call."""
    for label, value in (("name", name), ("namespace", namespace)):
        if not _SLUG_RE.match(value):
            raise ValueError(
                f"HPA {label} {value!r} must be a DNS label (lowercase alphanumeric and '-')"
            )
    if min_replicas < 1:
        raise ValueError(f"min_replicas must be >= 1, got {min_replicas}")
    if max_replicas < min_replicas:
        raise ValueError(
            f"max_replicas ({max_replicas}) must be >= min_replicas ({min_replicas})"
        )
    return {
        "apiVersion": "autoscaling/v2",
        "kind": "HorizontalPodAutoscaler",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"minReplicas": min_replicas, "maxReplicas": max_replicas},
    }
