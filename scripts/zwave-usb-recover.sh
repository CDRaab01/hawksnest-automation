#!/usr/bin/env bash
#
# zwave-usb-recover.sh — make the ZWA-2 reliably reach the zwave-js-ui pod after a
# (re)boot or replug. Run AFTER usbipd has attached the stick into Dragonfly
# (windows/boot.ps1 invokes this as root at logon). Also fine to run by hand.
#
# Fixes the two failure modes seen after an unclean WSL/host restart:
#   1. The kubelet creates an empty DIRECTORY squatting on the by-id symlink path
#      when zwave-js-ui starts BEFORE the stick is attached. That directory then
#      blocks udev from creating the real symlink, so the pod mounts a dir and the
#      driver fails with "is a directory, cannot open /dev/zwave (ZW0100)".
#   2. The pod started before the device node existed, so /dev/zwave is an empty
#      dir even though the device is now present — a rollout restart remounts it.
#
# Idempotent: safe to run on every boot and repeatedly. Exits 0 only once
# /dev/zwave is a character device inside the pod.
set -euo pipefail

NS="home-automation"
DEPLOY="zwave-js-ui"
BYID="/dev/serial/by-id/usb-Nabu_Casa_ZWA-2_9070690E14E4-if00"
RAW="/dev/ttyACM0"
WAIT="${WAIT:-60}"            # seconds to wait for the device / k3s API / rollouts

log()  { printf '\n==> %s\n' "$*"; }
warn() { printf '\n[warn] %s\n' "$*" >&2; }
die()  { printf '\n[error] %s\n' "$*" >&2; exit 1; }

# rm/udevadm need root; allow running either as root (no sudo) or as a sudoer.
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"

# kubeconfig: prefer the invoking user's, else the k3s server file (root-readable).
if [ -z "${KUBECONFIG:-}" ]; then
  if   [ -f "${HOME}/.kube/config" ];    then export KUBECONFIG="${HOME}/.kube/config"
  elif [ -f /etc/rancher/k3s/k3s.yaml ]; then export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
  fi
fi

# --- 1. wait for the raw device to enumerate (usbipd attach can lag a few s) ---
for _ in $(seq 1 "$WAIT"); do [ -e "$RAW" ] && break; sleep 1; done
[ -e "$RAW" ] || die "$RAW not present — is the ZWA-2 attached? Run 'usbipd attach --busid <id> --wsl Dragonfly' on Windows."
log "Raw device present: $RAW"

# --- 2. wait for the k3s API --------------------------------------------------
for _ in $(seq 1 "$WAIT"); do kubectl get nodes >/dev/null 2>&1 && break; sleep 2; done
kubectl get nodes >/dev/null 2>&1 || die "k3s API not reachable (KUBECONFIG=${KUBECONFIG:-unset})."

# --- 3. clear a stale directory squatting on the by-id path -------------------
# (kubelet creates this when the pod starts with no device; it blocks udev.)
if [ -e "$BYID" ] && [ ! -L "$BYID" ]; then
  warn "Stale non-symlink squatting on ${BYID} — removing (stopping the pod first)."
  kubectl -n "$NS" scale deploy/"$DEPLOY" --replicas=0 || true
  for _ in $(seq 1 30); do
    [ -z "$(kubectl -n "$NS" get pods -l app="$DEPLOY" -o name 2>/dev/null)" ] && break
    sleep 2
  done
  $SUDO rm -rf "$BYID"
fi

# --- 4. (re)create the by-id symlink via udev now that the path is free -------
if [ ! -L "$BYID" ]; then
  log "Triggering udev to (re)create the by-id symlink."
  $SUDO udevadm trigger --subsystem-match=tty || true
  $SUDO udevadm settle 2>/dev/null || true
  for _ in $(seq 1 10); do [ -L "$BYID" ] && break; sleep 1; done
fi
[ -L "$BYID" ] || die "${BYID} is still not a symlink after udev trigger — check the stick / udev."
log "Device ready: ${BYID} -> $(readlink "$BYID")"

# --- 5. ensure zwave-js-ui is running ----------------------------------------
kubectl -n "$NS" scale deploy/"$DEPLOY" --replicas=1
kubectl -n "$NS" rollout status deploy/"$DEPLOY" --timeout="${WAIT}s" || true

# --- 6. if the pod sees a dir (it started before the device), remount it ------
if ! kubectl -n "$NS" exec deploy/"$DEPLOY" -- sh -c '[ -c /dev/zwave ]' 2>/dev/null; then
  warn "/dev/zwave is not a char device in the pod — restarting to remount."
  kubectl -n "$NS" rollout restart deploy/"$DEPLOY"
  kubectl -n "$NS" rollout status deploy/"$DEPLOY" --timeout="${WAIT}s" || true
fi

# --- 7. final verification ----------------------------------------------------
if kubectl -n "$NS" exec deploy/"$DEPLOY" -- sh -c '[ -c /dev/zwave ]' 2>/dev/null; then
  log "OK: /dev/zwave is a character device inside the pod — Z-Wave is up."
else
  die "/dev/zwave still wrong inside the pod after restart — investigate manually."
fi
