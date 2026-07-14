# Roadmap — Phases 1–3

The build plan for Phases 1–3, mapped to the existing code so a fresh session
can start immediately. Phase 0 (foundations + guardrail spine) is complete; see
[`architecture.md`](architecture.md) for the design and the
[README](../README.md) for status. Each phase has a build-ready spec:
[`phase-2-plan.md`](phase-2-plan.md), [`phase-3-plan.md`](phase-3-plan.md).

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

## Phase 1 — Autonomous chaos MVP (local kind cluster) — ✅ complete

**Goal:** the agent runs one experiment end-to-end with **no human in the loop**,
on the local rig, with the guardrail spine intact.

**Shipped as:** `chaosagent run --target <id> (--spec FILE | --intent "...")`.
Exit codes: 0 verified · 2 policy/pre-flight denied · 3 auto-aborted · 1 error.

### The loop (one run = one state machine)

```
intent → PLAN → pre-flight policy self-check (dry-run) → baseline steady-state check
      → INJECT pod-kill → OBSERVE loop (PromQL) → verify hypothesis / AUTO-ABORT on SLO breach
      → ROLLBACK (self-reverting) → REPORT (resilience score + fixes)
```

### Components to build

| # | Component | Where | Status |
|---|---|---|---|
| 1 | **Chaos Mesh CR composer** — `FaultSpec` → `PodChaos` CR (fault_type→action, ratio→`mode: fixed-percent`+`value`, selector→`labelSelectors`, duration→`spec.duration`) | `src/chaosagent/faults/chaosmesh.py` | ✅ Kyverno-compatible by construction (value ≤ 50, duration set, never `mode: all`); asserted across the policy-passable input space |
| 2 | **Prometheus client** — instant + range PromQL over the HTTP API (`httpx`, already a dep) | `src/chaosagent/observe/prometheus.py` | ✅ sync client, TDD via `httpx.MockTransport` |
| 3 | **Steady-state hypothesis** — `SteadyStateHypothesis(query, comparator, threshold)` with `.evaluate(client)` | `src/chaosagent/observe/hypothesis.py` | ✅ frozen pydantic; fail-closed `on_no_data`; no `==` comparator |
| 4 | **Executor** — apply/delete the CR via the kubernetes client as the **experimenter** SA (impersonation); only after policy-allow + dry-run pass | `src/chaosagent/execute/kubernetes.py` | ✅ gate binding wired; abort delete is never gated |
| 5 | **Observe loop + auto-abort** — poll Prometheus every N s during injection; on hypothesis breach, delete the CR **immediately** (deterministic, beneath the LLM) | `src/chaosagent/observe/loop.py` | ✅ returns on the breaching tick with no sleep after detection |
| 6 | **Lifecycle state machine** — PLAN→PREFLIGHT→BASELINE→INJECT→OBSERVE→VERIFY/ABORT→ROLLBACK→REPORT | `src/chaosagent/experiment/lifecycle.py` | ✅ sync, injectable `Clock`; Temporal deferred to Phase 4 |
| 7 | **Analyst + resilience score** — compare baseline vs during vs recovery, score, emit a report + fixes ("add a PDB", "raise minReplicas") | `src/chaosagent/analyze/report.py` | ✅ pinned score, deterministic suggestion table |
| 8 | **Planner agent** — LLM turns intent into a bounded `ExperimentSpec` (fault + hypotheses + caps) | `src/chaosagent/agents/planner.py` | ✅ read-only MCP stack; typed intent only; one repair turn |
| 9 | **Pre-flight self-check** — `PolicyEngine` **plus** server-side dry-run (Kyverno admission runs on dry-run) | lifecycle PREFLIGHT | ✅ denial at either layer stops the run before injection |

### Verify (on the rig)

`scripts/kind-up.sh --with-rig` (Prometheus + Chaos Mesh + Online Boutique), then:
1. ✅ `chaosagent run --target kind-local --spec examples/experiment-cartservice.json`
   runs a pod-kill against cartservice autonomously,
