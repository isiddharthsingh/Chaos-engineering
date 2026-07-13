"""The Planner agent — LLM in, typed intent out, nothing else.

The planner runs on the same read-only MCP stack as the observer (OBSERVE-mode
gate), so it can inspect workloads and metrics while designing an experiment but
cannot change state. Its only output that matters is one fenced JSON block that
validates as an :class:`ExperimentSpec`; everything destructive still goes
through resolve -> PolicyEngine -> server-side dry-run -> executor downstream.

``claude_agent_sdk`` is optional (the ``agent`` extra) and imported lazily —
``extract_experiment_spec`` and the prompt are SDK-free and testable without it.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, cast

from pydantic import ValidationError

from chaosagent.agents.mcp_config import McpEndpoints, build_mcp_servers
from chaosagent.agents.permission import PermissionGate, RunMode
from chaosagent.domain.targets import Target
from chaosagent.experiment.spec import ExperimentSpec

if TYPE_CHECKING:
    from claude_agent_sdk import ClaudeAgentOptions


class PlannerError(RuntimeError):
    """The planner did not produce a valid experiment spec."""


PLANNER_SYSTEM_PROMPT = f"""\
You are the Planner agent of a chaos-engineering platform. You design ONE
experiment for the target and namespace you are given, using READ-ONLY tools
(Kubernetes / Prometheus / Grafana) to inspect what runs there and what its
steady state looks like. You cannot change state, and you must not try.

Hard caps — a deterministic policy engine and the cluster's admission layer
both enforce these, so exceeding them only gets the plan denied:
  * fault.ratio <= 0.5 (blast radius: at most half the matched pods)
  * fault.duration_seconds <= 900 (the fault must self-revert within 15m)
  * ttl_seconds <= 3600 (bounded lifetime for the whole experiment)
  * the experiment lands ONLY in the namespace you are given

Supported fault_type values and the parameter block each requires (set exactly
the matching block; pod faults take none):
  * pod_kill, pod_failure — no block
  * container_kill — no block, but fault.container_names must name a container
  * network_latency / network_loss / network_partition — block `network` with
    action delay / loss / partition (delay requires latency_ms; loss requires
    loss_percent; direction must stay "to" — target-side shaping is not
    supported; container_names is not accepted for network faults)
  * cpu_stress / memory_stress — block `stress` (cpu_stress requires
    cpu_workers; memory_stress requires memory_workers)
  * io_stress — block `io` with action latency or fault (volume_path is always
    required; latency requires delay_ms, fault requires errno)
  * dns_chaos — block `dns` (action error or random; at least one entry in
    patterns)
  * time_skew — block `time` (time_offset such as "-10m")

Design guidance: pick a fault the workload should survive, and steady-state
hypotheses as PromQL threshold checks (e.g. available replicas >= 1, error
rate < 5%). Prefer conservative blast radii.

Output format: end your reply with exactly one fenced ```json code block
containing an ExperimentSpec object matching this JSON schema — no other JSON
blocks after it:

{json.dumps(ExperimentSpec.model_json_schema(), indent=2)}
"""

# Fenced code blocks, optionally tagged (```json ... ``` or ``` ... ```).
_FENCED_RE = re.compile(r"```[a-zA-Z]*\s*\n(.*?)```", re.DOTALL)

_DISALLOWED_BUILTINS = ["Bash", "Write", "Edit", "NotebookEdit", "WebFetch"]


def extract_experiment_spec(text: str) -> ExperimentSpec:
    """Pull the experiment spec out of the planner's reply (pure, SDK-free).

    Only the *last* fenced block is the answer (the prompt requires the reply to
    end with exactly one). If it does not validate we raise so the repair turn
    fires — reaching back to an earlier, superseded draft would silently execute
    a spec the model chose to retract.
    """
    blocks = _FENCED_RE.findall(text)
    if not blocks:
        raise PlannerError("planner reply contains no fenced JSON block")
    try:
        return ExperimentSpec.model_validate_json(blocks[-1].strip())
    except ValidationError as exc:
        raise PlannerError(
            f"the final fenced block is not a valid ExperimentSpec: {exc}"
        ) from exc


def _check_scope(spec: ExperimentSpec, target: Target, namespace: str) -> None:
    """The spec must be for the target/namespace the planner was given — a
    mismatch is repairable, and downstream policy would refuse it anyway."""
    if spec.target_id != target.id:
        raise PlannerError(
            f"spec names target {spec.target_id!r} but the planner was given {target.id!r}"
        )
    if spec.namespace != namespace:
        raise PlannerError(
            f"spec lands in namespace {spec.namespace!r} but the planner was "
            f"confined to {namespace!r}"
        )


class PlannerHarness:
    """Turns natural-language intent into a validated ExperimentSpec."""

    def __init__(
        self,
        endpoints: McpEndpoints | None = None,
        *,
        model: str = "claude-opus-4-8",
        max_repair_turns: int = 1,
    ) -> None:
        self.endpoints = endpoints or McpEndpoints.from_env()
        self.model = model
        self.max_repair_turns = max_repair_turns
        self.gate = PermissionGate(mode=RunMode.OBSERVE)

    def build_options(self) -> ClaudeAgentOptions:
        from claude_agent_sdk import ClaudeAgentOptions

        from chaosagent.agents.harness import build_can_use_tool

        mcp_servers = cast(Any, build_mcp_servers(self.endpoints, read_only=True))
        return ClaudeAgentOptions(
            model=self.model,
            system_prompt=PLANNER_SYSTEM_PROMPT,
            mcp_servers=mcp_servers,
            can_use_tool=build_can_use_tool(self.gate),
            disallowed_tools=_DISALLOWED_BUILTINS,
            permission_mode="default",
        )

    async def plan(self, intent: str, *, target: Target, namespace: str) -> ExperimentSpec:
        """One planning turn plus up to ``max_repair_turns`` validation repairs."""
        prompt = (
            f"Design one chaos experiment.\n"
            f"Intent: {intent}\n"
            f"Target id: {target.id} ({target.provider}, {target.environment.value})\n"
            f"Namespace: {namespace}\n"
        )
        reply = await self._query(prompt)
        for turn in range(self.max_repair_turns + 1):
            try:
                spec = extract_experiment_spec(reply)
                _check_scope(spec, target, namespace)
                return spec
            except PlannerError as exc:
                if turn >= self.max_repair_turns:
                    raise
                reply = await self._query(
                    f"{prompt}\nYour previous reply was:\n{reply}\n\n"
                    f"It was rejected: {exc}\n"
                    "Reply again with exactly one corrected fenced ```json block."
                )
        raise PlannerError("unreachable")  # pragma: no cover

    async def _query(self, prompt: str) -> str:
        """One SDK turn; streaming input is required when can_use_tool is set."""
        from claude_agent_sdk import AssistantMessage, TextBlock, query

        async def _stream() -> AsyncIterator[dict[str, Any]]:
            yield {"type": "user", "message": {"role": "user", "content": prompt}}

        chunks: list[str] = []
        async for message in query(prompt=_stream(), options=self.build_options()):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        chunks.append(block.text)
        return "\n".join(chunks)
