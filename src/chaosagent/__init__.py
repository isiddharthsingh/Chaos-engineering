"""chaosagent — AI agents for chaos engineering, chaos testing, and capacity planning.

The package is layered defense-in-depth (see docs/architecture.md):

    deterministic fault engines
      -> policy-engine pre-flight check   (chaosagent.policy)
        -> RBAC / environment scoping     (config/rbac, chaosagent.domain)
          -> observability auto-abort     (chaosagent.observe)
            -> LLM judgement              (chaosagent.agents)  <- innermost, never alone

Phase 0 ships the guardrail spine: the target registry, the deterministic
pre-flight policy engine, and the config bundles (Kyverno + RBAC) that enforce
the same rules server-side.
"""

__version__ = "0.1.0"
