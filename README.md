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
         -> observability auto-abort       (src/chaosagent/observe)
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
| `src/chaosagent/agents` | MCP wiring, permission gate + action binding, observer/planner harnesses |
| `src/chaosagent/faults` | FaultSpec -> Chaos Mesh CR composers; `compose_cr` dispatches every fault family (Kyverno-compatible by construction) |
| `src/chaosagent/load` | LoadSpec -> k6 `TestRun` composer (load applied during a fault) |
| `src/chaosagent/observe` | Prometheus client, steady-state hypotheses, observe loop (auto-abort) |
| `src/chaosagent/execute` | Gate-checked executor (server-side dry-run, ungated abort delete) |
| `src/chaosagent/experiment` | Experiment spec, lifecycle state machine, `run` runner, GameDay `suite` runner |
| `src/chaosagent/analyze` | Probe-based resilience score (pinned default rubric) + deterministic fix suggestions |
| `src/chaosagent/capacity` | Capacity spec, deterministic recommender + signals (utilization, OpenCost, VPA), auto-reverting scale lifecycle, `recommend`/`scale` runners |
| `config/policies` | `engine.yaml` (source of truth) + Kyverno admission bundle |
| `config/rbac` | Tiered ServiceAccounts + least-privilege Roles |
| `scripts` | Local kind rig bring-up / teardown |
| `examples` | Sample target, action, and experiment JSON documents |

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

# Run one autonomous experiment end to end on the local rig:
scripts/kind-up.sh --with-rig      # Kyverno + Chaos Mesh + Prometheus + Boutique
kubectl -n monitoring port-forward svc/kps-kube-prometheus-stack-prometheus 9090:9090 &
uv run chaosagent run --target kind-local \
    --spec examples/experiment-cartservice.json --dry-run   # pre-flight only
uv run chaosagent run --target kind-local \
    --spec examples/experiment-cartservice.json             # the full loop
# exit codes: 0 verified | 2 policy denied | 3 auto-aborted on SLO breach

# Capacity (Phase 3): recommend is read-only; scale keeps a verified change
# and deterministically auto-reverts on a settle-window breach.
kubectl -n opencost port-forward svc/opencost 9003:9003 &   # optional cost signal
uv run chaosagent recommend --target kind-local --namespace boutique \
    --workload deployment/cartservice --opencost-url http://localhost:9003
# Stock cartservice runs 1 replica, and no change from 1 fits the +/-50% cap —
# cap-replica-change matches every /scale update in chaos-enabled namespaces,
# humans included. Step outside the guardrail deliberately to set the starting
# point: unlabel, scale, relabel.
kubectl label namespace boutique chaos-enabled-
kubectl -n boutique scale deploy/cartservice --replicas=2
kubectl label namespace boutique chaos-enabled=true --overwrite
uv run chaosagent scale --target kind-local \
    --spec examples/capacity-cartservice.json               # 2 -> 3 (+50%)
# exit codes: 0 change kept | 2 policy denied | 3 auto-reverted | 1 error
```

## Safety gate (release-gating test)

`tests/test_safety_gate.py` asserts the invariants that make "fully autonomous"
defensible, against the **shipped** policy config:

- a fault **cannot** execute outside a `chaos-enabled=true` namespace,
- a capacity action **cannot** exceed the replica cap (±50%),
- every admitted capacity change is **revertible under the same caps** that
  admitted it (`revert-admissible`), so the auto-revert can never be blocked
  by our own guardrails,
- **no** state-changing action can reach a prod target,
- the loop **auto-aborts** within one observe interval of a synthetic SLO breach,
  deleting the fault CR before any subsequent sleep,
- an **unbound write cannot reach the cluster** (zero API calls),
- the engine is **deterministic**.

## Status

- **Phase 0 — Foundations + guardrail spine — ✅ complete.** Registry, deterministic
  policy engine, Kyverno + RBAC bundle, read-only Claude Agent SDK harness.
- **Phase 1 — Autonomous chaos MVP on kind — ✅ complete.** One command drives
  intent/spec → pre-flight (engine + live Kyverno dry-run) → baseline → PodChaos
  inject → PromQL observe loop → deterministic auto-abort on SLO breach →
  rollback → resilience score + fixes. LLM-free `--spec` path included.
- **Phase 2 — Fault library + load + scoring — code complete; EKS parity pending.**
  `compose_cr` dispatches every fault family (pod / network / stress / io / dns /
  time) to a Kyverno-compatible composer; optional k6 `TestRun` load rides the
  fault's policy binding and is torn down with it; Litmus-style probe kinds feed
  a weighted scoring rubric whose default pins the Phase-1 formula; `chaosagent
  suite` runs GameDays sequentially (stops on abort or error by default); k6 and Litmus
  `ChaosEngine` each have their own `chaos-enabled` admission gate. Remaining:
  the cloud-parity run on EKS (`examples/target-eks-staging.json`, IRSA).
- **Phase 3 — Autonomous capacity planning — code complete; rig verification
  pending.** The second action family through the same spine: `chaosagent
  recommend` (read-only, deterministic proportional sizing clamped to the caps,
  OpenCost cost delta and VPA targets as advisory signals) and `chaosagent
  scale` (PREFLIGHT engine + `/scale` server-side dry-run → baseline → apply →
  settle-window observe → keep on green / **auto-revert on the breaching
  tick**). A new engine rule (`revert-admissible`) refuses any change whose
  inverse would breach the replica cap, so every autonomous change is
  revertible by construction; the HPA bounds admission cap ships before the
  experimenter's HPA grant (gate before grant, pairing-tested). Remaining: the
  live kind-rig verification pass.
- Phase 4: multi-cloud + VMs + Temporal durability + prod escalation +
  VPA/KEDA/Karpenter writes.

See [`docs/architecture.md`](docs/architecture.md) for the full design and
[`docs/roadmap.md`](docs/roadmap.md) for the phase build plans (components
mapped to files, verification, and definition of done).
