# DEPLOYMENT.md — As-Built State & Runbook

> **Read this first if you are a fresh Claude Code session.** This is the *as-built*
> record of the Hawksnest Home Assistant deployment — the real environment values and
> the decisions/gotchas discovered during bring-up. [`CLAUDE.md`](./CLAUDE.md) is the
> original *spec* (intent); this file is *reality*. Where they differ, reality wins —
> but flag the difference rather than silently diverging. This controls physical door
> locks: when unsure, stop and ask rather than guess.

Last updated: **2026-06-27** (added base+overlays, a staging smoke-test overlay, and HA
config-validation gates — see §11). Original V1 bring-up was 2026-06-22.

---

## 1. Current status

**Up and working:**
- K3s single-node cluster in WSL2, healthy.
- Synology NFS storage (v3), all PVCs bound, config persisting to the NAS.
- `mariadb`, `mosquitto`, `home-assistant` pods Running.
- HA recorder writing to MariaDB (not SQLite); gated on MariaDB readiness.
- HA reachable on LAN + Tailscale via a Windows portproxy.
- Ring (cloud) integration added.
- Windows logon scheduled task re-establishes the network after reboot.

**Deferred / pending (next session):**
- `zwave-js-ui` is deployed but **parked at `replicas: 0`** — the ZWA-2 USB controller
  had not physically arrived. No locks or dimmers paired yet.
- ZEN72 dimmers: **none in V1** (owner deferred them; pair later via the same flow).
- Garage interior-door deadbolt: **not yet installed** (lever-only). V1 pairs only the
  **front + back** deadbolts; slot structure left ready for the third.

---

## 2. Concrete environment facts (discovered, not assumed)

| Thing | Value |
|---|---|
| Windows host user | `Sonic` |
| **PC LAN IP** | `192.168.4.34` — subnet **`192.168.4.0/24`** (DHCP; may drift) |
| WSL2 distro for K3s | **`Dragonfly`** (dedicated; created this session), Ubuntu 26.04, systemd enabled, unix user `sonic` |
| Other WSL distros (ignore) | `Ubuntu` (default, unrelated), `docker-desktop`, `Pi-hole` (WSL1) |
| K3s | `v1.35.5+k3s1`, single node named `dragonfly`, containerd |
| kubeconfig | `~/.kube/config` on Dragonfly (`export KUBECONFIG=~/.kube/config` in `~/.bashrc`) |
| **NAS** | Synology DS214 at **`192.168.4.21`** (DSM web UI on `:5000`/`http`). Was `192.168.5.78` until the LAN was renumbered; see §"Moving the NAS" — the PVs pin this and cannot be patched in place |
| **NFS** | **v3 only** — the DS214 does *not* support NFSv4.1 (mount returns "Protocol not supported") |
| NFS export | **`/volume3/home-automation`** (chosen over the near-full Volume 1) |
| NFS export rule | allow **`192.168.4.0/24`**, Read/Write, **Map all users to admin**, async, non-privileged ports allowed |
| NFS subfolders (must exist) | `ha-config`, `zwavejs-config`, `mosquitto-data`, `ring-mqtt-data` under the export |
| Repo location | `~/hawksnest-automation` on Dragonfly |
| Active branch | `claude/happy-babbage-07raqh` |
| Secrets | `kustomize/overlays/prod/secrets/{mariadb.env,mosquitto.passwd,ring-mqtt.env}` (gitignored; **inside** the kustomize root on purpose — see §6) |

> ⚠️ **Cross-subnet gotcha:** the PC (`192.168.4.x`) and the NAS (`192.168.5.x`) are on
> **different /24s** that the router bridges. WSL2 NATs outbound traffic so the NAS sees
> the *PC's* LAN IP (`192.168.4.34`), **not** the internal WSL IP (`172.20.x`). The NFS
> rule therefore allows `192.168.4.0/24`. If NFS mounts start failing with
> "access denied by server," first check whether the PC's LAN IP changed subnet.

---

## 3. Architecture as-built

```
Windows 11 (user Sonic, LAN 192.168.4.34)
├─ Scheduled task "HomeAssistant-Boot" (at logon, highest priv) -> C:\ha\boot.ps1
│    ├─ C:\ha\attach-zwa2.ps1   (USB passthrough; no-op until controller present)
│    └─ C:\ha\portproxy-ha.ps1  (netsh portproxy 0.0.0.0:8123 -> <wsl-ip>:30123 + firewall)
└─ WSL2 "Dragonfly"
   └─ K3s (node: dragonfly)
      └─ namespace: home-automation
         ├─ Deployment home-assistant   -> Service NodePort 30123 (HA UI :8123)
         │     initContainers: wait-for-mariadb, seed-config
         ├─ Deployment zwave-js-ui       -> Service (ws :3000, ui :8091)  [PARKED replicas=0]
         ├─ Deployment mariadb           -> Service mariadb:3306  (recorder DB)
         ├─ Deployment mosquitto         -> Service (MQTT :1883)  (broker; used by ring-mqtt)
         └─ Deployment ring-mqtt         -> Service (rtsp :8554, webrtc :8555, api :1984, web :55123)
         PVCs:
           ha-config       -> NFS  (MUST BACK UP)
           zwavejs-config  -> NFS  (MUST BACK UP — losing it = re-pair every lock)
           ring-mqtt-data  -> NFS  (MUST BACK UP — holds the Ring refresh token)
           mosquitto-data  -> NFS
           mariadb-data    -> local-path (node-local; recorder history; regenerable)
```

