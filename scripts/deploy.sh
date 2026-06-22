#!/usr/bin/env bash
#
# deploy.sh — apply the Hawksnest kustomize stack to the local K3s cluster.
#
# Designed to run on the self-hosted GitHub Actions runner inside Dragonfly
# (see .github/workflows/deploy.yml), but it is a plain script and works just as
# well by hand:  ./scripts/deploy.sh
#
# It encodes the operational rules from DEPLOYMENT.md so a deploy can't silently
# break them:
#   * Secrets are gitignored, so a fresh checkout has none. They are copied in
#     from a stable on-host directory (HAWKSNEST_SECRETS_DIR) before apply.
#   * `apply -k` resets zwave-js-ui to replicas:1, which crash-loops while the
#     ZWA-2 USB controller is absent. We re-park it at 0 unless explicitly
#     unparked (and the by-id device path has actually been filled in).
#
# Environment knobs (all optional):
#   KUBECONFIG              kubeconfig path        (default: ~/.kube/config)
#   HAWKSNEST_SECRETS_DIR  where the real secrets live on the runner host
#                          (default: ~/hawksnest-secrets) — must contain
#                          mariadb.env and mosquitto.passwd
#   UNPARK_ZWAVE           "true" to let zwave-js-ui run at replicas:1
#                          (default: false → parked at 0)
#   ROLLOUT_TIMEOUT        per-deployment rollout wait (default: 180s)
#
set -euo pipefail

# --- locate the repo root (this script lives in <root>/scripts) ---------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

NS="home-automation"
KUSTOMIZE_DIR="kustomize"
SECRETS_DST="${KUSTOMIZE_DIR}/secrets"
ZWAVE_MANIFEST="${KUSTOMIZE_DIR}/zwave-js-ui/deployment.yaml"

export KUBECONFIG="${KUBECONFIG:-${HOME}/.kube/config}"
HAWKSNEST_SECRETS_DIR="${HAWKSNEST_SECRETS_DIR:-${HOME}/hawksnest-secrets}"
UNPARK_ZWAVE="${UNPARK_ZWAVE:-false}"
ROLLOUT_TIMEOUT="${ROLLOUT_TIMEOUT:-180s}"

log()  { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\n\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\n\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

# --- pre-flight ---------------------------------------------------------------
command -v kubectl >/dev/null 2>&1 || die "kubectl not found on PATH."
[ -f "${KUBECONFIG}" ] || die "KUBECONFIG not found at ${KUBECONFIG}."
kubectl cluster-info >/dev/null 2>&1 \
  || die "Cannot reach the cluster with KUBECONFIG=${KUBECONFIG}. Is K3s up?"

log "Cluster: $(kubectl config current-context 2>/dev/null || echo '?')  (KUBECONFIG=${KUBECONFIG})"

# --- materialize the gitignored secrets onto this checkout --------------------
# The repo only tracks *.example templates; the real secrets are kept on the
# runner host (HAWKSNEST_SECRETS_DIR) and copied into the kustomize tree so the
# secretGenerator can build them. Existing files in the checkout are left alone.
need_secret() {
  local name="$1"
  local dst="${SECRETS_DST}/${name}"
  local src="${HAWKSNEST_SECRETS_DIR}/${name}"
  if [ -f "${dst}" ]; then
    return 0
  fi
  if [ -f "${src}" ]; then
    install -m 0600 "${src}" "${dst}"
    log "Loaded secret ${name} from ${src}"
  else
    die "Missing secret '${name}'. Provide it at ${dst} or ${src}.
       See README.md §'Create the secrets'. Secrets are deliberately kept off
       GitHub; bootstrap them once on the runner host."
  fi
}
mkdir -p "${SECRETS_DST}"
need_secret "mariadb.env"
need_secret "mosquitto.passwd"

# --- validate the build before touching the cluster ---------------------------
log "Validating kustomize build"
kubectl kustomize "${KUSTOMIZE_DIR}" >/dev/null \
  || die "kustomize build failed — fix the manifests before deploying."

# --- apply --------------------------------------------------------------------
log "Applying ${KUSTOMIZE_DIR}/ to namespace ${NS}"
kubectl apply -k "${KUSTOMIZE_DIR}"

# --- zwave-js-ui parking guard ------------------------------------------------
# `apply -k` sets zwave-js-ui to replicas:1. While the ZWA-2 by-id path is still
# a REPLACE- placeholder the pod crash-loops on the missing device, so we park it
# at 0 unless the operator explicitly unparks AND the path looks real.
zwave_placeholder="false"
grep -q 'REPLACE-' "${ZWAVE_MANIFEST}" && zwave_placeholder="true"

if [ "${UNPARK_ZWAVE}" = "true" ]; then
  if [ "${zwave_placeholder}" = "true" ]; then
    warn "UNPARK_ZWAVE=true but ${ZWAVE_MANIFEST} still has a REPLACE- device path.
       Leaving zwave-js-ui running at replicas:1 as requested, but it will
       crash-loop until the real /dev/serial/by-id/... path is filled in."
  fi
  log "zwave-js-ui left UNPARKED (replicas per manifest)."
else
  log "Parking zwave-js-ui at replicas:0 (no controller / safety default)."
  kubectl scale deploy/zwave-js-ui --replicas=0 -n "${NS}"
fi

# --- wait for the always-on workloads to settle -------------------------------
WORKLOADS=(mariadb mosquitto home-assistant)
[ "${UNPARK_ZWAVE}" = "true" ] && [ "${zwave_placeholder}" = "false" ] \
  && WORKLOADS+=(zwave-js-ui)

log "Waiting for rollouts (timeout ${ROLLOUT_TIMEOUT} each): ${WORKLOADS[*]}"
rollout_failed="false"
for d in "${WORKLOADS[@]}"; do
  if ! kubectl rollout status "deploy/${d}" -n "${NS}" --timeout="${ROLLOUT_TIMEOUT}"; then
    warn "Rollout of ${d} did not complete within ${ROLLOUT_TIMEOUT}."
    rollout_failed="true"
  fi
done

log "Current pods in ${NS}:"
kubectl get pods -n "${NS}" -o wide || true

if [ "${rollout_failed}" = "true" ]; then
  die "One or more rollouts did not complete — check the pod output above and
       'kubectl logs' / 'kubectl describe' for the failing workload."
fi

log "Deploy complete. HA UI: http://<PC-LAN-IP>:8123 (LAN) or Tailscale IP."
