# Adding a Z-Wave device

The companion to [adding-a-camera.md](adding-a-camera.md), for the other physical fleet.
`README.md` §"Add a Z-Wave device (pairing)" is the nine-line version and is still correct as far
as it goes; this is the one to follow when the device is **not** a Schlage lock, and especially the
first time you add a *kind* of device this house has not had before.

Written out in full while adding the first outdoor device (a Minoston MP22ZD dimmer plug for the
backyard string lights, 2026-09-22). Everything in it is measured against this install.

## Read this first: running these commands from the Windows host

Identical trap to the camera runbook — see
[adding-a-camera.md](adding-a-camera.md) §"Running these commands from the Windows host".
Short version: **use Git Bash or WSL, never PowerShell**, and pipe file content on **stdin**
rather than embedding it in `sh -c '...'`. Variables and heredocs get eaten crossing
PowerShell → wsl → kubectl and you get empty strings instead of an error.

---

## The one that will actually bite you: the entity_id is minted ONCE

**Home Assistant mints `<domain>.<slug>` from the zwave-js node's name and location at the moment
it first creates the entity, and never revisits it.** Renaming the *device* afterwards — in
Z-Wave JS UI, or on HA's device page — does **not** change the entity_id. It only sets a display
name (`name_by_user`).

This install carries two permanent scars from getting that wrong:

| entity_id | Device display name today | What happened |
|---|---|---|
| `lock.lock` | "Back Door Lock" | Included before the node was named, so HA minted the slug from the bare product name `Lock`. Renaming the device later fixed the label and not the id. |
| `switch.s2_on_off_switch` | "Master Lounge Speaker System" | The ZEN76 Long Range re-inclusion (2026-08-14) destroyed the old device and minted a fresh one from the generic product name. Same outcome. |

Compare a node that *was* named first:

| entity_id | Node location + name at inclusion |
|---|---|
| `light.master_bedroom_master_lounge_dimmer` | `Master Bedroom` / `Master Lounge Dimmer` |
| `lock.front_door_lock` | `Front Door` / `Lock` |

So the slug is **`slugify(location)_slugify(name)`**, evaluated once. Plan it backwards from the
entity_id you want:

> Want `light.backyard_string_lights`? In Z-Wave JS UI set **Location = `Backyard`** and
> **Name = `String Lights`**. Not Name = "Backyard String Lights" — that yields
> `light.backyard_backyard_string_lights`.

**Recovery, if it still comes out wrong:** you *can* change the entity_id, but only by editing the
**entity** (HA → Settings → Devices & Services → Entities → the entity → gear → Entity ID), not the
device. `lock.garage_door` is how that looks when it works. Do it **immediately**, before any
automation, dashboard, Hawksnest override or `device_id` reference exists — nothing updates itself.

### Also: check the slug is free before you include

HA never reclaims a freed slug. A retired device's registry entry keeps the canonical id and forces
the new device onto `_2`. Disabled and unavailable entries count and do **not** appear in Developer
Tools → States, so check the registry itself:

```bash
kubectl -n home-automation exec deploy/home-assistant -c home-assistant -- \
  python -c "
import json
ents = json.load(open('/config/.storage/core.entity_registry'))['data']['entities']
for e in ents:
    if 'YOUR_SLUG_FRAGMENT' in e['entity_id']:
        print(e['entity_id'], '| disabled:', bool(e.get('disabled_by')), '|', e.get('platform'))
"
```

---

## Before you start

### 1. The controller must be a char device, not the boot-race directory

```bash
kubectl -n home-automation exec deploy/zwave-js-ui -- ls -l /dev/zwave
```

Good: `crw-rw---- 1 root dialout 166, 0 ... /dev/zwave`.
Bad: `total 0` — usbipd lost the boot race and the hostPath became an empty **directory**. The pod
still reports `1/1` and HA reports "Failed to connect to ws://zwave-js-ui:3000". Run the recovery
in [DEPLOYMENT.md](../DEPLOYMENT.md) §6 (scale to 0, re-attach, scale to 1) **before** including
anything. A half-included node is worse than no node.

### 2. Back up the network, because inclusion writes the NVM

Losing `zwavejs-config` means re-pairing every device, including three deadbolts. Two layers:

1. **Controller NVM backup** — Z-Wave JS UI → Settings → Controller → Backup NVM. This is the one
   that can restore the network with its S2 keys.
2. **Store PVC copy:**
   ```bash
   kubectl -n home-automation exec deploy/zwave-js-ui -- \
     tar czf - -C /usr/src/app/store . > zwavejs-store-$(date +%F).tgz
   ```
   (`/usr/src/app/store` is the mountPath in `kustomize/base/zwave-js-ui/deployment.yaml`.)