Manifests are kustomize, split into a shared `kustomize/base/` and per-environment
`kustomize/overlays/{prod,staging}/`. Deploy prod with `kubectl apply -k kustomize/overlays/prod/`
(or `./scripts/deploy.sh`). The `secretGenerator` in `kustomize/overlays/prod/kustomization.yaml`
builds `mariadb-credentials`, `mosquitto-credentials`, and `ring-mqtt-credentials` from the
gitignored files in `kustomize/overlays/prod/secrets/`. The prod overlay renders byte-equivalent
to the old flat tree (CI/scripts verify this with a normalized diff). See §11 for the staging
overlay and the Home Assistant config-validation gates.

**Networking decision:** HA is exposed via **NodePort 30123 + Windows portproxy**
(not host-network), keeping cluster DNS intact so HA resolves `mariadb`/`zwave-js-ui`
by service name. Remote access is **Tailscale only**; no public internet ports.

---

## 4. Bring-up from scratch (full recovery / new machine)

1. **WSL2 distro:** `wsl --install -d Ubuntu --name Dragonfly`; create unix user.
2. **systemd + DNS** in `/etc/wsl.conf`:
   ```ini
   [boot]
   systemd=true
   [network]
   generateResolvConf=true
   ```
   then `wsl --shutdown`, reopen; verify `ps -p 1 -o comm=` -> `systemd`.
3. **NFS client (before K3s):** `sudo apt-get update && sudo apt-get install -y nfs-common`.
4. **K3s:** `curl -sfL https://get.k3s.io | sh -`; wait for node Ready.
5. **kubeconfig:** copy `/etc/rancher/k3s/k3s.yaml` to `~/.kube/config`, `chown`, export `KUBECONFIG`.
6. **Synology NFS:** enable NFS service; share on Volume 3 (`/volume3/home-automation`);
   NFS rule `192.168.4.0/24` RW, map-all-to-admin, async, non-priv ports; create the
   four subfolders (`ha-config`, `zwavejs-config`, `mosquitto-data`, `ring-mqtt-data`).
   Verify from the node:
   `sudo mount -t nfs -o vers=3 192.168.4.21:/volume3/home-automation /mnt/x`.
7. **Repo + secrets:**
   ```bash
   git clone https://github.com/CDRaab01/hawksnest-automation.git ~/hawksnest-automation
   cd ~/hawksnest-automation && git checkout claude/happy-babbage-07raqh
   cp kustomize/overlays/prod/secrets/mariadb.env.example kustomize/overlays/prod/secrets/mariadb.env   # set strong pw (URL-safe; see §6)
   mosquitto_passwd -c -b kustomize/overlays/prod/secrets/mosquitto.passwd ring '<url-safe-pw>'  # needs the `mosquitto` apt pkg
   # mosquitto_passwd -b kustomize/overlays/prod/secrets/mosquitto.passwd ratgdo '<pw>'  # optional, appends (no -c)
   chmod 0600 kustomize/overlays/prod/secrets/mosquitto.passwd
   cp kustomize/overlays/prod/secrets/ring-mqtt.env.example kustomize/overlays/prod/secrets/ring-mqtt.env  # set RING_MQTT_PASSWORD=<url-safe-pw>
   ```
8. **Deploy:**
   ```bash
   kubectl apply -k kustomize/overlays/prod/
   kubectl scale deploy/zwave-js-ui --replicas=0 -n home-automation   # until the USB stick exists
   ```
9. **Windows host:** install `usbipd` (`winget install usbipd`); copy the two `windows/*.ps1`
   to `C:\ha\`; create `C:\ha\boot.ps1` (see `windows/README-windows.md`); register the
   `HomeAssistant-Boot` logon scheduled task; run it once.

---

## 5. Day-2 operations

- **Apply manifest changes:** `./scripts/deploy.sh` (preferred — validates the live HA config
  first, see §11) or `kubectl apply -k kustomize/overlays/prod/`.
  - ⚠️ **`apply -k` resets `zwave-js-ui` to `replicas: 1`** (the manifest says 1). Until the
    controller is attached and its `by-id` path is filled in, **re-run**
    `kubectl scale deploy/zwave-js-ui --replicas=0 -n home-automation` after every apply,
    or it crash-loops on the missing device. (`deploy.sh` handles this automatically.)
- **Test a change before prod:** `OVERLAY=staging ./scripts/deploy.sh` deploys the whole stack
  to an isolated `home-automation-staging` namespace (Z-Wave parked, storage on `local-path`,
  HA ClusterIP) so you can prove all pods reach Ready without touching the locks or the NAS.
  Tear down with `kubectl delete ns home-automation-staging`. See §11.
- **Access HA:** `http://localhost:8123` (on the PC), `http://192.168.4.34:8123` (LAN),
  `http://<PC-tailscale-ip>:8123` (Tailscale).
