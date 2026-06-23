# Bring-up Progress / Session Notes

Living status doc for the HA + Z-Wave deployment. Update at the end of each working
session so the next one can pick up without re-deriving the fiddly bits.

_Last updated: 2026-06-23_

## TL;DR — where we are

The full chain is **proven end to end** and the **front door lock is live in Home Assistant**:

```
Windows host → usbipd (303a:4001) → WSL2 /dev/ttyACM0 → by-id symlink
→ pod /dev/zwave → zwave-js-ui driver → controller ready
→ HA Z-Wave integration (ws://zwave-js-ui:3000) → lock.front_door_lock
```

## Done ✅

- **USB passthrough working.** ZWA-2 confirmed as VID:PID `303a:4001`, serial
  `9070690E14E4`, enumerates as `/dev/ttyACM0`. Stable mount path in
  `kustomize/zwave-js-ui/deployment.yaml`:
  `/dev/serial/by-id/usb-Nabu_Casa_ZWA-2_9070690E14E4-if00`.
  - usbipd is **5.x** — attach syntax is `usbipd attach --busid <id> --wsl Dragonfly`
    (the older `--distribution` flag is gone). `attach-zwa2.ps1` was fixed for this.
- **zwave-js-ui online.** Serial port set to `/dev/zwave`, driver ready, RF very quiet
  (background RSSI ~-90 to -97 dBm). WS server enabled on port 3000.
- **S2 security keys generated** and saved off (see "Secrets" below).
- **Front door lock — DONE.** Node 002, Schlage/Allegion BE469ZP, included with
  **S2 Access Control**, interview Complete, battery 100%, lock/unlock + state
  verified in both zwave-js-ui and HA. Named `Lock` / Location `Front Door`.
- **Home Assistant reachable** on LAN at `http://192.168.4.34:8123` and connected to
  Z-Wave via the integration pointed at `ws://zwave-js-ui:3000`.

## Pending ⏳

- **Back door lock** — NOT paired. No QR/DSK card came with it; the S2 DSK label is
  *inside* the lock (interior assembly, near the battery compartment). Must disassemble
  to read it. Once we have the DSK: Smart Start (if QR) or classic Include + first 5
  digits of the DSK.
- **Garage interior lock** — not started (deadbolt may still need installing per spec).
- **User code slots** — not set yet. Plan: slot 1 = Christian, slot 2 = Elizabeth.
  Do once all locks are in (one pass via the User Code CC / Users tab).
- **ZEN72 dimmer(s)** — not added. Worth doing one *between* the PC and the doors to
  build mesh; lock RSSI is ~-87 dBm (workable but middling, no repeaters yet).
- **Ring integration** — not started.
- **Tailscale** — not installed yet on the PC. Remote HA access (V1 item) still open.
  Once installed, `portproxy-ha.ps1` already exposes HA at `http://<tailscale-ip>:8123`.

## Lessons learned (don't relearn these)

- **Smart Start (scan QR) >> classic keypad inclusion** for these BE469ZP locks. The
  keypad enrollment sequence was flaky / never reached the controller; scanning the QR
  into the provisioning list + a **battery pull** (out ~10s, back in) made the lock join
  automatically with S2. Power-cycle is what triggers the broadcast.
- **PIN vs DSK:** Smart Start needs the **full DSK** (the QR). The **5-digit PIN** is
  only for **classic** inclusion (lock joins first, then you type the PIN). Don't paste
  the 5-digit PIN into the Smart Start screen — it won't include.
- **DSK label location** on the BE469ZP: on the interior assembly, near/behind the
  battery compartment — NOT only on the box card. The label with the default Programming
  Code / User Codes is a *different* label and is NOT the security key.
- **hostPath device mount:** the by-id symlink mounts fine **once the real path is
  applied** — the empty-directory (`total 0`) symptom earlier was just the stale
  `REPLACE-` placeholder still deployed (needed `git pull` + `kubectl apply -k`).
- **Stuck "starting inclusion":** was a stale zwave-js-ui frontend socket — a full page
  reload fixed it (the `AddNodeToNetwork` request then reached the driver).

## How to resume the Z-Wave UI from a phone (temporary, for pairing)

The `zwave-js-ui` Service is ClusterIP (8091), so it's not on the LAN by default. To
pair more devices from a phone at the lock:

```bash
# WSL (leave running):
kubectl port-forward --address 0.0.0.0 -n home-automation svc/zwave-js-ui 8091:8091
```
```powershell
# Windows (elevated) — reuses the portproxy pattern:
$wslIp = (wsl -d Dragonfly -- hostname -I).Trim().Split(" ")[0]
netsh interface portproxy delete v4tov4 listenport=8091 listenaddress=0.0.0.0 2>$null
netsh interface portproxy add v4tov4 listenport=8091 listenaddress=0.0.0.0 connectport=8091 connectaddress=$wslIp
New-NetFirewallRule -DisplayName "ZwaveJS-8091" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8091 -ErrorAction SilentlyContinue | Out-Null
```
Then `http://<PC-LAN-IP>:8091` from a phone on the same Wi-Fi. **Tear this down once all
devices are paired** — HA talks to zwave-js-ui internally over the WS service, so the
:8091 exposure isn't needed for normal operation.

## Secrets (NOT stored in this repo)

- **S2 keys** — generated in zwave-js-ui, persisted in the `zwavejs-config` PVC. Back
  these up to a password manager (still TODO — no manager set up yet; held offline for
  now). Losing them = re-pairing every device.
- **Lock programming / user codes** — printed on each lock's interior label. Keep in the
  password manager, not here.