Do both **before** touching the mesh. Staging cannot help you here — it parks zwave-js-ui at
`replicas: 0` because only one ZWA-2 exists and prod owns it, so all Z-Wave work is prod-only.

### 3. Decide the security class, and know which credential you need

| You have | Use | Notes |
|---|---|---|
| 5-digit **PIN** | **Classic inclusion** | Device joins first, then you type the PIN into the DSK prompt. |
| Full **DSK** / QR code | **SmartStart** | Add to the provisioning list, then power-cycle the device; it joins itself. |

Do not paste the 5-digit PIN into the SmartStart screen — it will not include. This cost a session
on the BE469ZP locks (`PROGRESS.md` §Lessons learned).

SmartStart is the better path for **battery devices that will not hold inclusion mode** (the back
door lock needed the QR plus a ~10 s battery pull). For mains-powered devices, classic inclusion is
fine and the PIN is enough.

On the grant screen, prefer the **strongest class the device offers**. If you are never prompted for
a DSK at all, you were granted S2 Unauthenticated — back out and redo if you wanted Authenticated.
Locks use S2 Access Control; nothing else should be touching those keys.

### 4. Long Range vs classic mesh — and why this is a one-way door

**Default to classic mesh.** Choose deliberately, once, because there is no cheap do-over:
switching modes is exclude + re-include, which mints a new node id, which makes `zwave_js` create a
**new device with new entities**. Everything breaks quietly — automations log "Referenced entities
are missing", any `device_id` hex goes stale, Hawksnest overrides miss. `switch.s2_on_off_switch`
is what that looks like a month later.

Reasons classic wins here almost always:

- **LR is star-only and never repeats.** This network wants repeaters: lock RSSI sits around
  −87 dBm and the mesh is thin. A mains-powered node on classic mesh helps every other node; the
  same node on LR helps only itself.
- **Measured, not assumed:** the ZEN76 LR migration (2026-08-14, node 42 → 256) bought *topology* —
  direct route, 100 kbps, 25 ms RTT — but **RSSI got worse**, −82 → −88. Judge LR by hops, rate and
  RTT, never by dBm, and do not reach for it to fix a weak link.
- LR mandates S2 and in practice wants the QR for SmartStart, so the 5-digit PIN alone is not the
  LR path.

Reach for LR only for a genuinely distant node that classic cannot hold, and write down that you
did.

---

## Inclusion

1. **Put the device in its final physical position first, powered.** Mains nodes should learn their
   real neighbours; a node included next to the controller keeps a route it cannot use until the
   next heal. (Battery locks are the opposite case — they do not repeat, so pair them close and
   move them afterwards.)
2. Reach the UI: `kubectl port-forward -n home-automation svc/zwave-js-ui 8091:8091` →
   `http://localhost:8091`.
3. **Manage nodes → Include**, choose the security class.
4. Put the device into inclusion mode per its own manual (button sequence varies; read the card).
5. Enter the DSK/PIN when prompted.
6. **Stuck on "starting inclusion"?** Full browser page reload. It is a stale frontend socket — the
   `AddNodeToNetwork` request never reached the driver.
7. **Wait for `interview: Complete`** before doing anything else. A partial interview shows partial
   command classes, and you will pin the wrong config parameters.
8. **Name it now** — Location and Name per the slug rule above. This is the load-bearing step.

---

## After inclusion: the HA-side work that lives nowhere in git

None of the following is in this repo, in CI, or in the drift check. It lives in `.storage` on the
`ha-config` PVC. It is invisible to every automated gate, which is exactly why it gets forgotten.

1. **Confirm the entity_id.** If it is wrong, fix it on the *entity* now (see the naming section).
2. **Assign the Area** — Settings → Devices → the device → Area. Without it the device lands in
   "Unassigned" and never appears on the right room screen in Hawksnest.
3. **Record the `device_id` hex** if you intend to write any `zwave_js.set_config_parameter`
   automation:
   ```
   Developer Tools → Template → {{ device_id('light.your_entity') }}
   ```
4. **Check the endpoint count.** Some devices (multi-outlet plugs, dual relays) create two entities.
   Decide which one is real and disable or rename the other before it squats a slug.
5. **Reboot drill.** Reboot the host, let the logon task re-attach the ZWA-2, and confirm the device
   reports state with no re-pairing.

## Pinning device parameters in git

Config parameters set by hand in Z-Wave JS UI are invisible and do not survive a re-include or a
factory reset. The established pattern (`'1785540000007'`, "Master Bedroom ZEN32 - LED colours") is
a `homeassistant start`-triggered automation that rewrites them every boot, so **git is the source
of truth and a reset self-heals**.

