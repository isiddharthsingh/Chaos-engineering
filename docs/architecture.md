# Architecture

## Design principle: defense in depth

The LLM's judgement is the *innermost* layer, never the only one. Because
dev/staging runs are fully autonomous (no per-run human approval), the outer
layers carry the full safety burden and all exist before the first autonomous
run:

1. **Deterministic, self-reverting fault engines** — Chaos Mesh / LitmusChaos /
   k6. Faults carry their own `spec.duration` and revert without the agent.
2. **Policy-engine pre-flight check** (`src/chaosagent/policy`) — the machine-speed
   "second signer" that replaces the human approval gate. Deterministic.
3. **RBAC / IAM least privilege** (`config/rbac`) — tiered ServiceAccounts;
   experimenter write access is namespaced by construction.
4. **Environment scoping** — dev/staging only; **prod excluded by a separate
   credential** that holds no chaos permissions.
5. **Observability auto-abort** (`src/chaosagent/observe`) — deterministic CR
   delete on the breaching tick of an SLO check, beneath the LLM.
6. **LLM judgement** (`src/chaosagent/agents`) — reasons about intent and results,
   but can never step outside the layers above.

Any single layer failing still bounds the blast radius.

## The experiment lifecycle (one durable state machine per run)

```
intent -> PLAN -> [policy pre-flight self-check] -> baseline steady-state check
      -> INJECT fault (+ optional load) -> OBSERVE loop (PromQL)
      -> verify hypothesis / AUTO-ABORT on SLO breach
      -> ROLLBACK -> REPORT (resilience score + fixes) -> LEARN
```

Mirrors the Chaos Toolkit spec (steady-state hypothesis -> method -> rollback)
and LitmusChaos probes — proven, not invented.

## Multi-agent decomposition

Built on Claude Agent SDK subagents. Phase 0 ships the **Observer** in read-only
mode; the rest arrive with the lifecycle in Phase 1+.

| Agent | Role |
|---|---|
| Orchestrator | Turns user intent into a set of experiments; owns the run. |
| Planner | Designs each experiment (hypothesis as PromQL thresholds, fault type, blast-radius caps, abort conditions); emits a Chaos Mesh CRD — never raw destructive calls. |
| Safety/Policy checker | Deterministic (policy engine + Kyverno/RBAC) + an LLM sanity pass; blocks prod/out-of-scope targets. |
| Executor | Applies the CRD via a permissioned MCP channel; drives k6/Locust load. |
| Observer | Continuously queries Prometheus/Grafana; on SLO breach triggers the native engine stop — the deterministic auto-abort. |
| Analyst | Post-run: correlates metrics/traces/logs, computes a resilience score, writes report + fixes. |
| Capacity-Planner | Separate flow: right-sizing via Karpenter/HPA/KEDA/VPA within policy caps. |

## The permission gate

`PermissionGate` is the single write gatekeeper for both paths: every MCP tool
call passes through `check()` (installed as the SDK's `can_use_tool` callback),
and the direct-client executor path calls `authorize_write()`. Reads always
pass; classification is default-deny — a tool is treated as a write unless its
name tokens positively mark it a read. In **EXPERIMENT** mode a write is
admitted only while a policy-approved, state-changing `ProposedAction` is
**bound** to the gate's single slot (`bind()` returns an `ActionBinding` that
expires with the action's TTL), and only into the bound action's namespace.
The abort/rollback delete is deliberately *not* gated — moving toward safety
must never be blockable by an expired binding.

## The policy engine (pre-flight self-check)

`PolicyEngine.evaluate(ProposedAction) -> PolicyDecision` is deterministic and
side-effect free. It judges the *resolved* action — `chaosagent.resolve.resolve_action`
first overrides the environment, target kind, and namespace scope from the
registry, so a caller cannot spoof `environment: dev` for a prod target. Rules
(ids match the Kyverno ClusterPolicy names):

| Rule id | Enforces |
|---|---|
| `env-scope` | No state change against a prod target. Unconditional — not a config knob. |
| `require-namespace-scope` | State-changing K8s actions must name their namespace (non-K8s targets exempt). |
| `namespace-scope` | The namespace must be within the target's `allowed_namespaces`. |
| `require-chaos-namespace` | Fault injection **and load** only in `chaos-enabled=true` namespaces. |
| `replica-cap` | Capacity actions bounded to ±`max_replica_pct_change`. |
| `fault-duration-cap` | Faults self-revert within `max_fault_duration_seconds`. |
| `fault-blast-radius` | A fault targets at most `max_fault_ratio` of matched pods. |
| `require-ttl` | Chaos/load actions declare a bounded TTL under the ceiling. |
| `single-experiment` | One chaos experiment at a time per target. |
| `incident-freeze` | No state-changing action (chaos or capacity) while an alert/incident is firing. |

Every structural rule except `single-experiment` has a Kyverno admission twin
under `config/policies/kyverno` (`cap-replica-change`, `cap-blast-radius`,
`require-chaos-namespace`, `require-experiment-ttl`), so the caps hold even if a
CR is applied by hand.

Caps live in `config/policies/engine.yaml` (single source of truth). The Kyverno
bundle mirrors the structural rules server-side; `tests/test_manifests.py` fails
the build if the two drift.

## Credential / safety model

- **One ServiceAccount per capability tier**, RBAC-scoped: `agent-observer`
  (get/list/watch only), `agent-experimenter` (create chaos CRs + patch replicas
  in labelled namespaces only). Never cluster-admin.
- Cloud access via **IRSA / Pod Identity / Workload Identity** (per-pod IAM);
  VMs via **SSM Run Command with tag-scoped IAM — no SSH keys** (Phase 4).
- **Kyverno admission policies** replace the human approval gate for autonomous
  runs — the machine-speed second signer at the cluster boundary.
- **Environment scoping is the autonomy boundary.** prod is excluded by separate
  credentials with no chaos permissions; reaching prod requires a human to issue
  a scoped, time-boxed credential (Phase 4).
- **Pre-flight self-check.** Before each run the agent validates its plan against
  the policy bundle (engine + `kubectl --dry-run=server` / Kyverno CLI) and
  refuses to proceed if it would be denied.
- Every action is logged immutably (agent transcript + K8s audit + CloudTrail).

## Runtime

Python + Claude Agent SDK. `uv` / `pytest` / `ruff` / `mypy` are the
build-test-lint gates. Files kept under 500 lines; inputs validated at
boundaries with pydantic.
