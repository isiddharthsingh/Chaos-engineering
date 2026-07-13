# Phase 3 — Autonomous capacity planning (build plan)

This is the build-ready spec for Phase 3. Point a fresh session at this file and
start at step 1. It is the Phase-3 analogue of the Phase-1/2 plans: numbered TDD
steps, exact files and signatures, each gated by
`uv run pytest && uv run ruff check && uv run mypy` (green **with and without**
the `agent` extra). The high-level roadmap lives in [`roadmap.md`](roadmap.md);
the design rationale in [`architecture.md`](architecture.md).

## Context

Phases 0–2 shipped the guardrail spine and the full chaos loop: typed intent →
`resolve_action` → `PolicyEngine` → server-side dry-run (Kyverno) → executor,
with deterministic auto-abort beneath the LLM. Phase 2 added the fault library,
k6 load, probe scoring, and GameDay suites.

Phase 3 adds the **second action family the policy engine already knows**:
capacity. The agent observes utilization, recommends a bounded replica change,
applies it through the same spine, verifies the steady state still holds, and
**auto-reverts deterministically** on breach — the capacity analogue of
auto-abort. Cost enters as a signal (OpenCost), never as an authority.

**Invariant preserved (unchanged since Phase 0):** the LLM only emits typed
intent; everything destructive goes resolve → engine → server-side dry-run →
executor. The auto-revert, like the abort delete, is deterministic and beneath
the LLM.

## What already exists for capacity (reuse, don't rebuild)

| Capability | Where |
|---|---|
| `ActionType.SCALE_WORKLOAD` / `RIGHT_SIZE` (state-changing, **not** chaos — chaos-namespace/ttl/single-experiment rules do not apply; env-scope, namespace-scope, replica-cap, incident-freeze do) | `src/chaosagent/domain/enums.py` |
| `ReplicaChange` (current/desired, `pct_change`, scale-from-zero = unbounded) + `ProposedAction` requires it for capacity actions | `src/chaosagent/domain/actions.py` |
| `replica-cap` engine rule + `max_replica_pct_change: 0.5` | `src/chaosagent/policy/engine.py`, `config/policies/engine.yaml` |
| Kyverno `cap-replica-change`: matches Deployment/StatefulSet **and their `/scale` subresource**, UPDATE, in `chaos-enabled=true` namespaces; denies >±50% and any scale-from-zero | `config/policies/kyverno/cap-replica-change.yaml` |
| Experimenter RBAC: `deployments/scale`, `statefulsets/scale` get/patch/update; parents get/list/watch | `config/rbac/02-experimenter-role.yaml` |
| PermissionGate single-slot binding (bound action **must declare ttl_seconds**) | `src/chaosagent/agents/permission.py` |
| Prometheus client, `SteadyStateHypothesis`, `observe_until`/`sample_window` | `src/chaosagent/observe/` |
| Registry / `resolve_action` / incident probe / impersonated API builder | `registry/`, `resolve.py`, `experiment/runner.py`, `execute/kubernetes.py` |
| Report conventions (pinned deterministic scoring, `render_text`), CLI + exit-code pattern, shared fakes | `analyze/`, `cli.py`, `tests/fakes.py` |

**Load-bearing facts:**
- The admission cap matches the `/scale` subresource, so the executor's
  server-side dry-run of a scale patch IS the live policy self-check — same
  mechanism as chaos CRs.
- Capacity writes are confined to `chaos-enabled=true` namespaces in practice:
  the experimenter Role only exists there and the Kyverno cap only matches
  there. The engine does not need a new namespace rule.
- **Revert admissibility is NOT free.** A −50% downscale (4→2) is allowed, but
  its revert (2→4) is +100% — denied by both the engine and Kyverno. Step 2
  makes reverts admissible *by construction*; without it the auto-revert path
  can be blocked by our own guardrails.

## Global design decisions

- **New bounded context `src/chaosagent/capacity/`** (spec, recommender,
  lifecycle, report). The executor joins the existing `execute/` package.
- **Revert-admissible by construction.** A new engine rule refuses any
  autonomous capacity change whose *inverse* would breach `replica-cap`
  (downscale floor: `desired >= current / (1 + cap)`, i.e. at most −33% at the
  0.5 cap). Engine-only — no Kyverno twin, because an admission twin would also
  constrain human operators, who can revert in two steps.
- **The recommender is deterministic and beneath the LLM** — pure math from
  utilization signals, clamped to the caps. An LLM capacity-planner (typed
  intent only) is a later step and optional; the `--spec` path ships first,
  exactly like Phase 1.
- **Auto-revert mirrors the abort delete:** not gate-checked (moving toward the
  recorded known-good state must not be blockable by an expired binding), only
  ever writes `applied.previous`, and is admissible by construction (rule
  above). Kyverno still sees it — that is belt and suspenders, not a bypass.