- **Restart HA / others:** `kubectl rollout restart deploy/home-assistant -n home-automation`.
- **Logs:** `kubectl logs deploy/home-assistant -n home-automation` (the `seed-config` /
  `wait-for-mariadb` initContainers are separate: add `-c <name>`).
- **After a host reboot:** logon triggers `C:\ha\boot.ps1` -> portproxy (and USB attach).
  If HA is unreachable, check the portproxy table: `netsh interface portproxy show v4tov4`.
- **Moving the NAS (or renumbering the LAN):** `spec.nfs` on a PersistentVolume is **immutable**.
  Editing `kustomize/base/storage/nfs-pv.yaml` alone is not enough — `kubectl apply` fails with
  `spec.persistentvolumesource is immutable after creation`, and because that aborts the whole
  apply, *every step after it in the deploy is skipped*. That is exactly how four consecutive
  deploys silently stopped rolling `ring-timeline` in July 2026: the NAS moved from `192.168.5.78`
  to `192.168.4.21`, the PVs were recreated by hand at the new address, and the manifests kept the
  old one. `deploy.sh` now pre-flights this with a server-side dry run and names the offending
  objects. The procedure:
  ```bash
  showmount -e <new-nas-ip>                       # confirm the address before touching anything
  # reclaimPolicy is Retain: deleting the PV object does NOT delete data on the NAS
  kubectl delete pv ha-config zwavejs-config mosquitto-data ring-mqtt-data --wait=false
  # update spec.nfs.server in kustomize/base/storage/nfs-pv.yaml, then:
  ./scripts/deploy.sh                             # recreates the PVs; the existing PVCs re-bind
  ```

---

## 6. Decisions & gotchas discovered (do not re-learn these the hard way)

1. **NFSv4.1 fails on the DS214** — it's a v3-only NAS. PVs use `nfsvers=3`
   (`kustomize/base/storage/nfs-pv.yaml`). Don't "upgrade" to v4.1.
2. **PC and NAS are on different subnets** (see §2 gotcha). NFS rule must allow the *PC's*
   subnet because of WSL2 NAT.
3. **`secrets/` must live inside each overlay** — kustomize's default load restrictor
   refuses files above the kustomization root (`"not in or below"` error). Prod's are at
   `kustomize/overlays/prod/secrets/` and gitignored; staging's dummies are at
   `kustomize/overlays/staging/secrets/` (the `*.example` files are tracked).
3b. **NEVER deploy with a bare `kubectl apply -k`. Use `scripts/deploy.sh`.** This one
   caused a real outage on 2026-08-02, and the failure is quiet enough to be worth
   spelling out.

   The real prod secrets live **only** on the runner host, at
   `HAWKSNEST_SECRETS_DIR` (`/home/sonic/hawksnest-secrets`) — never in the checkout,
   never in git. `deploy.sh` copies them into the overlay before applying. A bare
   `kubectl apply -k` skips that step, so `secretGenerator` builds the Secrets from
   whatever `secrets/*.env` happens to be in the working tree — and with
   `disableNameSuffixHash: true` those overwrite the live Secrets **in place, under the
   same name**, with no new object and nothing to notice.

   It gets worse in three specific ways:
   - **The checkout can contain convincing fakes.** CI stages `*.example` → `*.env` so
     `kustomize build` resolves. If that ever runs locally (or someone mirrors it), the
     tree holds byte-identical placeholder copies that look like real secrets. Compare
     against the `.example` to tell: `cmp -s foo.env foo.env.example` means placeholder.
   - **`need_secret()` / `optional_secret()` skip any file already present in the
     checkout.** So once placeholders are there, running `deploy.sh` does *not* rescue
     you — it sees the files and leaves them alone. **Delete the placeholder `*.env` /
     `*.passwd` from the overlay first**, then run `deploy.sh` so it copies the real
     ones in. That deletion was the actual fix on 2026-08-02.
   - **Nothing breaks until a pod restarts.** A Secret change does not roll pods, so
     everything keeps running on credentials held in its own environment. The cluster
     looks completely healthy while every workload is one restart away from coming up
     with `replace-with-…` values. On 2026-08-02 that landed on go2rtc first (all
     Reolink live view down), then Frigate (all seven cameras down) — hours after the
     apply that caused it.

   To check whether the live Secrets are real without printing them:
   ```sh
   kubectl get secret frigate-credentials -n home-automation \
     -o jsonpath='{.data.FRIGATE_REOLINK_PASSWORD}' | base64 -d | grep -q '^replace-with-' \
     && echo PLACEHOLDER || echo real
   ```
   Recovery is `deploy.sh` (after deleting the placeholders) followed by an explicit
   `rollout restart` of anything already running on the bad values — `deploy.sh` only
   waits on the lock/alarm-critical set (mariadb, mosquitto, home-assistant,
   zwave-js-ui, ring-mqtt) and will not restart frigate or go2rtc for you.

   Running it as root needs both `$HOME`-relative defaults overridden, since neither
   lives under `/root`:
   ```sh
   HAWKSNEST_SECRETS_DIR=/home/sonic/hawksnest-secrets \
   KUBECONFIG=/home/sonic/.kube/config ./scripts/deploy.sh
   ```
