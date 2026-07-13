# Phase 2 — Fault library + load + scoring, on real cloud K8s (build plan)

This is the build-ready spec for Phase 2. Point a fresh session at this file and
start at step 1. It is the Phase-2 analogue of the Phase-1 plan: numbered TDD
steps, exact files and signatures, each gated by
`uv run pytest && uv run ruff check && uv run mypy` (green **with and without**
the `agent` extra). The high-level roadmap lives in [`roadmap.md`](roadmap.md);
the design rationale in [`architecture.md`](architecture.md).

## Context

Phase 1 is complete and shipped: the full autonomous loop runs on kind
(`chaosagent run --target <id> (--spec FILE | --intent "…")`) — PLAN → PREFLIGHT
(engine + server-side Kyverno dry-run) → BASELINE → INJECT PodChaos → OBSERVE
(PromQL) → deterministic auto-abort on SLO breach → ROLLBACK → REPORT (resilience
score + fixes). The composer only speaks `PodChaos`; the observe loop, executor,
permission-gate binding, analyst, and planner are all in place.

Phase 2 broadens faults beyond the pod family, adds k6 load during a fault,
matures scoring, adds a GameDay suite runner, and proves **cloud parity** by
running the *same* agents against a real cluster (EKS).

**Invariant preserved throughout (unchanged from Phase 0/1):** the LLM only emits
typed intent (`FaultSpec` / `ExperimentSpec`); everything destructive goes
`resolve_action` → `PolicyEngine` → server-side dry-run (Kyverno) → executor. The
abort/rollback delete stays deterministic and beneath the LLM.

## What Phase 1 already gives you (reuse, don't rebuild)

| Capability | Module |
|---|---|
| `compose_podchaos`, `UnsupportedFaultError` (pod family only) | `src/chaosagent/faults/chaosmesh.py` |
| Executor (gate-checked apply, ungated delete, server-side dry-run), `PLURALS`, impersonated API | `src/chaosagent/execute/kubernetes.py` |
| Observe loop + auto-abort, Prometheus client, steady-state hypothesis | `src/chaosagent/observe/` |
| Lifecycle state machine + `LifecycleDeps` (incl. `incident_active` probe) | `src/chaosagent/experiment/lifecycle.py` |
| `ExperimentSpec` (planner schema + `--spec` format) | `src/chaosagent/experiment/spec.py` |
| Runner + `run` subcommand (exit 0/2/3/1) | `src/chaosagent/experiment/runner.py`, `cli.py` |
| Analyst (pinned score, deterministic fixes) | `src/chaosagent/analyze/report.py` |
| Read-only planner (typed-intent-only) | `src/chaosagent/agents/planner.py` |
| Shared test fakes | `tests/fakes.py` |

**Load-bearing fact — the guardrail spine already extends to the new kinds.**
The Kyverno chaos policies (`config/policies/kyverno/chaos/`) and the experimenter
RBAC (`config/rbac/02-experimenter-role.yaml`) already match/grant `NetworkChaos`,
`StressChaos`, `IOChaos`, `DNSChaos`, `TimeChaos` (verify:
`grep -A9 kinds config/policies/kyverno/chaos/*.yaml`). So the fault-library slice
needs **no** admission-policy or RBAC change for Chaos Mesh kinds — only the
composer, the domain model, the executor `PLURALS`, and tests. (k6 `TestRun` is
different — see step 5.)

## Global design decisions

- **Keep the guardrail spine unchanged for Chaos Mesh kinds.** Every composed CR
  stays Kyverno-compatible *by construction* — `mode: fixed-percent`,
  `value: str(max(1, round(ratio*100)))` (≤ 50 by policy), `duration` always set
  (≤ 900s). This is already true for `PodChaos`; every new builder inherits it.
- **`FaultSpec` gains typed, per-family parameter blocks** (step 1). One frozen
  model, `extra="forbid"`, with a `model_validator` that requires exactly the
  block matching `fault_type` and rejects mismatched blocks. Because the planner
  prompt embeds `ExperimentSpec.model_json_schema()`, the LLM contract updates
  automatically — only the caps *prose* in `PLANNER_SYSTEM_PROMPT` needs editing.
- **The composer becomes a dispatcher.** `compose_cr(fault, *, namespace, name)`
  switches on `fault_type` and delegates to a per-kind builder. `compose_podchaos`
  stays (pod family). The lifecycle calls `compose_cr`, not `compose_podchaos`.
