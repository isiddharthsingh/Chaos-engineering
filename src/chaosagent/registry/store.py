"""In-memory target registry with optional JSON file persistence.

Deliberately simple for Phase 0 — a dict keyed by target id, optionally mirrored
to a JSON file. A database-backed store can implement the same small surface
later without changing callers.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import TypeAdapter

from chaosagent.domain.enums import EnvironmentTier
from chaosagent.domain.targets import Target

_TARGET_LIST = TypeAdapter(list[Target])


class TargetNotFoundError(KeyError):
    """Raised when a target id is not registered."""


class DuplicateTargetError(ValueError):
    """Raised when registering an id that already exists."""


class TargetRegistry:
    """Holds the inventory of targets agents may act against."""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path is not None else None
        self._targets: dict[str, Target] = {}
        if self._path is not None and self._path.exists():
            self._load()

    def register(self, target: Target, *, overwrite: bool = False) -> Target:
        """Add a target. Raises on a duplicate id unless ``overwrite`` is set."""
        if target.id in self._targets and not overwrite:
            raise DuplicateTargetError(f"target id {target.id!r} already registered")
        self._targets[target.id] = target
        self._flush()
        return target

    def get(self, target_id: str) -> Target:
        try:
            return self._targets[target_id]
        except KeyError as exc:
            raise TargetNotFoundError(target_id) from exc

    def remove(self, target_id: str) -> None:
        if target_id not in self._targets:
            raise TargetNotFoundError(target_id)
        del self._targets[target_id]
        self._flush()

    def list(
        self,
        *,
        environment: EnvironmentTier | None = None,
        chaos_capable: bool | None = None,
    ) -> list[Target]:
        """List targets, optionally filtered by tier or chaos capability."""
        items = list(self._targets.values())
        if environment is not None:
            items = [t for t in items if t.environment is environment]
        if chaos_capable is not None:
            items = [t for t in items if t.is_chaos_capable == chaos_capable]
        return sorted(items, key=lambda t: t.id)

    def __len__(self) -> int:
        return len(self._targets)

    def __contains__(self, target_id: object) -> bool:
        return target_id in self._targets

    # -- persistence -----------------------------------------------------------

    def _flush(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = _TARGET_LIST.dump_json(self.list(), indent=2)
        self._path.write_bytes(payload)

    def _load(self) -> None:
        assert self._path is not None
        raw = self._path.read_bytes()
        if not raw.strip():
            return
        for target in _TARGET_LIST.validate_json(raw):
            self._targets[target.id] = target