4. **MariaDB readiness race** — HA's recorder fails *permanently for that boot* if it
   starts before MariaDB finishes first-boot DB init. Fixed with a `wait-for-mariadb`
   initContainer on the HA deployment. If recorder is ever down after a change, check that
   initContainer ran; a plain `rollout restart` recovers it once MariaDB is up.
5. **MariaDB password should be URL-safe.** It is embedded into the recorder
   `mysql://user:PASSWORD@mariadb/...` URL via `secrets.yaml` *without* URL-encoding. Avoid
   `/`, `@`, `+`, `=` etc. Prefer `openssl rand -hex 24`, not `-base64`. (Not yet hit, but
   latent.)
6. **`mosquitto_passwd` lives in the `mosquitto` apt package** (not `mosquitto-clients`),
   and needs `-c` to create the file; appending to an empty/world-readable file silently
   produces an empty file. `chmod 0600` after.
7. **`usbipd attach` is not persistent** across reboot/replug — re-attached by the logon
   task. This is the known fragile link (see `windows/README-windows.md`).
8. **`boot.ps1` runs each sub-script in its own `powershell -File` process** so that
   `attach-zwa2.ps1` failing (e.g. stick absent) doesn't abort the portproxy step.
9. **ZWA-2 VID:PID confirmed: `303a:4001`** (Espressif-based USB; enumerates as
   `/dev/ttyACM0`). `attach-zwa2.ps1` now defaults to this. Caveat: `303a` is Espressif's
   vendor ID, shared by bare ESP32 dev boards — if an ESP32 is plugged in at the same time,
   select by bus id instead. Stable device:
   `/dev/serial/by-id/usb-Nabu_Casa_ZWA-2_9070690E14E4-if00` (filled into the manifest).
   - ⚠️ **Keep `C:\ha\attach-zwa2.ps1` in sync with the repo.** A stale copy that hunts for
     the old `10c4:ea60` will error (`not found in 'usbipd list'`) and the logon task won't
     attach the stick. Refresh it with
     `cp ~/hawksnest-automation/windows/attach-zwa2.ps1 /mnt/c/ha/attach-zwa2.ps1`.
