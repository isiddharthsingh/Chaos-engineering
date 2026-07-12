"""Domain models — the shared vocabulary every layer speaks.

Pure data + validation, no I/O. Enums, targets, proposed actions, and policy
decision types live here so the registry, policy engine, composers, and agents
all agree on the same typed contracts.
"""

from chaosagent.domain.actions import FaultSpec, ProposedAction, ReplicaChange
from chaosagent.domain.enums import ActionType, EnvironmentTier, FaultType, TargetKind
from chaosagent.domain.policy import PolicyConfig, PolicyDecision, Violation
from chaosagent.domain.targets import CredentialRef, Target

__all__ = [
    "ActionType",
    "CredentialRef",
    "EnvironmentTier",
    "FaultSpec",
    "FaultType",
    "PolicyConfig",
    "PolicyDecision",
    "ProposedAction",
    "ReplicaChange",
    "Target",
    "TargetKind",
    "Violation",
]
