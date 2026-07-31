# Adding a Reolink camera

A runbook for putting one or more new Reolink cameras behind Frigate (recording + detection) and
go2rtc (live view), so they appear in Home Assistant and Hawksnest with no client-side work.

Written 2026-07-31 against the live cluster, from the three cameras already deployed. Every
capacity number here was **measured**, not estimated — see [Capacity](#capacity-what-four-more-actually-costs).

> **Ring cameras are a different path.** They come in through ring-mqtt, not this one:
> README §"Ring via ring-mqtt" and DEPLOYMENT.md §7b.

---

## The shape of the job

A camera touches **six places**, in two repos plus the Windows host:

| # | Where | What |
|---|---|---|
| 1 | Camera itself | static IP, RTSP creds, stream geometry |
| 2 | `overlays/prod/secrets/go2rtc.env` | `REOLINK_IP_<NAME>` |
| 3 | `overlays/prod/secrets/frigate.env` | `FRIGATE_REOLINK_IP_<NAME>` — **a second copy of the same IP** |
| 4 | `base/go2rtc/configmap.yaml` | `<name>` + `<name>_sub` streams |
| 5 | `base/frigate/configmap.yaml` | the camera block — **and a dual-write to the live config** |
| 6 | Windows host | Tailscale `/32` route |

Home Assistant and Hawksnest need **nothing**: the frigate-hass-integration creates the
`camera.<name>` entity automatically, and Hawksnest discovers cameras from HA. The two exceptions
are called out in [Steps 7–8](#7-home-assistant-manual-ui-work).

### Doing several at once

Adding four in one pass is fine, and the config work genuinely batches. Two things change:

- **The blast radius is every camera, not just the new ones.** An invalid Frigate config puts the
  whole service into **safe mode with all cameras stopped** — including the three that were
  working. This is why [step 5](#5-deploy-then-dual-write-frigate) verifies the staged file *inside
  the pod* before it is allowed to replace the live one. Do not skip that because it feels
  ceremonial; it is the only thing standing between a typo and a dark NVR.
- **The Tailscale command is not additive** — see [step 4](#4-tailscale-route--the-batch-trap). It
  replaces the route list, so a four-camera batch means re-issuing it with all seven.

If you would rather bound the risk, do the physical install and ffprobe for all four, then bring
them up **two at a time**: two full passes cost one extra Frigate restart and halve what a bad
block can take down.

### Two of those six fail silently

Both are worth reading before touching anything.

- **The Frigate seed is first-boot-only.** `base/frigate/deployment.yaml`'s `seed-config`
  initContainer copies `config.yml` *only if the file does not exist*, because Frigate's own UI
  writes that file when you draw masks and zones. Editing the ConfigMap alone changes nothing on
  the running instance — CI passes, the deploy goes green, and the camera never appears. Every
  Frigate config change is a **dual write**: ConfigMap *and* live `/config/config.yml`.
  `scripts/frigate-drift-check.sh` exists to catch exactly this. Run it.

- **go2rtc is the opposite** — its initContainer re-seeds `/config/go2rtc.yaml` on *every* start,
  because go2rtc takes ownership of the file and was observed clearing it. So for go2rtc you edit
  the ConfigMap and **restart the pod**; do not hand-edit the live file, it will be overwritten.

Getting these two backwards is the single most likely way to lose an hour.

---

## Capacity: what four more actually costs

Measured on the live cluster, 2026-07-31, with three cameras running:

| | Measured | Headroom for 7 cameras |
|---|---|---|
| Detector (`ov`, OpenVINO **CPU**) | **1.64 ms** inference | ~610 inferences/s theoretical; current demand ~12 fps. Not the constraint. |
| Recording disk | **3.9 GB/day** for 3 cameras (full day, 2026-07-30) | ~9 GB/day at 7 cameras · 3-day retention ≈ **27 GB** |
| Volume | 1007 GB, 62 GB used, **895 GB free** | Not the constraint either |

**The disk warnings in `configmap.yaml` and `plan.md` are ~4x pessimistic.** They budgeted
16.7 GB/day for three cameras from *nominal* sub-stream bitrates; the measured figure is 3.9. If
you want longer retention than 3 days, the room is there — but change it *after* the new cameras
have run a full day and you can measure again, not on this table.

**What to actually watch is CPU, not the detector or the disk.** Detection is cheap here; ffmpeg
decode and motion detection are not, and they scale with camera count. After the rollout, check
`process_fps` still tracks `camera_fps` on every camera and that `skipped_fps` stays at 0. A
camera that starts skipping frames is the first sign you have run out of CPU, and it will look
like "detection got worse" rather than like a resource problem.

**The real guardrail is unchanged and is not a number in this table:** `frigate-media` is k3s
`local-path`, which is hostPath-backed with **no quota enforcement**. Filling the Dragonfly
virtual disk takes down k3s, Home Assistant and the door locks with it. The PVC size is not a
limit. Treat `df -h` as the authority.

---

## Before you start: facts to collect per camera

Do this for **every** camera before editing anything. The differences between Reolink models are
the silent kind — copying another camera's numbers misplaces every bounding box without erroring.

### 1. A fixed IP

DHCP drift has already bitten this setup (the phone's IP moved and the docs were wrong for
months). Either set a static IP on the camera or add a DHCP reservation. Write it down — you need
it in three places.

### 2. The actual stream geometry, from ffprobe

**Do not trust the camera's own reporting.** On the E1 Pro's firmware `GetEnc` omits `vType`
entirely, so the camera cannot tell you its own codec. ffprobe is the only source of truth.

**Run ffprobe from the go2rtc container.** The WSL host has no ffprobe at all. The Frigate image
*does* have one, but **not on `PATH`** — it lives at `/usr/lib/ffmpeg/7.0/bin/ffprobe` (and a 5.0
copy), so the obvious `exec deploy/frigate -- ffprobe …` fails with "not found" and looks like the
image lacks it. go2rtc has it on `PATH` *and* already has the Reolink credentials in its
environment, which is why the recipe below uses it:

```bash
kubectl -n home-automation exec deploy/go2rtc -- sh -c '
  ffprobe -v error -select_streams v:0 \
    -show_entries stream=codec_name,width,height,avg_frame_rate \
    -of default=noprint_wrappers=1 \
    -rtsp_transport tcp \
    "rtsp://${REOLINK_USER}:${REOLINK_PASS}@<ip>:554/h264Preview_01_sub"'
```

Verified 2026-07-31 against the two deployed models — it returns exactly the numbers below, which
is also the evidence that the models really do differ:

```
192.168.4.37 (E1 Zoom)  codec_name=h264  width=640  height=360  avg_frame_rate=10/1
192.168.4.64 (E1 Pro)   codec_name=h264  width=896  height=512  avg_frame_rate=10/1
```

Record `codec_name`, `width`, `height` and the frame rate for the **sub** stream. Repeat with
`h264Preview_01_main` if you want to know what live view will carry. A benign
`Overread VUI by 8 bits` warning on some firmwares is not a failure — read the values below it.

Known differences among the three deployed cameras:

| Model | Sub stream | Main stream |
|---|---|---|
| E1 Zoom (`big_room`, `first_floor_stairway`) | 640x360 @10, 256 kbps | 2560x1440 @20 |
| E1 Pro / E330 (`kitchen`) | **896x512 @10**, 1024 kbps | 2880x1616 @20 |

If a new camera is 4K and H.265, read `plan.md` §"Going back to 4K later" first — `vType` **must**
be `h265` or a 4K+h264 SetEnc is silently ignored, and the encoder needs ~20 s to restart during
which the HTTP API 502s.

### 3. A slug

Lowercase, underscore-separated, and it must be **identical** in the go2rtc stream name, the
Frigate camera key, and therefore the HA entity object id — Hawksnest derives the camera name as
`camera.id.split(".")[1]`.

Check it does not collide with an existing stream. There are already **14**, most of them Ring:

```bash
kubectl -n home-automation exec deploy/frigate -c frigate -- python3 -c \
  "import json,urllib.request; print(sorted(json.load(urllib.request.urlopen('http://go2rtc:1984/api/streams'))))"
```

As of 2026-07-31: `back_side_yard`, `back_yard_patio`, `basement`, `bedroom`, `big_room`,
`big_room_sub`, `first_floor_stairway`, `first_floor_stairway_sub`, `front_door`, `front_driveway`,
`kitchen`, `kitchen_sub`, `pet_camera`, `puzzle_room`.

The Reolink migration deliberately *reused* `big_room`, `kitchen` and `first_floor_stairway` and
retired the Ring entries; there are comments at each retired entry explaining the swap. If a new
Reolink is going in a room that already has a **live** Ring camera, decide explicitly whether you
are replacing it (reuse the slug, retire the Ring entry, note it) or running both (pick a distinct
slug). Silently shadowing a live Ring camera is the failure to avoid.

---

## The steps

Everything below is safe to do for all four cameras in one pass. Where batching changes the
instruction, it says so.

### 1. Secrets — the same IP, twice, in two files

```
kustomize/overlays/prod/secrets/go2rtc.env    →  REOLINK_IP_<NAME>=192.168.4.x
kustomize/overlays/prod/secrets/frigate.env   →  FRIGATE_REOLINK_IP_<NAME>=192.168.4.x
```

The `FRIGATE_` prefix is not cosmetic: Frigate does its own `{VAR}` substitution and **requires**
that prefix on anything referenced as `{FRIGATE_FOO}`. go2rtc uses `${VAR}` with no prefix.

Credentials (`REOLINK_USER` / `REOLINK_PASS`, `FRIGATE_REOLINK_USER` / `FRIGATE_REOLINK_PASSWORD`)
are shared across cameras — only add them if the new cameras use different ones.

These files are **not** in git. `.env.example` alongside each is, and should gain the new keys so
a rebuild from scratch knows they exist.

### 2. go2rtc streams

In `base/go2rtc/configmap.yaml` under `streams:`, add **two** entries per camera:

```yaml
      <name>:     "rtsp://${REOLINK_USER}:${REOLINK_PASS}@${REOLINK_IP_<NAME>}:554/h264Preview_01_main"
      <name>_sub: "rtsp://${REOLINK_USER}:${REOLINK_PASS}@${REOLINK_IP_<NAME>}:554/h264Preview_01_sub"
```

Main for live view, sub for the Low quality toggle. The `_sub` suffix is load-bearing —
Hawksnest's quality toggle asks go2rtc for `<name>_sub` by name and hides the control when go2rtc
does not list it.

### 3. Frigate camera blocks

In `base/frigate/configmap.yaml` under `cameras:`, one block per camera. Minimum viable block,
with the parts that are per-camera marked:

```yaml
      <name>:
        ffmpeg:
          inputs:
            # SUB stream, read directly rather than through go2rtc, so recording and live view
            # have independent failure domains. NO input_args on purpose — 0.17's RTSP preset
            # already supplies transport/timeout/timestamp flags; pinning them here freezes
            # today's preset and opts out of upstream fixes on the next upgrade.
            - path: "rtsp://{FRIGATE_REOLINK_USER}:{FRIGATE_REOLINK_PASSWORD}@{FRIGATE_REOLINK_IP_<NAME>}:554/h264Preview_01_sub"
              roles:
                - detect
                - record
        detect:
          # 0.17 defaults this to FALSE when absent. Frigate recorded for hours and detected
          # nothing before this was found — see plan.md.
          enabled: true
          # <-- FROM YOUR ffprobe, NOT copied from another camera
          width: 640
          height: 360
          fps: 5
        objects:
          track:
            - person
            - dog
            - cat
          genai: *person_genai      # <-- reuse the anchor, do not re-type the prompt
```

Two things to get right:

- **`detect.enabled: true` is mandatory.** Frigate 0.17 defaults it to `false` when the key is
  absent, and the failure mode is a camera that records perfectly and never detects anything.
- **`genai: *person_genai`** — the AI-description config is a YAML anchor defined on the
  `big_room` block. Alias it; do not copy the prompt. The wording is deliberately identical across
  cameras and a copy will drift.

Masks and zones are **not** committed. They are drawn against a live frame in Frigate's UI and
land in the live `/config/config.yml`. Expect to add a timestamp-OSD motion mask per camera after
the fact (all three existing cameras needed one; the stairway needed three, for the OSD clock and
two ceiling fans).

Retention is per-camera and inherits the global `record.continuous.days: 3`. If any new camera
overlooks somewhere sensitive, give it its own shorter window rather than raising the global one.

### 4. Tailscale route — the batch trap

`tailscale set --advertise-routes` **replaces** the list. Adding four cameras means re-issuing the
command with **all seven** `/32`s, not just the new four.

Current, as measured 2026-07-31:

```
192.168.4.37/32   192.168.4.53/32   192.168.4.64/32
```

```powershell
& "C:\Program Files\Tailscale\tailscale.exe" set --advertise-routes=192.168.4.37/32,192.168.4.53/32,192.168.4.64/32,<new1>/32,<new2>/32,<new3>/32,<new4>/32
```

Then **approve the new routes in the Tailscale admin console** — CLI first, console second.
Verify with `PrimaryRoutes`, which means advertised *and* approved:

```powershell
& "C:\Program Files\Tailscale\tailscale.exe" status --json | ConvertFrom-Json | Select -Expand Self | Select PrimaryRoutes
```

**Do not skip that check.** An unapproved route makes RTSP unreachable from the phone, and
Hawksnest's transport ladder silently steps down to go2rtc — so the symptom is "live view feels a
bit slower", not an error. This is the single hardest failure in the whole runbook to attribute.

Per-camera `/32` is deliberate: a whole-LAN route would give every tailnet device a path to the
NAS, printer, eero and every IoT device.

### 5. Deploy, then dual-write Frigate

```bash
# Normal deploy (or push to main and let the workflow do it)
kubectl kustomize kustomize/overlays/prod | kubectl apply -f -

# go2rtc picks up its ConfigMap on restart — this is all it needs
kubectl -n home-automation rollout restart deploy/go2rtc
kubectl -n home-automation rollout status deploy/go2rtc
```

Frigate needs the dual write. **Stage and verify in the pod before replacing the live file** — the
guard exists because a careless `kubectl exec` without `-i` once truncated a live config to zero
bytes:

```bash
POD=$(kubectl -n home-automation get pod -l app=frigate -o name | head -1)

# 1. extract config.yml from the ConfigMap to a local file, then stage it (NOTE the -i)
kubectl -n home-automation exec -i "$POD" -c frigate -- sh -c 'cat > /config/config.yml.new' < ./frigate-config.yml

# 2. verify the STAGED file in the pod, before it can replace anything
kubectl -n home-automation exec -i "$POD" -c frigate -- python3 - <<'PY'
import yaml
c = yaml.safe_load(open("/config/config.yml.new"))
cams = list(c["cameras"])
print("cameras:", cams)
assert len(cams) == 7, cams
for n in cams:
    assert c["cameras"][n]["detect"]["enabled"] is True, f"{n}: detect not enabled"
print("STAGED OK")
PY

# 3. only now replace, keeping a backup
kubectl -n home-automation exec "$POD" -c frigate -- sh -c \
  'cp /config/config.yml /config/config.yml.bak-$(date +%s) && mv /config/config.yml.new /config/config.yml'

kubectl -n home-automation rollout restart deploy/frigate
kubectl -n home-automation rollout status deploy/frigate --timeout=240s
```

The container has `python3`, **not** a bare `python`. Using `python` fails the check — which is
the guard working, but wastes a cycle.

### 6. Verify Frigate before moving on

```bash
kubectl -n home-automation logs deploy/frigate -c frigate --tail=200 | grep -iE "safe mode|not valid" && echo "!! SAFE MODE"
bash scripts/frigate-drift-check.sh
```

An invalid config puts Frigate in **safe mode with every camera stopped** — including the three
that were working. It says so in the logs and nowhere else. Then:

```bash
kubectl -n home-automation exec -i deploy/frigate -c frigate -- python3 - <<'PY'
import json, urllib.request
s = json.load(urllib.request.urlopen("http://127.0.0.1:5000/api/stats"))
for n, c in s.get("cameras", {}).items():
    print(f"{n:22} camera_fps={c.get('camera_fps')} process={c.get('process_fps')} "
          f"detect={c.get('detection_fps')} skipped={c.get('skipped_fps')} pid={bool(c.get('pid'))}")
print("detector:", {k: v.get('inference_speed') for k, v in s.get('detectors', {}).items()})
PY
```

Every camera wants a live `pid`, `camera_fps` near 5, `process_fps` tracking it, and
`skipped_fps` at **0**. Non-zero skipped = out of CPU (see [Capacity](#capacity-what-four-more-actually-costs)).

### 7. Home Assistant (manual UI work)

1. The **frigate-hass-integration** creates `camera.<name>` automatically once Frigate reports the
   camera. Confirm each new entity appears with exactly the slug you chose.
2. Add the official **Reolink** integration per camera for PTZ / IR / privacy — then **disable its
   camera entities**. They end in suffixes `cameraModel.classify()` parses and would collide with
   the Frigate camera in Hawksnest's `resolveCameras`.
3. Add a `recorder.exclude.entity_globs` block for the new fps/process sensors. Frigate + Reolink
   add **~25–30 entities per camera**, several updating continuously, against
   `purge_keep_days: 30` — four cameras is ~100–120 new entities. This is a **dual write** to the
   live `configuration.yaml` as well as the seed.

**Step 3 is the one most likely to be skipped, and at this scale it shouldn't be.** Checked
2026-07-31: the live `configuration.yaml` has a `recorder:` block with `purge_keep_days: 30` and
`commit_interval: 5` — but **no `exclude:` at all**. `plan.md` called for one during the original
three-camera rollout and it was never added. So today every Frigate and Reolink diagnostic sensor
is being recorded.

That is already measurable: the recorder holds **97,804 state rows for the last 24 h** and
781,751 over 30 days, which is what made Hawksnest's History screen fall over before it was
capped and made lazy. Four more cameras roughly doubles the camera-derived share of that.

```yaml
recorder:
  db_url: !secret recorder_db_url
  purge_keep_days: 30
  commit_interval: 5
  exclude:
    entity_globs:
      - sensor.*_camera_fps
      - sensor.*_detection_fps
      - sensor.*_process_fps
      - sensor.*_skipped_fps
      - sensor.*_detection_cpu_usage
      - sensor.*_ffmpeg_cpu_usage
      - sensor.*_capture_cpu_usage
```

Confirm the real entity ids against your instance before pasting — glob a name that doesn't exist
and it silently excludes nothing. Then **Developer Tools → YAML → Check Configuration** and reload,
and verify the row count stops growing as fast:

```bash
kubectl -n home-automation exec deploy/mariadb -- sh -c \
  "mariadb -u homeassistant -p'<pw>' homeassistant -N -e \
   \"SELECT COUNT(*) FROM states WHERE last_updated_ts > UNIX_TIMESTAMP() - 3600;\""
```

### 8. Hawksnest — usually nothing

Cameras are discovered from HA. Two things can need attention:

- **PTZ alias.** Hawksnest resolves movement controls by matching the Frigate slug against the
  Reolink device's entity ids. If HA names the Reolink device differently from the Frigate camera
  (it did for the stairway: `stairway` vs `first_floor_stairway`), add an entry to `aliases` in
  `src/lib/cameraPtz.ts` **and** its Kotlin twin `core/logic/CameraPtz.kt` — they are kept in
  lockstep and both have tests.
- **Display name.** Only if HA's friendly name is wrong; overrides live in `src/config/overrides.ts`
  and `config/Overrides.kt`, never in components.

---

## Verification checklist

Per camera, in order — each step's failure has a different cause, so do not batch the checks:

- [ ] `ffprobe` against the sub stream returns the geometry you put in `detect:`
- [ ] `tailscale status` `PrimaryRoutes` lists the camera's `/32` (advertised **and** approved)
- [ ] Frigate `/api/stats` shows a live `pid`, `camera_fps` ≈ 5, `skipped_fps` = 0
- [ ] `scripts/frigate-drift-check.sh` reports live == seed
- [ ] go2rtc lists both `<name>` and `<name>_sub` (`/go2rtc/api/streams`)
- [ ] `camera.<name>` exists in HA with the exact slug
- [ ] Reolink integration's camera entities are **disabled**
- [ ] Hawksnest shows the camera; live view and the recorded timeline both play
- [ ] `df -h /media/frigate` after 24 h — then decide whether to raise retention

---

## If it goes wrong

**Frigate in safe mode** (all cameras stopped): the live config is invalid. Restore the backup you
made in step 5 and restart:

```bash
kubectl -n home-automation exec deploy/frigate -c frigate -- sh -c 'ls -la /config/config.yml.bak-*'
kubectl -n home-automation exec deploy/frigate -c frigate -- sh -c 'cp /config/config.yml.bak-<ts> /config/config.yml'
kubectl -n home-automation rollout restart deploy/frigate
```

**Live view works but is jumpy / slower than the others:** almost always the Tailscale route is
advertised but not approved, so RTSP-direct is unreachable and the ladder fell to go2rtc or HLS.
Check `PrimaryRoutes`, not the app.

**Camera records but never detects:** `detect.enabled` is missing. It defaults to `false` in 0.17.

**Bounding boxes are consistently offset:** `detect.width`/`height` do not match the actual sub
stream. Re-run ffprobe; do not assume the model matches its siblings.

**Everything looks right but the new camera never appears:** you edited the Frigate ConfigMap and
not the live config. Run the drift check.
