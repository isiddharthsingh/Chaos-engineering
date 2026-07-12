#!/usr/bin/env bash
# Tear down the local kind rig.
set -euo pipefail
CLUSTER="${CHAOSAGENT_CLUSTER:-chaosagent}"
kind delete cluster --name "${CLUSTER}"
