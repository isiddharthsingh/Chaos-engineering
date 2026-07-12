"""Minimal CLI: register/list targets and run the pre-flight policy check.

Enough to exercise the guardrail spine from a shell without a cluster. The
richer agent-driven flows arrive with the harness in later phases.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from chaosagent.config import load_policy_config
from chaosagent.domain.actions import ProposedAction
from chaosagent.domain.enums import EnvironmentTier
from chaosagent.domain.targets import Target
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

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
