"""Configuration loading.

The policy caps live in ``config/policies/engine.yaml`` as the single source of
truth. The Python engine loads them here; ``tests/test_manifests.py`` asserts the
Kyverno bundle embeds the same numbers, so the pre-flight and in-cluster
enforcement layers can never silently drift apart.

The same file is packaged into the wheel (pyproject ``force-include`` maps it to
``chaosagent/_data/engine.yaml``) so an installed, non-editable chaosagent loads
the shipped caps rather than silently falling back to code defaults.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from chaosagent.domain.policy import PolicyConfig

# Prefer the copy packaged inside the wheel; fall back to the repo-root source
# file when running from a source checkout (where force-include hasn't run).
_PACKAGED_POLICY_PATH = Path(__file__).resolve().parent / "_data" / "engine.yaml"
_REPO_POLICY_PATH = Path(__file__).resolve().parents[2] / "config" / "policies" / "engine.yaml"

#: Default location of the policy config (packaged copy if present, else source).
DEFAULT_POLICY_PATH = _PACKAGED_POLICY_PATH if _PACKAGED_POLICY_PATH.exists() else _REPO_POLICY_PATH


def load_policy_config(path: str | Path | None = None) -> PolicyConfig:
    """Load :class:`PolicyConfig` from YAML, falling back to shipped defaults.

    An empty file (or the absence of the default) yields the code defaults so the
    engine is always usable; a present-but-malformed file raises, because a broken
    guardrail config must never be silently ignored.
    """
    resolved = Path(path) if path is not None else DEFAULT_POLICY_PATH
    if not resolved.exists():
        return PolicyConfig()
    data = yaml.safe_load(resolved.read_text())
    if data is None:  # genuinely empty file -> use defaults
        return PolicyConfig()
    if not isinstance(data, dict):
        raise ValueError(f"policy config {resolved} must be a mapping, got {type(data).__name__}")
    return PolicyConfig.model_validate(data)
