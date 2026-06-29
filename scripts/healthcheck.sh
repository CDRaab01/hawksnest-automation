#!/usr/bin/env bash
#
# healthcheck.sh — read-only "is the stack up?" probe for the Hawksnest / Home
# Assistant deployment. Run it on the Dragonfly host (it needs kubectl access to
# the K3s cluster) or anywhere with the kubeconfig; the optional HTTP probes can
# run from any LAN/Tailscale machine.
#
#   ./scripts/healthcheck.sh
#
# It is STRICTLY READ-ONLY: it never applies, scales, restarts, or deletes
# anything. It only describes the current state so you can decide what to do.
# (A redeploy runs ON this cluster, so if the checks below say the cluster is
#  unreachable, a redeploy cannot run yet — fix the host/K3s layer first.)
#
# Exit codes:
#   0  everything critical is healthy
#   1  a critical workload/cluster check failed (locks/HA may be affected)
#   2  could not even reach the cluster (K3s/WSL/host is down)
#
# Environment knobs (all optional):
#   OVERLAY        'prod' (default) -> namespace home-automation
#                  'staging'        -> namespace home-automation-staging
#   KUBECONFIG     kubeconfig path (default: ~/.kube/config; on the runner host
#                  the k3s config is /etc/rancher/k3s/k3s.yaml — pass it in if so)
#   HA_URL         if set, HTTP-probe Home Assistant   (e.g. http://192.168.4.34:8123)
#   HAWKSNEST_URL  if set, HTTP-probe the Hawksnest UI (e.g. http://192.168.4.34:8080)
#   HTTP_TIMEOUT   per-curl timeout in seconds (default: 5)
#
set -uo pipefail   # NOT -e: this is a diagnostic; we want every check to run.

export KUBECONFIG="${KUBECONFIG:-${HOME}/.kube/config}"
OVERLAY="${OVERLAY:-prod}"
HTTP_TIMEOUT="${HTTP_TIMEOUT:-5}"

case "${OVERLAY}" in
  prod)    NS="home-automation" ;;
  staging) NS="home-automation-staging" ;;
  *) printf 'Unknown OVERLAY=%s (expected prod|staging)\n' "${OVERLAY}" >&2; exit 1 ;;
esac

# Workloads that, if down, mean the system is degraded. zwave-js-ui owns the
# door locks; home-assistant is the brain. ring-mqtt is best-effort (cameras) and
# go2rtc ships parked, so neither is treated as critical here.
CRITICAL_WORKLOADS=(mariadb mosquitto home-assistant zwave-js-ui)
BESTEFFORT_WORKLOADS=(ring-mqtt go2rtc)
# hawksnest lives in the Hawksnest repo's deploy/k8s but lands in the same
# namespace; probe it if present, don't fail if it isn't part of this checkout.
EXTRA_WORKLOADS=(hawksnest)
PVCS=(ha-config zwavejs-config mariadb-data mosquitto-data ring-mqtt-data)

green=$'\033[1;32m'; red=$'\033[1;31m'; yellow=$'\033[1;33m'; blue=$'\033[1;34m'; reset=$'\033[0m'
ok()   { printf '  %s✓%s %s\n' "${green}"  "${reset}" "$*"; }
bad()  { printf '  %s✗%s %s\n' "${red}"    "${reset}" "$*"; }
warn() { printf '  %s!%s %s\n' "${yellow}" "${reset}" "$*"; }
head() { printf '\n%s==>%s %s\n' "${blue}" "${reset}" "$*"; }

rc=0   # rolls up to the worst problem seen

# --- 0. tooling ---------------------------------------------------------------
if ! command -v kubectl >/dev/null 2>&1; then
  printf '%skubectl not found on PATH — run this on the Dragonfly host.%s\n' "${red}" "${reset}" >&2
  # We can still do HTTP probes below, but cluster checks are impossible.
  KUBECTL_OK="false"
else
  KUBECTL_OK="true"
fi

# --- 1. cluster reachability (the thing a "redeploy" presupposes) -------------
if [ "${KUBECTL_OK}" = "true" ]; then
  head "Cluster reachability (KUBECONFIG=${KUBECONFIG})"
  if kubectl cluster-info >/dev/null 2>&1; then
    ctx="$(kubectl config current-context 2>/dev/null || echo '?')"
    ok "API server reachable (context: ${ctx})"
    notready="$(kubectl get nodes --no-headers 2>/dev/null | awk '$2 !~ /^Ready/ {print $1}')"
    if [ -n "${notready}" ]; then
      bad "Node(s) not Ready: ${notready}"; rc=1
    else
      ok "All nodes Ready"
    fi
  else
    bad "Cannot reach the API server — K3s / WSL2 (Dragonfly) / the host is down."
    warn "A redeploy runs ON this cluster and cannot fix this. Bring the host layer"
    warn "back first: start WSL (wsl -d Dragonfly), confirm k3s is running, then"
    warn "re-run the Windows portproxy scripts (WSL2's IP changes on reboot)."
    # No point in the per-workload checks; jump to HTTP probes.
    KUBECTL_OK="cluster-down"
    rc=2
  fi
fi

