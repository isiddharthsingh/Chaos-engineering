# Roadmap — Phase 1 & Phase 2

This is the build plan for the next two phases, mapped to the existing code so a
fresh session can start immediately. Phase 0 (foundations + guardrail spine) is
complete; see [`architecture.md`](architecture.md) for the design and the
[README](../README.md) for status.

## What Phase 0 already gives you (reuse, don't rebuild)

| Capability | Module |
|---|---|
| Typed intent: `FaultSpec`, `ProposedAction`, `ReplicaChange`, `FaultType` (pod/network/cpu/mem/io/dns/time) | `src/chaosagent/domain/` |
| Deterministic pre-flight policy engine (9 rules) | `src/chaosagent/policy/engine.py` |
| Bind action → registered target (env/kind/scope, anti-spoof) | `src/chaosagent/resolve.py` |
| Target inventory (env tiers, namespace scope, credential refs) | `src/chaosagent/registry/` |
| Permission gate with an **`EXPERIMENT` mode stub** to wire | `src/chaosagent/agents/permission.py` |
| Read-only agent harness + MCP wiring (K8s / Prometheus / Grafana) | `src/chaosagent/agents/` |
| Kyverno admission bundle (chaos-namespace, replica-cap, blast-radius, TTL) + tiered RBAC | `config/policies/`, `config/rbac/` |
| Local rig + live guardrail checks | `scripts/kind-up.sh --with-rig`, `scripts/verify-guardrails.sh` |

**Invariant to preserve in every phase:** the LLM only emits typed intent
(`FaultSpec` / `ProposedAction`); everything destructive goes through
`resolve_action` → `PolicyEngine` → server-side dry-run → the executor. Never let
the model make a raw destructive call.

---

## Phase 1 — Autonomous chaos MVP (local kind cluster)

**Goal:** the agent runs one experiment end-to-end with **no human in the loop**,
on the local rig, with the guardrail spine intact.

### The loop (one run = one state machine)

```
intent → PLAN → pre-flight policy self-check (dry-run) → baseline steady-state check
      → INJECT pod-kill → OBSERVE loop (PromQL) → verify hypothesis / AUTO-ABORT on SLO breach
      → ROLLBACK (self-reverting) → REPORT (resilience score + fixes)
```

### Components to build

| # | Component | Where | Notes / TDD |
|---|---|---|---|
| 1 | **Chaos Mesh CR composer** — `FaultSpec` → `PodChaos` CR (fault_type→action, ratio→`mode: fixed-percent`+`value`, selector→`labelSelectors`, duration→`spec.duration`) | new `src/chaosagent/faults/chaosmesh.py` | Pure, TDD-able. **Must** emit CRs that pass the Kyverno bundle (value ≤ 50, duration set & ≤ 900s) — assert that in tests |
| 2 | **Prometheus client** — instant + range PromQL over the HTTP API (`httpx`, already a dep) | new `src/chaosagent/observe/prometheus.py` | TDD with mocked `httpx` |
| 3 | **Steady-state hypothesis** — `SteadyStateHypothesis(query, comparator, threshold)` with `.evaluate(client)` | new `src/chaosagent/observe/hypothesis.py` | Pure logic + the client from #2 |
| 4 | **Executor** — apply/delete the CR via the kubernetes client (or K8s MCP) as the **experimenter** SA; only after policy-allow + dry-run pass | new `src/chaosagent/execute/kubernetes.py` | Wire the `PermissionGate` `EXPERIMENT` path: bind an approved `ProposedAction` to the write |
| 5 | **Observe loop + auto-abort** — poll Prometheus every N s during injection; on hypothesis breach, delete the CR **immediately** (deterministic, beneath the LLM) | part of the lifecycle | Chaos Mesh also self-reverts on `duration` — abort is the fast path |
| 6 | **Lifecycle state machine** — PLAN→PREFLIGHT→BASELINE→INJECT→OBSERVE→VERIFY/ABORT→ROLLBACK→REPORT | new `src/chaosagent/experiment/lifecycle.py` | Simple in-process machine; Temporal deferred to Phase 4 |
| 7 | **Analyst + resilience score** — compare baseline vs during vs recovery, score, emit a Chaos-Toolkit-style report + fixes ("add a PDB", "raise HPA minReplicas") | new `src/chaosagent/analyze/report.py` | Pure given the metric series |
| 8 | **Planner / Orchestrator agents** — LLM turns intent ("test the cart service's resilience") into a bounded `FaultSpec` + hypothesis + caps | extend `src/chaosagent/agents/` | Emits typed intent only |
| 9 | **Pre-flight self-check** — `PolicyEngine` (have it) **plus** `kubectl --dry-run=server` / Kyverno; refuse before injecting | uses existing engine | |

