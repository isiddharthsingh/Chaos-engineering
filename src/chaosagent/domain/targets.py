"""Target inventory models — clusters, cloud accounts, and VM groups."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

from chaosagent.domain.enums import EnvironmentTier, TargetKind

# \Z (not $) anchors the true end of string; $ would also match just before a
# trailing newline, letting "payments\n" through as a valid DNS label.
_SLUG_RE = re.compile(r"\A[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\Z")


class CredentialRef(BaseModel):
    """A *reference* to a credential — never the secret itself.

    Points at where the executor should obtain scoped access at run time
    (a Kubernetes ServiceAccount, an IRSA role ARN, a secret-store path). Keeping
    only references in the registry means the inventory can be persisted to disk
    or a database without ever holding a live secret.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Capability tier this credential grants, e.g. "agent-observer" or
    #: "agent-experimenter". Mirrors the tiered ServiceAccounts in config/rbac.
    service_account: str = Field(min_length=1)
    #: Optional cloud IAM role assumed per-pod (IRSA / Workload Identity).
    iam_role_arn: str | None = None
    #: Optional path in an external secret store; resolved at execution time.
    secret_store_path: str | None = None

    @field_validator("iam_role_arn")
    @classmethod
    def _no_inline_secret(cls, value: str | None) -> str | None:
        # Cheap guard against someone stuffing an actual key into the ARN slot.
        if value and value.lower().startswith(("akia", "asia")):
            raise ValueError("iam_role_arn looks like an access key, not a role ARN")
        return value


class Target(BaseModel):
    """A registered piece of infrastructure an agent may act against.

    The environment tier and scope labels on the target are the first hard
    boundary: the policy engine reads them before any action is allowed.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Stable slug, DNS-label form.")
    name: str = Field(min_length=1)
    kind: TargetKind
    environment: EnvironmentTier
    provider: str = Field(
        default="unknown",
        description="eks | aks | gke | kind | onprem | aws | gcp | azure",
    )
    #: For Kubernetes targets, the namespaces the agent is scoped to. Empty means
    #: "not yet scoped" — chaos actions are refused until a scope is set.
    allowed_namespaces: list[str] = Field(default_factory=list)
    #: Free-form labels used by policy/scoping (e.g. team, cost-center).
    labels: dict[str, str] = Field(default_factory=dict)
    credential: CredentialRef

    @field_validator("id")
    @classmethod
    def _valid_slug(cls, value: str) -> str:
        if not _SLUG_RE.match(value):
            raise ValueError(
                f"target id {value!r} must be a DNS label (lowercase alphanumeric and '-')"
            )
        return value

    @field_validator("allowed_namespaces")
    @classmethod
    def _valid_namespaces(cls, value: list[str]) -> list[str]:
        for ns in value:
            if not _SLUG_RE.match(ns):
                raise ValueError(f"namespace {ns!r} is not a valid DNS label")
        return value

    @property
    def is_chaos_capable(self) -> bool:
        """Whether this target may host destructive experiments at all.

        prod is never chaos-capable through an autonomous credential; a scoped,
        time-boxed human escalation is required (Phase 4). A K8s target with no
        namespace scope is also not chaos-capable yet.
        """
        if not self.environment.is_autonomous:
            return False
        unscoped_k8s = self.kind is TargetKind.KUBERNETES and not self.allowed_namespaces
        return not unscoped_k8s
