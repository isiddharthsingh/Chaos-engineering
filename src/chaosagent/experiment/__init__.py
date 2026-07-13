"""The experiment lifecycle — one run, one deterministic state machine.

PLAN -> PREFLIGHT -> BASELINE -> INJECT -> OBSERVE -> VERIFY/ABORT -> ROLLBACK
-> REPORT -> DONE | FAILED. The whole machine is synchronous and sits beneath
the LLM: the planner may author the spec, but nothing here consults a model.
"""

from chaosagent.experiment.lifecycle import (
    ExperimentRun,
    ExperimentState,
    LifecycleDeps,
    StateTransition,
    run_lifecycle,
)
from chaosagent.experiment.spec import ExperimentSpec

__all__ = [
    "ExperimentRun",
    "ExperimentSpec",
    "ExperimentState",
    "LifecycleDeps",
    "StateTransition",
    "run_lifecycle",
]
