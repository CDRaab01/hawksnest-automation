# CLAUDE.md — Household Home Assistant Deployment

## Purpose

This document is the implementation spec for deploying Home Assistant (HA) and supporting
services into an existing K3s cluster. It is written for Claude Code to drive implementation.
Follow it top to bottom. Where a decision is required that this spec does not cover, stop and
ask rather than guessing — this controls physical door locks, so correctness matters more than speed.

## Owner / Environment

- **Operator:** Christian (systems engineer; comfortable with Python, Docker, Kubernetes, FastAPI)
- **Host:** Windows 11 PC (powerful; primary always-on machine)
- **Cluster:** K3s running inside WSL2 (distro name: `Dragonfly`)
- **Container runtime:** Docker Desktop is present, but workloads run in K3s
- **Remote access:** Tailscale already in use on this network
- **NAS:** Synology DS214 — used for NFS persistence only (old; HA container will NOT run on it).
  Treat as replaceable; a future NAS upgrade is expected. Do not hard-code model-specific paths
  beyond the NFS export.

## Hardware In Scope

- **Z-Wave controller:** Home Assistant Connect ZWA-2 (Nabu Casa, 800-series Silicon Labs).
  USB serial device. Positioned on USB extension away from PC case to reduce RF interference.
- **Locks (Z-Wave):** 3x Schlage BE469ZP deadbolts
  - Front door (deadbolt)
  - Back door (deadbolt)
  - Garage-to-house interior door (deadbolt to be added; currently lever-only)
- **Light switches (Z-Wave):** Zooz ZEN72 dimmers (basement, single-pole). Quantity TBD by Christian.
- **Cameras / Doorbell:** Existing Ring devices (cloud integration).

## V1 Scope (this deliverable)

1. Home Assistant Core running in K3s, reachable on the LAN and over Tailscale.
2. Z-Wave JS controlling the 3 Schlage BE469ZP locks via the ZWA-2 stick.
3. Ring integration (cloud) for existing cameras + doorbell.
4. Zooz ZEN72 dimmers added to the Z-Wave network and exposed in HA.

**Explicitly deferred (do not build in V1):** WLED, MyQ/Ratgdo garage control, Zigbee,
Plex-triggered lighting automations, presence-based auto-lock. Architect so these can be
added later without rework (see Future-Proofing).

## Architecture Overview

```
Windows 11 host
└─ WSL2 (Dragonfly)
   └─ K3s
      ├─ namespace: home-automation
      │  ├─ Deployment: home-assistant      (host network, /dev/serial passthrough)
      │  ├─ Deployment: zwave-js-ui          (serial passthrough; HA connects via websocket)
      │  └─ (optional) Deployment: mosquitto (MQTT broker — install now, harmless, used later)
      └─ PersistentVolumes (NFS → Synology DS214)
```

Z-Wave JS UI runs as its own pod and owns the serial device. HA talks to it over the
Z-Wave JS websocket (port 3000). This decouples the radio from HA restarts and is the
recommended pattern over the built-in add-on (which requires HA OS — not our case).

## USB Serial Passthrough (the critical, fiddly part)

The ZWA-2 is on the Windows host. It must reach a pod inside K3s-in-WSL2. Chain:

1. **Windows → WSL2** via `usbipd-win`:
   - `usbipd list` to find the ZWA-2 bus ID.
   - `usbipd bind --busid <id>` (once, as admin).
   - `usbipd attach --wsl --busid <id>` to attach to the Dragonfly distro.
   - Confirm inside WSL2 it appears as `/dev/ttyUSB0` or `/dev/ttyACM0`
     (ZWA-2 typically enumerates as ACM). Verify with `ls -l /dev/serial/by-id/`.
   - **Use the stable `by-id` path, never the raw ttyACM number** — it can renumber on replug.
2. **WSL2 → K3s pod:** mount the device into the zwave-js-ui pod via a `hostPath` volume
   pointing at the `by-id` symlink target, with `securityContext.privileged: true`
   (or a narrower device cgroup rule if you prefer — document whichever you choose).
