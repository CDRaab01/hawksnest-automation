#!/usr/bin/env bash
# Heal the ZWA-2 device mount after a reboot and (re)start zwave-js-ui so the Schlage
# locks come back automatically. Run as root inside WSL (the Windows logon task calls
# this after attaching the USB stick).
#
# Why this is needed: zwave-js-ui mounts the stick's /dev/serial/by-id symlink via
# hostPath. If the pod starts before the stick is attached, containerd auto-creates a
# DIRECTORY at that path, which then masks the real device ("/dev/zwave: Is a directory")
# even once the stick is attached. This clears that and bounces the pod.
set -u
export KUBECONFIG="${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"
DEV=/dev/serial/by-id/usb-Nabu_Casa_ZWA-2_9070690E14E4-if00

# Wait up to ~60s for the cluster API (it may still be starting at logon).
for _ in $(seq 1 30); do
  kubectl get ns home-automation >/dev/null 2>&1 && break
  sleep 2
done

# Only act if the device path is masked by a directory or missing entirely.
if [ -d "$DEV" ] || [ ! -e "$DEV" ]; then
  kubectl -n home-automation scale deploy/zwave-js-ui --replicas=0 >/dev/null 2>&1
  sleep 4
  rm -rf "$DEV"
  udevadm trigger --action=add --subsystem-match=tty 2>/dev/null || true
  sleep 1
  [ -e "$DEV" ] || ln -s ../../ttyACM0 "$DEV"
  kubectl -n home-automation scale deploy/zwave-js-ui --replicas=1 >/dev/null 2>&1
  echo "zwave-attach-heal: cleared mask and restarted zwave-js-ui"
else
  echo "zwave-attach-heal: device OK ($DEV), nothing to do"
fi
