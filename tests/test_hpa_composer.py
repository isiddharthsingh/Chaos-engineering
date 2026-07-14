"""compose_hpa_patch and the replica-cap extension to HPA bound changes."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from chaosagent.capacity.hpa import compose_hpa_patch
from chaosagent.domain.actions import HpaBoundsChange, ProposedAction, ReplicaChange
from chaosagent.domain.enums import ActionType, EnvironmentTier
from chaosagent.policy import PolicyEngine


def _bounds(
    min_from: int = 4, min_to: int = 4, max_from: int = 8, max_to: int = 8
) -> HpaBoundsChange:
    return HpaBoundsChange(
        min_replicas=ReplicaChange(current=min_from, desired=min_to),
        max_replicas=ReplicaChange(current=max_from, desired=max_to),
    )


def _action(bounds: HpaBoundsChange) -> ProposedAction:
    return ProposedAction(
        action_type=ActionType.RIGHT_SIZE,
        target_id="cluster-a",
        environment=EnvironmentTier.DEV,
        namespace="boutique",
        hpa_bounds=bounds,
    )


def _rules(action: ProposedAction) -> set[str]:
    return {v.rule for v in PolicyEngine().evaluate(action).violations}


# -- the pure patch builder -----------------------------------------------------


def test_patch_carries_both_bounds_and_addressing() -> None:
    patch = compose_hpa_patch("cartservice", "boutique", min_replicas=2, max_replicas=8)
    assert patch == {
        "apiVersion": "autoscaling/v2",
        "kind": "HorizontalPodAutoscaler",
        "metadata": {"name": "cartservice", "namespace": "boutique"},
        "spec": {"minReplicas": 2, "maxReplicas": 8},
    }


def test_min_below_one_is_refused() -> None:
    with pytest.raises(ValueError, match="min_replicas"):
        compose_hpa_patch("cartservice", "boutique", min_replicas=0, max_replicas=8)


def test_max_below_min_is_refused() -> None:
    with pytest.raises(ValueError, match="max_replicas"):
        compose_hpa_patch("cartservice", "boutique", min_replicas=4, max_replicas=3)


@pytest.mark.parametrize("name", ["CartService", "cart_service", ""])
def test_invalid_names_are_refused(name: str) -> None:
    with pytest.raises(ValueError, match="DNS label"):
        compose_hpa_patch(name, "boutique", min_replicas=1, max_replicas=2)


# -- the engine judges HPA bounds under replica-cap ------------------------------


def test_hpa_bounds_within_cap_are_allowed() -> None:
    action = _action(_bounds(min_to=5, max_to=12))  # +25% and +50%
    assert PolicyEngine().evaluate(action).allowed is True


def test_min_bound_over_cap_is_denied() -> None:
    decision = PolicyEngine().evaluate(_action(_bounds(min_to=9)))  # 4 -> 9 = +125%
    assert not decision.allowed
    assert "replica-cap" in {v.rule for v in decision.violations}
    assert "minReplicas" in decision.reason()


def test_max_bound_over_cap_is_denied() -> None:
    decision = PolicyEngine().evaluate(_action(_bounds(max_to=20)))  # 8 -> 20 = +150%
    assert not decision.allowed
    assert "maxReplicas" in decision.reason()


def test_both_breaching_bounds_are_reported_together() -> None:
    decision = PolicyEngine().evaluate(_action(_bounds(min_to=9, max_to=20)))
    replica_cap = [v for v in decision.violations if v.rule == "replica-cap"]
    assert len(replica_cap) == 2


def test_hpa_bounds_shrink_with_inadmissible_revert_is_denied() -> None:
    # min 4->2 / max 8->4 are each -50% (inside the cap), but restoring them
    # (2->4 / 4->8) would be +100% — the revert-admissible guarantee must
    # cover HPA bounds exactly like direct scales.
    decision = PolicyEngine().evaluate(_action(_bounds(min_to=2, max_to=4)))
    assert not decision.allowed
    revert = [v for v in decision.violations if v.rule == "revert-admissible"]
    assert len(revert) == 2
    assert "minReplicas" in decision.reason() and "maxReplicas" in decision.reason()


def test_admitted_hpa_bounds_change_is_always_revertible() -> None:
    # The same property the safety gate proves for direct scales, over bounds.
    engine = PolicyEngine()
    for current in range(1, 15):
        for desired in range(1, 15):
            action = _action(
                _bounds(min_from=current, min_to=desired, max_from=current, max_to=desired)
            )
            if engine.evaluate(action).allowed:
                inverse = _action(
                    _bounds(
                        min_from=desired, min_to=current, max_from=desired, max_to=current
                    )
                )
                assert engine.evaluate(inverse).allowed, (current, desired)


def test_capacity_action_accepts_hpa_bounds_without_a_replica_change() -> None:
    action = _action(_bounds())
    assert action.replica_change is None and action.hpa_bounds is not None


def test_capacity_action_requires_some_change() -> None:
    with pytest.raises(ValidationError, match="replica_change or hpa_bounds"):
        ProposedAction(
            action_type=ActionType.RIGHT_SIZE,
            target_id="cluster-a",
            environment=EnvironmentTier.DEV,
            namespace="boutique",
        )
