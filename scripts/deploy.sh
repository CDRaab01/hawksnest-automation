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
#     ZWA-2 USB controller is absent. We park it at 0 ONLY while the device path
#     is still a REPLACE- placeholder; once the real by-id path is filled in we
#     leave it running, so a routine deploy never takes the door locks offline.
#
# Environment knobs (all optional):
#   OVERLAY                which kustomize overlay to apply: 'prod' (default) or
#                          'staging'. prod -> namespace home-automation (live locks);
#                          staging -> home-automation-staging (throwaway smoke test,
#                          Z-Wave parked, local-path storage, dummy secrets).
#   KUBECONFIG              kubeconfig path        (default: ~/.kube/config)
#   HAWKSNEST_SECRETS_DIR  where the real secrets live on the runner host
#                          (default: ~/hawksnest-secrets) — must contain
#                          mariadb.env, mosquitto.passwd and ring-mqtt.env.
#                          Ignored for OVERLAY=staging, which uses the dummy
#                          *.example secrets straight from the overlay.
#   UNPARK_ZWAVE           "true" forces zwave-js-ui to run even while the device
#                          path is still a REPLACE- placeholder (escape hatch).
#                          Default false: park ONLY if the path is a placeholder;
#                          a real by-id path always runs. (prod only)
#   SKIP_HA_CONFIG_CHECK   "true" skips the pre-apply Home Assistant config check
#                          (default false). The check validates the LIVE config on
#                          the ha-config PVC and aborts the deploy BEFORE apply if
#                          it is invalid, so a broken config can't crash-loop HA and
#                          take the locks offline. A mount/scheduling stall only
#                          warns; only a genuine check_config failure aborts.
#   HA_CHECK_TIMEOUT       how long to wait for the config-check Job (default 120s)
#   ROLLOUT_TIMEOUT        per-deployment rollout wait (default: 180s)
#   RING_MQTT_TIMEOUT      best-effort wait for ring-mqtt (default: 420s); a miss
#                          only warns, it never fails the deploy
#
set -euo pipefail

# --- locate the repo root (this script lives in <root>/scripts) ---------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

export KUBECONFIG="${KUBECONFIG:-${HOME}/.kube/config}"
OVERLAY="${OVERLAY:-prod}"
HAWKSNEST_SECRETS_DIR="${HAWKSNEST_SECRETS_DIR:-${HOME}/hawksnest-secrets}"
UNPARK_ZWAVE="${UNPARK_ZWAVE:-false}"
SKIP_HA_CONFIG_CHECK="${SKIP_HA_CONFIG_CHECK:-false}"
HA_IMAGE="ghcr.io/home-assistant/home-assistant:stable"   # match the Deployment tag
HA_CHECK_TIMEOUT="${HA_CHECK_TIMEOUT:-120s}"
ROLLOUT_TIMEOUT="${ROLLOUT_TIMEOUT:-180s}"
RING_MQTT_TIMEOUT="${RING_MQTT_TIMEOUT:-420s}"
# The Z-Wave device path lives in the shared base manifest now.
ZWAVE_MANIFEST="kustomize/base/zwave-js-ui/deployment.yaml"

log()  { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\n\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\n\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

# --- resolve the target overlay -> namespace + paths --------------------------
case "${OVERLAY}" in
  prod)    NS="home-automation" ;;
  staging) NS="home-automation-staging" ;;
  *) die "Unknown OVERLAY='${OVERLAY}' (expected 'prod' or 'staging')." ;;
esac
KUSTOMIZE_DIR="kustomize/overlays/${OVERLAY}"
SECRETS_DST="${KUSTOMIZE_DIR}/secrets"