10. **zwave-js-ui empty-dir boot race (USB ordering).** On boot the pod can start *before*
   `attach-zwa2.ps1` passes the ZWA-2 into WSL2. With the device path absent, kubelet
   (hostPath `type` unset) bind-creates an **empty directory** at
   `/dev/serial/by-id/usb-Nabu_Casa_ZWA-2_...-if00` and mounts it at `/dev/zwave`. The pod
   then runs **1/1 but the WS server never starts**, so HA shows *"Cannot connect to host
   zwave-js-ui:3000"* even though the Service endpoints are present. Worse, that squatting
   directory **blocks udev** from creating the real by-id symlink when the stick attaches a
   moment later.
   - **Symptoms:** `kubectl exec deploy/zwave-js-ui -- ls -l /dev/zwave` shows `total 0`
     (a directory) instead of a `crw-` char device; `ls -l /dev/serial/by-id/` shows the
     `...-if00` name as a **directory** while `/dev/ttyACM0` exists as a real char device
     (compare timestamps — the dir predates the device by a minute or two).
   - **Recovery:**
     ```bash
     kubectl scale deploy/zwave-js-ui --replicas=0 -n home-automation
     sudo rm -rf /dev/serial/by-id/usb-Nabu_Casa_ZWA-2_9070690E14E4-if00   # the bogus dir
     sudo udevadm trigger --action=add /dev/ttyACM0                         # recreate symlink
     ls -l /dev/serial/by-id/                                               # expect ...-if00 -> ../../ttyACM0
     kubectl scale deploy/zwave-js-ui --replicas=1 -n home-automation
     ```
     If the symlink doesn't reappear, force a fresh enumeration from Windows
     (`usbipd detach --busid <id>` then `usbipd attach --busid <id> --wsl Dragonfly`).
   - **Prevent:** make sure the logon task attaches the stick *before* K3s starts pods, and
     keep `attach-zwa2.ps1` current (gotcha #9). After any host reboot, verify
     `ls -l /dev/zwave` in the pod is a `crw-` device, not `total 0`.

---

## 7. Remaining work: Z-Wave bring-up (next session, once the ZWA-2 is in hand)

1. ~~Install/confirm `usbipd`; confirm the ZWA-2 bus id + VID:PID.~~ ✅ Done: `303a:4001`,
   bus `6-4`, serial `9070690E14E4`.
2. ~~`usbipd bind` + `usbipd attach`.~~ ✅ Attached. (usbipd 5.x syntax: `usbipd attach
   --busid 6-4 --wsl Dragonfly`, or just run `attach-zwa2.ps1`.)
3. ~~`ls -l /dev/serial/by-id/` -> copy the `usb-...-if00` path.~~ ✅
   `usb-Nabu_Casa_ZWA-2_9070690E14E4-if00`.
4. ~~Put that path into `kustomize/base/zwave-js-ui/deployment.yaml`.~~ ✅ Filled in. Now
   `kubectl apply -k kustomize/overlays/prod/` and **let zwave-js-ui run at replicas 1** (the deploy script
   no longer parks it, since the `REPLACE-` placeholder is gone).
5. Z-Wave JS UI at `http://localhost:8091` (port-forward or temporary portproxy): set serial
   port to **`/dev/zwave`**, **generate S2 security keys** (persist in `zwavejs-config`,
   **also store in the password manager**), enable WS server :3000.
6. In HA: add the **Z-Wave** integration pointed at `ws://zwave-js-ui:3000`.
7. Pair the **front + back Schlage BE469ZP** deadbolts with **S2** (near the controller if it
   fails at distance — battery locks don't repeat). Assign user code slots: **1=Christian,
   2=Elizabeth**, 3+ reserved for guests.
8. **Reboot drill:** reboot the host, let the logon task re-attach, confirm zwave-js-ui sees
   the controller and locks report state with **no re-pairing**.

See [`README.md`](./README.md) for the fuller post-deploy/pairing walkthrough and
[`windows/README-windows.md`](./windows/README-windows.md) for the USB chain detail.

---

## 7b. ring-mqtt bring-up (Ring cameras/doorbell into HA)

`ring-mqtt` bridges Ring devices to MQTT and exposes **on-demand** live video over RTSP.
It is **not** a Frigate/NVR source — Ring has no continuous local stream, and streaming
continuously would suppress Ring's own motion/ding events. **Frigate is LIVE since
2026-07-29** with three Reolink cameras (sub-stream → Frigate detect+record, main
stream → go2rtc live); bring-up findings and the operating rules are in
[plan.md](./plan.md).

1. Ensure the **`ring` user** is in `kustomize/overlays/prod/secrets/mosquitto.passwd` and
   `RING_MQTT_PASSWORD` in `kustomize/overlays/prod/secrets/ring-mqtt.env` **matches** it. The password
   is embedded in the broker URL, so keep it **URL-safe** (letters/digits/`-`/`_`).
   (Mosquitto must be restarted after adding the user: `kubectl rollout restart
   deploy/mosquitto -n home-automation`.)
2. `kubectl apply -k kustomize/overlays/prod/`. ring-mqtt waits for Mosquitto, seeds `/data/config.json`
   (broker URL injected from the Secret), then serves its web UI on `:55123`.
3. **Generate the Ring refresh token (one-time, interactive 2FA) via the web UI:**
   ```bash
   kubectl port-forward deploy/ring-mqtt 55123:55123 -n home-automation
   ```
   Open `http://localhost:55123` (WSL2 forwards localhost to Windows), sign in with the
   Ring email/password + 2FA code. The token is stored in `ring-state.json` on the
   `ring-mqtt-data` PVC (NFS, backed up). (ring-mqtt v5.x generates the token through this
   web UI; the older `init-ring-mqtt.js` CLI no longer exists.)
4. In HA, add the **MQTT** integration (broker `mosquitto`, port `1883`) if not already.
   Ring devices then appear automatically via MQTT discovery (camera live view, doorbell
   ding, motion, battery).
5. **Arm/disarm panel (Ring Location Modes):** the deployment sets `ENABLEMODES=true`, so
   ring-mqtt publishes the Ring **Location Modes** (Disarmed / Home / Away) as an HA
   `alarm_control_panel` entity. This is the "ring alarm" Hawksnest's security panel arms
   and disarms — it works even on camera/doorbell-only accounts with no Ring Alarm base
   station. (If you *do* have a Ring Alarm system, its own `alarm_control_panel` appears
   regardless.) Confirm with **Developer Tools → States** in HA: an
   `alarm_control_panel.*` entity should report `disarmed` / `armed_home` / `armed_away`.
   > `enable_modes` is also set in `configmap.yaml`, but that seed only applies on FIRST
   > boot — an already-running ring-mqtt keeps its PVC `config.json`, so the `ENABLEMODES`
   > env var is what enables modes on an existing deployment. After changing it,
   > `kubectl rollout restart deploy/ring-mqtt -n home-automation`.
6. **Reboot drill:** reboot the host → ring-mqtt reconnects and the token persists on NFS;
   no re-auth needed.

> The refresh token grants full access to the Ring account. It lives only on the
> backed-up `ring-mqtt-data` PVC, never in git.

---

## 7c. go2rtc two-way audio ("talk") bring-up

The Hawksnest app's **walkie-talkie** (push-to-talk) and siren-adjacent live view need a
**back-channel** (audio *toward* the camera). ring-mqtt's embedded go2rtc can't provide
this — it runs with its API/WebRTC disabled and bridges Ring as a **one-way** RTSP `exec:`
source. So we run a **dedicated go2rtc** (`kustomize/base/go2rtc/`) using go2rtc's **native
`ring:` source**, which supports two-way audio. (It also gives lower-latency live than the
HA path, though live still works fine without any of this.)

It runs at **`replicas: 1` in prod** (enabled 2026-07-13; it was parked at 0 while
unconfigured). This is deploy-safe even before the real secret lands: `deploy.sh` falls back
to the dummy example secret, go2rtc still boots and serves `/api/streams` (probes pass — the
`ring:` streams just error until real creds arrive), and go2rtc is deliberately **not** in
`deploy.sh`'s rollout-wait list, so it can never fail a lock-cluster deploy. Staging keeps it
parked permanently via its overlay patch (and its `go2rtc-webrtc` Service is ClusterIP so the
NodePort can't collide).

The app talks to it as: browser/app → `/go2rtc/` nginx proxy → go2rtc API (`:1984`,
signaling) → WebRTC **media** on the host at `GO2RTC_HOST_IP:8555/tcp` (socat →
NodePort 30855).

**Setup:**

1. **Fill `kustomize/overlays/prod/secrets/go2rtc.env`** (copy from `go2rtc.env.example`).
   Until this exists with real values, deploys fall back to the dummy template — go2rtc runs
   but its `ring:` streams error (harmless).
   - `RING_REFRESH_TOKEN` — generate from a **separate** Ring login than ring-mqtt's
     (two clients sharing one token rotate each other out). Easiest: port-forward the
     running pod (`kubectl port-forward deploy/go2rtc 1984 -n home-automation`), open
     `http://localhost:1984`, **Add > Ring**, sign in; copy the resulting `device_id`s too.
   - `GO2RTC_HOST_IP` — the Windows host's **Tailscale** IP (or LAN IP) clients reach.
   - `RING_DEVICE_ID_*` — one per camera.
2. **Edit `kustomize/base/go2rtc/configmap.yaml`** so each `streams:` entry is named
   **exactly the HA camera base** (`camera.<base>` → `<base>`); the app derives the go2rtc
   `src` from it. Add one line per camera.
3. **Deploy + roll:** `./scripts/deploy.sh` (or `kubectl apply -k kustomize/overlays/prod/`);
   `kubectl rollout restart deploy/go2rtc` after any secret edit — stable secret names don't
   auto-roll. (`replicas: 1` already ships in base; staging stays parked via its overlay patch.)
4. **Media-port exposure — socat, NOT netsh portproxy** (portproxy is dead under WSL
   mirrored networking: it targeted a NAT-era 172.x WSL IP, and a NodePort/hostPort is DNAT
   with no listening socket, unreachable from Windows — same story as HA's
   `ha-forwarder.service`). Install the systemd unit once in the Dragonfly distro:
   ```bash
   sudo cp windows/go2rtc-forwarder.service /etc/systemd/system/
   sudo systemctl daemon-reload && sudo systemctl enable --now go2rtc-forwarder.service
   ```
   (socat `:8555` → the `go2rtc-webrtc` NodePort `30855`.) Plus a one-time (elevated)
   Hyper-V firewall allow for LAN/Tailscale clients, mirroring `HomeAssistant-8123`:
   ```powershell
   New-NetFirewallHyperVRule -Name 'Go2rtc-8555' -DisplayName 'Go2rtc-8555' `
     -Direction Inbound -VMCreatorId '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' `
     -Protocol TCP -LocalPorts 8555 -Action Allow
   ```
   Survives reboots (systemd unit auto-starts) — no per-boot re-add like the old portproxies.
5. **HTTPS for the browser mic:** browsers only grant microphone access in a **secure
   context** (HTTPS or `localhost`). If you open Hawksnest over plain `http://…:30123` the
   talk button can't get the mic. Reach it via a Tailscale HTTPS name (`tailscale cert` /
   MagicDNS) or `localhost`. The **Android** app has no such constraint (runtime
   `RECORD_AUDIO` permission).

> **Step 4 was never actually done, and that is why talk never worked** (found 2026-08-05).
> No `go2rtc-forwarder.service` had ever existed in the distro, nothing listened on `:8555`
> (`Test-NetConnection 127.0.0.1 -Port 8555` → False, against True for 8090/8123/8391), and no
> `Go2rtc-8555` firewall rule existed. go2rtc advertised an ICE candidate pointing at a port
> with no socket behind it, so every talk session opened its WebSocket, negotiated, and simply
> never connected — on **every** camera, doorbell included. Nothing in the app looked broken and
> nothing logged an error.
>
> Quick replies kept working throughout, which is what made this hard to see: they are a
> server-side `dst=` call to go2rtc's API on `:1984` through the nginx proxy, so they never touch
> `:8555`. **"Replies work" is not evidence that the media path is up.** The check that is
> evidence is the `Test-NetConnection` above.

**Verification checklist:**

- [ ] `curl http://localhost:1984/api/streams` (port-forwarded) lists each camera by its
      `<base>` name, producers present (Ring online).
      **The name is the HA ENTITY BASE, not the camera's friendly name** — four Ring streams
      were named by the friendly slug until 2026-08-05 and were unreachable the whole time.
      See the ConfigMap's Ring block for the mapping and why the entity id is the stable side.
- [ ] Something is listening on the host: `Test-NetConnection -ComputerName 127.0.0.1 -Port 8555`
      returns True. This is step 4 and it is the one that gets skipped.
- [ ] In go2rtc's web UI, the camera's **stream** plays *with* a microphone option
      (two-way) — confirms the back-channel. True for the native `ring:` sources and for the
      Reolinks, which carry it over ONVIF (they advertise `PCMU/8000` sendonly).
- [ ] From the app over Tailscale, opening a camera and pressing **Talk** connects (ICE
      reaches `GO2RTC_HOST_IP:8555`); audio is heard from the camera.
- [ ] After a host reboot, talk still connects with no re-auth (the socat unit auto-starts).

> `go2rtc-config` is node-local (`local-path`), not backed up: it only caches the rotated
> token, which re-seeds from `go2rtc.env` + the ConfigMap. Losing it just needs a
> `rollout restart` (and possibly a fresh token if Ring rotated it).

---

## 8. Backups (critical)

- **`zwavejs-config`**, **`ha-config`**, and **`ring-mqtt-data`** PVCs are the must-back-up
  volumes (`ring-mqtt-data` holds the Ring account refresh token; losing it means
  re-authenticating with 2FA). Enable **Synology snapshots** on the `home-automation`
  shared folder. The S2 keys also go in the password manager.
- `mariadb-data` is node-local and regenerable — not backed up by design.

## 9. Possible follow-on: GitOps

Pull-based GitOps (Argo CD or Flux) suits this NAT'd home cluster (GitHub can't reach in).
Easiest secret strategy: keep the two secrets bootstrapped by hand (they rarely change) and
let GitOps manage everything else; alternatives are Sealed Secrets or SOPS+age. Not yet set up.

A lighter-weight middle ground is now in place — see §10 (deploy from GitHub via a
self-hosted Actions runner). GitOps remains the longer-term option if continuous
reconciliation is wanted.

---

## 10. Deploy from GitHub (self-hosted Actions runner)

> **Why self-hosted:** the cluster is behind NAT (§9) — GitHub's hosted runners cannot
> reach `kubectl`. So a runner is installed **inside Dragonfly**, where it reaches the
> cluster locally via `~/.kube/config`. GitHub only ever hands it a job; nothing is exposed
> to the internet. This is the same trust boundary as Tailscale-only access.

**What's in the repo:**
- `.github/workflows/deploy.yml` — runs on `[self-hosted, linux, dragonfly]`. Triggers on the
  manual **Run workflow** button (`workflow_dispatch`, with an optional `unpark_zwave`
  checkbox) **and** on push to `main` that touches `kustomize/**`, `scripts/deploy.sh`, or the
  workflow itself.
- `scripts/deploy.sh` — does the actual work and can also be run by hand on Dragonfly. It
  encodes the door-lock safety rules so a deploy can't silently break them:
  - **Secrets:** the real `mariadb.env` / `mosquitto.passwd` are gitignored, so a fresh
    checkout has none. The script copies them in from a stable on-host dir
    (`HAWKSNEST_SECRETS_DIR`, default `~/hawksnest-secrets`) before apply, and **fails loudly**
    if they're absent. Secrets stay **off GitHub** (no repo/Actions secrets needed).
  - **zwave-js-ui parking:** `apply -k` resets it to `replicas:1`, which crash-loops while the
    ZWA-2 is absent (§6.9). The script parks it at `0` **only while the device path is still a
    `REPLACE-` placeholder**. Once the real `by-id` path is committed (controller wired, locks
    paired), every deploy — push-triggered or manual — **leaves zwave-js-ui running**, so a
    routine deploy never scales the controller down and takes the door locks offline.
    `UNPARK_ZWAVE=true` is just an escape hatch to force-run while the path is still a placeholder.
  - Validates the kustomize build first, then runs the **live HA config check** (§11) before
    apply, then waits on the always-on rollouts and fails the job if any don't settle.
  - **Overlay:** defaults to `prod`. The workflow's `overlay` dropdown (or `OVERLAY=staging`
    by hand) targets the staging namespace instead; a push to `main` always deploys prod.

