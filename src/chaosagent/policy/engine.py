"""The deterministic policy engine and its individual rules."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from chaosagent.domain.actions import ProposedAction
from chaosagent.domain.enums import ActionType, TargetKind
from chaosagent.domain.policy import PolicyConfig, PolicyDecision, Violation

# Target kinds that have no namespace concept; namespace rules do not apply.
_NON_NAMESPACED_KINDS = (TargetKind.CLOUD_ACCOUNT, TargetKind.VM_GROUP)

# A rule maps (action, config) -> the violations it found (possibly none).
Rule = Callable[[ProposedAction, PolicyConfig], Iterable[Violation]]


def _env_scope(action: ProposedAction, config: PolicyConfig) -> Iterable[Violation]:
    """State-changing actions are refused against prod. prod is meant to be
    unreachable by credential; this is the belt to that suspenders. The freeze is
    unconditional — it is deliberately NOT a config knob, so no policy file can
    turn off the prod block."""
    if not action.action_type.is_state_changing:
        return
    if not action.environment.is_autonomous:
        yield Violation(
            rule="env-scope",
            message=(
                f"{action.action_type.value} denied against {action.environment.value} target "
                f"{action.target_id!r}; autonomous actions are confined to dev/staging"
            ),
        )


def _require_chaos_namespace(action: ProposedAction, config: PolicyConfig) -> Iterable[Violation]:
    """Disruptive chaos actions — fault injection AND load application — are only
    allowed in a namespace opted in via the ``chaos-enabled=true`` label."""
    if not action.action_type.is_chaos:
        return
    if not action.namespace_chaos_enabled:
        yield Violation(
            rule="require-chaos-namespace",
            message=(
                f"namespace {action.namespace!r} is not labelled chaos-enabled=true; "
                f"{action.action_type.value} refused"
            ),
        )


def _require_namespace_scope(action: ProposedAction, config: PolicyConfig) -> Iterable[Violation]:
    """Any state-changing Kubernetes action must name the namespace it lands in,
    so it can be scoped and audited. Non-namespaced targets (cloud accounts, VM
    groups) are exempt — they have no namespace."""
    if not action.action_type.is_state_changing:
        return
    if action.target_kind in _NON_NAMESPACED_KINDS:
        return
    if action.namespace is None:
        yield Violation(
            rule="require-namespace-scope",
            message=f"{action.action_type.value} must declare a target namespace",
        )


def _namespace_scope(action: ProposedAction, config: PolicyConfig) -> Iterable[Violation]:
    """A state-changing action must land inside the target's declared namespace
    scope. Empty scope means unresolved/unrestricted and is not enforced here —
    resolve the action from the registry (chaosagent.resolve) to populate it."""
    if not action.action_type.is_state_changing:
        return
    if action.namespace is None or not action.target_allowed_namespaces:
        return
    if action.namespace not in action.target_allowed_namespaces:
        yield Violation(
            rule="namespace-scope",
            message=(
                f"namespace {action.namespace!r} is outside the target's scope "
                f"{list(action.target_allowed_namespaces)}"
            ),
        )


def _replica_cap(action: ProposedAction, config: PolicyConfig) -> Iterable[Violation]:
    """Capacity actions may not move replica counts by more than the cap.
    Covers direct scales AND HPA bound changes — each bound is judged with the
    same math (the Kyverno twin for bounds is ``cap-hpa-bounds``)."""
    if action.action_type not in (ActionType.SCALE_WORKLOAD, ActionType.RIGHT_SIZE):
        return
    changes = [("replica", action.replica_change)]
    if action.hpa_bounds is not None:
        changes.append(("HPA minReplicas", action.hpa_bounds.min_replicas))
        changes.append(("HPA maxReplicas", action.hpa_bounds.max_replicas))
    for label, change in changes:
        if change is None:
            continue
        if abs(change.pct_change) > config.max_replica_pct_change:
            pct = (
                "unbounded" if change.pct_change == float("inf") else f"{change.pct_change:+.0%}"
            )
            yield Violation(
                rule="replica-cap",
                message=(
                    f"{label} change {change.current}->{change.desired} ({pct}) exceeds cap "
                    f"of +/-{config.max_replica_pct_change:.0%}"
                ),
            )


def _revert_admissible(action: ProposedAction, config: PolicyConfig) -> Iterable[Violation]:
    """An autonomous capacity change must be revertible under the same cap that
    admitted it: the inverse change (desired back to current) may not exceed
    ``max_replica_pct_change``. A -50% downscale (4->2) fits the cap, but its
    revert (2->4) is +100% — refused here so the deterministic auto-revert can
    never be blocked by our own guardrails. Judges direct scales AND each HPA
    bound, exactly like ``_replica_cap``. Engine-only, deliberately without a
    Kyverno twin: an admission twin would also constrain human operators, who
    can revert in two steps."""
    if action.action_type not in (ActionType.SCALE_WORKLOAD, ActionType.RIGHT_SIZE):
        return
    changes = [("replica", action.replica_change)]
    if action.hpa_bounds is not None:
        changes.append(("HPA minReplicas", action.hpa_bounds.min_replicas))
        changes.append(("HPA maxReplicas", action.hpa_bounds.max_replicas))
    for label, change in changes:
        # desired == 0 (scale-to-zero) is already denied by replica-cap: its
        # revert would be a scale *from* zero, which pct_change treats as
        # unbounded.
        if change is None or change.desired <= 0:
            continue
        inverse_pct = (change.current - change.desired) / change.desired
        if abs(inverse_pct) > config.max_replica_pct_change:
            yield Violation(
                rule="revert-admissible",
                message=(
                    f"{label} change {change.current}->{change.desired} is not "
                    f"autonomously revertible: the revert "
                    f"{change.desired}->{change.current} would be {inverse_pct:+.0%}, "
                    f"over the cap of +/-{config.max_replica_pct_change:.0%}"
                ),
            )


def _fault_duration_cap(action: ProposedAction, config: PolicyConfig) -> Iterable[Violation]:
    """A fault must self-revert within the configured window."""
    if action.fault is None:
        return
    if action.fault.duration_seconds > config.max_fault_duration_seconds:
        yield Violation(
            rule="fault-duration-cap",
            message=(
                f"fault duration {action.fault.duration_seconds}s exceeds cap of "
                f"{config.max_fault_duration_seconds}s"
            ),
        )


def _fault_blast_radius(action: ProposedAction, config: PolicyConfig) -> Iterable[Violation]:
    """A single fault may not exceed the configured blast-radius fraction."""
    if action.fault is None:
        return
    if action.fault.ratio > config.max_fault_ratio:
        yield Violation(
            rule="fault-blast-radius",
            message=(
                f"fault ratio {action.fault.ratio:.0%} exceeds blast-radius cap of "
                f"{config.max_fault_ratio:.0%}"
            ),
        )


def _require_ttl(action: ProposedAction, config: PolicyConfig) -> Iterable[Violation]:
    """Chaos and load actions must declare a bounded TTL under the ceiling."""
    if not action.action_type.is_chaos:
        return
    if action.ttl_seconds is None:
        yield Violation(
            rule="require-ttl",
            message=f"{action.action_type.value} must declare a bounded ttl_seconds",
        )
    elif action.ttl_seconds > config.max_ttl_seconds:
        yield Violation(
            rule="require-ttl",
            message=(
                f"ttl_seconds {action.ttl_seconds} exceeds ceiling of {config.max_ttl_seconds}"
            ),
        )


def _single_experiment(action: ProposedAction, config: PolicyConfig) -> Iterable[Violation]:
    """One experiment at a time per target keeps blast radii from compounding."""
    if not action.action_type.is_chaos:
        return
    if action.concurrent_experiments >= config.max_concurrent_experiments:
        yield Violation(
            rule="single-experiment",
            message=(
                f"{action.concurrent_experiments} experiment(s) already running; "
                f"limit is {config.max_concurrent_experiments}"
            ),
        )


def _incident_freeze(action: ProposedAction, config: PolicyConfig) -> Iterable[Violation]:
    """Never take a state-changing action while an incident/alert is firing —
    not chaos, and not a capacity change that could compound the incident."""
    if not action.action_type.is_state_changing:
        return
    if action.incident_active:
        yield Violation(
            rule="incident-freeze",
            message=(
                f"an incident/alert is firing for this target; {action.action_type.value} is frozen"
            ),
        )


#: Evaluated in order; every rule runs so the decision lists *all* reasons.
DEFAULT_RULES: tuple[Rule, ...] = (
    _env_scope,
    _require_namespace_scope,
    _namespace_scope,
    _require_chaos_namespace,
    _replica_cap,
    _fault_duration_cap,
    _fault_blast_radius,
    _require_ttl,
    _single_experiment,
    _incident_freeze,
    _revert_admissible,
)


class PolicyEngine:
    """Evaluates proposed actions against the policy bundle.

    Deterministic and side-effect free: given the same action and config it
    always returns the same decision. That property is what the release-gating
    autonomous-safety test relies on.
    """

    def __init__(
        self,
        config: PolicyConfig | None = None,
        rules: Iterable[Rule] | None = None,
    ) -> None:
        self.config = config or PolicyConfig()
        self._rules: tuple[Rule, ...] = tuple(rules) if rules is not None else DEFAULT_RULES

    def evaluate(self, action: ProposedAction) -> PolicyDecision:
        """Return the verdict, collecting every violation across all rules."""
        violations: list[Violation] = []
        for rule in self._rules:
            violations.extend(rule(action, self.config))
        if violations:
            return PolicyDecision.deny(violations)
        return PolicyDecision.allow()
