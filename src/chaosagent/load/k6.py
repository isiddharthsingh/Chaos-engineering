"""Compose k6-operator ``TestRun`` CRs — load applied alongside a fault.

Pure functions like the fault composers: LoadSpec in, CR dict out, no I/O.

The k6 script ConfigMap must PRE-EXIST in the target namespace: creating a
ConfigMap is a write the experimenter RBAC deliberately does not grant, so
inline-script -> ConfigMap creation is deferred until that grant is an explicit
decision. The composed TestRun only *references* the ConfigMap
(``spec.script.configMap.{name,file}``).

Blast-radius note: a TestRun is gated by its own Kyverno namespace policy
(``config/policies/kyverno/load/require-chaos-namespace-k6.yaml``) — the chaos
policies match Chaos Mesh kinds only and would not refuse it.
"""

from __future__ import annotations

import secrets
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from chaosagent.faults.chaosmesh import MANAGED_BY_LABEL, MANAGED_BY_VALUE

K6_API_VERSION = "k6.io/v1alpha1"


class LoadSpec(BaseModel):
    """Parameters for k6 load applied during an experiment (engine-agnostic)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Name of the pre-existing ConfigMap holding the k6 script.
    script_configmap: str = Field(min_length=1)
    #: File key inside that ConfigMap.
    script_file: str = Field(default="script.js", min_length=1)
    #: Number of k6 runner pods. Capped: load blast radius does not pass through
    #: the policy engine's fault-ratio rule, so the bound lives here.
    parallelism: int = Field(default=1, ge=1, le=10)
    #: How long the load runs — emitted as k6's ``--duration``, overriding
    #: whatever the script declares, so an orphaned TestRun still self-stops.
    duration_seconds: int = Field(gt=0)
    #: Bounded lifetime for the load resources; must cover the run duration and
    #: is capped by the experiment's own ttl (enforced on ExperimentSpec).
    ttl_seconds: int = Field(gt=0)

    @model_validator(mode="after")
    def _ttl_covers_duration(self) -> LoadSpec:
        if self.ttl_seconds < self.duration_seconds:
            raise ValueError(
                f"load ttl_seconds ({self.ttl_seconds}) must cover duration_seconds "
                f"({self.duration_seconds})"
            )
        return self


def compose_testrun(
    load: LoadSpec,
    *,
    namespace: str,
    name: str | None = None,
) -> dict[str, Any]:
    """Compose a k6-operator ``TestRun`` referencing the pre-existing script.

    ``--duration`` and ``cleanup: post`` make the load self-bounding like every
    fault CR: even if the agent dies after apply, k6 stops at duration_seconds
    and the operator tears its runner jobs down.
    """
    return {
        "apiVersion": K6_API_VERSION,
        "kind": "TestRun",
        "metadata": {
            "name": name or f"chaosagent-load-{secrets.token_hex(4)}",
            "namespace": namespace,
            "labels": {MANAGED_BY_LABEL: MANAGED_BY_VALUE},
        },
        "spec": {
            "parallelism": load.parallelism,
            "arguments": f"--duration {load.duration_seconds}s",
            "cleanup": "post",
            "script": {
                "configMap": {
                    "name": load.script_configmap,
                    "file": load.script_file,
                }
            },
        },
    }