- **Confirm every CR field against the installed CRD**, not from memory:
  `kubectl explain networkchaos.spec`, `… stresschaos.spec.stressors`, etc. The
  `--with-rig` cluster has the CRDs. The mappings below are the target; the CRD is
  the authority (Chaos Mesh ~2.6/2.7).
- **k6 load is a new resource family that DOES need its own namespace gate.** The
  chaos Kyverno policies only match Chaos Mesh kinds; a `TestRun` in an
  unlabelled namespace would not be refused. Add a `require-chaos-namespace`
  twin for `k6.io/TestRun` (step 5).
- **Sync core / async edge unchanged. LLM-free `--spec` path preserved.** No new
  hard dependencies (`kubernetes` is already in the `agent` extra; k6 is a CRD,
  not a Python dep).
- **TDD, test file first, each step gated.** Hand-rolled fakes only (extend
  `tests/fakes.py`); no mock libraries.

---

## Build steps

### 1. Extend `FaultSpec` with per-family parameters (pure domain)
Modify: `src/chaosagent/domain/actions.py`. Extend: `tests/test_domain_actions.py`.

Add frozen, `extra="forbid"` sub-models and optional fields on `FaultSpec`. Target
shape (confirm names/units against the CRD):

```python
class NetworkFault(BaseModel):   # action delay | loss | partition
    action: Literal["delay", "loss", "partition"]
    latency_ms: int | None = None        # delay: -> delay.latency "Nms"
    jitter_ms: int = 0                   # delay: -> delay.jitter
    loss_percent: float | None = None    # loss: -> loss.loss "N" (0-100)
    correlation_percent: float = 0       # -> {delay,loss}.correlation
    direction: Literal["to", "from", "both"] = "to"  # -> spec.direction

class StressFault(BaseModel):    # cpu and/or memory
    cpu_workers: int | None = None       # -> stressors.cpu.workers
    cpu_load_percent: int | None = None  # -> stressors.cpu.load (0-100)
    memory_workers: int | None = None    # -> stressors.memory.workers
    memory_size: str | None = None       # -> stressors.memory.size "256MB"

class IOFault(BaseModel):        # action latency | fault
    action: Literal["latency", "fault"]
    volume_path: str                     # -> spec.volumePath (required)
    path_glob: str | None = None         # -> spec.path
    delay_ms: int | None = None          # latency -> spec.delay "Nms"
    errno: int | None = None             # fault -> spec.errno
    percent: int = 100                   # -> spec.percent

class DNSFault(BaseModel):       # action error | random
    action: Literal["error", "random"]
    patterns: tuple[str, ...] = ()       # -> spec.patterns

class TimeFault(BaseModel):
    time_offset: str                     # -> spec.timeOffset "-10m100ns"
    clock_ids: tuple[str, ...] = ()      # -> spec.clockIds
```

On `FaultSpec` add `network|stress|io|dns|time: <Model> | None = None` plus a
`model_validator(mode="after")` that maps `fault_type` → required block
(`NETWORK_* → network`, `CPU_STRESS/MEMORY_STRESS → stress`, `IO_STRESS → io`,
`DNS_CHAOS → dns`, `TIME_SKEW → time`; pod family → none) and rejects any other
block being set. Keep `selector`/`ratio`/`duration_seconds`/`container_names`.
- **Test:** each fault family requires its block and rejects a foreign one; pod
  family still validates with no block; existing Phase-1 pod specs unchanged.

### 2. NetworkChaos composer
New: `src/chaosagent/faults/network.py`. New: `tests/test_network_composer.py`.
- `compose_networkchaos(fault, *, namespace, name=None) -> dict`.
- `network_latency→action delay` (`spec.delay.latency/jitter/correlation`),
  `network_loss→action loss` (`spec.loss.loss/correlation`),
  `network_partition→action partition` (`spec.direction`).
- Common: `mode: fixed-percent`, `value: str(max(1, round(ratio*100)))`,
  `duration`, `selector.{namespaces,labelSelectors}`, managed-by label. Empty
  selector refused (mirror `compose_podchaos`).
- **Test:** Kyverno compatibility (value ≤ 50, `mode != all`, duration ≤ 900s);
  action-specific sub-blocks present; missing `network` block raises.

### 3. StressChaos composer
New: `src/chaosagent/faults/stress.py`. New: `tests/test_stress_composer.py`.
- `compose_stresschaos(...)`. No `action` field — the stressor type lives in
  `spec.stressors.cpu` / `spec.stressors.memory`. `cpu_stress` emits `cpu`,
  `memory_stress` emits `memory`. Optional `containerNames`.