# --- pre-apply Home Assistant config validation (Gate B) ----------------------
# Validate the LIVE config on the ha-config PVC with HA's own check_config, in a
# one-shot Job, BEFORE `kubectl apply`. If it's invalid we abort here — the old
# (working) HA pod keeps running its good config, so a bad edit never rolls out a
# crash-looping HA and takes the door locks offline.
#   * Skipped on a fresh cluster (no ha-config PVC yet — nothing to validate).
#   * A mount/scheduling stall (e.g. RWO held by the running HA pod, slow image
#     pull) only WARNS and continues — it must never block a deploy.
#   * Only a genuine non-zero check_config aborts the deploy.
ha_config_check() {
  if ! kubectl get pvc ha-config -n "${NS}" >/dev/null 2>&1; then
    log "No ha-config PVC in ${NS} yet — skipping live HA config check (fresh install)."
    return 0
  fi
  log "Validating live HA config on the ha-config PVC (check_config, timeout ${HA_CHECK_TIMEOUT})"
  kubectl delete job ha-config-check -n "${NS}" --ignore-not-found >/dev/null 2>&1 || true
  cat <<EOF | kubectl apply -f - >/dev/null
apiVersion: batch/v1
kind: Job
metadata:
  name: ha-config-check
  namespace: ${NS}
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 120
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: check
          image: ${HA_IMAGE}
          command: ["python", "-m", "homeassistant", "--script", "check_config", "--config", "/config"]
          volumeMounts:
            - name: config
              mountPath: /config
      volumes:
        - name: config
          persistentVolumeClaim:
            claimName: ha-config
EOF
  if kubectl wait --for=condition=complete job/ha-config-check -n "${NS}" \
       --timeout="${HA_CHECK_TIMEOUT}" >/dev/null 2>&1; then
    log "HA config check passed."
    kubectl delete job ha-config-check -n "${NS}" --ignore-not-found >/dev/null 2>&1 || true
    return 0
  fi
  if kubectl wait --for=condition=failed job/ha-config-check -n "${NS}" \
       --timeout=5s >/dev/null 2>&1; then
    warn "Home Assistant config check FAILED — the live config is invalid:"
    kubectl logs job/ha-config-check -n "${NS}" --tail=60 >&2 || true
    kubectl delete job ha-config-check -n "${NS}" --ignore-not-found >/dev/null 2>&1 || true
    die "Aborting BEFORE apply so the running HA pod keeps its good config.
       Fix configuration.yaml (HA UI / file editor) and re-deploy, or set
       SKIP_HA_CONFIG_CHECK=true to override (not recommended on prod)."
  fi
  warn "HA config check did not finish within ${HA_CHECK_TIMEOUT} — could not mount
       ha-config (RWO held by the running HA pod) or the image pull was slow.
       Skipping the check, NOT failing the deploy."
  kubectl logs job/ha-config-check -n "${NS}" --tail=20 >&2 2>/dev/null || true
  kubectl delete job ha-config-check -n "${NS}" --ignore-not-found >/dev/null 2>&1 || true
  return 0
}

# --- pre-flight ---------------------------------------------------------------
command -v kubectl >/dev/null 2>&1 || die "kubectl not found on PATH."
[ -f "${KUBECONFIG}" ] || die "KUBECONFIG not found at ${KUBECONFIG}."
kubectl cluster-info >/dev/null 2>&1 \
  || die "Cannot reach the cluster with KUBECONFIG=${KUBECONFIG}. Is K3s up?"

log "Cluster: $(kubectl config current-context 2>/dev/null || echo '?')  (KUBECONFIG=${KUBECONFIG})"
log "Overlay: ${OVERLAY}  ->  namespace ${NS}"

# --- materialize the gitignored secrets onto this checkout --------------------
# prod: the repo only tracks *.example templates; the real secrets are kept on the
# runner host (HAWKSNEST_SECRETS_DIR) and copied into the kustomize tree so the
# secretGenerator can build them. Existing files in the checkout are left alone.
# staging: there are no real secrets — it materializes the dummy *.example files in
# the overlay so a smoke deploy needs nothing bootstrapped on the host.
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
if [ "${OVERLAY}" = "staging" ]; then
  for s in mariadb.env mosquitto.passwd ring-mqtt.env; do
    [ -f "${SECRETS_DST}/${s}" ] \
      || install -m 0600 "${SECRETS_DST}/${s}.example" "${SECRETS_DST}/${s}"
  done
  log "Staging: using dummy credentials from the overlay's *.example templates."
else
  need_secret "mariadb.env"
  need_secret "mosquitto.passwd"
  need_secret "ring-mqtt.env"
fi

# --- validate the build before touching the cluster ---------------------------
log "Validating kustomize build"
kubectl kustomize "${KUSTOMIZE_DIR}" >/dev/null \
  || die "kustomize build failed — fix the manifests before deploying."

# --- validate the LIVE Home Assistant config (Gate B) before apply ------------
if [ "${SKIP_HA_CONFIG_CHECK}" = "true" ]; then
  warn "SKIP_HA_CONFIG_CHECK=true — skipping the live HA config validation."
else
  ha_config_check
fi

# --- apply --------------------------------------------------------------------
log "Applying ${KUSTOMIZE_DIR}/ to namespace ${NS}"
kubectl apply -k "${KUSTOMIZE_DIR}"

