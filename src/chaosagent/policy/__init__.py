"""Deterministic pre-flight policy engine.

This is the machine-speed "second signer" that replaces the human approval gate
on autonomous runs. It evaluates a :class:`ProposedAction` against the tunable
:class:`PolicyConfig` and returns a :class:`PolicyDecision`. The rule ids here
are identical to the Kyverno ClusterPolicy names in ``config/policies`` so that
a denial caught in Python pre-flight matches what the cluster would enforce.
"""

from chaosagent.policy.engine import PolicyEngine

__all__ = ["PolicyEngine"]