### Verify (on the rig)

`scripts/kind-up.sh --with-rig` (Prometheus + Chaos Mesh + Online Boutique), then:
1. agent autonomously runs a `pod-delete` against a demo service,
2. **auto-aborts within N seconds** of a synthetic SLO breach,
3. rolls back and reports a resilience score,
4. is **blocked by policy** if the target namespace lacks `chaos-enabled=true`.

Extend the release-gating test (`tests/test_safety_gate.py`) with:
> *"the agent always auto-aborts within N seconds of a synthetic SLO breach."*

### Definition of done
One command: intent → autonomous experiment → auto-abort → rollback → report on
kind, guardrails intact, no human approval, prod unreachable.

### Suggested build order
Start with **#1 composer + #2/#3 Prometheus + hypothesis** (all pure, no cluster
needed, fully TDD-able). Then **#4 executor + #5 observe loop** against the
`--with-rig` cluster. Then **#6 lifecycle** to stitch them, then **#7 report** and
**#8 the planner agent**.

---

## Phase 2 — Fault library + load + scoring, on a real cloud K8s

**Goal:** broaden faults, add load generation, mature scoring, and prove **cloud
parity** by running the *same* agents against one real cluster (e.g. EKS).

### Components to build

| # | Component | Where | Notes |
|---|---|---|---|
| 1 | **Full fault library** — extend the composer to `NetworkChaos` (latency/loss/partition), `StressChaos` (CPU/memory), `IOChaos`, `DNSChaos`, `TimeChaos` | `src/chaosagent/faults/` | `FaultType` enum already lists these; each CR must satisfy the Kyverno caps |
| 2 | **Load generation** — compose a k6 `TestRun` CRD (k6-operator, GA); run during an experiment; Prometheus remote-write output feeds the verifier | new `src/chaosagent/load/k6.py` | Experimenter RBAC already grants `k6.io/testruns`; add a Kyverno gate if load needs one |
| 3 | **Resilience scoring maturation** — probes across start/end/continuous windows; a scoring rubric (borrow LitmusChaos' probe model) | `src/chaosagent/analyze/` | |
| 4 | **Scheduling / GameDay mode** — schedule experiments; run a suite | new `src/chaosagent/experiment/schedule.py` | Respect `single-experiment` policy |
| 5 | **Cloud parity** — register a real EKS cluster as a target; deploy the same agents; cloud creds via **IRSA / Pod Identity** (per-pod IAM, least privilege) | `config/rbac/` + registry | Chaos Mesh/Litmus install identically into any cluster |
| 6 | **Litmus namespace gate (deferred from Phase 0)** — add the `chaos-enabled=true` admission policy for `litmuschaos.io ChaosEngine`, then re-add the Litmus write grant to the experimenter Role | `config/policies/kyverno/chaos/`, `config/rbac/02-experimenter-role.yaml` | See the NOTE comments left in those files |

### Verify
An **autonomous multi-fault experiment** against a staging service on **EKS**,
with auto-abort intact and k6 load applied during the fault.

### Definition of done
The same agents that pass on kind pass on a real cloud K8s cluster, driving a
multi-fault + load experiment autonomously with the guardrail spine unchanged.

---

## Later (context only)

- **Phase 3** — autonomous capacity planning (Karpenter `NodePool` / HPA / KEDA /
  VPA `InPlaceOrRecreate`) within policy caps; cost signal via OpenCost. The
  policy engine already has `replica-cap` and the capacity action types.
- **Phase 4** — multi-cloud (AKS/GKE) + VM faults via `chaosd`/ChaosBlade over
  **SSM (no SSH)**; cloud-service faults via AWS FIS / Azure Chaos Studio;
  **Temporal** for durable multi-hour runs; the human-escalation path for prod
  (scoped, time-boxed credential); web dashboard.

## Key reference
**ChaosEater** (`ntt-dkiku/chaos-eater`, arXiv:2501.11107) — the closest prior
art: an LLM-driven full chaos cycle on K8s with Chaos Mesh. Study before Phase 1.