3. **Persistence of attachment:** `usbipd attach` does not survive reboot/replug by default.
   Provide a small documented procedure (and optionally a scheduled task) to re-attach on boot.
   Flag this clearly to Christian as the known fragile link in the chain.

> Acceptance: after a host reboot + documented re-attach steps, zwave-js-ui sees the controller
> and all locks report state without re-pairing.

## Persistence

- NFS export from the Synology DS214 backs all PersistentVolumes.
- Separate PVCs for: `ha-config`, `zwavejs-config` (the Z-Wave network/security keys live here —
  back this up; losing it means re-pairing every device), and `mosquitto-data` if installed.
- Document the Synology NFS export path and the PV `server:/path` values in the manifests.

## Security Keys

- Z-Wave JS S2 security keys must be generated once and stored in the zwavejs-config PVC.
- Record them in Christian's password manager as well. The Schlage locks pair with S2
  Access Control — they will not include securely without the keys present.

## Networking & Access

- HA reachable on LAN at a stable address (host network or a LoadBalancer/NodePort —
  pick one and document; host network is simplest given WSL2 quirks).
- Remote access via existing Tailscale, NOT via cloud. No ports forwarded to the internet.
- Set `trusted_proxies`/`use_x_forwarded_for` appropriately if any reverse proxy is added later.

## Integrations

### Z-Wave (locks + dimmers)
- Pair each BE469ZP near the controller first if inclusion fails at distance
  (locks are battery devices and do NOT repeat; the ZEN72 dimmers DO repeat and form the mesh).
- Inclusion: Schlage — tap the Schlage button + enter programming code; include from Z-Wave JS UI.
- Expose each lock as a `lock` entity; assign user code slots (slot 1 = Christian, 2 = Elizabeth,
  3+ = guests). Guest codes should be created with automation-driven expiry (deferred logic, but
  leave the entity/slot structure ready).
- ZEN72: single-pole, neutral required. Confirm neutral present in each basement box before install.

### Ring (cloud)
- Official HA Ring integration. Expect cloud dependency and periodic re-auth.
- Pull in doorbell press events, motion, and camera snapshots as entities for future automations.
- Do not block any lock functionality on Ring availability.

## Future-Proofing (build the seams, not the features)

- Install Mosquitto now so Ratgdo (ESP32, MQTT, local) drops in later with no rearchitecting.
- Keep namespace, PVC naming, and HA `configuration.yaml` modular (use `!include` splits:
  automations, scripts, scenes) so WLED, Zigbee (separate coordinator), and Plex automations
  can be added incrementally.
- Christian has spare ESP32s — Ratgdo will likely be flashed onto those rather than buying modules.

## Deliverables

1. K8s manifests (or a Helm/Kustomize layout) for: namespace, HA Deployment + Service,
   zwave-js-ui Deployment + Service, PVCs, and Mosquitto.
2. The documented `usbipd-win` attach procedure (including reboot re-attach).
3. A README with: bring-up order, how to pair a new Z-Wave device, how to back up the
   zwavejs-config PVC, and the recovery procedure if the cluster or stick is replaced.
4. A backup note: ha-config and zwavejs-config PVCs are the two things that must be backed up.

## Acceptance Criteria

- [ ] HA UI reachable on LAN and over Tailscale.
- [ ] ZWA-2 visible to zwave-js-ui via stable `by-id` path.
- [ ] All 3 Schlage locks included with S2, lock/unlock + state reporting verified.
- [ ] User code slots configured for Christian + Elizabeth.
- [ ] Basement ZEN72 dimmer(s) included and dimmable from HA.
- [ ] Ring cameras + doorbell visible in HA.
- [ ] Documented recovery after a host reboot (USB re-attach) with no device re-pairing.
- [ ] ha-config and zwavejs-config persist on Synology NFS and survive pod restarts.

## Open Questions For Christian

1. Synology DS214 NFS export path + which share to use for the PVs?
2. Final count of ZEN72 dimmers for V1?
3. HA exposure preference: host network vs NodePort/LoadBalancer?
4. Confirm the garage interior door deadbolt will be drilled/installed before lock pairing.