- **Success keeps the change; breach reverts it.** A verified right-size is the
  deliverable, not an experiment to undo.
- **Cost is a signal, not an authority.** OpenCost lookups feed the report and
  the recommendation rationale; a cost number can never *raise* a cap.
- **Recommend-only for VPA / Karpenter / KEDA in this phase.** Karpenter
  `NodePool` is cluster-scoped and would break the namespaced-write invariant;
  VPA/KEDA writes need their own gates. Phase 3 reads their signals and emits
  recommendations; writes are Phase 4 decisions.
- No new hard dependencies (OpenCost speaks HTTP via `httpx`; the k8s client
  stays in the `agent` extra). Sync core / async edge unchanged. TDD, test file
  first, hand-rolled fakes only (extend `tests/fakes.py`).

---

## Build steps

### 1. Capacity spec (pure domain)
New: `src/chaosagent/capacity/{__init__,spec}.py`, `tests/test_capacity_spec.py`.

```python
class WorkloadRef(BaseModel):        # frozen, extra="forbid"
    kind: Literal["deployment", "statefulset"]
    name: str                        # DNS-label validated (reuse targets._SLUG_RE style)

class CapacitySpec(BaseModel):       # frozen, extra="forbid"
    title: str
    target_id: str
    namespace: str
    workload: WorkloadRef
    desired_replicas: int = Field(ge=1)   # scale-to-zero refused at the model
    #: Steady state that must hold before and after the change.
    hypotheses: tuple[SteadyStateHypothesis, ...] = Field(min_length=1)
    #: Binding TTL (the gate requires one even though the engine's require-ttl
    #: rule does not apply to capacity actions).
    ttl_seconds: int = Field(gt=0)
    observe_interval_seconds: float = 5.0
    baseline_seconds: int = 30
    #: Post-change window that must stay green before the change is kept.
    settle_seconds: int = 120
```
- Validator: `ttl_seconds > baseline_seconds` (binding must survive to APPLY),
  unique hypothesis names — mirror `ExperimentSpec._validate_shape`.
- **Test:** shape validation, refusals, JSON round-trip (`--spec` format).

### 2. Revert-admissible engine rule
Modify: `src/chaosagent/policy/engine.py`. Extend: `tests/test_policy_engine.py`,
`tests/test_safety_gate.py`.
- New rule `_revert_admissible` (capacity actions with a `replica_change`
  only): deny when the inverse change exceeds the cap —
  `abs((current - desired) / desired) > max_replica_pct_change` (guard
  `desired > 0`; scale-to-zero is already refused). Rule id
  `revert-admissible`, appended to `DEFAULT_RULES`.
- **Test:** 4→3 allowed (revert +33%); 4→2 denied (revert +100%); upscales
  within cap always pass (their inverse is smaller); deterministic. Safety-gate
  addition: *an autonomous capacity change is always revertible under the same
  caps that admitted it*.

### 3. Scale executor (gate-checked apply, ungated bounded revert)
New: `src/chaosagent/execute/scale.py`, `tests/test_scale_executor.py`.
Modify: `src/chaosagent/execute/__init__.py`, `tests/fakes.py` (a
`FakeScaleApi` recording reads/patches with a scripted current-replica count).

```python
class ScaleApiProtocol(Protocol):    # slice of AppsV1Api the executor uses
    def read_scale(self, kind: str, name: str, namespace: str) -> int: ...
    def patch_scale(self, kind: str, name: str, namespace: str, replicas: int,
                    *, dry_run: str | None = None) -> None: ...

@dataclass(frozen=True)
class AppliedScale:
    kind: str; name: str; namespace: str
    previous: int; desired: int; applied_at: float

class ScaleExecutor:
    def read_replicas(self, ref, namespace) -> int: ...
    def dry_run(self, ref, namespace, replicas, binding) -> None: ...
    def apply(self, ref, namespace, replicas, binding) -> AppliedScale: ...
    def revert(self, applied) -> None: ...   # ungated; writes ONLY applied.previous
```
- `_admit` mirrors the chaos executor: gate `authorize_write(namespace)`,
  namespace must match the binding's action. `apply` = dry-run then patch
  (each step fatal). `revert` is not gate-checked (abort philosophy) and is
  idempotent. Denials surface the Kyverno rule id (`_denial_message` reuse).
- A `build_scale_api(...)` twin of `build_experimenter_api` (lazy `kubernetes`
  import, impersonates the experimenter; wraps
  `AppsV1Api.{read,patch}_namespaced_deployment_scale` / statefulset variants).