### One-time runner setup (on Dragonfly)

Run as the same unix user that owns `~/.kube/config` (i.e. `sonic`):

```bash
# 1. Bootstrap the secrets once on the host (kept out of git AND off GitHub):
mkdir -p ~/hawksnest-secrets && chmod 700 ~/hawksnest-secrets
cp ~/hawksnest-automation/kustomize/overlays/prod/secrets/mariadb.env   ~/hawksnest-secrets/   # if already created
cp ~/hawksnest-automation/kustomize/overlays/prod/secrets/mosquitto.passwd ~/hawksnest-secrets/
cp ~/hawksnest-automation/kustomize/overlays/prod/secrets/ring-mqtt.env ~/hawksnest-secrets/
chmod 600 ~/hawksnest-secrets/*
#   (or create them fresh here per README §"Create the secrets")

# 2. Install the runner (get the token from GitHub:
#    repo → Settings → Actions → Runners → New self-hosted runner → Linux):
mkdir -p ~/actions-runner && cd ~/actions-runner
curl -o runner.tar.gz -L https://github.com/actions/runner/releases/latest/download/actions-runner-linux-x64.tar.gz
tar xzf runner.tar.gz
./config.sh --url https://github.com/CDRaab01/hawksnest-automation \
            --token <RUNNER_TOKEN> \
            --name dragonfly \
            --labels self-hosted,linux,dragonfly \
            --unattended

# 3. Run it as a systemd service so it survives reboots (Dragonfly has systemd):
sudo ./svc.sh install sonic
sudo ./svc.sh start
```

