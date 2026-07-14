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
| `replica-cap` | Capacity actions bounded to ±`max_replica_pct_change` — direct scales AND each HPA bound (`HpaBoundsChange` reuses `ReplicaChange` per bound). |
| `fault-duration-cap` | Faults self-revert within `max_fault_duration_seconds`. |
| `fault-blast-radius` | A fault targets at most `max_fault_ratio` of matched pods. |
| `require-ttl` | Chaos/load actions declare a bounded TTL under the ceiling. |
| `single-experiment` | One chaos experiment at a time per target. |
| `incident-freeze` | No state-changing action (chaos or capacity) while an alert/incident is firing. |
| `revert-admissible` | A capacity change whose *inverse* would breach `replica-cap` is refused (downscale floor `desired >= current / (1 + cap)`) — direct scales and each HPA bound alike — so the auto-revert is admissible by construction. |

Every structural rule except `single-experiment` and `revert-admissible` has a
Kyverno admission twin under `config/policies/kyverno` (`cap-replica-change` —
which matches the `/scale` subresource, so a scale dry-run is a live policy
check — `cap-hpa-bounds`, `cap-blast-radius`, `require-chaos-namespace` — plus
its `-k6` and `-litmus` twins for kinds the chaos policies don't match — and
`require-experiment-ttl`), so the caps hold even if a CR is applied by hand.
`revert-admissible` is deliberately engine-only: an admission twin would also
constrain human operators, who can revert in two steps.

Caps live in `config/policies/engine.yaml` (single source of truth). The Kyverno
bundle mirrors the structural rules server-side; `tests/test_manifests.py` fails
the build if the two drift.

## Fault library, load, and scoring

`compose_cr` (`src/chaosagent/faults`) dispatches every `FaultType` to a
per-kind Chaos Mesh composer (PodChaos, NetworkChaos, StressChaos, IOChaos,
DNSChaos, TimeChaos), each built on one Kyverno-compatible skeleton: `mode:
fixed-percent` (never `all`), value ≤ 50 by policy, `duration` always set, and
a bounded blast radius — an empty label selector is refused, as is an empty
DNSChaos pattern list (which would fault *every* domain). Family-specific knobs
live in typed parameter blocks on `FaultSpec` (`network`/`stress`/`io`/`dns`/
`time`); a validator requires exactly the block matching the fault type, so the
planner's JSON contract updates with the schema.

k6 load (`src/chaosagent/load`) composes a `TestRun` referencing a
**pre-existing** script ConfigMap (creating ConfigMaps is a write the
experimenter RBAC deliberately does not grant); the CR is self-bounding like a
fault (`--duration` from the spec, `cleanup: post`), and parallelism is capped
on the model since load does not pass the fault-ratio rule. When `spec.load` is
set, PREFLIGHT server-side dry-runs the TestRun alongside the fault CR and
probes that the script ConfigMap exists (a TestRun referencing a missing one is
admitted but starts no load), so a doomed run is refused before any injection;
the lifecycle then applies it right after INJECT **on the same policy-approved
binding** as the fault (the gate stays single-slot) and deletes it alongside
the fault CR on rollback and on abort. `TestRun` has its own
`require-chaos-namespace-k6` admission gate because the chaos policies match
Chaos Mesh kinds only.

The analyst scores each hypothesis over Litmus-style probe kinds — `start`
(one-shot before inject), `continuous` (fault and recovery windows), `end`
(one-shot after recovery) — with a weighted rubric (`ProbeWeights`) whose
default (0 / 0.6 / 0.4 / 0) pins the Phase-1 formula exactly; overall score =
min across hypotheses, capped at 30 on abort.

## The capacity lifecycle (Phase 3)

The second action family through the same spine. `chaosagent scale` drives:

```
CapacitySpec -> PLAN -> PREFLIGHT (registry -> incident probe -> live replica
      read -> engine -> bind -> /scale server-side dry-run) -> BASELINE
      -> APPLY -> OBSERVE settle window -> VERIFY (change kept)
                                         | REVERT on the breaching tick
      -> REPORT -> DONE | FAILED
```

Three properties carry the safety story:

- **Revert-admissible by construction.** The `revert-admissible` engine rule
  refuses at PREFLIGHT any change whose inverse the caps would deny (a −50%
  downscale is capped, but its revert would be +100%), so the deterministic
  auto-revert can never be blocked by our own guardrails. The release-gating
  safety test proves the property over a grid: every admitted change's revert
  is also admitted.
- **The auto-revert mirrors the abort delete.** It runs on the breaching tick
  before any sleep, is not gate-checked (moving toward the recorded known-good
  count must not be blockable by an expired binding), and only ever moves
  toward `applied.previous` — directly when admissible, or in cap-compliant
  steps when the live count drifted after apply (the admission cap judges
  against the live count, so a drifted direct revert could otherwise be denied
  by our own guardrail). Kyverno still sees every patch — belt and suspenders,
  not a bypass. Success keeps the change: a verified right-size is the
  deliverable. A revert that cannot be confirmed is surfaced (`revert_error`,
  exit 1, `final_replicas: null`) — never reported as a completed revert.
- **Cost is a signal, never an authority.** `chaosagent recommend` is read-only
  (its dependency set carries no gate and no executor): utilization vs requests
  feeds a deterministic proportional recommender clamped to `replica-cap` AND
  the revert-admissible floor — a property test asserts it can never emit a
  change the engine would deny. OpenCost supplies an advisory monthly delta
  (any failure yields "no data", never an error); VPA targets fold into the
  rationale read-only. VPA/KEDA/Karpenter *writes* are Phase-4 decisions —
  Karpenter's NodePool is cluster-scoped and would break the namespaced-write
  invariant.

HPA bounds are the second capacity write family, shipped gate-before-grant:
the `cap-hpa-bounds` admission policy (same ±50% shape) lands before the
experimenter's `horizontalpodautoscalers` grant, and `test_manifests.py` fails
the build if the pairing drifts. This phase recommends bounds
(`set-hpa-bounds` in reports); it does not move them autonomously.

## Credential / safety model

- **One ServiceAccount per capability tier**, RBAC-scoped: `agent-observer`
  (get/list/watch only), `agent-experimenter` (create chaos CRs + patch replicas
  in labelled namespaces only). Never cluster-admin.
- Cloud access via **IRSA / Pod Identity / Workload Identity** (per-pod IAM).
  On EKS the experimenter SA carries an `eks.amazonaws.com/role-arn` annotation
  pointing at a least-privilege role (documented in
  `config/rbac/00-namespace-and-serviceaccounts.yaml`), and the cluster is
  registered as a `staging` target (`examples/target-eks-staging.json`) whose
  credential holds a role-ARN *reference*, never a key. The K8s RBAC is
  identical on kind and EKS — only the cloud IAM edge differs. VMs via **SSM
  Run Command with tag-scoped IAM — no SSH keys** (Phase 4).
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