Read the parameter numbers off the live node — **Z-Wave JS UI → the node → Configuration tab**,
which renders the device's own list with numbers, labels, ranges and defaults. **Never guess a
parameter number**; a wrong number writes a real setting on real hardware. An empty tab means the
device is not in the zwave-js device database yet, not that it has no parameters.

Two things the shape of that automation has already had to learn:

- **The startup guard is the config ENTRY, not the entity.** `zwave_js` creates its entities inside
  `async_setup_entry`, so every entity — including `sensor.*_node_status` reading `alive` — answers
  before the entry reaches `loaded`, which is the state a `device_id`-targeted call actually needs.
  Guarding on the node's own entities cannot see the window it is meant to close. The symptom is
  "No zwave_js nodes found for given targets" at boot.
- **`now()` must lead the wait template.** `config_entry_attr` registers no listener of its own, so
  without a time-dependent term the wait never re-renders and sits forever.

```yaml
- wait_template: '{{ now().timestamp() > 0 and
    has_value(''light.your_entity'') and
    config_entry_attr(config_entry_id(''light.your_entity'')
    or ''none'', ''state'') == ''loaded'' }}'
  timeout: '00:05:00'
  continue_on_timeout: false
```

Put a **400 ms delay between writes**. This mesh has no spare bandwidth.

## Automations: the dual write is the whole job

`kustomize/base/home-assistant/configmap.yaml` is a **fresh-install-only seed** — the
`seed-config` initContainer copies each file only `if [ ! -f /config/$f ]`. Committing and merging
an automation changes **nothing** on the live HA.

Every HA change is therefore two writes:

1. The seed, in git, via a PR.
2. The live `/config/automations.yaml` on the `ha-config` PVC, then
   **Developer Tools → YAML → Check Configuration → Reload Automations**.

Then prove it:

```bash
./scripts/ha-config-drift-check.sh
```

Success is one line: `OK — live HA config matches the seed.` Anything else names your automation
under `SEED-ONLY` (you forgot the live edit — it is committed but never runs) or `LIVE-ONLY` (you
forgot the commit — it runs but is one disk failure from gone). The weekly Tuesday cron runs the
same check, so half a dual write goes red with your automation's id in it either way.

> **`check_config` exits 0 even on broken automations.** Both CI's Gate A and the Developer Tools
> check will look green while naming errors in their output. **Grep the text for `ERROR`.** A green
> check is not evidence.

## Hawksnest: usually nothing

Hawksnest is registry-driven. It reads HA's live area/entity/device registries, and
`src/lib/cards.ts` maps by domain — so a new `light.*` with an Area appears as a `LightCard` (web)
and a `LightPillar` (Android) with no code change, and gets a working brightness slider if
`supported_color_modes` contains anything other than `onoff`.

Two things to know:

- **Check which domain it landed in.** `light.*` is fully handled. `switch.*` is handled on Android
  (`RockerSwitch`) but **not on web** — `CARD_BY_DOMAIN` has no `switch` key, so it falls through to
  the read-only `GenericCard` and the device cannot be operated from a browser. That gap is tracked
  in Hawksnest's `AUDIT-2026-08.md`.
- **`light.*` does not mean dimmable, and the widgets know it.** Z-Wave exposes the Inovelli
  VZW30-SN — an on/off switch — as a Multilevel Switch, so HA reports
  `supported_color_modes: ["brightness"]` for hardware with no dimmer. The in-app cards trust that
  attribute; the Android **widgets deliberately ignore it** and let the chosen widget kind decide.
  If a genuinely dimmable device has a flicker floor, pin it in the device's minimum-dim-level
  parameter rather than expecting the app to know.

## Verification checklist

- [ ] `/dev/zwave` is a `crw-` char device
- [ ] NVM backup + store tarball taken **before** inclusion
- [ ] Target slug confirmed free in `core.entity_registry` (including disabled entries)
- [ ] Node Location + Name set so the minted entity_id is the one you wanted
- [ ] `interview: Complete`
- [ ] Area assigned in HA
- [ ] `device_id` recorded if any parameter automation references it
- [ ] Endpoint count checked; extra entities disabled
- [ ] Device parameters pinned in git, not just set in the UI
- [ ] Automations dual-written; `ha-config-drift-check.sh` prints `OK`
- [ ] `check_config` output grepped for `ERROR`, not just observed green
- [ ] Host reboot drill: device reports state with no re-pairing
- [ ] Hawksnest renders it (web **and** Android), in the right room
