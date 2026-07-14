"""Execution — the only layer that touches the cluster with write credentials.

Everything here runs *after* the policy engine has approved an action and the
permission gate holds its binding; the abort/rollback delete path is the sole
exception, deliberately ungated so moving toward safety can never be blocked.
"""

from chaosagent.execute.kubernetes import (
    PLURALS,
    AppliedExperiment,
    ChaosMeshExecutor,
    ExecutionDenied,
    build_experimenter_api,
    read_configmap_exists,
    read_namespace_chaos_enabled,
)
from chaosagent.execute.scale import (
    AppliedScale,
    ScaleApiProtocol,
    ScaleExecutor,
    build_scale_api,
)

__all__ = [
    "PLURALS",
    "AppliedExperiment",
    "AppliedScale",
    "ChaosMeshExecutor",
    "ExecutionDenied",
    "ScaleApiProtocol",
    "ScaleExecutor",
    "build_experimenter_api",
    "build_scale_api",
    "read_configmap_exists",
    "read_namespace_chaos_enabled",
]