# --- 2. namespace + workloads + PVCs ------------------------------------------
if [ "${KUBECTL_OK}" = "true" ]; then
  head "Namespace ${NS}"
  if kubectl get ns "${NS}" >/dev/null 2>&1; then
    ok "namespace exists"
  else
    bad "namespace ${NS} not found — nothing is deployed."
    rc=1
  fi

  # ready/desired replicas for one deployment; prints status, returns non-zero if unhealthy
  check_deploy() {
    local name="$1" critical="$2"
    if ! kubectl get deploy "${name}" -n "${NS}" >/dev/null 2>&1; then
      if [ "${critical}" = "critical" ]; then
        bad "${name}: deployment missing"; return 1
      else
        warn "${name}: not present (skipped)"; return 0
      fi
    fi
    local ready desired
    ready="$(kubectl get deploy "${name}" -n "${NS}" -o jsonpath='{.status.readyReplicas}' 2>/dev/null)"
    desired="$(kubectl get deploy "${name}" -n "${NS}" -o jsonpath='{.spec.replicas}' 2>/dev/null)"
    ready="${ready:-0}"; desired="${desired:-0}"
    if [ "${desired}" = "0" ]; then
      warn "${name}: parked (replicas:0)"; return 0
    fi
    if [ "${ready}" = "${desired}" ]; then
      ok "${name}: ${ready}/${desired} ready"; return 0
    fi
    if [ "${critical}" = "critical" ]; then
      bad "${name}: ${ready}/${desired} ready"; return 1
    else
      warn "${name}: ${ready}/${desired} ready (best-effort)"; return 0
    fi
  }

  head "Critical workloads (locks + HA)"
  for d in "${CRITICAL_WORKLOADS[@]}"; do check_deploy "${d}" critical || rc=1; done

  head "Best-effort workloads (cameras / talk — never gate the locks)"
  for d in "${BESTEFFORT_WORKLOADS[@]}"; do check_deploy "${d}" besteffort; done
  for d in "${EXTRA_WORKLOADS[@]}";       do check_deploy "${d}" besteffort; done

  head "Persistent volumes (must be Bound — ha-config & zwavejs-config are the irreplaceable ones)"
  for pvc in "${PVCS[@]}"; do
    if ! kubectl get pvc "${pvc}" -n "${NS}" >/dev/null 2>&1; then
      warn "${pvc}: not present"
      continue
    fi
    phase="$(kubectl get pvc "${pvc}" -n "${NS}" -o jsonpath='{.status.phase}' 2>/dev/null)"
    if [ "${phase}" = "Bound" ]; then
      ok "${pvc}: Bound"
    else
      bad "${pvc}: ${phase:-Unknown}"
      rc=1
    fi
  done

  # Surface anything actively crash-looping / not-Running for a quick eyeball.
  head "Pods not in a healthy state"
  bad_pods="$(kubectl get pods -n "${NS}" --no-headers 2>/dev/null \
    | awk '$3 != "Running" && $3 != "Completed" {print "  "$1"  "$3"  restarts="$4}')"
  if [ -n "${bad_pods}" ]; then
    printf '%s\n' "${bad_pods}"
    rc=1
  else
    ok "all pods Running/Completed"
  fi
fi

# --- 3. HTTP reachability (optional; run from any LAN/Tailscale host) ----------
http_probe() {
  local label="$1" url="$2"
  local code
  code="$(curl -s -o /dev/null -m "${HTTP_TIMEOUT}" -w '%{http_code}' "${url}" 2>/dev/null)"
  if [ "${code}" = "000" ] || [ -z "${code}" ]; then
    bad "${label}: no response from ${url} (connection refused/timeout — check portproxy & pods)"
    return 1
  fi
  # Any HTTP response (200, 401, 302…) means the server is up and answering.
  ok "${label}: HTTP ${code} from ${url}"
  return 0
}
if [ -n "${HA_URL:-}" ] || [ -n "${HAWKSNEST_URL:-}" ]; then
  head "HTTP endpoint probes"
  [ -n "${HA_URL:-}" ]        && { http_probe "Home Assistant" "${HA_URL}"        || rc=1; }
  [ -n "${HAWKSNEST_URL:-}" ] && { http_probe "Hawksnest UI"   "${HAWKSNEST_URL}" || rc=1; }
else
  head "HTTP endpoint probes"
  warn "skipped — set HA_URL and/or HAWKSNEST_URL to probe (e.g. HA_URL=http://192.168.4.34:8123)"
fi

# --- summary ------------------------------------------------------------------
head "Summary"
if [ "${KUBECTL_OK}" = "false" ]; then
  warn "kubectl was unavailable, so cluster/workload state is UNKNOWN — only HTTP"
  warn "probes ran. Run this on the Dragonfly host for the full picture."
fi
case "${rc}" in
  0) printf '  %sHEALTHY%s — cluster reachable and all critical workloads ready.\n' "${green}" "${reset}" ;;
  1) printf '  %sDEGRADED%s — something critical is failing (see ✗ above).\n' "${yellow}" "${reset}" ;;
  2) printf '  %sCLUSTER DOWN%s — API server unreachable. Recover the host/K3s layer before deploying.\n' "${red}" "${reset}" ;;
esac
exit "${rc}"