> The runner needs `kubectl` on its `PATH` (already true for `sonic`) and a working
> `~/.kube/config`. The workflow passes no kubeconfig — `deploy.sh` defaults to
> `~/.kube/config`. Override with the `KUBECONFIG` env if yours lives elsewhere.

### Using it

- **Manual:** GitHub → **Actions → Deploy Hawksnest → Run workflow**. Leave `unpark_zwave`
  unchecked normally — now that the real `by-id` path is committed, zwave-js-ui runs on every
  deploy anyway. The checkbox is only an escape hatch to force-run while the path is still a
  `REPLACE-` placeholder.
- **Automatic:** merge a manifest change to `main` and it deploys. (The active dev branch is
  `claude/happy-babbage-07raqh`; the push trigger watches `main` — adjust `branches:` in the
  workflow if you want a different deploy branch.)
- **By hand (no GitHub):** `cd ~/hawksnest-automation && ./scripts/deploy.sh`
  (add `UNPARK_ZWAVE=true` once the controller is attached).

> ⚠️ This applies changes to **live door locks**. Keep the default trigger conservative; the
> manual button is the safest path, and pushes only fire on `main` for manifest paths.

---

## 11. Pre-prod testing & Home Assistant config validation

Added after an incident where a deploy left a service down because the new pod never came up
on the live cluster. Two layers now sit in front of that failure class.

