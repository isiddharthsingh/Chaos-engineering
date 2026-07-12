"""TargetRegistry behaviour and persistence."""

from __future__ import annotations

from pathlib import Path

import pytest

from chaosagent.domain.enums import EnvironmentTier, TargetKind
from chaosagent.domain.targets import CredentialRef, Target
from chaosagent.registry import DuplicateTargetError, TargetNotFoundError, TargetRegistry

CRED = CredentialRef(service_account="agent-experimenter")


def _target(tid: str, env: EnvironmentTier, namespaces: list[str] | None = None) -> Target:
    return Target(
        id=tid,
        name=tid,
        kind=TargetKind.KUBERNETES,
        environment=env,
        allowed_namespaces=["payments"] if namespaces is None else namespaces,
        credential=CRED,
    )


def test_register_and_get() -> None:
    reg = TargetRegistry()
    reg.register(_target("a", EnvironmentTier.DEV))
    assert reg.get("a").id == "a"
    assert "a" in reg
    assert len(reg) == 1


def test_duplicate_rejected_unless_overwrite() -> None:
    reg = TargetRegistry()
    reg.register(_target("a", EnvironmentTier.DEV))
    with pytest.raises(DuplicateTargetError):
        reg.register(_target("a", EnvironmentTier.STAGING))
    reg.register(_target("a", EnvironmentTier.STAGING), overwrite=True)
    assert reg.get("a").environment is EnvironmentTier.STAGING


def test_get_missing_raises() -> None:
    with pytest.raises(TargetNotFoundError):
        TargetRegistry().get("nope")


def test_remove() -> None:
    reg = TargetRegistry()
    reg.register(_target("a", EnvironmentTier.DEV))
    reg.remove("a")
    assert "a" not in reg
    with pytest.raises(TargetNotFoundError):
        reg.remove("a")


def test_list_filters() -> None:
    reg = TargetRegistry()
    reg.register(_target("dev1", EnvironmentTier.DEV))
    reg.register(_target("prod1", EnvironmentTier.PROD))
    reg.register(_target("unscoped", EnvironmentTier.DEV, namespaces=[]))

    assert [t.id for t in reg.list(environment=EnvironmentTier.DEV)] == ["dev1", "unscoped"]
    assert [t.id for t in reg.list(chaos_capable=True)] == ["dev1"]
    assert {t.id for t in reg.list(chaos_capable=False)} == {"prod1", "unscoped"}


def test_persistence_round_trip(tmp_path: Path) -> None:
    store = tmp_path / "targets.json"
    reg = TargetRegistry(path=store)
    reg.register(_target("a", EnvironmentTier.DEV))
    reg.register(_target("b", EnvironmentTier.STAGING))

    reloaded = TargetRegistry(path=store)
    assert len(reloaded) == 2
    assert reloaded.get("b").environment is EnvironmentTier.STAGING
