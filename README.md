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
| `mosquitto`      | MQTT broker — installed now for future Ratgdo; harmless idle. | `mosquitto-data` (NFS)   |

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
  mariadb/  mosquitto/  zwave-js-ui/  home-assistant/
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

- **NFS** — in `kustomize/storage/nfs-pv.yaml`, replace `REPLACE_NAS_IP` and the
  `/volume1/REPLACE/...` paths for all three PVs.
- **ZWA-2 device** — in `kustomize/zwave-js-ui/deployment.yaml`, replace the
  `hostPath.path` with the real `/dev/serial/by-id/usb-...-if00` value
  (find it inside WSL with `ls -l /dev/serial/by-id/`).
- **Timezone** — adjust `TZ` in the HA and zwave-js-ui deployments if not US/Eastern.

### 2. Create the secrets (never committed)

```bash
cp kustomize/secrets/mariadb.env.example kustomize/secrets/mariadb.env
# edit kustomize/secrets/mariadb.env: set strong MYSQL_ROOT_PASSWORD and MYSQL_PASSWORD
#   openssl rand -base64 24

# create the mosquitto password file (hashed). Either install the tool
# (apt-get install -y mosquitto) and run mosquitto_passwd directly, or via docker:
mosquitto_passwd -c -b kustomize/secrets/mosquitto.passwd ratgdo "SOME_PASSWORD"
chmod 0600 kustomize/secrets/mosquitto.passwd
```

> The `secrets/` dir lives **inside** `kustomize/` on purpose: kustomize refuses to
> read files above its root, so a top-level `secrets/` would break `kubectl apply -k`.

### 3. Attach the USB stick and expose the network (Windows host)

Run the scripts in [`windows/`](./windows/README-windows.md) (`attach-zwa2.ps1`,
`portproxy-ha.ps1`) and register the scheduled task so they re-run on boot.

### 4. Apply

```bash
kubectl apply -k kustomize/
kubectl get pods -n home-automation -w
```

Or use the wrapper that encodes the safety rules (re-parks `zwave-js-ui`, loads secrets,
waits on rollouts): `./scripts/deploy.sh`.

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
python3 tests/validate_manifests.py   # cross-resource invariants (PyYAML only)
kubectl kustomize kustomize/ | kubeconform -strict -ignore-missing-schemas -   # schema
```

`validate_manifests.py` asserts the wiring schema validation can't see: every PVC / Secret /
ConfigMap a workload references exists, NFS PVs stay on **v3** (the DS214 is v3-only) with no
`REPLACE` placeholders, the Z-Wave controller `hostPath` is the real committed by-id path (not a
stub that would crash-loop zwave-js-ui and drop the locks), the must-back-up PVCs are present,
and the documented NodePort/websocket ports (`30123`, `3000`) don't drift. CI also runs
`kustomize build | kubeconform` for Kubernetes schema validation.

## Bring-up order

`kubectl apply -k` creates everything at once, but services settle in this order:

1. **Storage** — PVs/PVCs bind (NFS reachable; `local-path` provisions `mariadb-data`).
2. **MariaDB** — becomes Ready (initializes the `homeassistant` DB on first boot).
3. **Mosquitto** — Ready (idle until a device connects).
4. **Z-Wave JS UI** — requires the USB stick already attached into WSL2.
5. **Home Assistant** — initContainer seeds config + `secrets.yaml`, then HA starts and
   connects to MariaDB.

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

### Ring (cloud)

**Settings → Devices & Services → Add Integration → Ring**, sign in (expect periodic
re-auth). Doorbell/motion/snapshot entities appear for future automations. Nothing about
lock control depends on Ring.

## Backups

The two PVCs that **must** be backed up:

- **`zwavejs-config`** — the Z-Wave network + S2 security keys. Losing it means
  **re-pairing every device** (and re-entering each lock's programming code).
- **`ha-config`** — HA configuration, automations, dashboards.

Both live on the Synology NFS export. Recommended: enable **Synology snapshots** on that
shared folder (cheapest, off-host). The S2 keys are additionally stored in the password
manager as a belt-and-suspenders copy.

`mariadb-data` (recorder history) is **not** critical — it regenerates and is intentionally
node-local.

## Recovery

**Pod / cluster rebuild:** re-`apply -k` with the same NFS PVs. With `zwavejs-config` and
`ha-config` intact, HA and the Z-Wave mesh come back with **no re-pairing**.

**Host reboot:** run the Windows scheduled task (USB re-attach + portproxy). See the
reboot drill in [`windows/README-windows.md`](./windows/README-windows.md).

**HA unreachable but pods are `Running`:** the Windows→WSL2 portproxy is stale (WSL2 got a
new IP on reboot). Run `windows\heal-ha.ps1` (elevated) to detect and repair it, or see the
["HA unreachable after a reboot" troubleshooting](./windows/README-windows.md) section.

**Stick replaced (same controller restored):** restore `zwavejs-config`, attach the new
stick, set the port to `/dev/zwave`. Note: a brand-new controller without the restored
NVM backup requires re-pairing — keep the `zwavejs-config` backup current.

## Deferred (not in V1)

WLED, MyQ/Ratgdo garage control, Zigbee, Plex-triggered lighting, presence auto-lock,
guest-code expiry logic. The seams are built in (Mosquitto present, modular `!include`
config, stable PVC naming) so these add later without rework.
