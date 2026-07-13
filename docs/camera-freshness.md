# Camera snapshot freshness (battery Ring cams)

**Symptom:** battery-powered Ring cameras show hours-stale snapshot tiles in Hawksnest.

**Root cause:** ring-mqtt exposes per-camera snapshot control as HA entities —
`select.<base>_snapshot_mode` (default **Auto**) and `number.<base>_snapshot_interval`
(battery default 600 s, wired 30 s). Under **Auto**, interval snapshots are enabled only for
**wired** cameras (`interval = !operatingOnBattery` in ring-mqtt's `camera.js`); battery cams
snapshot **only on motion/ding**, so between events the tile is a stale frame no matter how
often the frontend re-polls it. (A `"snapshot_mode"` key in `config.json` is **inert** at
ring-mqtt 5.9.2 — the entities + ring-mqtt saved state on the `ring-mqtt-data` PVC own this.)

**Policy (owner-accepted battery trade-off, 2026-07-13):**
- every camera: `snapshot_mode` → **All** (= Auto's ding+motion **plus** interval on battery),
- battery cameras (those with a `sensor.<base>_battery`): `snapshot_interval` → **300 s**,
- wired cameras keep ring-mqtt's 30 s default interval.

Result: a battery tile is at most ~5 min old (plus the frontend's 10 s poll bucket) instead of
"since the last motion event". Tune per-camera via the number entity if battery drain bites
(600 s is the conservative fallback).

## How it's enforced — `hawksnest_ring_snapshot_policy`

An entity-ID-free HA automation (seeded in
`kustomize/base/home-assistant/configmap.yaml` → `automations.yaml`) applies the policy on
**HA start + hourly**. Self-healing matters: the settings live in ring-mqtt saved state on the
PVC, so a PVC restore or a camera re-discovery silently reverts them to Auto — the automation
puts them back within the hour.

## Live-apply (the seed only reaches fresh installs)

Same dual-write drill as [ntfy-push.md](ntfy-push.md): the running HA's PVC predates this
change, so add the automation to the **live** instance too:

1. Append the `hawksnest_ring_snapshot_policy` block (verbatim from the seed configmap) to
   `/config/automations.yaml`, or recreate it via the UI (Settings → Automations → Edit in YAML).
2. Developer Tools → YAML → **Check configuration**, then **Reload Automations**.
3. Trigger it once by hand (Settings → Automations → Run) — or just wait for the top of the
   hour — then confirm per battery camera:
   - `select.<base>_snapshot_mode` reads **All**
   - `number.<base>_snapshot_interval` reads **300**
4. Watch a battery camera's Hawksnest tile: the age badge should now cycle ≤ ~5 min.

> Battery cams can't take a snapshot **while recording/streaming** — occasional interval gaps
> during events are expected; the motion snapshot covers those windows.
