"""Policy-config loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from chaosagent.config import DEFAULT_POLICY_PATH, load_policy_config


def test_shipped_config_loads() -> None:
    config = load_policy_config()
    assert config.max_replica_pct_change == 0.5
    assert config.max_concurrent_experiments == 1
    assert config.max_ttl_seconds == 3600


def test_shipped_config_file_exists() -> None:
    assert DEFAULT_POLICY_PATH.exists(), "config/policies/engine.yaml must ship"


def test_missing_file_uses_defaults(tmp_path: Path) -> None:
    config = load_policy_config(tmp_path / "absent.yaml")
    assert config.max_ttl_seconds == 3600


def test_malformed_config_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("- just\n- a\n- list\n")
    with pytest.raises(ValueError, match="must be a mapping"):
        load_policy_config(bad)


@pytest.mark.parametrize("content", ["[]", "false", "0", "'a string'"])
def test_falsy_non_mapping_raises(tmp_path: Path, content: str) -> None:
    # A falsy-but-non-mapping value must NOT be silently coerced to defaults.
    bad = tmp_path / "bad.yaml"
    bad.write_text(content + "\n")
    with pytest.raises(ValueError, match="must be a mapping"):
        load_policy_config(bad)


def test_empty_file_uses_defaults(tmp_path: Path) -> None:
    empty = tmp_path / "empty.yaml"
    empty.write_text("\n")
    assert load_policy_config(empty).max_replica_pct_change == 0.5


def test_override_from_file(tmp_path: Path) -> None:
    override = tmp_path / "engine.yaml"
    override.write_text("max_replica_pct_change: 0.25\nmax_ttl_seconds: 1800\n")
    config = load_policy_config(override)
    assert config.max_replica_pct_change == 0.25
