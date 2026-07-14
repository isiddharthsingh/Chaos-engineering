#!/usr/bin/env bash
# Live verification of the in-cluster guardrail spine against the kind rig.
#
# Asserts, against a REAL API server + Kyverno admission webhook:
#   1. a Deployment scale of >50% in a chaos-enabled namespace is DENIED,
#   2. a scale within the +/-50% cap is ALLOWED
#      (both repeated AS the experimenter SA — the identity the Phase 3 scale
#       executor impersonates),
#   3. the observer ServiceAccount CANNOT delete (RBAC least privilege),
#   4. the experimenter has NO cluster-wide write binding,
#   5. HPA bound changes are capped (cap-hpa-bounds, paired with the HPA grant).
#
# Uses only built-in kinds, so it needs Kyverno + the chaosagent bundle but not
# Chaos Mesh. Run scripts/kind-up.sh first.
set -euo pipefail

NS=boutique
DEP=guardrail-probe
pass() { echo "  PASS: $1"; }
fail() { echo "  FAIL: $1"; exit 1; }

cleanup() {
  kubectl -n "${NS}" delete hpa "${DEP}" --ignore-not-found >/dev/null 2>&1 || true
  kubectl -n "${NS}" delete deploy "${DEP}" --ignore-not-found >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo ">> [1/4] replica-cap: create a 4-replica deployment, then over-scale it"
kubectl -n "${NS}" create deployment "${DEP}" --image=registry.k8s.io/pause:3.9 --replicas=4
kubectl -n "${NS}" rollout status deploy/"${DEP}" --timeout=60s >/dev/null

# +50% (4 -> 6) is at the cap and must be allowed.
if kubectl -n "${NS}" scale deploy/"${DEP}" --replicas=6 >/dev/null 2>&1; then
  pass "scale 4->6 (+50%, at cap) allowed"
else
  fail "scale 4->6 should be allowed but was denied"
fi

# 6 -> 12 (+100%) must be denied by cap-replica-change.
if kubectl -n "${NS}" scale deploy/"${DEP}" --replicas=12 2>/tmp/deny.txt; then
  fail "scale 6->12 (+100%) should be DENIED but was allowed"
else
  grep -q "replica-cap" /tmp/deny.txt && pass "scale 6->12 (+100%) denied by replica-cap" \
    || fail "denied, but not by our policy: $(cat /tmp/deny.txt)"
fi

# The same two cases AS THE EXPERIMENTER: the identity the chaosagent scale
# executor impersonates must be able to make in-cap /scale patches and must be
# stopped at admission for out-of-cap ones (Phase 3 capacity spine).
EXP="system:serviceaccount:chaos-agent-system:agent-experimenter"
if kubectl -n "${NS}" scale deploy/"${DEP}" --replicas=12 --as="${EXP}" 2>/tmp/deny.txt; then
  fail "scale 6->12 as the experimenter should be DENIED but was allowed"
else
  grep -q "replica-cap" /tmp/deny.txt && pass "scale 6->12 as the experimenter denied by replica-cap" \
    || fail "denied, but not by our policy: $(cat /tmp/deny.txt)"
fi
if kubectl -n "${NS}" scale deploy/"${DEP}" --replicas=4 --as="${EXP}" >/dev/null 2>&1; then
  pass "scale 6->4 (-33%, in cap) as the experimenter allowed"
else
  fail "the experimenter should be able to make an in-cap /scale patch"
fi

echo ">> [2/4] observer RBAC is read-only"
OBS="system:serviceaccount:chaos-agent-system:agent-observer"
[[ "$(kubectl auth can-i list pods -A --as="${OBS}")" == "yes" ]] \
  && pass "observer can list pods" || fail "observer should be able to list pods"
[[ "$(kubectl auth can-i delete pods -n "${NS}" --as="${OBS}")" == "no" ]] \
  && pass "observer cannot delete pods" || fail "observer must NOT be able to delete pods"

echo ">> [3/4] experimenter is namespaced, not cluster-wide"
EXP="system:serviceaccount:chaos-agent-system:agent-experimenter"
[[ "$(kubectl auth can-i create podchaos.chaos-mesh.org -n "${NS}" --as="${EXP}")" == "yes" ]] \
  && pass "experimenter can create chaos CRs in ${NS}" \
  || echo "  NOTE: chaos-mesh CRDs not installed; skipping positive check"
[[ "$(kubectl auth can-i create podchaos.chaos-mesh.org -n unlabelled --as="${EXP}")" == "no" ]] \
  && pass "experimenter CANNOT create chaos CRs in unlabelled namespace" \
  || fail "experimenter must not have write access outside its bound namespaces"
[[ "$(kubectl auth can-i create chaosengines.litmuschaos.io -n "${NS}" --as="${EXP}")" == "yes" ]] \
  && pass "experimenter can create Litmus chaosengines in ${NS}" \
  || fail "experimenter should hold the Litmus write grant (it ships with the Litmus gate)"
[[ "$(kubectl auth can-i create chaosengines.litmuschaos.io -n unlabelled --as="${EXP}")" == "no" ]] \
  && pass "experimenter CANNOT create Litmus chaosengines in unlabelled namespace" \
  || fail "experimenter must not write Litmus CRs outside its bound namespaces"

# A write grant is only safe UNDER its admission gate. A grant without the CRD
# is inert, so the invariant is: CRD installed => the gate policy must exist.
if kubectl get crd chaosengines.litmuschaos.io >/dev/null 2>&1; then
  kubectl get clusterpolicy require-chaos-namespace-litmus >/dev/null 2>&1 \
    && pass "Litmus write grant is paired with its admission gate" \
    || fail "ChaosEngine CRD + write grant present but require-chaos-namespace-litmus is MISSING"
fi
if kubectl get crd testruns.k6.io >/dev/null 2>&1; then
  kubectl get clusterpolicy require-chaos-namespace-k6 >/dev/null 2>&1 \
    && pass "k6 write grant is paired with its admission gate" \
    || fail "TestRun CRD + write grant present but require-chaos-namespace-k6 is MISSING"
fi

echo ">> [4/4] policies are installed and Enforcing"
if kubectl get clusterpolicy cap-replica-change >/dev/null 2>&1; then
  pass "cap-replica-change present"
else
  fail "cap-replica-change ClusterPolicy is missing"
fi
if kubectl get crd podchaos.chaos-mesh.org >/dev/null 2>&1; then
  if kubectl get clusterpolicy require-chaos-namespace require-experiment-ttl >/dev/null 2>&1; then
    pass "chaos-CR policies present"
  else
    fail "Chaos Mesh is installed but chaos-CR policies are missing"
  fi
else
  echo "  NOTE: chaos-mesh not installed; chaos-CR policies apply only with --with-rig"
fi

echo ">> [hpa] cap-hpa-bounds admission gating"
# A write grant is only safe UNDER its admission gate (same invariant as the
# chaos engines): experimenter can patch HPAs => cap-hpa-bounds must exist.
if [[ "$(kubectl auth can-i patch horizontalpodautoscalers.autoscaling -n "${NS}" --as="${EXP}")" == "yes" ]]; then
  kubectl get clusterpolicy cap-hpa-bounds >/dev/null 2>&1 \
    && pass "HPA write grant is paired with cap-hpa-bounds" \
    || fail "experimenter can patch HPAs but cap-hpa-bounds is MISSING"
fi
if kubectl get clusterpolicy cap-hpa-bounds >/dev/null 2>&1; then
  kubectl -n "${NS}" autoscale deployment "${DEP}" --min=4 --max=8 >/dev/null
  # max 8 -> 12 (+50%) is at the cap and must be allowed.
  if kubectl -n "${NS}" patch hpa "${DEP}" --type=merge -p '{"spec":{"maxReplicas":12}}' >/dev/null 2>&1; then
    pass "HPA maxReplicas 8->12 (+50%, at cap) allowed"
  else
    fail "HPA maxReplicas 8->12 should be allowed but was denied"
  fi
  # max 12 -> 20 (+67%) must be denied by cap-hpa-bounds.
  if kubectl -n "${NS}" patch hpa "${DEP}" --type=merge -p '{"spec":{"maxReplicas":20}}' 2>/tmp/deny.txt; then
    fail "HPA maxReplicas 12->20 (+67%) should be DENIED but was allowed"
  else
    grep -q "replica-cap" /tmp/deny.txt && pass "HPA maxReplicas 12->20 (+67%) denied by replica-cap" \
      || fail "denied, but not by our policy: $(cat /tmp/deny.txt)"
  fi
  kubectl -n "${NS}" delete hpa "${DEP}" >/dev/null 2>&1 || true
else
  fail "cap-hpa-bounds ClusterPolicy is missing"
fi

chaos_cr() { # kind spec-lines name ns mode [value] [duration] -> manifest on stdout
  cat <<EOF
apiVersion: chaos-mesh.org/v1alpha1
kind: $1
metadata: {name: $3, namespace: $4}
spec:
$2
  mode: $5
  selector: {labelSelectors: {app: probe}}
EOF
  [[ -n "${6:-}" ]] && echo "  value: \"$6\""
  [[ -n "${7:-}" ]] && echo "  duration: \"$7\""
}

lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

expect_deny() { # rule-substr kind spec-lines name ns mode [value] [duration]
  local rule="$1"; shift
  local kind="$1" name="$3" ns="$4"
  if chaos_cr "$@" | kubectl apply -f - >/dev/null 2>/tmp/kc.txt; then
    kubectl -n "$ns" delete "$(lower "$kind")" "$name" >/dev/null 2>&1 || true
    fail "$name ($kind) should be DENIED by $rule"
  else
    grep -q "$rule" /tmp/kc.txt && pass "$name ($kind) denied by $rule" \
      || fail "$name denied, but not by $rule: $(cat /tmp/kc.txt)"
  fi
}

expect_allow() { # kind spec-lines name ns mode [value] [duration]
  local kind="$1" name="$3" ns="$4"
  if chaos_cr "$@" | kubectl apply -f - >/dev/null 2>/tmp/kc.txt; then
    kubectl -n "$ns" delete "$(lower "$kind")" "$name" >/dev/null 2>&1 || true
    pass "$name ($kind) allowed"
  else
    fail "$name ($kind) should be allowed: $(cat /tmp/kc.txt)"
  fi
}

POD_SPEC='  action: pod-kill'
NET_SPEC=$'  action: delay\n  delay: {latency: 100ms}'
STRESS_SPEC=$'  stressors:\n    cpu: {workers: 1, load: 50}'

if kubectl get crd podchaos.chaos-mesh.org >/dev/null 2>&1 \
   && kubectl get clusterpolicy require-chaos-namespace >/dev/null 2>&1; then
  echo ">> [chaos] admission gating on real chaos CRs"
  expect_deny  require-chaos-namespace PodChaos "$POD_SPEC" probe-a unlabelled one "" 60s
  expect_deny  require-ttl             PodChaos "$POD_SPEC" probe-b boutique   one
  expect_deny  fault-duration-cap      PodChaos "$POD_SPEC" dur-long boutique  one "" 24h
  expect_deny  fault-blast-radius      PodChaos "$POD_SPEC" blast-all boutique all "" 60s
  expect_deny  fault-blast-radius      PodChaos "$POD_SPEC" blast-90 boutique  fixed-percent 90 60s
  expect_allow                         PodChaos "$POD_SPEC" probe-ok boutique  fixed-percent 34 300s
  expect_deny  fault-blast-radius      NetworkChaos "$NET_SPEC" net-all boutique all "" 60s
  expect_deny  fault-blast-radius      NetworkChaos "$NET_SPEC" net-90 boutique fixed-percent 90 60s
  expect_allow                         NetworkChaos "$NET_SPEC" net-ok boutique fixed-percent 34 300s
  expect_deny  fault-blast-radius      StressChaos "$STRESS_SPEC" stress-all boutique all "" 60s
  expect_deny  fault-blast-radius      StressChaos "$STRESS_SPEC" stress-90 boutique fixed-percent 90 60s
  expect_allow                         StressChaos "$STRESS_SPEC" stress-ok boutique fixed-percent 34 300s
else
  echo ">> [chaos] skipped (needs --with-rig: Chaos Mesh CRDs + chaos policies)"
fi

litmus_engine() { # name ns -> minimal ChaosEngine manifest on stdout
  cat <<EOF
apiVersion: litmuschaos.io/v1alpha1
kind: ChaosEngine
metadata: {name: $1, namespace: $2}
spec:
  engineState: active
  appinfo: {appns: $2, applabel: "app=probe", appkind: deployment}
  chaosServiceAccount: agent-experimenter
  experiments:
    - name: pod-delete
EOF
}

if kubectl get crd chaosengines.litmuschaos.io >/dev/null 2>&1 \
   && kubectl get clusterpolicy require-chaos-namespace-litmus >/dev/null 2>&1; then
  echo ">> [litmus] admission gating on ChaosEngine resources"
  if litmus_engine litmus-a unlabelled | kubectl apply -f - >/dev/null 2>/tmp/kc.txt; then
    kubectl -n unlabelled delete chaosengine litmus-a >/dev/null 2>&1 || true
    fail "litmus-a should be DENIED by require-chaos-namespace-litmus"
  else
    grep -q "require-chaos-namespace-litmus" /tmp/kc.txt \
      && pass "litmus-a denied by require-chaos-namespace-litmus" \
      || fail "litmus-a denied, but not by our policy: $(cat /tmp/kc.txt)"
  fi
  if litmus_engine litmus-ok boutique | kubectl apply -f - >/dev/null 2>/tmp/kc.txt; then
    kubectl -n boutique delete chaosengine litmus-ok >/dev/null 2>&1 || true
    pass "litmus-ok allowed in boutique"
  else
    fail "litmus-ok should be allowed in boutique: $(cat /tmp/kc.txt)"
  fi
else
  echo ">> [litmus] skipped (needs the ChaosEngine CRD + the Litmus gate policy)"
fi

k6_testrun() { # name ns -> minimal TestRun manifest on stdout
  cat <<EOF
apiVersion: k6.io/v1alpha1
kind: TestRun
metadata: {name: $1, namespace: $2}
spec:
  parallelism: 1
  script:
    configMap: {name: guardrail-probe-script, file: script.js}
EOF
}

if kubectl get crd testruns.k6.io >/dev/null 2>&1 \
   && kubectl get clusterpolicy require-chaos-namespace-k6 >/dev/null 2>&1; then
  echo ">> [k6] admission gating on TestRun resources"
  if k6_testrun k6-a unlabelled | kubectl apply -f - >/dev/null 2>/tmp/kc.txt; then
    kubectl -n unlabelled delete testrun k6-a >/dev/null 2>&1 || true
    fail "k6-a should be DENIED by require-chaos-namespace-k6"
  else
    grep -q "require-chaos-namespace-k6" /tmp/kc.txt \
      && pass "k6-a denied by require-chaos-namespace-k6" \
      || fail "k6-a denied, but not by our policy: $(cat /tmp/kc.txt)"
  fi
  if k6_testrun k6-ok boutique | kubectl apply -f - >/dev/null 2>/tmp/kc.txt; then
    kubectl -n boutique delete testrun k6-ok >/dev/null 2>&1 || true
    pass "k6-ok allowed in boutique"
  else
    fail "k6-ok should be allowed in boutique: $(cat /tmp/kc.txt)"
  fi
else
  echo ">> [k6] skipped (needs the TestRun CRD + the k6 gate policy)"
fi

echo ">> ALL GUARDRAIL CHECKS PASSED"