- **Test:** same Kyverno caps; stressor block matches the fault type; at least one
  stressor present.

### 4. IO / DNS / Time composers + dispatcher + wiring
New: `src/chaosagent/faults/{io,dns,timechaos}.py`,
`tests/test_{io,dns,time}_composer.py`. Modify: `src/chaosagent/faults/__init__.py`
(add `compose_cr`), `src/chaosagent/execute/kubernetes.py` (`PLURALS`),
`src/chaosagent/experiment/lifecycle.py`, `src/chaosagent/agents/planner.py`.
- `compose_iochaos` (`action latency|fault`, `volumePath` required, `path`,
  `delay`/`errno`, `percent`), `compose_dnschaos` (`action error|random`,
  `patterns`), `compose_timechaos` (`timeOffset`, `clockIds`).
- `compose_cr(fault, *, namespace, name=None, container_names=())` dispatches on
  `fault_type`; `UnsupportedFaultError` only for genuinely unmapped types (after
  this step every `FaultType` value is supported).
- Extend `PLURALS` → `{"PodChaos":"podchaos","NetworkChaos":"networkchaos",
  "StressChaos":"stresschaos","IOChaos":"iochaos","DNSChaos":"dnschaos",
  "TimeChaos":"timechaos"}`.
- Lifecycle: replace the `compose_podchaos(...)` call with `compose_cr(...)`.
- Planner: replace the "pod faults only in this phase" cap line with the full
  supported list and each family's required params; keep ratio ≤ 0.5 / duration
  ≤ 900 / ttl ≤ 3600 / single-namespace caps.
- **Test:** `compose_cr` routes each `FaultType` to the right kind; a lifecycle
  test drives a `network_latency` spec end to end over the fakes (dry-run + apply
  see kind `NetworkChaos`).
- **Rig:** DNSChaos needs the DNS chaos server — add `--set dnsServer.create=true`
  to the Chaos Mesh install in `scripts/kind-up.sh`. Add network + stress
  admission cases to `scripts/verify-guardrails.sh` (deny value 90 / mode all;
  allow fixed-percent 34 / 300s) for each new kind.

### 5. k6 load: composer + admission gate
New: `src/chaosagent/load/{__init__,k6.py}`, `tests/test_k6_composer.py`,
`config/policies/kyverno/load/require-chaos-namespace-k6.yaml`. Modify:
`scripts/render_kyverno` inputs if applicable, `scripts/kind-up.sh` (install
k6-operator with `--with-rig`).
- `LoadSpec` (frozen): `script_configmap: str`, `script_file: str = "script.js"`,
  `parallelism: int = 1`, `duration_seconds: int` (≤ engine ttl caps),
  `ttl_seconds: int`. **The script ConfigMap must pre-exist** — creating one is a
  ConfigMap write the experimenter RBAC deliberately does not grant; inline-script
  → ConfigMap creation is deferred (would need an explicit RBAC grant). Note this
  in the module docstring.
- `compose_testrun(load, *, namespace, name=None) -> dict` → `k6.io/v1alpha1`
  `TestRun` referencing `spec.script.configMap.{name,file}`, `spec.parallelism`,
  managed-by label. Confirm fields via `kubectl explain testrun.spec`.
- New Kyverno `ClusterPolicy` gating `k6.io/TestRun` to `chaos-enabled=true`
  namespaces (twin of `require-chaos-namespace`, matching kind `TestRun`). RBAC
  already grants `k6.io/testruns` — no RBAC change.
- **Test:** composed `TestRun` carries the managed-by label and the ConfigMap ref;
  `tests/test_manifests.py`-style check that the new policy names the k6 kind.

### 6. Load during the fault (lifecycle integration)
Modify: `src/chaosagent/experiment/spec.py` (optional `load: LoadSpec | None`),
`src/chaosagent/experiment/lifecycle.py`, `src/chaosagent/execute/kubernetes.py`
(`PLURALS` + a `k6.io` group/version constant), `tests/test_lifecycle.py`.
- If `spec.load` is set: after INJECT, apply the `TestRun` (bound to the SAME
  policy-approved action — one binding, `single-experiment` still holds), and
  tear it down in ROLLBACK alongside the fault CR (both via `_safe_delete`). Load
  is `APPLY_LOAD` semantics but rides the existing fault binding; do **not** add a
  second binding (the gate is single-slot by design).
- k6 remote-writes k6 metrics to Prometheus; hypotheses may reference them
  (e.g. `k6_http_req_failed`). No new observe machinery needed.