### Base + overlays

`kustomize/` is `base/` (shared workloads + storage) plus `overlays/prod/` and
`overlays/staging/`. **prod** pins the real namespace + secrets and renders byte-equivalent to
the old flat tree (verify: `kustomize build kustomize/overlays/prod` vs a baseline — CI checks
both overlays build + schema-validate). **staging** is the same stack made safe to run beside
prod on the one K3s node:

| Concern | Staging divergence (patch) | Why |
|---|---|---|
| Namespace | `home-automation-staging` | no resource collision with prod |
| Z-Wave | `replicas: 0` | only one ZWA-2 stick exists; prod owns it |
| HA Service | `ClusterIP` (no NodePort) | NodePort `30123` is unique per **cluster**, not namespace |
| Storage | all PVCs → `local-path`, NFS PVs deleted | never touches the Synology / prod data |
| Secrets | dummy `*.example` files | no real credentials needed |

Smoke-test then promote:
```bash
OVERLAY=staging ./scripts/deploy.sh                 # all pods must reach Ready; prod untouched
kubectl port-forward -n home-automation-staging deploy/home-assistant 8124:8123   # optional peek
./scripts/teardown-staging.sh                       # teardown (or the Actions "Teardown Staging" button)
# promote: merge to main (auto-deploys prod) or run OVERLAY=prod ./scripts/deploy.sh
```

### HA config-validation gates

A malformed `configuration.yaml` used to surface only as a failed HA boot on the live cluster.
Now Home Assistant's own `check_config` runs in two places (same image tag as the Deployment,
`:stable`):

- **Gate A — CI (`ha-config-check` job):** validates the committed **seed** config in
  `kustomize/base/home-assistant/configmap.yaml`. Catches a bad seed before it reaches a fresh
  cluster's first boot. CI can't see live edits, so:
- **Gate B — `deploy.sh`, before apply:** runs `check_config` against the **live** config on the
  `ha-config` PVC via a one-shot Job. If it's invalid, the deploy **aborts before `kubectl apply`**
  — the running HA keeps its good config, so a bad edit can't roll out a crash-looping HA and drop
  the locks. A mount/scheduling stall (e.g. the RWO PVC is held by the running HA pod) only warns
  and continues; only a genuine `check_config` failure aborts. Skip with `SKIP_HA_CONFIG_CHECK=true`
  (not recommended on prod); on a fresh cluster with no `ha-config` PVC yet it's skipped automatically.

### Validate locally
```bash
python3 tests/validate_manifests.py            # builds & checks BOTH overlays (needs kustomize/kubectl)
```