# --- zwave-js-ui parking guard ------------------------------------------------
# `apply -k` sets zwave-js-ui to replicas:1. The pod can only run when the ZWA-2
# device path is real; while it is still a REPLACE- placeholder the pod crash-loops
# on the missing device.
#
# Rule: PARK ONLY WHEN THE PATH IS A PLACEHOLDER. Once the by-id path is filled in
# (controller wired, devices paired), the deploy must LEAVE zwave-js-ui RUNNING —
# otherwise every push-triggered deploy would scale the controller to 0 and take
# the door locks offline. UNPARK_ZWAVE=true is an escape hatch to force it on even
# while the path is still a placeholder.
# Staging parks Z-Wave permanently via the overlay (replicas:0) and must never own
# the single USB stick, so skip the prod parking logic entirely and exclude it from
# the rollout wait below.
if [ "${OVERLAY}" = "staging" ]; then
  log "Staging: zwave-js-ui stays parked (replicas:0 from the overlay)."
  zwave_should_run="false"
else
zwave_placeholder="false"
grep -q 'REPLACE-' "${ZWAVE_MANIFEST}" && zwave_placeholder="true"

if [ "${zwave_placeholder}" = "false" ] || [ "${UNPARK_ZWAVE}" = "true" ]; then
  if [ "${zwave_placeholder}" = "true" ]; then
    warn "UNPARK_ZWAVE=true but ${ZWAVE_MANIFEST} still has a REPLACE- device path.
       Leaving zwave-js-ui running at replicas:1 as requested, but it will
       crash-loop until the real /dev/serial/by-id/... path is filled in."
  else
    log "Device path is real — leaving zwave-js-ui RUNNING (replicas per manifest)."
  fi
  zwave_should_run="true"
else
  log "Parking zwave-js-ui at replicas:0 (device path is still a REPLACE- placeholder)."
  kubectl scale deploy/zwave-js-ui --replicas=0 -n "${NS}"
  zwave_should_run="false"
fi
fi  # end OVERLAY=staging wrapper

# --- wait for the always-on workloads to settle -------------------------------
# ring-mqtt is handled separately (best-effort) below: its readiness depends on the
# Ring cloud API + per-camera setup, which can take minutes, so a slow or unhealthy
# ring-mqtt must NEVER fail the deploy that also rolls the lock-critical workloads.
WORKLOADS=(mariadb mosquitto home-assistant)
[ "${zwave_should_run}" = "true" ] && WORKLOADS+=(zwave-js-ui)

log "Waiting for rollouts (timeout ${ROLLOUT_TIMEOUT} each): ${WORKLOADS[*]}"
rollout_failed="false"
for d in "${WORKLOADS[@]}"; do
  if ! kubectl rollout status "deploy/${d}" -n "${NS}" --timeout="${ROLLOUT_TIMEOUT}"; then
    warn "Rollout of ${d} did not complete within ${ROLLOUT_TIMEOUT}."
    rollout_failed="true"
  fi
done

# ring-mqtt: best-effort. It boots slowly (Ring cloud login + many cameras) and has
# no liveness probe by design, so give it a longer wait but only WARN if it isn't
# ready — never fail the deploy on it (locks/HA must not hinge on Ring availability).
log "Waiting for ring-mqtt (best-effort, timeout ${RING_MQTT_TIMEOUT})"
if ! kubectl rollout status deploy/ring-mqtt -n "${NS}" --timeout="${RING_MQTT_TIMEOUT}"; then
  warn "ring-mqtt not ready within ${RING_MQTT_TIMEOUT} — continuing anyway.
       Ring cloud may be slow; check 'kubectl logs deploy/ring-mqtt -n ${NS}'."
fi

log "Current pods in ${NS}:"
kubectl get pods -n "${NS}" -o wide || true

if [ "${rollout_failed}" = "true" ]; then
  die "One or more rollouts did not complete — check the pod output above and
       'kubectl logs' / 'kubectl describe' for the failing workload."
fi

if [ "${OVERLAY}" = "staging" ]; then
  log "Staging deploy complete. HA is ClusterIP (no NodePort) — reach it with:
       kubectl port-forward -n ${NS} deploy/home-assistant 8124:8123
       then open http://localhost:8124/ . Tear down with: kubectl delete ns ${NS}"
else
  log "Deploy complete. HA UI: http://<PC-LAN-IP>:8123 (LAN) or Tailscale IP."
fi