- **Test:** a spec with `load` applies both CRs and deletes both on rollback; a
  spec without `load` is unchanged (all Phase-1 lifecycle tests still pass).

### 7. Resilience-scoring maturation
Modify: `src/chaosagent/analyze/report.py`, `tests/test_report.py`.
- Add probe *kinds* borrowed from the LitmusChaos model: `start` (one-shot before
  fault), `continuous` (the existing during-fault sampling), `end` (one-shot after
  recovery). Extend `HypothesisResult`/verdicts to tag the window. Keep the pinned
  overall formula (`100*(0.6*during + 0.4*recovery)`, min across hypotheses,
  capped 30 on abort) as the default; add per-probe weights behind a documented
  rubric so scores stay reproducible and comparable.
- **Test:** the pinned score is unchanged for a Phase-1-shaped run; new probe
  kinds contribute deterministically.

### 8. GameDay suite runner
New: `src/chaosagent/experiment/schedule.py`, `tests/test_suite.py`. Modify:
`src/chaosagent/cli.py` (a `suite` subcommand).
- A `SuiteSpec` = ordered `list[ExperimentSpec]`; run them **sequentially**
  (never concurrently — `single-experiment` policy), each through `run_lifecycle`,
  aggregating reports. Stop-on-first-abort is the default; `--continue-on-abort`
  opts out. Exit code = worst run's code.
- **Test:** a two-experiment suite runs both over fakes; an abort in the first
  stops the second by default and runs it with `--continue-on-abort`.

### 9. Litmus namespace gate (deferred from Phase 0)
New: `config/policies/kyverno/chaos/require-chaos-namespace-litmus.yaml`. Modify:
`config/rbac/02-experimenter-role.yaml`, `tests/test_manifests.py`,
`scripts/verify-guardrails.sh`.
- Add the `chaos-enabled=true` admission twin for `litmuschaos.io ChaosEngine`
  (see the NOTE comments already in both files), **then** re-add the Litmus write
  grant to the experimenter Role. Order matters: the gate must exist before the
  grant, or a Litmus fault could land in an unopted namespace.
- **Test/verify:** `verify-guardrails.sh` denies a `ChaosEngine` in `unlabelled`
  and allows it in `boutique`.

### 10. EKS cloud parity
New: `examples/target-eks-staging.json`. Modify: `config/rbac/` (IRSA annotation
on the experimenter SA), `docs/architecture.md` (credential model),
`README.md`. Mostly config + a real cluster; the agents are cloud-agnostic.
- Register a real EKS cluster as a `staging` target; scope credentials via **IRSA
  / Pod Identity** (per-pod IAM, least privilege). Chaos Mesh installs identically.
- **Verify (the Phase-2 definition of done):** the *same* agents that pass on kind
  drive an autonomous **multi-fault + k6-load** experiment against a staging
  service on EKS, auto-abort intact, guardrail spine unchanged.

### 11. Docs
Update `README.md` status, `roadmap.md` Phase 2 checkboxes, `architecture.md`
(fault library + load + cloud credential model). Keep every file under 500 lines.

---

## Verification (rig + cloud)

```bash
scripts/kind-up.sh --with-rig          # now also: k6-operator, DNS chaos server
scripts/verify-guardrails.sh           # spine green incl. new kinds + k6 + Litmus
uv run pytest && uv run ruff check && uv run mypy   # with AND without the agent extra
```
1. `chaosagent run --spec examples/experiment-network-latency.json` injects a
   `NetworkChaos` against a demo service, observes, rolls back, scores.
2. A `StressChaos` / `IOChaos` / `DNSChaos` / `TimeChaos` spec each runs and the
   CR is gone afterward (`kubectl -n boutique get <kind> -w`).
3. A spec with `load` applies a k6 `TestRun` during the fault; both CRs deleted on
   rollback; a k6-metric hypothesis can auto-abort.
4. **Negative:** a `TestRun` (and a Litmus `ChaosEngine`) in `unlabelled` is denied
   by its new Kyverno gate with the matching rule id.
5. **Cloud parity:** the same multi-fault + load run passes on EKS.

## Suggested build order
Steps **1–4** (domain + composers + dispatcher) are pure/TDD-able with no cluster —
do them first, exactly as Phase 1 started with its pure pieces. Then **5–6** (k6 +
load-in-lifecycle) against `--with-rig`. Then **7 scoring**, **8 suite**,
**9 Litmus gate**. Finish with **10 EKS parity** and **11 docs**.

## Working agreement
No commits/pushes until explicitly told; commit messages one line, no attribution.