- **Test:** dry-run precedes patch; gate denial = zero API calls; namespace
  mismatch refused; revert works with an expired binding; revert only ever
  writes `previous`.

### 4. Capacity lifecycle (auto-revert beneath the LLM)
New: `src/chaosagent/capacity/lifecycle.py`, `tests/test_capacity_lifecycle.py`.

States: `PLAN → PREFLIGHT → BASELINE → APPLY → OBSERVE → VERIFY | REVERT →
REPORT → DONE | FAILED`.
- PREFLIGHT: registry get → live probes (`namespace_chaos_enabled` not needed;
  `incident_active` **is** — the engine's incident-freeze covers capacity) →
  `executor.read_replicas` for `current` → build
  `ProposedAction(action_type=SCALE_WORKLOAD, replica_change=ReplicaChange(current, desired), ttl_seconds=...)`
  → `resolve_action` → engine → bind → `executor.dry_run`. Denial at any layer
  = FAILED before any write (same shape as `experiment/lifecycle.py`).
- BASELINE: steady state must hold before changing anything (reuse
  `observe_until`; breach = FAILED, unbind, nothing applied).
- APPLY → OBSERVE over `settle_seconds`: on hypothesis breach,
  `executor.revert(applied)` **on the breaching tick, before any sleep**, then
  REVERT state; else VERIFY (change kept). Rollback path always unbinds; a
  failed revert is recorded in `revert_error` (append, never overwrite).
- `CapacityRun` record mirrors `ExperimentRun` (baseline/settle results,
  `previous_replicas`, `desired_replicas`, `reverted_at`, `revert_error`).
- **Test:** verified change walks the full machine and keeps the new count; a
  breach reverts on the same tick (journal ordering, like
  `test_slo_breach_auto_aborts_before_any_sleep`); revert-inadmissible spec
  (4→2) fails PREFLIGHT with rule id `revert-admissible`; incident freeze
  blocks; binding always released.

### 5. Utilization signals + deterministic recommender
New: `src/chaosagent/capacity/{signals,recommend}.py`,
`tests/test_recommender.py`.
- `signals.py`: PromQL builders + a `WorkloadUsage` snapshot (avg/p95 CPU and
  memory utilization vs requests over a lookback window, current replicas)
  fetched via the existing `ScalarSource` protocol — fakeable with
  `ScriptedPrometheus`.
- `recommend.py`: pure function
  `recommend_replicas(usage: WorkloadUsage, *, target_utilization: float = 0.6, config: PolicyConfig) -> Recommendation`
  — proportional sizing (`ceil(current * observed/target)`), then clamped to
  the replica cap AND the revert-admissible floor, min 1. `Recommendation`
  carries the rationale (inputs, clamps applied) so reports are reproducible.
- **Test:** golden cases (over/under-provisioned, already-right), every clamp
  exercised, determinism (same inputs → same recommendation), never emits a
  change the engine would deny (property-style loop over a grid).

### 6. OpenCost signal (optional, HTTP-only)
New: `src/chaosagent/capacity/opencost.py`, `tests/test_opencost.py`.
- `OpenCostClient(base_url)` via `httpx` (mirror `PrometheusClient`, incl.
  `close()`; TDD with `httpx.MockTransport`):
  `workload_monthly_cost(namespace, workload) -> float | None` (None = no data;
  never raises for missing cost — cost is advisory).
- `Recommendation.estimated_monthly_delta` populated when a client is wired;
  reports render it; absence changes nothing.
- **Rig:** `scripts/kind-up.sh --with-rig` installs OpenCost
  (`helm upgrade --install opencost opencost/opencost -n opencost
  --create-namespace`, wired to the kps Prometheus); README quickstart notes
  the port-forward.

### 7. Capacity report
New: `src/chaosagent/capacity/report.py`, `tests/test_capacity_report.py`.
- `build_capacity_report(run) -> CapacityReport`: previous/desired/final
  replicas, kept-or-reverted, per-phase hypothesis stats (reuse the analyze
  helpers where they fit), recommendation rationale, cost delta, deterministic
  suggestions (`raise-requests`, `lower-requests`, `set-hpa-bounds`,
  `investigate-settle-breach`). `render_text` in the run-report style;
  `revert_error` surfaced like `rollback_error`.
- **Test:** pinned report for a kept change and for a reverted one; no
  suggestion appears without its trigger present.

### 8. CLI: `recommend` (read-only) and `scale` (the loop)
New: `src/chaosagent/capacity/runner.py`, `tests/test_cli_capacity.py`.
Modify: `src/chaosagent/cli.py`.
- `chaosagent recommend --target <id> --namespace <ns> --workload
  deployment/<name> [--prom-url] [--opencost-url] [--output]` — read-only:
  signals → recommender → printed recommendation + rationale (+ cost). Never
  binds, never writes; exit 0 (or 1 on operational error).
- `chaosagent scale --target <id> --spec capacity.json [--dry-run] [--output]`
  — drives the capacity lifecycle. Exit codes mirror `run`: 0 change verified
  and kept · 2 policy/pre-flight denied · 3 auto-reverted · 1 error.
- `CapacitySettings` + `build_capacity_deps` follow `RunSettings` /
  `build_live_deps` exactly (injectable deps for tests; live path builds the
  impersonated scale API + Prometheus + probes).
- **Test:** exit-code mapping over fakes; `--dry-run` stops after preflight
  with zero writes; example spec file validates (add
  `examples/capacity-cartservice.json`).

### 9. HPA bounds (second write family — gate before grant)
New: `config/policies/kyverno/cap-hpa-bounds.yaml`,
`tests/test_hpa_composer.py`, `src/chaosagent/capacity/hpa.py`. Modify:
`config/rbac/02-experimenter-role.yaml`, `src/chaosagent/policy/engine.py`,
`scripts/verify-guardrails.sh`, `tests/test_manifests.py`.
- Order matters (Litmus lesson, enforced by `test_manifests.py` pairing tests):
  ship the Kyverno policy capping `autoscaling/v2 HorizontalPodAutoscaler`
  min/max changes (same ±50% shape, `chaos-enabled` namespaces) **before**
  granting the experimenter `horizontalpodautoscalers` get/patch/update.
- Engine: extend `_replica_cap` semantics to HPA bound changes (reuse
  `ReplicaChange` per bound). `compose_hpa_patch(ref, min, max)` stays a pure
  builder; apply path goes through the same gate + dry-run.
- `verify-guardrails.sh`: deny an out-of-cap HPA patch / allow an in-cap one;
  pairing check `HPA grant ⇒ cap-hpa-bounds present`.
- **Test:** manifests pairing test first (grant without gate fails the build).

### 10. Recommend-only VPA / KEDA / Karpenter signals
Modify: `src/chaosagent/capacity/signals.py`, `docs/architecture.md`.
- If the VPA CRD is present, read `VerticalPodAutoscaler` recommendations
  (observer credentials, read-only) and fold them into `Recommendation`
  rationale; note KEDA/Karpenter equivalents as Phase-4 write targets. No new
  RBAC writes anywhere in this step.
- **Test:** recommendation report includes the VPA signal when supplied by a
  fake; absent CRD changes nothing.

### 11. Rig + docs
Modify: `scripts/kind-up.sh` (OpenCost install under `--with-rig`),
`scripts/verify-guardrails.sh` (impersonated `/scale` allow/deny cases:
deny 6→12 as the experimenter, allow 4→6), `README.md` (status + quickstart),
`docs/roadmap.md` (Phase 3 checkboxes), `docs/architecture.md` (capacity
lifecycle + revert-admissibility + cost-signal sections). Keep every file
under 500 lines.

---

## Verification (rig)

```bash
scripts/kind-up.sh --with-rig          # now also: OpenCost
scripts/verify-guardrails.sh           # spine green incl. /scale + HPA cases
uv run pytest && uv run ruff check && uv run mypy   # with AND without the agent extra
```
1. `chaosagent recommend --target kind-local --namespace boutique --workload
   deployment/cartservice` prints a bounded recommendation with rationale (and
   a cost line once OpenCost is up) and writes nothing.
2. `chaosagent scale --target kind-local --spec examples/capacity-cartservice.json`
   scales within the cap, holds the steady state through `settle_seconds`, and
   keeps the change (exit 0); `kubectl -n boutique get deploy -w` shows exactly
   one transition.
3. **Auto-revert:** a spec whose hypothesis is engineered to breach post-change
   reverts to the previous count on the breaching tick (exit 3).
4. **Negative:** a 4→2 spec is denied at PREFLIGHT by `revert-admissible`; a
   6→12 `/scale` patch as the experimenter is denied by `replica-cap` at
   admission; an HPA patch without the gate cannot exist (pairing test).
5. Incident freeze: with a synthetic firing alert, `scale` exits 2 before any
   write.

## Suggested build order
Steps **1–2** (spec + engine rule) are pure and gate the rest. Then **3–4**
(executor + lifecycle) over fakes, then **5 recommender**, **7 report**,
**8 CLI** — that is the LLM-free MVP loop. Then **6 OpenCost** and **9 HPA**
(gate before grant), **10 signals**, **11 rig + docs**. An LLM
capacity-planner agent (typed `CapacitySpec` out, read-only MCP stack) can
follow as a Phase-3.5 addendum once the deterministic loop is verified.

## Working agreement
No commits/pushes until explicitly told; commit messages one line, no attribution.
