#!/usr/bin/env bash
# Bring up the local verification rig on kind.
#
# Base (fast, always): a kind cluster, the namespace layout, Kyverno, and the
# chaosagent RBAC + policy bundle. This is enough to live-verify the guardrail
# spine (Phase 0).
#
# Full rig (--with-rig, slower): also installs kube-prometheus-stack, Chaos Mesh,
# and the Online Boutique demo app for the Phase 1 chaos loop.
#
# Usage: scripts/kind-up.sh [--with-rig]
set -euo pipefail

CLUSTER="${CHAOSAGENT_CLUSTER:-chaosagent}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WITH_RIG=0
[[ "${1:-}" == "--with-rig" ]] && WITH_RIG=1

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

echo ">> applying chaosagent RBAC + base policy bundle"
kubectl apply -f "${ROOT}/config/rbac/00-namespace-and-serviceaccounts.yaml"
kubectl apply -f "${ROOT}/config/rbac/01-observer-clusterrole.yaml"
kubectl apply -f "${ROOT}/config/rbac/02-experimenter-role.yaml"
# Built-in-kind policies apply now; chaos-CR policies need Chaos Mesh CRDs first
# (applied in the --with-rig branch below).
kubectl apply -f "${ROOT}/config/policies/kyverno/cap-replica-change.yaml"

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
    --wait --timeout 10m

  echo ">> applying chaos-CR Kyverno policies (Chaos Mesh CRDs now exist)"
  kubectl apply -f "${ROOT}/config/policies/kyverno/chaos/"

  echo ">> deploying Online Boutique into 'boutique'"
  kubectl apply -n boutique -f \
    https://raw.githubusercontent.com/GoogleCloudPlatform/microservices-demo/main/release/kubernetes-manifests.yaml
fi

echo ">> rig is up. verify with: scripts/verify-guardrails.sh"