2. ✅ **auto-aborts within one observe interval** of a synthetic SLO breach (exit 3),
3. ✅ rolls back (CR deleted) and reports a resilience score,
4. ✅ is **blocked by policy** (exit 2, `require-chaos-namespace`) if the target
   namespace lacks `chaos-enabled=true`.

The release-gating test (`tests/test_safety_gate.py`) now also asserts:
> *auto-abort lands within the deadline of a synthetic SLO breach (delete before
> any sleep)* and *an unbound write cannot reach the cluster*.

### Definition of done — met
One command: intent → autonomous experiment → auto-abort → rollback → report on
kind, guardrails intact, no human approval, prod unreachable. The whole loop also
runs LLM-free from a `--spec` file, without the `agent` extra installed.

---

## Phase 2 — Fault library + load + scoring, on a real cloud K8s

**Goal:** broaden faults, add load generation, mature scoring, and prove **cloud
parity** by running the *same* agents against one real cluster (e.g. EKS).

**Status: code complete (components 1–4, 6); the EKS parity run (5) remains.**

> **Build-ready spec:** [`phase-2-plan.md`](phase-2-plan.md) has the numbered
> TDD steps, exact files/signatures, per-fault CR field mappings, and the rig
> verification — point a fresh session there to start implementing. The table
> below is the summary.

### Components to build

