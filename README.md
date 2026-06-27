# Hawksnest Automation — Home Assistant on K3s

Home Assistant + Z-Wave JS UI and supporting services deployed into an existing K3s
cluster (running in WSL2 / `Dragonfly` on a Windows 11 host). This system controls
physical door locks, so the design prioritizes **recoverability** over speed.

Full requirements (intent) live in [`CLAUDE.md`](./CLAUDE.md). The **as-built state,
real environment values, and runbook** live in [`DEPLOYMENT.md`](./DEPLOYMENT.md) —
start there if you're picking this up fresh.

## What's deployed

| Component        | Purpose                                                        | Storage                  |
|------------------|---------------------------------------------------------------|--------------------------|
| `home-assistant` | HA Core; UI on a NodePort. Talks to Z-Wave JS over websocket. | `ha-config` (NFS) ⭐      |
| `zwave-js-ui`    | Owns the ZWA-2 serial stick; manages the Z-Wave mesh.         | `zwavejs-config` (NFS) ⭐ |
| `mariadb`        | HA recorder database (replaces SQLite-on-NFS).                | `mariadb-data` (local-path) |
| `mosquitto`      | MQTT broker — used by `ring-mqtt`; also seam for future Ratgdo. | `mosquitto-data` (NFS)   |
| `ring-mqtt`      | Bridges Ring cameras/doorbell/sensors to MQTT; on-demand live video via RTSP. | `ring-mqtt-data` (NFS) ⭐ |

