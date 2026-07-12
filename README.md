# chaosagent

AI agents that plan, run, and verify **chaos-engineering experiments**, **stress/load
tests**, and **capacity-planning actions** against infrastructure targets
(Kubernetes clusters, cloud accounts, VMs) — fully autonomously on dev/staging,
and **safely**.

The platform does **not** reinvent fault injection. It parameterizes proven,
self-reverting engines (Chaos Mesh, LitmusChaos, k6) and adds the missing layer:
**LLM-planned experiments with steady-state hypothesis verification and
deterministic auto-abort.** The agent's judgement is the *innermost* safety
layer, never the only one.

## Defense in depth

Because dev/staging runs have **no human approval gate**, safety is enforced by
outward, machine-speed boundaries — all of which exist before the first
autonomous run:

```
 deterministic self-reverting fault engines
   -> policy-engine pre-flight check      (src/chaosagent/policy)
     -> RBAC / IAM least privilege         (config/rbac)
       -> environment scoping              dev/staging only; prod excluded by credential
         -> observability auto-abort       (Phase 1)
           -> LLM judgement                (src/chaosagent/agents)   <- innermost
```

Any single layer failing still bounds the blast radius. **prod is unreachable by
an autonomous credential** — not a prompt rule, a credential boundary.

## Layout

| Path | What |
|---|---|
| `src/chaosagent/domain` | Typed models: targets, actions, policy decisions |
| `src/chaosagent/registry` | Target inventory (env tiers, scope labels, credential refs) |
| `src/chaosagent/policy` | Deterministic pre-flight policy engine (the "second signer") |
| `src/chaosagent/agents` | MCP wiring, permission gate, Claude Agent SDK harness |
| `config/policies` | `engine.yaml` (source of truth) + Kyverno admission bundle |
| `config/rbac` | Tiered ServiceAccounts + least-privilege Roles |
| `scripts` | Local kind rig bring-up / teardown |
| `examples` | Sample target + action JSON documents |

## Quickstart

```bash
uv sync                     # core + dev deps
uv sync --extra agent       # add the Claude Agent SDK + k8s client (the "hands")

uv run pytest               # unit + safety-gate tests
uv run ruff check .         # lint
uv run mypy                 # types

# Register a target and run the pre-flight policy check on an action:
uv run chaosagent register examples/target-kind-local.json
uv run chaosagent list
uv run chaosagent check examples/action-denied-unlabelled-ns.json   # -> denied
uv run chaosagent check examples/action-allowed-poddelete.json      # -> allowed
```

## Safety gate (release-gating test)

`tests/test_safety_gate.py` asserts the invariants that make "fully autonomous"
defensible, against the **shipped** policy config:

- a fault **cannot** execute outside a `chaos-enabled=true` namespace,
- a capacity action **cannot** exceed the replica cap (±50%),
- **no** state-changing action can reach a prod target,
- the engine is **deterministic**.

## Status

- **Phase 0 — Foundations + guardrail spine — ✅ complete.** Registry, deterministic
  policy engine, Kyverno + RBAC bundle, read-only Claude Agent SDK harness.
- **Phase 1 — Autonomous chaos MVP on kind** — next: Chaos Mesh CRD composer,
  PromQL steady-state evaluator, observe loop + auto-abort, resilience scoring.
- Phases 2–4: fault library + k6 load + cloud K8s; capacity planning; multi-cloud
  + VMs + Temporal durability + prod escalation.

See [`docs/architecture.md`](docs/architecture.md) for the full design.
