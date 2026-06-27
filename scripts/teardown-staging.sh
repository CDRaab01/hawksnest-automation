#!/usr/bin/env bash
#
# teardown-staging.sh — delete the throwaway staging namespace and everything in it.
#
# Companion to `OVERLAY=staging ./scripts/deploy.sh`. Removes the smoke-test stack
# (pods + Services + the local-path PVCs, which are node-local and regenerable). It
# is **hard-scoped to `home-automation-staging`** and refuses to touch anything else —
# in particular it can never delete the live `home-automation` (prod) namespace.
#
# Runs on the self-hosted Dragonfly runner (see .github/workflows/teardown-staging.yml)
# or by hand:  ./scripts/teardown-staging.sh
#
# Environment knobs (all optional):
#   KUBECONFIG   kubeconfig path (default: ~/.kube/config)
#   CONFIRM      if set, must equal "home-automation-staging" or the run aborts
#                (a typo-guard for the Actions button; leave empty to just proceed)
#
set -euo pipefail

NS="home-automation-staging"
export KUBECONFIG="${KUBECONFIG:-${HOME}/.kube/config}"

log()  { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
die()  { printf '\n\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

# --- safety: this script may ONLY ever delete the staging namespace ------------
[ "${NS}" = "home-automation-staging" ] || die "refusing: NS='${NS}' is not the staging namespace."
[ "${NS}" != "home-automation" ] || die "refusing to delete the PROD namespace."

# Optional typed confirmation from the workflow input.
if [ -n "${CONFIRM:-}" ] && [ "${CONFIRM}" != "${NS}" ]; then
  die "CONFIRM='${CONFIRM}' does not match '${NS}' — aborting (no changes made)."
fi

command -v kubectl >/dev/null 2>&1 || die "kubectl not found on PATH."
[ -f "${KUBECONFIG}" ] || die "KUBECONFIG not found at ${KUBECONFIG}."
kubectl cluster-info >/dev/null 2>&1 \
  || die "cannot reach the cluster with KUBECONFIG=${KUBECONFIG}."

if ! kubectl get ns "${NS}" >/dev/null 2>&1; then
  log "Namespace ${NS} does not exist — nothing to tear down."
  exit 0
fi

log "Deleting namespace ${NS} (its local-path PVCs go with it; prod NFS is untouched)."
kubectl delete ns "${NS}" --wait=true --timeout=120s

log "Staging torn down. Prod (home-automation) was not touched."