⭐ = **must be backed up** (see [Backups](#backups)).

> **HA does NOT get the serial device.** Only `zwave-js-ui` owns the ZWA-2; HA connects
> to it at `ws://zwave-js-ui:3000`. This decouples the radio from HA restarts.
>
> **MariaDB datadir is node-local, not on NFS** — a DB datadir over NFS reintroduces the
> file-locking risk we're avoiding. Recorder history is regenerable and intentionally
> not stored on the NAS.

## Repository layout

```
CLAUDE.md                 # the spec
kustomize/                # all Kubernetes manifests (apply with kustomize)
  namespace.yaml
  kustomization.yaml      # wires components + secretGenerator
  storage/                # NFS PVs + PVCs (+ mariadb local-path PVC)
  mariadb/  mosquitto/  zwave-js-ui/  home-assistant/  ring-mqtt/
  secrets/                # *.example templates only; real secrets are gitignored
windows/                  # usbipd attach + portproxy PowerShell + host README
```

## Prerequisites

- K3s reachable via `kubectl` (it ships the `local-path` StorageClass).
- The Synology DS214 NFS export reachable from the cluster node.
- `kustomize` (or `kubectl` ≥ 1.14 with `-k`).
- Windows host set up per [`windows/README-windows.md`](./windows/README-windows.md).

## First-time setup

### 1. Fill in the placeholders

The manifests are split into a shared `kustomize/base/` and per-environment
`kustomize/overlays/{prod,staging}/`. **prod** is the live lock cluster; **staging**
is a throwaway smoke-test namespace on the same K3s (see *Staging* below). Edit
shared values in `base/`:

- **NFS** — in `kustomize/base/storage/nfs-pv.yaml`, replace `REPLACE_NAS_IP` and the
  `/volume1/REPLACE/...` paths for all three PVs.
- **ZWA-2 device** — in `kustomize/base/zwave-js-ui/deployment.yaml`, replace the
  `hostPath.path` with the real `/dev/serial/by-id/usb-...-if00` value
  (find it inside WSL with `ls -l /dev/serial/by-id/`).
- **Timezone** — adjust `TZ` in the HA and zwave-js-ui deployments if not US/Eastern.

### 2. Create the secrets (never committed)

Real (prod) secrets live in `kustomize/overlays/prod/secrets/`:

```bash
cp kustomize/overlays/prod/secrets/mariadb.env.example kustomize/overlays/prod/secrets/mariadb.env
# edit it: set strong MYSQL_ROOT_PASSWORD and MYSQL_PASSWORD   (openssl rand -base64 24)

# create the mosquitto password file (hashed). Either install the tool
# (apt-get install -y mosquitto) and run mosquitto_passwd directly, or via docker.
# Create the 'ring' user (used by ring-mqtt); -c only on the FIRST user. Use a
# URL-safe password (letters/digits/-/_) — it is embedded in the ring-mqtt MQTT URL.
mosquitto_passwd -c -b kustomize/overlays/prod/secrets/mosquitto.passwd ring "URL_SAFE_PASSWORD"
# optional extra user for future Ratgdo (no -c, so it appends):
# mosquitto_passwd -b kustomize/overlays/prod/secrets/mosquitto.passwd ratgdo "SOME_PASSWORD"
chmod 0600 kustomize/overlays/prod/secrets/mosquitto.passwd

# ring-mqtt MQTT password — must match the 'ring' user password above.
cp kustomize/overlays/prod/secrets/ring-mqtt.env.example kustomize/overlays/prod/secrets/ring-mqtt.env
# edit it: set RING_MQTT_PASSWORD=URL_SAFE_PASSWORD
```

> The `secrets/` dir lives **inside** each overlay on purpose: kustomize refuses to
> read files above its root, so a top-level `secrets/` would break `kubectl apply -k`.
> Staging needs no real secrets — it uses the dummy `*.example` files in
> `kustomize/overlays/staging/secrets/` automatically.

### 3. Attach the USB stick and expose the network (Windows host)

Run the scripts in [`windows/`](./windows/README-windows.md) (`attach-zwa2.ps1`,
`portproxy-ha.ps1`) and register the scheduled task so they re-run on boot.

### 4. Apply

```bash
kubectl apply -k kustomize/overlays/prod/
kubectl get pods -n home-automation -w
```

Or use the wrapper that encodes the safety rules (validates the live HA config, re-parks
`zwave-js-ui`, loads secrets, waits on rollouts): `./scripts/deploy.sh`.

### Staging (test before prod)

A `staging` overlay deploys the whole stack to an isolated `home-automation-staging`
namespace on the **same** K3s — Z-Wave parked, all storage on node-local `local-path`
(never touches the Synology), HA on ClusterIP — so a change can be proven to actually
come up before it reaches the live locks:

```bash
OVERLAY=staging ./scripts/deploy.sh          # smoke-deploy; waits for all pods Ready
kubectl port-forward -n home-automation-staging deploy/home-assistant 8124:8123  # peek
./scripts/teardown-staging.sh                # tear down (local-path PVCs go with it)
# or: kubectl delete ns home-automation-staging
```

Teardown is also a one-click button: GitHub → Actions → *Teardown Staging* → Run workflow
(runs on the Dragonfly runner; hard-scoped so it can only ever delete the staging namespace).

Promote by merging to `main` (auto-deploys the prod overlay) or running the deploy with
`OVERLAY=prod`. From GitHub, the *Deploy Hawksnest* workflow has an `overlay` dropdown.

### Deploying from GitHub

A GitHub Actions workflow (`.github/workflows/deploy.yml`) can deploy via a **self-hosted
runner installed inside Dragonfly** — manually (Actions → *Deploy Hawksnest* → Run workflow)
or automatically on push to `main`. The cluster is behind NAT, so the runner lives on the
cluster side; nothing is exposed to the internet. Setup and usage:
[`DEPLOYMENT.md` §10](./DEPLOYMENT.md#10-deploy-from-github-self-hosted-actions-runner).

## Validation / tests

Static checks run in CI (`.github/workflows/ci.yml`, GitHub-hosted — no cluster needed)
and can be run by hand:

```bash
python3 tests/validate_manifests.py   # builds & validates BOTH overlays (needs kustomize/kubectl)
kustomize build kustomize/overlays/prod    | kubeconform -strict -ignore-missing-schemas -
kustomize build kustomize/overlays/staging | kubeconform -strict -ignore-missing-schemas -
```

`validate_manifests.py` validates the **rendered** output of each overlay (it builds them, so
patches are applied) and asserts the wiring schema validation can't see: every PVC / Secret /
ConfigMap a workload references exists, the must-back-up PVCs are present, and the documented
ports (zwave-js-ui WS `3000`, ring-mqtt RTSP `8554`) don't drift. Per overlay it also checks
that **prod** keeps NFS on **v3** (the DS214 is v3-only) with no `REPLACE` placeholders, a real
Z-Wave by-id device path, and HA NodePort `30123`; and that **staging** stays isolated — no NFS
PVs, all storage on `local-path`, Z-Wave parked at `replicas:0`, HA on ClusterIP. CI runs this
plus `kubeconform` on both overlays, and a separate `ha-config-check` job that runs Home
Assistant's own `check_config` against the seed config so a malformed `configuration.yaml`
fails CI instead of HA's first boot.

## Bring-up order

`kubectl apply -k` creates everything at once, but services settle in this order:

1. **Storage** — PVs/PVCs bind (NFS reachable; `local-path` provisions `mariadb-data`).
2. **MariaDB** — becomes Ready (initializes the `homeassistant` DB on first boot).
3. **Mosquitto** — Ready (the `ring` user must exist in the password file).
4. **Z-Wave JS UI** — requires the USB stick already attached into WSL2.
5. **Home Assistant** — initContainer seeds config + `secrets.yaml`, then HA starts and
   connects to MariaDB.
6. **ring-mqtt** — waits for Mosquitto, seeds `config.json`, then serves its web UI
   on `:55123` (the Ring token is generated once there, post-deploy — see below).

If HA starts before MariaDB is ready it will retry the recorder connection; no action needed.

## Accessing HA

- **LAN:** `http://<PC-LAN-IP>:8123` (via the Windows portproxy → NodePort `30123`).
- **Tailscale:** `http://<PC-tailscale-ip>:8123`. No public internet ports are opened.

Complete onboarding (create the owner account, set name/timezone).

## Post-deploy configuration (UI-driven)

### Connect HA to Z-Wave JS

1. Open Z-Wave JS UI at `http://<PC-LAN-IP>:8091` (port-forward or add a temporary
   portproxy if needed: `kubectl port-forward -n home-automation svc/zwave-js-ui 8091:8091`).
2. **Settings → Z-Wave**: set the serial port to **`/dev/zwave`**.
3. **Generate the S2 security keys** (Settings → Z-Wave → Security Keys) if not present.
   These persist in the `zwavejs-config` PVC. **Also record them in the password manager** —
   the Schlage locks pair with S2 Access Control and will not include without them.
4. Enable the **WS Server** (port 3000).
5. In HA: **Settings → Devices & Services → Add Integration → Z-Wave**, point it at
   `ws://zwave-js-ui:3000` (uncheck "use the supervisor").

### Add a Z-Wave device (pairing)

1. In Z-Wave JS UI click **Manage nodes → Include**, choose **S2** (scan/enter DSK as prompted).
2. **Schlage BE469ZP:** tap the outside **Schlage** button, then enter the programming code
   to put the lock in inclusion mode. Pair **near the controller** first if it fails at
   distance — locks are battery devices and do **not** repeat.
3. **Zooz ZEN72:** tap up/down per the manual. These are mains-powered and **do** repeat,
   forming the mesh. Confirm a neutral is present in the box before install.
4. After inclusion, the device appears in HA automatically (locks as `lock` entities,
   dimmers as `light`).

### Lock user codes

Set code slots on each lock (via the lock entity / Z-Wave JS UI user-code panel):
slot **1 = Christian**, slot **2 = Elizabeth**, slots **3+** reserved for guests.
Guest-code expiry automation is deferred, but the slot structure is in place.

### Ring via ring-mqtt (live video + events)

`ring-mqtt` bridges Ring devices into HA over MQTT and exposes **on-demand** live video
via an RTSP/go2rtc gateway. (Ring has no continuous local stream, so this is *not* a
Frigate/NVR source — see [DEPLOYMENT.md](./DEPLOYMENT.md). Frigate is parked until an
RTSP-capable camera exists.)

1. **Add the MQTT integration in HA** (if not already): **Settings → Devices & Services →
   Add Integration → MQTT**, broker `mosquitto`, port `1883`, with a broker user.
2. **Generate the Ring token (one-time, interactive 2FA) via the ring-mqtt web UI:**
   ```bash
   kubectl port-forward deploy/ring-mqtt 55123:55123 -n home-automation
   ```
   Open `http://localhost:55123` (WSL2 forwards localhost to Windows), sign in with your
   Ring email/password + 2FA code. The refresh token is written to `ring-state.json` on
   the `ring-mqtt-data` PVC.
3. ring-mqtt connects to Ring and publishes MQTT discovery — **Ring cameras, doorbell
   ding, motion, and battery entities appear in HA automatically.** Open a camera to
   confirm on-demand live view.
4. **Arm/disarm panel:** the deployment runs ring-mqtt with `ENABLEMODES=true`, so Ring
   **Location Modes** (Disarmed / Home / Away) surface as an HA `alarm_control_panel` —
   the panel Hawksnest's dashboard arms and disarms. This works even on
   camera/doorbell-only Ring accounts (no Ring Alarm base station). If the panel reads
   "No alarm panel" in Hawksnest, check that an `alarm_control_panel.*` entity exists in
   HA (**Developer Tools → States**); if not, ensure `ENABLEMODES=true` and
   `kubectl rollout restart deploy/ring-mqtt -n home-automation`.

> The token grants full access to the Ring account — it lives only on the backed-up
> `ring-mqtt-data` PVC, never in git.

### Ring (official cloud integration — optional)

The built-in HA Ring integration (**Add Integration → Ring**) can be used instead of, or
alongside, ring-mqtt for doorbell/motion/snapshot entities. Nothing about lock control
depends on Ring either way.

## Backups

The PVCs that **must** be backed up:

- **`zwavejs-config`** — the Z-Wave network + S2 security keys. Losing it means
  **re-pairing every device** (and re-entering each lock's programming code).
- **`ha-config`** — HA configuration, automations, dashboards.
- **`ring-mqtt-data`** — the Ring account refresh token. Losing it means
  **re-authenticating with 2FA** (a quick re-run of the token step, but back it up).

All live on the Synology NFS export. Recommended: enable **Synology snapshots** on that
shared folder (cheapest, off-host). The S2 keys are additionally stored in the password
manager as a belt-and-suspenders copy.

`mariadb-data` (recorder history) is **not** critical — it regenerates and is intentionally
node-local.

## Recovery

**Pod / cluster rebuild:** re-`apply -k` with the same NFS PVs. With `zwavejs-config` and
`ha-config` intact, HA and the Z-Wave mesh come back with **no re-pairing**.

**Host reboot:** run the Windows scheduled task (USB re-attach + portproxy). See the
reboot drill in [`windows/README-windows.md`](./windows/README-windows.md).

**Stick replaced (same controller restored):** restore `zwavejs-config`, attach the new
stick, set the port to `/dev/zwave`. Note: a brand-new controller without the restored
NVM backup requires re-pairing — keep the `zwavejs-config` backup current.

## Deferred (not in V1)

WLED, MyQ/Ratgdo garage control, Zigbee, Plex-triggered lighting, presence auto-lock,
guest-code expiry logic. The seams are built in (Mosquitto present, modular `!include`
config, stable PVC naming) so these add later without rework.