| # | Component | Where | Status |
|---|---|---|---|
| 1 | **Full fault library** — extend the composer to `NetworkChaos` (latency/loss/partition), `StressChaos` (CPU/memory), `IOChaos`, `DNSChaos`, `TimeChaos` | `src/chaosagent/faults/` | ✅ typed per-family parameter blocks on `FaultSpec`; per-kind composers on one Kyverno-compatible skeleton; `compose_cr` dispatches every `FaultType` |
| 2 | **Load generation** — compose a k6 `TestRun` CRD (k6-operator, GA); run during an experiment; Prometheus remote-write output feeds the verifier | `src/chaosagent/load/k6.py` | ✅ `spec.load` applies the TestRun after INJECT on the fault's own binding and deletes it on rollback/abort; `require-chaos-namespace-k6` admission gate added |
| 3 | **Resilience scoring maturation** — probes across start/end/continuous windows; a scoring rubric (borrow LitmusChaos' probe model) | `src/chaosagent/analyze/` | ✅ probe kinds tag window + kind; weighted rubric defaults pin the Phase-1 formula |
| 4 | **Scheduling / GameDay mode** — schedule experiments; run a suite | `src/chaosagent/experiment/schedule.py` | ✅ `chaosagent suite`: sequential, stops on abort or operational error by default, `--continue-on-abort`, worst exit code |
| 5 | **Cloud parity** — register a real EKS cluster as a target; deploy the same agents; cloud creds via **IRSA / Pod Identity** (per-pod IAM, least privilege) | `config/rbac/` + registry | ◻ config shipped (`examples/target-eks-staging.json`, IRSA annotation documented on the experimenter SA); the live EKS run remains |
| 6 | **Litmus namespace gate (deferred from Phase 0)** — add the `chaos-enabled=true` admission policy for `litmuschaos.io ChaosEngine`, then re-add the Litmus write grant to the experimenter Role | `config/policies/kyverno/chaos/`, `config/rbac/02-experimenter-role.yaml` | ✅ gate ships with the grant; `test_manifests.py` fails the build if they drift |

### Verify
An **autonomous multi-fault experiment** against a staging service on **EKS**,
with auto-abort intact and k6 load applied during the fault.

### Definition of done
The same agents that pass on kind pass on a real cloud K8s cluster, driving a
multi-fault + load experiment autonomously with the guardrail spine unchanged.

---

## Phase 3 — Autonomous capacity planning

**Goal:** the agent observes utilization, recommends a bounded replica change,
applies it through the same guardrail spine, verifies the steady state, and
**auto-reverts deterministically** on breach — the capacity analogue of
auto-abort. Cost (OpenCost) is a signal, never an authority.

**Status: code complete (all components); the live kind-rig verification pass
remains.**

> **Build-ready spec:** [`phase-3-plan.md`](phase-3-plan.md) has the numbered
> TDD steps, exact files/signatures, the revert-admissibility design, and the
> rig verification. The table below is the summary.

### Components to build

| # | Component | Where | Status |
|---|---|---|---|
| 1 | **Capacity spec + revert-admissible engine rule** — `CapacitySpec` (workload ref, desired replicas, hypotheses, ttl); refuse any change whose *inverse* would breach `replica-cap` (a −50% downscale has a +100% revert) | `src/chaosagent/capacity/spec.py`, `src/chaosagent/policy/engine.py` | ✅ `revert-admissible` rule (engine-only by design); `test_safety_gate.py` proves every admitted change is revertible under the same caps |
| 2 | **Scale executor** — gate-checked dry-run + patch of the `/scale` subresource as the experimenter; **ungated bounded revert** (writes only the recorded previous count, abort-delete philosophy) | `src/chaosagent/execute/scale.py` | ✅ Kyverno `cap-replica-change` matches `/scale`, so the dry-run IS the live self-check; revert works with an expired binding and swallows 404 |
| 3 | **Capacity lifecycle** — PREFLIGHT (engine + server dry-run) → BASELINE → APPLY → OBSERVE settle window → keep on green / **auto-revert on the breaching tick** | `src/chaosagent/capacity/lifecycle.py` | ✅ revert on the breaching tick before any sleep (journal-ordered test); success keeps the change; binding always released |
| 4 | **Deterministic recommender + signals** — utilization vs requests (PromQL) → proportional sizing clamped to the caps; VPA/KEDA/Karpenter read as signals only (writes are Phase 4) | `src/chaosagent/capacity/{signals,recommend}.py` | ✅ pure math beneath the LLM, clamped to replica-cap + revert-admissible floor; property test: never emits a change the engine would deny; VPA targets fold into the rationale |
| 5 | **OpenCost signal** — `httpx` client feeding an estimated monthly delta into recommendations and reports | `src/chaosagent/capacity/opencost.py` | ✅ advisory by construction (any failure → None); rig installs OpenCost with `--with-rig` |
| 6 | **CLI** — `chaosagent recommend` (read-only) and `chaosagent scale --spec` (exit 0 kept / 2 denied / 3 auto-reverted / 1 error) | `src/chaosagent/capacity/runner.py`, `cli.py` | ✅ mirrors the `run` runner + settings pattern; `recommend` deps carry no gate and no executor |
| 7 | **HPA bounds cap (gate before grant)** — Kyverno `cap-hpa-bounds` ships **before** the experimenter gets the HPA write grant, with a manifests pairing test | `config/policies/kyverno/`, `config/rbac/` | ✅ pairing + anti-drift tests in `test_manifests.py`; engine `replica-cap` judges both HPA bounds; `compose_hpa_patch` is a pure builder |

### Verify
`chaosagent recommend` prints a bounded, reproducible recommendation writing
nothing; `chaosagent scale` applies within the cap and keeps a verified change;
an engineered post-change breach reverts on the same tick (exit 3); a
revert-inadmissible downscale (4→2) is denied at PREFLIGHT; an over-cap `/scale`
patch is denied at admission by `replica-cap`. ◻ Remaining: this pass on the
live kind rig (`scripts/kind-up.sh --with-rig && scripts/verify-guardrails.sh`).

### Definition of done
The agent right-sizes a live workload end to end — observe → recommend → bounded
apply → verify → keep (or deterministic revert) — on the kind rig, with the
guardrail spine unchanged and every autonomous change provably revertible under
the same caps that admitted it.

---

## Later (context only)

- **Phase 4** — multi-cloud (AKS/GKE) + VM faults via `chaosd`/ChaosBlade over
  **SSM (no SSH)**; cloud-service faults via AWS FIS / Azure Chaos Studio;
  **Temporal** for durable multi-hour runs; the human-escalation path for prod
  (scoped, time-boxed credential); Karpenter/KEDA/VPA *writes* (cluster-scoped
  credentials need their own gate design); web dashboard.

## Key reference
**ChaosEater** (`ntt-dkiku/chaos-eater`, arXiv:2501.11107) — the closest prior
art: an LLM-driven full chaos cycle on K8s with Chaos Mesh. Worth studying for the
Phase 2 fault library and scoring model.
