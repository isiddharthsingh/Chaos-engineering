"""CLI: register/list targets, pre-flight policy checks, and autonomous runs.

`register`/`list`/`check` exercise the guardrail spine without a cluster;
`run` drives one experiment end to end (spec file or LLM intent) on the rig;
`recommend` prints a bounded, read-only capacity recommendation; `scale`
drives one replica change through the capacity lifecycle (verify or
auto-revert).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from chaosagent.capacity.runner import CapacitySettings, run_recommend, run_scale
from chaosagent.config import load_policy_config
from chaosagent.domain.actions import ProposedAction
from chaosagent.domain.enums import EnvironmentTier
from chaosagent.domain.targets import Target
from chaosagent.experiment.runner import RunSettings, run_experiment
from chaosagent.experiment.schedule import SuiteSettings, run_suite_command
from chaosagent.policy import PolicyEngine
from chaosagent.registry import TargetNotFoundError, TargetRegistry
from chaosagent.resolve import resolve_action

_DEFAULT_STORE = Path.home() / ".chaosagent" / "targets.json"


def _registry(args: argparse.Namespace) -> TargetRegistry:
    return TargetRegistry(path=args.store)


def _cmd_register(args: argparse.Namespace) -> int:
    target = Target.model_validate_json(Path(args.file).read_text())
    _registry(args).register(target, overwrite=args.overwrite)
    print(f"registered {target.id} ({target.kind.value}, {target.environment.value})")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    env = EnvironmentTier(args.env) if args.env else None
    for target in _registry(args).list(environment=env):
        flag = "chaos-capable" if target.is_chaos_capable else "observe-only"
        print(f"{target.id:20} {target.kind.value:14} {target.environment.value:8} {flag}")
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    action = ProposedAction.model_validate_json(Path(args.file).read_text())
    # Resolve environment/kind/scope from the registered target — never trust the
    # environment declared in the action file. An unregistered target cannot be
    # scoped, so it is refused.
    try:
        target = _registry(args).get(action.target_id)
    except TargetNotFoundError:
        reason = f"target {action.target_id!r} is not registered; cannot verify scope"
        print(json.dumps({"allowed": False, "reason": reason}, indent=2))
        return 2
    action = resolve_action(action, target)
    engine = PolicyEngine(config=load_policy_config(args.policy))
    decision = engine.evaluate(action)
    print(json.dumps({"allowed": decision.allowed, "reason": decision.reason()}, indent=2))
    return 0 if decision.allowed else 2


def _cmd_run(args: argparse.Namespace) -> int:
    settings = RunSettings(
        target_id=args.target,
        store=args.store,
        spec_file=args.spec,
        intent=args.intent,
        namespace=args.namespace,
        prometheus_url=args.prom_url,
        kubeconfig=args.kubeconfig,
        context=args.context,
        interval_seconds=args.interval,
        baseline_seconds=args.baseline,
        recovery_seconds=args.recovery,
        dry_run=args.dry_run,
        output=args.output,
        policy=args.policy,
    )
    return run_experiment(settings)


def _cmd_suite(args: argparse.Namespace) -> int:
    settings = SuiteSettings(
        target_id=args.target,
        spec_file=args.spec,
        store=args.store,
        prometheus_url=args.prom_url,
        kubeconfig=args.kubeconfig,
        context=args.context,
        continue_on_abort=args.continue_on_abort,
        output=args.output,
        policy=args.policy,
    )
    return run_suite_command(settings)


def _cmd_recommend(args: argparse.Namespace) -> int:
    settings = CapacitySettings(
        target_id=args.target,
        store=args.store,
        namespace=args.namespace,
        workload=args.workload,
        target_utilization=args.target_utilization,
        lookback_minutes=args.lookback,
        prometheus_url=args.prom_url,
        opencost_url=args.opencost_url,
        kubeconfig=args.kubeconfig,
        context=args.context,
        output=args.output,
        policy=args.policy,
    )
    return run_recommend(settings)


def _cmd_scale(args: argparse.Namespace) -> int:
    settings = CapacitySettings(
        target_id=args.target,
        store=args.store,
        spec_file=args.spec,
        prometheus_url=args.prom_url,
        kubeconfig=args.kubeconfig,
        context=args.context,
        dry_run=args.dry_run,
        output=args.output,
        policy=args.policy,
    )
    return run_scale(settings)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chaosagent", description=__doc__)
    parser.add_argument("--store", type=Path, default=_DEFAULT_STORE, help="target store path")
    sub = parser.add_subparsers(dest="command", required=True)

    reg = sub.add_parser("register", help="register a target from a JSON file")
    reg.add_argument("file", help="path to a Target JSON document")
    reg.add_argument("--overwrite", action="store_true")
    reg.set_defaults(func=_cmd_register)

    lst = sub.add_parser("list", help="list registered targets")
    lst.add_argument("--env", choices=[e.value for e in EnvironmentTier])
    lst.set_defaults(func=_cmd_list)

    chk = sub.add_parser("check", help="run the pre-flight policy check on an action")
    chk.add_argument("file", help="path to a ProposedAction JSON document")
    chk.add_argument("--policy", type=Path, default=None, help="policy config YAML")
    chk.set_defaults(func=_cmd_check)

    run = sub.add_parser(
        "run",
        help="run one autonomous chaos experiment (plan -> inject -> observe -> report)",
    )
    run.add_argument("--target", required=True, help="registered target id")
    source = run.add_mutually_exclusive_group(required=True)
    source.add_argument("--spec", type=Path, help="path to an ExperimentSpec JSON document")
    source.add_argument("--intent", help="natural-language intent (needs the agent extra)")
    run.add_argument("--namespace", help="namespace for --intent planning")
    run.add_argument("--prom-url", help="Prometheus base URL (or CHAOSAGENT_PROMETHEUS_URL)")
    run.add_argument("--kubeconfig", help="kubeconfig path (default: standard discovery)")
    run.add_argument("--context", help="kubeconfig context")
    run.add_argument("--interval", type=float, help="observe interval seconds override")
    run.add_argument("--baseline", type=int, help="baseline window seconds override")
    run.add_argument("--recovery", type=int, help="recovery window seconds override")
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="stop after pre-flight (engine + server-side dry-run); inject nothing",
    )
    run.add_argument("--output", type=Path, help="write the report JSON here")
    run.add_argument("--policy", type=Path, default=None, help="policy config YAML")
    run.set_defaults(func=_cmd_run)

    suite = sub.add_parser(
        "suite",
        help="run a GameDay suite (ordered experiments, sequential, stop on abort)",
    )
    suite.add_argument("--target", required=True, help="registered target id")
    suite.add_argument("--spec", type=Path, required=True, help="path to a SuiteSpec JSON document")
    suite.add_argument(
        "--continue-on-abort",
        action="store_true",
        help="keep running after an aborted or errored experiment (default: stop)",
    )
    suite.add_argument("--prom-url", help="Prometheus base URL (or CHAOSAGENT_PROMETHEUS_URL)")
    suite.add_argument("--kubeconfig", help="kubeconfig path (default: standard discovery)")
    suite.add_argument("--context", help="kubeconfig context")
    suite.add_argument("--output", type=Path, help="write the aggregate report JSON here")
    suite.add_argument("--policy", type=Path, default=None, help="policy config YAML")
    suite.set_defaults(func=_cmd_suite)

    rec = sub.add_parser(
        "recommend",
        help="recommend a bounded replica change from utilization signals (read-only)",
    )
    rec.add_argument("--target", required=True, help="registered target id")
    rec.add_argument("--namespace", required=True, help="namespace of the workload")
    rec.add_argument(
        "--workload", required=True, help="deployment/<name> or statefulset/<name>"
    )
    rec.add_argument(
        "--target-utilization",
        type=float,
        default=0.6,
        help="utilization the sizing aims for (default: 0.6)",
    )
    rec.add_argument(
        "--lookback", type=int, default=60, help="signal lookback window in minutes"
    )
    rec.add_argument("--prom-url", help="Prometheus base URL (or CHAOSAGENT_PROMETHEUS_URL)")
    rec.add_argument(
        "--opencost-url",
        help="OpenCost base URL (or CHAOSAGENT_OPENCOST_URL); enables the cost signal",
    )
    rec.add_argument("--kubeconfig", help="kubeconfig path for the VPA signal read")
    rec.add_argument("--context", help="kubeconfig context for the VPA signal read")
    rec.add_argument("--output", type=Path, help="write the recommendation JSON here")
    rec.add_argument("--policy", type=Path, default=None, help="policy config YAML")
    rec.set_defaults(func=_cmd_recommend)

    scale = sub.add_parser(
        "scale",
        help="apply one bounded replica change (baseline -> apply -> verify or auto-revert)",
    )
    scale.add_argument("--target", required=True, help="registered target id")
    scale.add_argument(
        "--spec", type=Path, required=True, help="path to a CapacitySpec JSON document"
    )
    scale.add_argument("--prom-url", help="Prometheus base URL (or CHAOSAGENT_PROMETHEUS_URL)")
    scale.add_argument("--kubeconfig", help="kubeconfig path (default: standard discovery)")
    scale.add_argument("--context", help="kubeconfig context")
    scale.add_argument(
        "--dry-run",
        action="store_true",
        help="stop after pre-flight (engine + server-side dry-run); change nothing",
    )
    scale.add_argument("--output", type=Path, help="write the report JSON here")
    scale.add_argument("--policy", type=Path, default=None, help="policy config YAML")
    scale.set_defaults(func=_cmd_scale)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
