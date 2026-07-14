#!/usr/bin/env bash
# Bring up the local verification rig on kind.
#
# Base (fast, always): a kind cluster, the namespace layout, Kyverno, and the
# chaosagent RBAC + policy bundle. This is enough to live-verify the guardrail
# spine (Phase 0).
#
# Full rig (--with-rig, slower): also installs kube-prometheus-stack, Chaos Mesh,
# k6-operator, OpenCost (the Phase 3 cost signal), and the Online Boutique demo
# app for the chaos + capacity loops.
#
# Usage: scripts/kind-up.sh [--with-rig]
set -euo pipefail

CLUSTER="${CHAOSAGENT_CLUSTER:-chaosagent}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WITH_RIG=0
[[ "${1:-}" == "--with-rig" ]] && WITH_RIG=1

# Kyverno refreshes API discovery on a delay after a CRD lands; a policy naming
# the new kind is rejected until then. Retry instead of failing the bring-up.
apply_policy_with_retry() { # file
  for _ in 1 2 3 4 5 6; do
    if kubectl apply -f "$1"; then return 0; fi
    echo "   (kyverno discovery not ready yet; retrying in 10s)"
    sleep 10
  done
  echo "failed to apply $1 after retries"
  return 1
}

echo ">> creating kind cluster '${CLUSTER}'"
if ! kind get clusters | grep -qx "${CLUSTER}"; then
  kind create cluster --name "${CLUSTER}" --wait 120s
fi
kubectl config use-context "kind-${CLUSTER}"

echo ">> creating namespaces"
kubectl create namespace boutique --dry-run=client -o yaml | kubectl apply -f -
# The opt-in label that gates chaos. boutique is enabled; 'unlabelled' is not.
kubectl label namespace boutique chaos-enabled=true --overwrite
kubectl create namespace unlabelled --dry-run=client -o yaml | kubectl apply -f -

echo ">> installing Kyverno (admission-side guardrails)"
# Server-side apply: Kyverno's CRDs exceed the 262144-byte client-side
# last-applied-configuration annotation limit.
kubectl apply --server-side --force-conflicts \
  -f https://github.com/kyverno/kyverno/releases/download/v1.13.4/install.yaml
kubectl -n kyverno rollout status deploy/kyverno-admission-controller --timeout=180s

echo ">> installing the LitmusChaos ChaosEngine CRD (gate target only; no operator)"
# Just the CRD, pinned: Kyverno rejects a policy naming a kind that does not
# resolve. Installed in the BASE path because the experimenter RBAC below
# grants litmuschaos.io writes — the admission gate must exist first.
kubectl apply --server-side --force-conflicts \
  -f https://raw.githubusercontent.com/litmuschaos/chaos-operator/3.19.0/deploy/crds/chaosengine_crd.yaml

echo ">> applying the Litmus admission gate BEFORE the RBAC grant (order matters)"
apply_policy_with_retry "${ROOT}/config/policies/kyverno/chaos/require-chaos-namespace-litmus.yaml"

echo ">> applying chaosagent RBAC + base policy bundle"
kubectl apply -f "${ROOT}/config/rbac/00-namespace-and-serviceaccounts.yaml"
kubectl apply -f "${ROOT}/config/rbac/01-observer-clusterrole.yaml"
kubectl apply -f "${ROOT}/config/rbac/02-experimenter-role.yaml"
# Built-in-kind policies apply now; chaos-CR policies need Chaos Mesh CRDs first
# (applied in the --with-rig branch below).
kubectl apply -f "${ROOT}/config/policies/kyverno/cap-replica-change.yaml"
# The HPA bounds cap ships BEFORE the experimenter's autoscaling grant above is
# usable in anger (gate before grant; test_manifests.py enforces the pairing).
kubectl apply -f "${ROOT}/config/policies/kyverno/cap-hpa-bounds.yaml"

if [[ "${WITH_RIG}" == "1" ]]; then
  echo ">> installing kube-prometheus-stack"
  helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
  helm repo update >/dev/null
  helm upgrade --install kps prometheus-community/kube-prometheus-stack \
    -n monitoring --create-namespace --wait --timeout 10m

  echo ">> installing Chaos Mesh"
  helm repo add chaos-mesh https://charts.chaos-mesh.org >/dev/null 2>&1 || true
  helm repo update >/dev/null
  helm upgrade --install chaos-mesh chaos-mesh/chaos-mesh \
    -n chaos-mesh --create-namespace \
    --set chaosDaemon.runtime=containerd \
    --set chaosDaemon.socketPath=/run/containerd/containerd.sock \
    --set dnsServer.create=true \
    --wait --timeout 10m

  echo ">> applying chaos-CR Kyverno policies (Chaos Mesh + Litmus CRDs now exist)"
  kubectl apply -f "${ROOT}/config/policies/kyverno/chaos/"

  echo ">> installing k6-operator (load during faults)"
  helm repo add grafana https://grafana.github.io/helm-charts >/dev/null 2>&1 || true
  helm repo update >/dev/null
  helm upgrade --install k6-operator grafana/k6-operator \
    -n k6-operator --create-namespace --wait --timeout 5m

  echo ">> applying k6 load Kyverno policy (k6 CRDs now exist)"
  apply_policy_with_retry "${ROOT}/config/policies/kyverno/load/require-chaos-namespace-k6.yaml"

  echo ">> installing OpenCost (Phase 3 cost signal), wired to the kps Prometheus"
  helm repo add opencost https://opencost.github.io/opencost-helm-chart >/dev/null 2>&1 || true
  helm repo update >/dev/null
  helm upgrade --install opencost opencost/opencost \
    -n opencost --create-namespace \
    --set opencost.prometheus.internal.serviceName=kps-kube-prometheus-stack-prometheus \
    --set opencost.prometheus.internal.namespaceName=monitoring \
    --set opencost.prometheus.internal.port=9090 \
    --wait --timeout 5m

  echo ">> deploying Online Boutique into 'boutique'"
  kubectl apply -n boutique -f \
    https://raw.githubusercontent.com/GoogleCloudPlatform/microservices-demo/main/release/kubernetes-manifests.yaml
fi

echo ">> rig is up. verify with: scripts/verify-guardrails.sh"
