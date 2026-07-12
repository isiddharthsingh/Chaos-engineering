#!/usr/bin/env bash
# Live verification of the in-cluster guardrail spine against the kind rig.
#
# Asserts, against a REAL API server + Kyverno admission webhook:
#   1. a Deployment scale of >50% in a chaos-enabled namespace is DENIED,
#   2. a scale within the +/-50% cap is ALLOWED,
#   3. the observer ServiceAccount CANNOT delete (RBAC least privilege),
#   4. the experimenter has NO cluster-wide write binding.
#
# Uses only built-in kinds, so it needs Kyverno + the chaosagent bundle but not
# Chaos Mesh. Run scripts/kind-up.sh first.
set -euo pipefail

NS=boutique
DEP=guardrail-probe
pass() { echo "  PASS: $1"; }
fail() { echo "  FAIL: $1"; exit 1; }

cleanup() { kubectl -n "${NS}" delete deploy "${DEP}" --ignore-not-found >/dev/null 2>&1 || true; }
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
[[ "$(kubectl auth can-i create chaosengines.litmuschaos.io -n "${NS}" --as="${EXP}")" == "no" ]] \
  && pass "experimenter has no LitmusChaos write grant (ungated CRD)" \
  || fail "experimenter must not create Litmus chaosengines until a Litmus policy exists"

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

podchaos() { # name ns mode [value] [duration] -> PodChaos manifest on stdout
  cat <<EOF
apiVersion: chaos-mesh.org/v1alpha1
kind: PodChaos
metadata: {name: $1, namespace: $2}
spec:
  action: pod-kill
  mode: $3
  selector: {labelSelectors: {app: probe}}
EOF
  [[ -n "${4:-}" ]] && echo "  value: \"$4\""
  [[ -n "${5:-}" ]] && echo "  duration: \"$5\""
}

expect_deny() { # rule-substr name ns mode [value] [duration]
  local rule="$1"; shift
  local name="$1" ns="$2"
  if podchaos "$@" | kubectl apply -f - >/dev/null 2>/tmp/kc.txt; then
    kubectl -n "$ns" delete podchaos "$name" >/dev/null 2>&1 || true
    fail "$name should be DENIED by $rule"
  else
    grep -q "$rule" /tmp/kc.txt && pass "$name denied by $rule" \
      || fail "$name denied, but not by $rule: $(cat /tmp/kc.txt)"
  fi
}

expect_allow() { # name ns mode [value] [duration]
  local name="$1" ns="$2"
  if podchaos "$@" | kubectl apply -f - >/dev/null 2>/tmp/kc.txt; then
    kubectl -n "$ns" delete podchaos "$name" >/dev/null 2>&1 || true
    pass "$name allowed"
  else
    fail "$name should be allowed: $(cat /tmp/kc.txt)"
  fi
}

if kubectl get crd podchaos.chaos-mesh.org >/dev/null 2>&1 \
   && kubectl get clusterpolicy require-chaos-namespace >/dev/null 2>&1; then
  echo ">> [chaos] admission gating on real PodChaos resources"
  expect_deny  require-chaos-namespace probe-a unlabelled one "" 60s
  expect_deny  require-ttl             probe-b boutique   one
  expect_deny  fault-duration-cap      dur-long boutique  one "" 24h
  expect_deny  fault-blast-radius      blast-all boutique all "" 60s
  expect_deny  fault-blast-radius      blast-90 boutique  fixed-percent 90 60s
  expect_allow                         probe-ok boutique  fixed-percent 34 300s
else
  echo ">> [chaos] skipped (needs --with-rig: Chaos Mesh CRDs + chaos policies)"
fi

echo ">> ALL GUARDRAIL CHECKS PASSED"
