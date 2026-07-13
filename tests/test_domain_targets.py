"""Target and CredentialRef validation."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from chaosagent.domain.enums import EnvironmentTier, TargetKind
from chaosagent.domain.targets import CredentialRef, Target

OBSERVER = CredentialRef(service_account="agent-observer")
EXPERIMENTER = CredentialRef(service_account="agent-experimenter")


def _k8s(env: EnvironmentTier, namespaces: list[str]) -> Target:
    return Target(
        id="cluster-a",
        name="Cluster A",
        kind=TargetKind.KUBERNETES,
        environment=env,
        allowed_namespaces=namespaces,
        credential=EXPERIMENTER,
    )


def test_slug_must_be_dns_label() -> None:
    with pytest.raises(ValidationError):
        Target(
            id="Not A Slug",
            name="x",
            kind=TargetKind.KUBERNETES,
            environment=EnvironmentTier.DEV,
            credential=OBSERVER,
        )


def test_namespaces_must_be_valid_labels() -> None:
    with pytest.raises(ValidationError):
        _k8s(EnvironmentTier.DEV, ["Bad NS"])


@pytest.mark.parametrize("value", ["payments\n", "web\n"])
def test_trailing_newline_rejected(value: str) -> None:
    # \Z anchoring must reject a trailing newline that `$` would have allowed.
    with pytest.raises(ValidationError):
        _k8s(EnvironmentTier.DEV, [value])
    with pytest.raises(ValidationError):
        Target(
            id=value,
            name="x",
            kind=TargetKind.KUBERNETES,
            environment=EnvironmentTier.DEV,
            credential=OBSERVER,
        )


def test_credential_rejects_inline_access_key() -> None:
    with pytest.raises(ValidationError):
        CredentialRef(service_account="x", iam_role_arn="AKIAEXAMPLEKEY")


def test_dev_k8s_with_scope_is_chaos_capable() -> None:
    assert _k8s(EnvironmentTier.DEV, ["payments"]).is_chaos_capable is True


def test_k8s_without_namespace_scope_is_not_chaos_capable() -> None:
    assert _k8s(EnvironmentTier.DEV, []).is_chaos_capable is False


def test_prod_target_is_never_chaos_capable() -> None:
    assert _k8s(EnvironmentTier.PROD, ["payments"]).is_chaos_capable is False


def test_environment_autonomy_boundary() -> None:
    assert EnvironmentTier.DEV.is_autonomous
    assert EnvironmentTier.STAGING.is_autonomous
    assert not EnvironmentTier.PROD.is_autonomous


def test_example_eks_target_is_valid() -> None:
    path = Path(__file__).resolve().parents[1] / "examples" / "target-eks-staging.json"
    target = Target.model_validate_json(path.read_text())
    assert target.kind is TargetKind.KUBERNETES
    assert target.environment is EnvironmentTier.STAGING
    assert target.environment.is_autonomous  # staging sits inside the autonomy boundary
    assert target.provider == "eks"
    # Cloud credentials are scoped per-pod via IRSA — a role ARN reference, never a key.
    assert target.credential.iam_role_arn
    assert target.credential.service_account == "agent-experimenter"
    assert target.allowed_namespaces  # scoped to named namespaces, never cluster-wide
