"""GameDay suite runner — an ordered list of experiments, strictly sequential.

Experiments never run concurrently (the ``single-experiment`` policy rule and
the single-slot permission gate both forbid it), so a suite is a plain loop.
An abort — or an operational error (exit 1), where the system's health is
UNKNOWN (e.g. metrics went dark with a fault live) — stops the suite by
default: continuing to fault an unhealthy or unobservable system is a
GameDay-operator decision (``--continue-on-abort``), not a default. A policy
denial (exit 2) does not stop the suite: nothing was injected.

Exit codes reuse the run mapping (0 verified / 2 denied / 3 aborted / 1 error);
the suite exits with the WORST run's code, ranked ``1 > 2 > 3 > 0``. Harness
breakage outranks a policy denial outranks an abort — an abort is the safety
loop *working*, while 1 and 2 mean the suite could not do its job.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from chaosagent.analyze import ExperimentReport, build_report, render_text
from chaosagent.experiment.lifecycle import ExperimentRun, LifecycleDeps, run_lifecycle
from chaosagent.experiment.runner import (
    RunnerDeps,
    RunSettings,
    build_live_deps,
    exit_code,
)
from chaosagent.experiment.spec import ExperimentSpec
from chaosagent.observe import PrometheusClient
from chaosagent.registry import TargetRegistry


class SuiteSpec(BaseModel):
    """A GameDay: ordered experiments, run one at a time."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(min_length=1)
    experiments: tuple[ExperimentSpec, ...] = Field(min_length=1)


class SuiteReport(BaseModel):
    """Aggregate report for ``--output`` — one entry per executed run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str
    stopped_early: bool
    exit_code: int
    reports: tuple[ExperimentReport, ...]


@dataclass
class SuiteOutcome:
    """What a suite run produced, in execution order."""

    runs: list[ExperimentRun]
    reports: list[ExperimentReport]
    stopped_early: bool
    exit_code: int


#: Worst-first ranking of run exit codes (see module docstring).
_EXIT_PRECEDENCE = (1, 2, 3, 0)


def run_suite(
    suite: SuiteSpec, deps: LifecycleDeps, *, continue_on_abort: bool = False
) -> SuiteOutcome:
    """Run the suite's experiments sequentially through the lifecycle."""
    runs: list[ExperimentRun] = []
    reports: list[ExperimentReport] = []
    stopped_early = False
    for index, spec in enumerate(suite.experiments):
        run = run_lifecycle(spec, deps)
        runs.append(run)
        reports.append(build_report(run))
        # Stop on an abort (SLO breached) or an operational error (health
        # unverified with a fault having been live); a denial injected nothing.
        unsafe_to_continue = run.aborted or exit_code(run) == 1
        if unsafe_to_continue and not continue_on_abort and index + 1 < len(suite.experiments):
            stopped_early = True
            break
    codes = {exit_code(run) for run in runs}
    worst = next((code for code in _EXIT_PRECEDENCE if code in codes), 0)
    return SuiteOutcome(
        runs=runs, reports=reports, stopped_early=stopped_early, exit_code=worst
    )


@dataclass
class SuiteSettings:
    """Everything `chaosagent suite` collected from the command line."""

    target_id: str
    spec_file: Path
    store: Path | None = None
    prometheus_url: str | None = None
    kubeconfig: str | None = None
    context: str | None = None
    continue_on_abort: bool = False
    output: Path | None = None
    policy: Path | None = None


def _error(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)


def run_suite_command(settings: SuiteSettings, deps: RunnerDeps | None = None) -> int:
    """Run a GameDay suite end to end and return the process exit code."""
    try:
        suite = SuiteSpec.model_validate_json(settings.spec_file.read_text())
    except (OSError, ValidationError) as exc:
        _error(f"could not load suite {settings.spec_file}: {exc}")
        return 1
    for spec in suite.experiments:
        if spec.target_id != settings.target_id:
            _error(
                f"experiment {spec.title!r} targets {spec.target_id!r} but "
                f"--target is {settings.target_id!r}"
            )
            return 1

    if deps is not None:
        outcome = run_suite(suite, deps.lifecycle, continue_on_abort=settings.continue_on_abort)
    else:
        run_settings = RunSettings(
            target_id=settings.target_id,
            store=settings.store,
            prometheus_url=settings.prometheus_url,
            kubeconfig=settings.kubeconfig,
            context=settings.context,
            policy=settings.policy,
        )
        lifecycle = build_live_deps(run_settings, TargetRegistry(settings.store))
        if isinstance(lifecycle, int):
            return lifecycle
        metrics = lifecycle.metrics
        try:
            outcome = run_suite(
                suite, lifecycle, continue_on_abort=settings.continue_on_abort
            )
        finally:
            if isinstance(metrics, PrometheusClient):
                metrics.close()

    total = len(suite.experiments)
    for index, report in enumerate(outcome.reports, start=1):
        print(f"== run {index}/{total}: {report.title} ==")
        print(render_text(report))
    if outcome.stopped_early:
        print(
            f"suite stopped after run {len(outcome.runs)}/{total} aborted "
            "(pass --continue-on-abort to keep going)"
        )
    print(f"suite      : {suite.title} -> exit {outcome.exit_code}")
    if settings.output is not None:
        suite_report = SuiteReport(
            title=suite.title,
            stopped_early=outcome.stopped_early,
            exit_code=outcome.exit_code,
            reports=tuple(outcome.reports),
        )
        settings.output.write_text(suite_report.model_dump_json(indent=2))
        print(f"report written to {settings.output}")
    return outcome.exit_code
