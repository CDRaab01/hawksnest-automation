# Adding a Reolink camera

A runbook for putting one or more new Reolink cameras behind Frigate (recording + detection) and
go2rtc (live view), so they appear in Home Assistant and Hawksnest with no client-side work.

Written 2026-07-31 against the live cluster, from the three cameras already deployed. Every
capacity number here was **measured**, not estimated — see [Capacity](#capacity-what-four-more-actually-costs).

> **Ring cameras are a different path.** They come in through ring-mqtt, not this one:
> README §"Ring via ring-mqtt" and DEPLOYMENT.md §7b.

---

## Read this first: running these commands from the Windows host

Every `kubectl exec` in this runbook that references a shell variable **must be fed over
stdin**, not passed as an argument:

```bash
# RIGHT — the heredoc reaches `sh` intact
kubectl -n home-automation exec -i deploy/go2rtc -c go2rtc -- sh -s <<'EOF'
  echo "${REOLINK_USER}"
EOF

# WRONG — ${REOLINK_USER} is eaten crossing PowerShell -> wsl -> kubectl
kubectl -n home-automation exec deploy/go2rtc -- sh -c 'echo "${REOLINK_USER}"'
```

The variable arrives **empty**, and the failure does not look like a quoting failure. It looks
like whatever the empty value causes downstream:

| What you run | What you see | What it actually is |
|---|---|---|
| ffprobe | `401 Unauthorized` | empty credentials, not wrong ones |
| `mariadb -p"$PW"` | `Access denied … (using password: NO)` | empty password |

**The tell is the echoed URL.** `rtsp://:@192.168.4.30` means the variables were eaten;
`rtsp://frigate:xxxx@192.168.4.30` means the camera genuinely rejected you. Check that before
concluding anything about credentials — on 2026-07-31 this cost two full diagnostic rounds.

Also note **ffprobe prints the password in cleartext** in its error line. Redact it:

```bash
... 2>&1 | sed "s/${REOLINK_PASS}/<redacted>/g"
```

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

> **Main-stream recording was tried FLEET-WIDE and reverted (2026-08-24 → 2026-08-26).** It would
> have cost ~180 GB/day, ~16x this table. The fleet-wide version is not in effect and these
> sub-stream figures remain current for seven of the eight cameras.
>
> **One camera deviates as of 2026-08-30: `nursery_high` records from main** (4K H.265,
> 4370 kbps measured → ~47 GB/day → ~142 GB at 3-day retention). Read the deviation block on that
> camera in `configmap.yaml` before copying it — the case for it is camera-specific. In short:
> the August failure was ~24 Mbps of contention from seven main streams, this is ~4.4 Mbps from
> one, and `nursery_high` is the stronger radio in that room. The camera it can still break is
> `nursery`, its roommate, which is the weakest link in the house.
>
> **Updated 2026-08-30 — that camera now runs 4K h264, and getting there corrected two things
> this runbook asserted.** First, **"4K forces h265" is per-model, not a fleet law**: it was
> measured on the E1 Zoom and is false on the E1 Outdoor Pro, which ffprobes as
> `h264, 3840, 2160, 20/1`. Re-probe a new model rather than assuming the 1440p downgrade is
> required. Second, **HEVC on the WebRTC tier does not degrade — it CRASHES the Android app**:
> libwebrtc null-derefs (`SIGSEGV` in `libjingle_peerconnection_so.so`), taking the process down
> rather than showing no picture. Treat an HEVC main stream as a hard blocker for any camera the
> app can reach, not a quality trade. And note the RTSP-direct tier does **not** save you from it
> by default: `rtspUser`/`rtspPass` were never configured here, so every camera streams over
> go2rtc/WebRTC. Before re-attempting the fleet-wide version, read the `record:` comment in
> `configmap.yaml`, and remember a ConfigMap edit alone cannot change a running Frigate.

**Update, 2026-07-31 — measured at 7 cameras.** Adding four moved detector inference from
**1.64 ms → 1.74 ms**, and `skipped_fps` stayed at 0 on every camera with `camera_fps` ≈ 5 and
`process_fps` tracking it. The detector was not the constraint at 3 and is not at 7.

One number does jump, and it is expected: a camera with **no motion mask yet** runs
`detection_fps` at ~4.5–4.7 continuously, versus 0.0 on the three masked cameras, because the OSD
clock repaints every second. That is the cost of deferring masks (see step 3) and it disappears
once they are drawn — but it means "detection_fps is high on the new cameras" is not a fault.

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

### 0. The `frigate` service account must exist ON EACH CAMERA

**This blocks everything else and is easy to miss.** A camera out of the box has only the
`admin` account created during app setup. Enabling RTSP does **not** create the service user
that `REOLINK_USER` / `REOLINK_PASS` refer to. Until you create it, every ffprobe, every
go2rtc stream and every Frigate input gets a real 401.

Discovered 2026-07-31 on all four new cameras — the shared credentials worked against the three
deployed cameras and failed on all four new ones in the same breath, which is what proves it is
camera-side and not a credential problem.

**Check before anything else** (this is a better first probe than ffprobe: it separates
reachable-but-unauthorized from unreachable in one shot, and reports the lockout budget):

```bash
kubectl -n home-automation exec -i deploy/go2rtc -c go2rtc -- sh -s <<'EOF'
curl -s -m 10 "http://<ip>/cgi-bin/api.cgi?cmd=GetDevInfo&user=${REOLINK_USER}&password=${REOLINK_PASS}" \
  | sed "s/${REOLINK_PASS}/<redacted>/g"
EOF
```

`"code": 0` + device info = the account exists. `"rspCode": -7, "detail": "login failed"` = it
does not. That response also carries `auth_warning_info.remain_times` — Reolink locks the account
out after repeated failures, so **do not probe usernames speculatively**; a lockout turns a
five-minute fix into a factory reset.

Create it either in the Reolink app (Settings → System → User Management) or over the API:

```
Login  -> [{"cmd":"Login","action":0,"param":{"User":{"Version":"0","userName":"admin","password":"<pw>"}}}]
AddUser-> [{"cmd":"AddUser","action":0,"param":{"User":{"userName":"frigate","password":"<pw>","level":"guest"}}}]
GetUser-> confirm the account is listed, then Logout
```

Two traps in that sequence, both of which cost a round on 2026-07-31:

- **Reolink pretty-prints its JSON**, so a naive token extraction (`grep -o '"name"[^,}]*'`)
  captures the trailing spaces before the closing brace. The token then looks fine but every
  authed URL fails with `curl: (3) URL rejected: Malformed input to a URL function`. Anchor on
  the closing quote instead: `sed -n 's/.*"name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p'`,
  and assert the token is alphanumeric before using it.
- **Use `curl -sS`, never bare `-s`.** With `-s` the above failure prints an empty body and
  looks like "the firmware returned nothing", sending you off investigating the camera instead
  of your own script.

`go2rtc.env.example` calls for a **non-admin** account. Reolink's levels are `admin` and
`guest`; create it as `guest` and verify RTSP still authenticates before moving on. There is no
"change level" call — changing it is DelUser + AddUser.

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
# NOTE: stdin heredoc, not `sh -c '...'` — see "Running these commands from the
# Windows host" above. As an argument the credentials arrive empty and you get a 401.
kubectl -n home-automation exec -i deploy/go2rtc -c go2rtc -- sh -s <<'EOF'
for ip in <ip1> <ip2>; do
  echo "=== $ip ==="
  ffprobe -v error -select_streams v:0 \
    -show_entries stream=codec_name,width,height,avg_frame_rate \
    -of default=noprint_wrappers=1 \
    -rtsp_transport tcp \
    "rtsp://${REOLINK_USER}:${REOLINK_PASS}@${ip}:554/h264Preview_01_sub" 2>&1 \
    | sed "s/${REOLINK_PASS}/<redacted>/g"
done
EOF
```

Verified 2026-07-31 against the two deployed models — it returns exactly the numbers below, which
is also the evidence that the models really do differ:

```
192.168.4.37 (E1 Zoom)  codec_name=h264  width=640  height=360  avg_frame_rate=10/1
192.168.4.64 (E1 Pro)   codec_name=h264  width=896  height=512  avg_frame_rate=10/1
```

Record `codec_name`, `width`, `height` and the frame rate for the **sub** stream. Repeat with
`h264Preview_01_sub` if you want to know what live view will carry. A benign
`Overread VUI by 8 bits` warning on some firmwares is not a failure — read the values below it.
(It is emitted by every E1 Pro even on a completely successful probe or mux. Do not write a check
that treats *any* ffmpeg stderr as failure — that produced a false "all five cameras cannot mux"
result on 2026-07-31. Judge the output file, not stderr.)

### …and probe the AUDIO too — this is now load-bearing

The recipe above is `-select_streams v:0`, video only, and until 2026-07-31 **no camera's audio had
ever been probed here**. It is no longer optional, because Frigate now records with
`preset-record-generic-audio-copy` (`-c copy`) — it muxes the camera's audio **as-is** instead of
re-encoding it. That requires the codec to be MP4-legal:

```bash
kubectl -n home-automation exec -i deploy/go2rtc -c go2rtc -- sh -s <<'EOF'
for ip in <ip1> <ip2>; do
  echo "=== $ip ==="
  # 1. what codec is it?
  ffprobe -v error -select_streams a -rtsp_transport tcp \
    -show_entries stream=codec_name,sample_rate,channels \
    -of default=noprint_wrappers=1 \
    "rtsp://${REOLINK_USER}:${REOLINK_PASS}@${ip}:554/h264Preview_01_sub" 2>&1 \
    | sed "s/${REOLINK_PASS}/<redacted>/g"
  # 2. THE REAL GATE: does -c copy actually mux into mp4?
  #    Probe the SUB stream: it carries the `record` role, so its audio codec is the one
  #    that has to be MP4-legal. (This probed `main` while main-stream recording was in
  #    effect, 2026-08-24 to 2026-08-26 — move it back if that is ever re-enabled.)
  ffmpeg -v error -rtsp_transport tcp \
    -i "rtsp://${REOLINK_USER}:${REOLINK_PASS}@${ip}:554/h264Preview_01_sub" \
    -t 8 -c copy -f mp4 -y /tmp/probe.mp4 2>&1 | grep -v 'Overread VUI' \
    | sed "s/${REOLINK_PASS}/<redacted>/g"
  ffprobe -v error -select_streams a -show_entries stream=codec_name,bit_rate \
    -of csv=p=0 /tmp/probe.mp4
done
EOF
```

Expected on the deployed fleet (measured 2026-07-31, all seven):

| Model | Audio |
|---|---|
| E1 Zoom (`big_room`, `first_floor_stairway`) | aac, 16 kHz mono, **~31 kbps** |
| E1 Pro (`kitchen`, `nursery`, `basement`, `bedroom`, `garage`) | aac, 16 kHz mono, **~64 kbps** |

**If a camera is not AAC** — or has its mic disabled so only the `PCMU/8000` *sendonly* talk
backchannel remains — the mp4 muxer refuses the stream and **ffmpeg exits**. Frigate runs ONE
ffmpeg per camera with TWO outputs, so that kills **detection as well as recording** for that
camera. Do not fix it by reverting the global; give the odd camera its own override:

```yaml
    <camera>:
      ffmpeg:
        output_args:
          record: preset-record-generic-audio-aac
```

For **live** audio the camera must additionally appear in the go2rtc config with a second,
audio-only `ffmpeg:…#audio=opus` source — WebRTC cannot carry AAC, and a bare RTSP entry gives a
silent live view. See the comment above `big_room` in `kustomize/base/go2rtc/configmap.yaml`.

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

**Edit the canonical copies on the runner host, NOT the repo checkout:**

```
/home/sonic/hawksnest-secrets/go2rtc.env    →  REOLINK_IP_<NAME>=192.168.4.x
/home/sonic/hawksnest-secrets/frigate.env   →  FRIGATE_REOLINK_IP_<NAME>=192.168.4.x
```

`scripts/deploy.sh` copies these into a fresh checkout at deploy time (`HAWKSNEST_SECRETS_DIR`),
and `need_secret()`/`optional_secret()` **skip the copy when the destination already exists**.
That last detail is load-bearing — see the warning in [step 5](#5-deploy-then-dual-write-frigate)
about what a checkout with stale `.env` files does. Back up before editing; that directory
already uses a `<file>.bak.<YYYYMMDDHHMMSS>` convention. Guard the write (line delta, expected
key count, password hash unchanged) — the suite rule about never blind-rewriting a live `.env`
applies here.

The old paths below are where the secrets are *consumed*, and the `.example` templates next to
them are tracked in git and should gain the new keys:

```
kustomize/overlays/prod/secrets/go2rtc.env.example    →  REOLINK_IP_<NAME>=replace-with-...
kustomize/overlays/prod/secrets/frigate.env.example   →  FRIGATE_REOLINK_IP_<NAME>=replace-with-...
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
      <name>:     "rtsp://${REOLINK_USER}:${REOLINK_PASS}@${REOLINK_IP_<NAME>}:554/h264Preview_01_sub"
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
            # Both inputs are read directly rather than through go2rtc, so recording and live
            # view have independent failure domains. NO input_args on purpose — 0.17's RTSP
            # preset already supplies transport/timeout/timestamp flags; pinning them here
            # freezes today's preset and opts out of upstream fixes on the next upgrade.
            #
            # SUB stream -> detect. Detection wants geometry matching `detect:` below, not
            # pixels; pointing it at main would cost decode CPU for no accuracy.
            - path: "rtsp://{FRIGATE_REOLINK_USER}:{FRIGATE_REOLINK_PASSWORD}@{FRIGATE_REOLINK_IP_<NAME>}:554/h264Preview_01_sub"
              roles:
                - detect
            # MAIN stream -> record (2026-08-24). Both roles used to share the sub input, which
            # made the recorded timeline the 10 fps detect stream — choppy playback, and the
            # reason this split exists. Recording is `-c:v copy`, so it costs disk and NIC, not
            # detector CPU. Budget ~25-45 GB/day per E1 Pro and ~10 GB/day per E1 Zoom; read
            # the `record:` comment in configmap.yaml before adding a camera.
            - path: "rtsp://{FRIGATE_REOLINK_USER}:{FRIGATE_REOLINK_PASSWORD}@{FRIGATE_REOLINK_IP_<NAME>}:554/h264Preview_01_sub"
              roles:
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

> **CORRECTED 2026-08-29 — the advertised list had drifted badly, and re-measuring is now
> mandatory before you touch it.** `tailscale debug prefs` actually reported:
>
> ```
> 192.168.4.37/32  192.168.4.45/32  192.168.4.53/32  192.168.4.64/32  192.168.4.65/32
> ```
>
> Cross-referenced against ARP and a port-554 sweep of the whole subnet:
>
> | IP | Reality |
> |---|---|
> | `.37`, `.53`, `.64` | cameras, correctly routed |
> | `.45` | **no ARP entry at all** — a dead IP |
> | `.65` | live host, **not a camera** (no port 554 open) |
> | `.23`, `.30`, `.46`, `.62` | **cameras with no route at all** — including `nursery` |
>
> So four of seven cameras had no `/32`, which is precisely the failure this step warns
> about: RTSP-direct is unreachable from the phone, Hawksnest's transport ladder silently
> steps down to go2rtc, and the only symptom is live view feeling slower. It looks like
> DHCP drift that the route list never caught up with — the `.45`/`.65` entries are most
> likely cameras' *old* addresses.
>
> **Read the list with `tailscale debug prefs` and rebuild it from a measured camera
> inventory — never from this document.** A ping sweep is not a reliable inventory either:
> several cameras here do not answer ICMP, and `.40` was missed by two separate scans
> before a batched port-554 sweep found it. Sweep port 554 in small batches, not 254
> parallel jobs.

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

> ### ⚠ DO NOT run `kubectl kustomize kustomize/overlays/prod | kubectl apply -f -`
> ### from a checkout whose `secrets/*.env` are placeholder copies.
>
> Found 2026-07-31: in `C:\Code\hawksnest-automation` all six `secrets/*.env` files are
> **byte-identical to their `.example` templates**, because `deploy.sh` leaves an existing
> destination file alone. The real values live only on the runner host and in the cluster.
>
> The overlay uses `generatorOptions: disableNameSuffixHash: true`, so that apply **overwrites
> all six live Secrets in place with dummy values** — mosquitto auth for Frigate *and*
> ring-mqtt, every Reolink credential, mariadb (Home Assistant's recorder DB), ring-timeline
> and go2rtc. On the cluster that runs the door locks.
>
> `reolink_cred_check()` in deploy.sh cannot save you: it deliberately returns early when a
> file is byte-identical to its `.example`, which is exactly this state.
>
> Check before any apply:
> ```bash
> cd kustomize/overlays/prod/secrets
> for f in go2rtc frigate mariadb ring-mqtt ring-timeline; do
>   cmp -s $f.env $f.env.example && echo "DUMMY: $f.env"; done
> ```

Use one of these instead:

**(a) Surgical — smallest blast radius, and what was used on 2026-07-31.** Touches only the
camera objects; HA, mariadb, zwave-js-ui and the locks are never involved. The seed ConfigMaps
carry no labels or name prefixes, so they apply by exact name.

```bash
# add ONLY the new keys to the two Secrets, values read from the canonical files
kubectl -n home-automation patch secret frigate-credentials --type merge \
  -p '{"data":{"FRIGATE_REOLINK_IP_<NAME>":"<base64 of the IP>"}}'
kubectl -n home-automation patch secret go2rtc-credentials --type merge \
  -p '{"data":{"REOLINK_IP_<NAME>":"<base64 of the IP>"}}'

kubectl -n home-automation apply -f kustomize/base/go2rtc/configmap.yaml
kubectl -n home-automation apply -f kustomize/base/frigate/configmap.yaml
```

**(b) `./scripts/deploy.sh` from the WSL checkout** — the normal path, with real guards (live HA
config check, server-side dry run, zwave parking). It applies the whole overlay, so a camera
change puts HA and mariadb in the blast radius.

**(c) Merge to main and let CI deploy** — correct, but the change reaches prod before you can
verify it.

```bash
# go2rtc picks up its ConfigMap on restart — this is all it needs
kubectl -n home-automation rollout restart deploy/go2rtc
kubectl -n home-automation rollout status deploy/go2rtc
```

**ORDER MATTERS: the Secret must carry the new IPs BEFORE the new Frigate config loads.**
Frigate resolves `{FRIGATE_*}` at config-load time from its environment, so a config naming a
variable the pod doesn't have yet dies with `KeyError: 'FRIGATE_REOLINK_IP_<NAME>'` and boots
into safe mode with **all** cameras stopped. Patch the Secret first; the pod picks the new
values up on the restart that also loads the new config, so one restart covers both.

Frigate needs the dual write. **Stage and verify in the pod before replacing the live file** — the
guard exists because a careless `kubectl exec` without `-i` once truncated a live config to zero
bytes.

**First, check what replacing the live file would destroy.** This step overwrites masks and zones
that were drawn in Frigate's UI and never committed. Run the drift check *before* editing the
seed, so you know whether live and seed already agree, and list the zones:

```bash
bash scripts/frigate-drift-check.sh          # clean == safe to replace wholesale
kubectl -n home-automation exec deploy/frigate -c frigate -- \
  python3 -c "import yaml;print({n:list((c or {}).get('zones') or []) for n,c in yaml.safe_load(open('/config/config.yml'))['cameras'].items()})"
```

If either shows live-only content, merge it into the seed first rather than clobbering it. (On
2026-07-31 the baseline was clean and no camera had zones, so a wholesale replace was safe.)

```bash
POD=$(kubectl -n home-automation get pod -l app=frigate -o name | head -1)

# 1. extract config.yml from the ConfigMap to a local file, then stage it (NOTE the -i)
kubectl -n home-automation exec -i "$POD" -c frigate -- sh -c 'cat > /config/config.yml.new' < ./frigate-config.yml

# 2. verify the STAGED file in the pod, before it can replace anything
#
# yaml.safe_load ONLY proves the file is YAML. It does NOT catch the schema errors that
# actually cause safe mode. Use Frigate's own validator as well — it is the same code path
# whose failure stops every camera:
kubectl -n home-automation exec -i "$POD" -c frigate -- python3 -s <<'PY'
import os, yaml
# If the pod does not have the new IP vars yet (see the ORDER note above), inject them so the
# validator can resolve {FRIGATE_*}; otherwise it raises KeyError and tells you nothing else.
os.environ.update({"FRIGATE_REOLINK_IP_<NAME>": "192.168.4.x"})
import importlib, frigate.config.env as e; importlib.reload(e)
import frigate.config.config as m; importlib.reload(m)

raw = open("/config/config.yml.new").read()
c = yaml.safe_load(raw)
cams = list(c["cameras"])
print("cameras:", cams)
assert len(cams) == 11, cams   # the CURRENT camera count, not a constant — update it every time
for n in cams:
    assert c["cameras"][n]["detect"]["enabled"] is True, f"{n}: detect not enabled"
# the genai anchor must resolve to ONE shared prompt, not drifted copies
assert len({c["cameras"][n]["objects"]["genai"]["prompt"] for n in cams}) == 1

cfg = m.FrigateConfig.parse_yaml(raw)          # <-- the real check
print("STAGED OK — FrigateConfig accepts", sorted(cfg.cameras))
PY
# `parse_file` is NOT the YAML entry point — it is pydantic's deprecated JSON loader and
# fails with a JSONDecodeError. Use parse_yaml (or parse_object on a dict).

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
   camera — but **not until its config entry is reloaded**. It builds the entity list at setup, so
   after a Frigate restart the new cameras are absent from HA and it looks like the rollout failed.
   Reload the entry (no HA restart needed — returns `require_restart: False`):

   ```bash
   # find the entry id
   GET  /api/config/config_entries/entry            -> domain "frigate"
   POST /api/config/config_entries/entry/<id>/reload
   ```

   Then confirm each new entity appears with exactly the slug you chose.

   **If the new camera replaces a Ring camera, its Ring entities do not disappear on their own.**
   `camera.<room>_snapshot` from the Ring integration survives retiring the go2rtc stream, so
   Hawksnest will show that room twice. The three cameras retired in July no longer have Ring
   entities, so removing them is the established outcome — do the same for each replacement.

   **This step was skipped for `basement` and `bedroom` on 2026-07-31, and the failure mode is
   worse than a duplicate tile.** The stale entities come from **ring-mqtt** (platform `mqtt`, not
   the disabled `ring` config entry), and they keep the *canonical* entity IDs, which forces
   Frigate's real entities onto suffixed ones:

   | entity | platform | state |
   |---|---|---|
   | `camera.<room>_snapshot`, `binary_sensor.<room>_motion` | `mqtt` (ring-mqtt) | `unavailable` |
   | `binary_sensor.<room>_motion_2` | `frigate` | the live one |
   | `binary_sensor.<room>_motion_3` | `reolink` | |

   Hawksnest resolves by base slug, so it binds the **dead** entity — which is what made the
   basement / Cooper's-bedroom **scrubber and thumbnail tiles** wrong while the cameras themselves
   were fine. Removing the `mqtt` entities is not enough on its own: afterwards **rename Frigate's
   `_motion_2` back to `_motion`**, because HA does not reclaim a freed slug automatically. Check
   for references to the suffixed name before renaming (there were none in July).
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

**CORRECTED 2026-07-31 — the globs this runbook used to recommend match ZERO entities here.**
`sensor.*_camera_fps`, `*_detection_fps`, `*_process_fps`, `*_skipped_fps` and the `*_cpu_usage`
family do not exist on this instance. The runbook warned "glob a name that doesn't exist and it
silently excludes nothing" and then did exactly that. What Frigate's integration actually creates
here are per-camera **count** sensors:

```yaml
recorder:
  db_url: !secret recorder_db_url
  purge_keep_days: 30
  commit_interval: 5
  exclude:
    entity_globs:
      - sensor.*_all_count
      - sensor.*_all_active_count
      - sensor.*_person_count
      - sensor.*_person_active_count
```

Measured over 24 h on 2026-07-31 with 3 cameras: **12 entities, 15,643 of 102,335 state rows
(~15%)** — about 8–9k rows per camera per day, so seven cameras is ~36k/day from this source
alone. Verified these globs catch *only* Frigate camera entities.

**Deliberately not excluded:** `binary_sensor.<cam>_person` / `_motion*` (21 entities, 7,859
rows/day). Those are real history — "when was there motion in the kitchen" is worth being able to
answer — and the motion glob also catches non-Frigate motion sensors.

**Also worth knowing:** the single biggest churn source is not Frigate at all. `camera.*_snapshot`
(Ring) is 8 entities and 13,742 rows/day. Excluding it is a bigger win than anything above, but it
is Ring churn, unrelated to adding a Reolink.

**`recorder` is not a reloadable domain** — the exclude takes effect only on an HA restart. Write
it, validate it, and let it apply on the next restart rather than bouncing HA for it.

Measure the real list on your own instance rather than trusting any of the above:

```sql
SELECT sm.entity_id, COUNT(*) AS rows_24h
FROM states s JOIN states_meta sm ON s.metadata_id = sm.metadata_id
WHERE s.last_updated_ts > UNIX_TIMESTAMP() - 86400
GROUP BY sm.entity_id ORDER BY rows_24h DESC LIMIT 30;
``` Then **Developer Tools → YAML → Check Configuration** and reload,
and verify the row count stops growing as fast:

```bash
kubectl -n home-automation exec deploy/mariadb -- sh -c \
  "mariadb -u homeassistant -p'<pw>' homeassistant -N -e \
   \"SELECT COUNT(*) FROM states WHERE last_updated_ts > UNIX_TIMESTAMP() - 3600;\""
```

### 8. Hawksnest — usually nothing (BUT NOT FOR A DOORBELL)

> **If the device is a doorbell, this heading is wrong** — the press sensor needs app-side
> support and HA-side naming care. See [Doorbells are a different animal](#doorbells-are-a-different-animal)
> before you assume there is nothing to do.

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

- [ ] the `frigate` service account exists ON the camera (`GetDevInfo` returns `code: 0`)
- [ ] `ffprobe` against the sub stream returns the geometry you put in `detect:`
- [ ] `ffprobe` against the **main** stream reports **H.264** and MP4-legal audio — main is what
      gets recorded now, and h265 there breaks recorded playback as well as WebRTC live
- [ ] the camera's RTSP session budget still fits: Frigate takes **two** sessions (sub + main),
      go2rtc one, and each viewing phone one more
- [ ] `tailscale status` `PrimaryRoutes` lists the camera's `/32` (advertised **and** approved)
- [ ] Frigate `/api/stats` shows a live `pid`, `camera_fps` ≈ 5, `skipped_fps` = 0
- [ ] `scripts/frigate-drift-check.sh` reports live == seed
- [ ] go2rtc lists both `<name>` and `<name>_sub` (`/go2rtc/api/streams`)
- [ ] the Frigate config entry has been **reloaded** in HA (otherwise the camera never appears)
- [ ] `camera.<name>` exists in HA with the exact slug
- [ ] Reolink integration's camera entities are **disabled**
- [ ] if this REPLACES a Ring camera: the Ring `camera.<room>_snapshot` entity is gone too
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


---

## Doorbells are a different animal

Written 2026-08-30 from the **Reolink Video Doorbell WiFi (D340W)** rollout — the first doorbell
on this path. Everything above still applies; these are the parts that bit, in order.

### The sub stream is 4:3, and no sibling's numbers transfer

`640x480 @10` h264, verified by ffprobe **and** `GetEnc`. Every other Reolink here is 16:9
(`640x360` Zooms, `896x512` Pros). The runbook already says not to copy geometry between models;
a doorbell makes it concrete, because the aspect ratio itself differs. Both streams carry
**AAC 16 kHz mono**.

### RTSP over UDP hangs — ffprobe never returns

A bare `ffprobe` against this camera produces no output and no error; it just blocks until you
kill it. It is not a credential problem and not a reachability problem — port 554 answers fine.
**Use `-rtsp_transport tcp`** for any manual probe:

```bash
ffprobe -v error -rtsp_transport tcp -rw_timeout 20000000 -show_entries stream=... "rtsp://..."
```

Frigate's own 0.17 preset already passes `-rtsp_transport tcp`, and go2rtc negotiates its own
transport, so **only manual probing is affected**. Budget for this: it cost a 3-minute timeout
before the cause was obvious.

### Two different accounts, and the split is not optional

- **RTSP (go2rtc + Frigate)** uses the shared `frigate` **guest** account, as for every camera.
- **HA's Reolink integration requires `admin`.** All nine reolink config entries on this
  instance use `admin`. The guest account will not work for it.

You need the integration because **Frigate cannot see a doorbell press**. The button is
`binary_sensor.<base>_visitor`, published only by the official Reolink integration. Frigate gives
you video, motion and objects; it has no concept of a ring.

### The doubled-slug trap when you rename

The Reolink integration names entities `<host device>_<channel device>_<entity>`. Renaming only
the *channel* device to match the Frigate slug produces:

```
binary_sensor.front_door_front_door_reolink_visitor
```

because the host device is still called "Front door". Hawksnest resolves cameras by base slug, so
that binds to nothing and the doorbell silently rings nothing. **Fix the entity id directly**
(Settings -> Entities -> the entity -> ⚙ -> Entity ID), not the device name:

```
binary_sensor.front_door_reolink_visitor
```

Then disable the integration's leftover `camera.*` entity (`..._fluent`) as step 7.2 says — on a
doorbell it survives the usual sweep because its name is doubled too.

### A doorbell may need NO motion mask

Step 3 defers masks and warns `detection_fps` will sit at ~4.5-5 until the OSD clock is masked.
**Measured on the D340W: `detection_fps` 0.1**, against 0.0-0.1 on the masked wall cameras. This
doorbell paints no repainting clock, so there is nothing to mask. Measure before drawing one.

### `yaml.safe_dump` will silently gut the live config

Editing `/config/config.yml` by loading it with PyYAML and dumping it back **strips every
comment**: 38 KB -> 6 KB on this instance, with all the trap documentation gone. The file still
validates and Frigate still runs, so nothing tells you. **Insert new camera blocks as text**, and
copy any shared anchor content (the genai block) out of the parsed tree so the prompt cannot
drift:

```python
objs = yaml.safe_dump(parsed['cameras']['garage']['objects'], sort_keys=False)
```

Check the byte count after staging. A big drop means you round-tripped it.

### Retiring the Ring doorbell: check WHICH "Front Door" device

`ring-mqtt` publishes **two** devices whose entities both start `front_door` on this instance:

| device | model | entities | status |
|---|---|---|---|
| `8543dfb1-…` | **Contact Sensor** | `binary_sensor.front_door`, `_tamper`, `select.*_bypass_mode`, `_chirp_tone`, battery/info | **LIVE — part of the alarm** |
| `387c76abb2e9` | **Doorbell Pro** | `_ding`, `_motion`, `camera.front_door_snapshot`, event/live-stream switches, … | retired |

Deleting by entity-id prefix would take out an armed contact sensor. **Group by `device_id`
first** (`core/device_registry` + `core/entity_registry`) and delete only the doorbell's.

Note also that **disabling is not enough if you intend to reuse the slug** — a disabled entity
still owns its entity id, so Frigate's replacement lands on `_motion_2`. Delete, do not disable.
And a deleted MQTT-discovery entity comes back when ring-mqtt republishes; remove the doorbell
from the **Ring account** to make it permanent.

## Battery cameras behind a Reolink Home Hub are a third animal

Written 2026-09-12 from the **Argus 4 Pro** rollout (two cameras, `backyard_patio` and
`front`, on a **Reolink Home Hub** at 192.168.4.44, firmware v3.3.0.456). Almost every step
above assumes one wired camera per IP that streams whenever asked. A battery camera behind the hub
breaks that in five places, and each one was measured, not assumed.

### The hub is the RTSP server, and it has exactly one account

- The cameras have no RTSP of their own. The hub serves each bound camera as a **channel on the
  hub's IP**: `rtsp://<user>:<pass>@<hub-ip>:554/h264Preview_0N_{main,sub}`. **N is 1-based in
  the RTSP path** (`_01_` = channel 0). The hub's CGI API (`GetChannelstatus`) and the FLV path
  (`channel<N>_main.bcs`) count from 0. Get the map from the API, never by guessing:

  ```bash
  curl -sS -m 15 -X POST "http://<hub-ip>/cgi-bin/api.cgi?cmd=GetChannelstatus&user=admin&password=<pw>" \
    -H 'Content-Type: application/json' -d '[{"cmd":"GetChannelstatus","action":0,"param":{}}]'
  ```
  An unbound hub answers with eight empty channels (`name:""`, `uid:""`, `online:0`) — that is
  "the cameras are not on the hub yet", which is done in the Reolink app, not by any API.
- **The hub has no user table.** `AddUser` returns `cmd: Unknown / rspCode -9 "not support"`, the
  app hides user management, and the hub's web UI (`http://<hub-ip>`) is a viewer only — Preview,
  Playback, Channels, logout. So step 0's `frigate` guest account cannot exist on the hub, and the
  hub is the **one RTSP source that carries the admin login**. It gets its own variables —
  `REOLINK_HUB_USER/PASS` (go2rtc) and `FRIGATE_REOLINK_HUB_USER/PASSWORD` (Frigate) plus one
  `*_IP_HOME_HUB` — never the fleet `REOLINK_USER/PASS`. `reolink_cred_check()` in `deploy.sh`
  knows nothing about them, deliberately.
- **The lockout counter is real and shared.** Every failed login (the `frigate` probe, a wrong
  admin password) decrements `auth_warning_info.remain_times` from 10; at 0 the hub freezes
  logins. Verify a password with ONE `GetDevInfo` and stop on failure — never loop.
- Enable **RTSP + HTTP + ONVIF** in the app (hub → Network → Advanced → Server Settings) before
  any of this; the hub ships with only :9000 (the app protocol) open. `GetNetPort` confirms.
  RTMP (:1935) stays off unless the FLV escape hatch below is ever needed.

### The hub wakes the camera on every RTSP request, and force-sleeps it after 5 minutes

Reolink's own documentation, confirmed by ffprobe: a battery camera is asleep until an RTSP
request arrives, takes **7–15 s** to deliver a first frame (`-rw_timeout 30000000` on the
*ffprobe*; the container's ffmpeg does not accept that flag — omit it there), streams for at most
**5 minutes**, then the hub disconnects the session and the camera sleeps. Frigate's normal
`[detect, record]` input would reconnect every `retry_interval` — a wake→sleep→wake loop, 24/7,
on solar. **So Frigate never holds these streams.** The design, in the order it must be applied:

1. The camera block is seeded **`enabled: true`** — and that is the design, not an oversight. A
   camera seeded `enabled: false` can **never** be turned on over MQTT: 0.17.2 `dispatcher.py`
   refuses `ON` unless `enabled_in_config`. Runtime `enabled` is in-memory, so every Frigate
   restart brings the camera back ON.
2. The HA automation **`hawksnest_argus_park`** (seed + live `automations.yaml`) publishes
   `frigate/<cam>/enabled/set OFF` for every on-demand camera whose PIR is not on, on
   `frigate/available = online` and on HA start. Cost: one ≤30 s wake per Frigate restart.
3. **`hawksnest_argus_on_demand_<cam>`** publishes ON when the hub's PIR
   (`binary_sensor.<cam>_motion`, the Reolink integration's, renamed) goes on, waits for it to
   go off (≤3 min), tails 1 min, publishes OFF. `mode: single` makes the 4-minute bound absolute
   — always under the hub's 5-minute cap, so Frigate ends the session, never the hub.
4. **The health CronJob needs an allow-list.** A parked camera stays in `/api/stats` with
   `camera_fps` **frozen** at its last value — 0 if it was parked before its first frame, which is
   every restart — and would page "NOT RECORDING" every 30 minutes forever. `ON_DEMAND` in
   `base/frigate/health-cronjob.yaml` names them; `tests/test_frigate_health.py` pins both that a
   parked camera is silent and that a wired camera dead beside it still pages. The HA
   `hawksnest_frigate_detection_watchdog` needs nothing: its `_camera_fps` sensors are
   `disabled_by: integration`.
5. **Order of the dual write:** Secrets → CronJob → automations appended to the live file **and
   reloaded** → go2rtc → Frigate. The automations must be *loaded* before Frigate restarts, or
   the new cameras stay ON pulling the hub until someone notices.

The verification checklist's "live `pid`, `camera_fps` ≈ 5" line is **false by design** for these
cameras while parked; the gate is instead: `Turning off camera <cam>` in Frigate's log within ~5 s
of `frigate/available online`, then a hand-wave producing `Turning on camera`, frames, a review,
and `Turning off` ≤ 4 min later — with the next two CronJob runs still `healthy`.

### Live and detect both come from the SUB stream

ffprobed per channel from the go2rtc container (the recipe in §2, with the hub URL):

| stream | codec | geometry | fps | audio |
|---|---|---|---|---|
| sub  | **h264** | **1536×432** (the stitched dual-lens 32:9 frame — not a 16:9 sibling's numbers) | 25 | AAC 16 kHz mono |
| main | **hevc** | 5120×1440 | 25 | AAC 16 kHz mono |

HEVC on WebRTC crashes the Android app (CLAUDE.md), so go2rtc's `<name>` points at **sub**, with
the `ffmpeg:…#audio=opus` producer as usual, and there is **no `<name>_sub` entry** (a Low
toggle would be a no-op; Hawksnest hides it when go2rtc doesn't list it). If the app ever offers
h264 on the Clear stream, move `<name>` to main. Frigate's single input is sub, `[detect,
record]`, `detect: 1536x432 @5`, `-c copy` verified by the mux gate (5 s → a playable 557 KB
mp4 with h264 + aac). **Three concurrent sessions on one channel worked** (Frigate's one + go2rtc's
two per viewer) — the budget the checklist asks for.

The FLV escape hatch, if the hub's RTSP ever proves flaky: `ffmpeg:http://<hub-ip>/flv?port=1935&app=bcs&stream=channel<N-1>_main.bcs&user=…&password=…` — 0-based channel, and it needs
`rtmpEnable` on. Documented, not deployed.

### The first ffmpeg attempt after a wake needs a longer probe window

Measured on the first walk test: the hub accepts the RTSP connection and starts sending packets
at once while the camera is still waking, so Frigate's default probe window (`analyzeduration`
0 = auto, 5 s) closed before a keyframe/SPS arrived — `Could not find codec parameters for
stream 0 (Video: h264, none): unspecified size` — and the first attempt died every time; the
10 s retry then succeeded, costing ~30 s of a ≤4.5-minute window. These two cameras therefore
carry the **only `ffmpeg.input_args` in the file**: `preset-rtsp-generic` verbatim plus
`-analyzeduration 10000000 -probesize 10000000` and `-timeout 30000000`. Wall cameras keep the
preset. **Not longer than 10 s**: Frigate's frame watchdog kills an ffmpeg with no frame in 20 s,
and a 20 s probe window sat exactly on that deadline — the first attempt then died to "No frames
received in 20 seconds" instead (arrival test, 21:17Z). If a wake ever exceeds ~15 s the symptom
returns and there is no client-side fix left; the retry below is the answer.

The automation window is shaped by how a **driveway arrival** looks: the PIR fires on the *car*
and clears in seconds; the person steps out 20–40 s later while Frigate is still ~15 s from its
first frame. A 1-minute tail after the PIR cleared closed the window before anyone was in it —
hence `wait_for_trigger` ≤ 2:30 and a **2-minute tail** (4.5 min worst case, under the hub's 5).

A **second, hub-side race** survives any client option: while the camera finishes waking, the
hub answers ffmpeg's `SETUP` with `454 Session Not Found` and the first attempt dies anyway
(walk test 2). Frigate's retry always succeeds, so the lever is the retry cadence:
`ffmpeg.retry_interval: 3` on these two cameras (the global stays 10 — a wall camera that needs
retries has a real problem and the slow cadence keeps its log readable). Expect the first
recorded frames ~10–15 s after the PIR, never at 0; the hub's own recording covers the pre-roll.

### Jumpy playback: the hub stamps frames as they arrive, and `front` was losing two-thirds of them

Measured 2026-09-13, after the owner reported both hub cameras playing "jumps, speeds up, slows
down" in Hawksnest. The tool is `scripts/frigate-segment-jitter.py` (ffprobes the recorded
segments inside the pod — wakes nothing). Against a wired control:

| camera | median fps | frame-spacing stdev | frames <10 ms apart | longest gap |
|---|---|---|---|---|
| `basement` (wired) | 10.0 | 38 ms | 3 % | 0.3 s |
| `backyard_patio`, morning | 15.1 | 35–57 ms | 8–16 % | 0.6–1.5 s |
| `backyard_patio`, afternoon | 15.1 | 170–190 ms | ~40 % | 2–3 s |
| `front` (46 of 48 segments) | **2.5** | 250 ms – 4.9 s | 40–60 % | **17 s** |

Two findings, and a negative result worth more than either:

- **Dropping `-use_wallclock_as_timestamps` does NOT help — do not re-try it.** A 60 s capture of
  `front`'s sub stream taken twice concurrently from the frigate pod, once stamping arrival time
  (what these input args do) and once keeping the hub's RTP timestamps, produced the *same* gaps
  (6529 vs 6498 ms, 4592 vs 4595, …) and the same 50 % of frames under 10 ms apart. The hub
  timestamps a frame when *it* receives it from the camera, so the burstiness is in the RTP clock
  before ffmpeg sees it. The FLV escape hatch above would carry the same clock (and the hub has
  `rtmpEnable: 0` anyway). Nothing on the Frigate side can smooth this footage without re-encoding.
- **`front` was losing frames, not just bunching them.** The same capture carried 313 AAC packets
  in 58 s where a 16 kHz stream produces ~905 (`backyard_patio`'s segments have the full 16/s) —
  about a third of everything the camera sent arrived at the hub. Not signal: `sensor.<cam>_wi_fi_signal`
  (disabled by the integration; enable it — it costs nothing and needs no wake) read **−60 dBm on
  `front`, −54 dBm on `backyard_patio`**. The difference was **bitrate**: `GetEnc` showed `front` at
  Fluent **1024** kbit/s + Clear **4096** (the hub records Clear from the camera while Frigate pulls
  Fluent, so ~5 Mbit/s over that link during every wake) against the healthy camera's 512 + 2048.
  Both are 15 fps, not the 25 the SPS advertises. Fix applied 2026-09-13 through the now-enabled
  `select.front_fluent_bit_rate` / `select.front_clear_bit_rate`: **512 / 2048**, matching the
  sibling. Firmware is not a lever: `front` runs `v3.0.0.4978_25060601`, `backyard_patio`
  `v3.0.0.6066_26022801`, and Reolink offers nothing newer for `front`'s batch.
- Also enabled for good, both cameras: `sensor.<cam>_battery_state`, the four `select.<cam>_{fluent,clear}_{frame,bit}_rate`,
  and `sensor.reolink_hub_cpu_usage`. They are read from the hub's cache, so they never wake a camera.

Read the jitter numbers as: stdev high with fps at the configured rate = arrival-time jitter
(the radio path is congested, nothing lost); fps well below the configured rate = frames missing
(radio path or camera). The radio path is camera → eero satellite (wireless backhaul) → gateway →
Ethernet → hub; the hub itself is on `LAN` (`GetLocalLink`).

### Retention, slugs, and the things deliberately NOT done

- `record.continuous.days: 1` on an on-demand camera means "keep every woken window for a day"
  whether or not the detector fired inside it — the only shape of "1 day" the hardware allows.
  Alert/detection clips follow the global 30 days. Disk: minutes of footage per day.
- **Slugs must not collide with the Ring cameras they replace.** `front` was the live Ring
  "Front Driveway" camera's go2rtc stream and HA base, so the Argus spent its first day as
  `front_yard`; `back_yard_patio` is Ring's too, hence `backyard_patio`. Reusing a slug while the
  Ring entities exist binds Hawksnest to the dead ones (the basement/bedroom lesson at step 7.1).
  The rename to `front` happened only after the Ring camera was **deleted** — the MQTT device in
  HA (via the device page's MQTT-info menu, not by entity prefix), its go2rtc `ring:` stream, and
  the device in the Ring account (so ring-mqtt cannot republish it) — then the Reolink entity ids
  moved onto `front_*`, then Frigate/go2rtc/the automations were renamed and reloaded.
- **Trigger Frigate on the hub's AI classes, not the PIR.** Measured over 3 h on `front`: 31 PIR
  wakes, 73 `vehicle` detections, 19 `person`, 5 `animal` — it watches the street, and every
  passing car bought a 2-minute Frigate session (~65 of 180 minutes awake, on solar). The
  on-demand automations now trigger on `binary_sensor.<cam>_person` / `_vehicle` (front) and
  `_person` / `_animal` (backyard), and wait for those to clear. What keeps road traffic out of
  the *camera's* wakes is its detection zone in the Reolink app — the PIR is hardware.
- **No Tailscale `/32` for the hub, and never the hub's IP in the phone's RTSP map.** Both serve
  only the Android direct-RTSP tier, which hardcodes channel `01` and would silently play channel
  1 for every hub camera.
- No motion mask yet — the camera is awake too little to measure one. Check `detection_fps`
  during a woken window before drawing any.
- The `assert len(cams) == …` line in the §5 validator is a **camera count**, not a constant —
  it is 11 as of this rollout (nine wired + two hub). Update it every time.

### HA: one integration entry for the hub, not one per camera

Add the **Reolink** integration once, host = the hub's IP, `admin` credentials. It creates a hub
device with a child device per channel, and names every entity `<hub>_<channel>_<entity>` — the
doubled-slug trap above, now guaranteed rather than accidental. Per channel, rename the **entity
ids** (never the device): `binary_sensor.<cam>_motion`, `binary_sensor.<cam>_person`,
`sensor.<cam>_battery`. Do this **before** reloading the Frigate config entry so the Reolink PIR
owns `_motion` and Frigate's lands on `_motion_2` (Frigate's person sensor is
`_person_occupancy`; no collision). Disable `camera.<hub>_<cam>_fluent` (Hawksnest would render a
ghost tile) and set the integration's **"Preload camera stream" OFF** — it would hold the cameras
awake. Battery-camera entities are otherwise polled only every 6 h by design; the PIR sensors
arrive by push and are fresh.

### Pushes: the instant one comes from the hub, the clip one from Frigate

Frigate's review push (`hawksnest_push_camera_object`, now with an `outdoor` list that bypasses
the armed gate) cannot be instant for these cameras — Frigate is ~10–15 s behind the PIR, and a
quick arrival can produce no review at all. So each outdoor camera also has
**`hawksnest_push_outdoor_person_<cam>`**: triggered by the hub's own on-camera AI,
`binary_sensor.<cam>_person`, which arrives by push within a second and needs no wake. Person
only, same `walking` tag/route, same fail-open `hawksnest_alert_person` gate, no armed gate, no
image (the camera's snapshot entity is disabled and Frigate's `latest.jpg` is its error image
while parked), `mode: single` + a 1-minute trailing delay as the per-visit cooldown. Two
notifications per event is the intended trade: "now", then "here's the footage".

### Side finding, not fixed here

`REOLINK_IP_FRONT_DOOR_REOLINK` / `FRIGATE_REOLINK_IP_FRONT_DOOR_REOLINK` exist only in the patched
Secrets — they are **absent from `/home/sonic/hawksnest-secrets/*.env`**. A `deploy.sh` that ever
regenerates the Secrets from those files would drop the doorbell's IP. Add them.
