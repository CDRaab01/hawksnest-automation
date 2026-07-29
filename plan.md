# Reolink + Frigate migration — working plan

**Branch (both repos):** `claude/reolink-frigate-integration-kfvz3h`
**Started:** 2026-07-29 · **Status:** automation side landed, Hawksnest side not started

Replacing Ring with Reolink + Frigate: local RTSP, 24/7 recording, and AI event search,
all on-prem. Phase 1 is the indoor cameras; the first is a **Reolink E1 Pro (E330)**
taking over the **bedroom**. Design rationale lives in the personal camera-plan doc —
this file is the executable version and the source of truth for status.

Spans two repos:
- **`hawksnest-automation`** (this one) — the cluster: go2rtc, Frigate, storage, secrets.
- **`Hawksnest`** — the app: live view, recorded timeline, search UI, Android.

---

## Status

| # | Repo | Commit | State |
|---|---|---|---|
| 1 | automation | Retire Ring bedroom camera, free the `bedroom` slug | ✅ `dfee869` |
| 2 | automation | Reolink main stream via go2rtc (live view) | ✅ `98e411d` |
| 3 | automation | Frigate deployment, PVCs, config seed, staging park, invariants | ✅ `d7d7321` |
| 4 | automation | Frigate admin UI exposure (NodePort + socat + firewall rule) | ⬜ |
| 5 | HA | HACS + frigate-hass-integration + Reolink integration | ⬜ manual |
| 6 | Hawksnest | PTZ / IR / **privacy mode** controls | ⬜ |
| 7 | Hawksnest | Dev proxy for `/go2rtc/` + `/ring-timeline/` | ✅ `4a56ef2` |
| 8a | Hawksnest | Backend-capability refactor (`recordedBackend.ts`, `frigate.ts`) + tests | ✅ `4a56ef2` |
| 8b | Hawksnest | Footage generalization — Frigate `/recordings` → `ringFootage` segments | ⬜ |
| 6 | Hawksnest | PTZ / IR / **privacy mode** controls | ⬜ needs #5 |
| 9 | Hawksnest | AI search screen, mock-ha fixtures, E2E | ⬜ |
| 10 | Hawksnest | Android parity (`RecordedBackend.kt`, go2rtc stream-list gate) | ⬜ |
| — | both | Remove the Ring bedroom camera from the Ring account for good | ⬜ after verify |

Nothing is deployed. The branch has never been applied to a cluster. **The two repos are now
knowingly out of lockstep on one point:** web derives its recorded backend, Android still gates on
its own `isRing`. Item 10 closes that and should not be left to drift.

Retention decision (2026-07-29): bedroom is `continuous.days: 3`, not 14 — see below.

---

## Blocked on physical setup

Commits 1-3 cannot deploy until these exist. All are Phase 0, all need the camera in hand.

- [ ] **DHCP reservation** for the E1 Pro; note the IP.
- [ ] **Dedicated non-admin RTSP user** on the camera. Password must be URL-safe
      (no `/ @ : # ?`) — it goes into an `rtsp://` URL unencoded in two configs.
- [ ] **UID / cloud disabled** in the Reolink app, and the camera blocked from WAN egress
      at the router. The UID is Reolink's P2P relay identity; turning it off is most of
      the point of leaving Ring.
- [ ] **`ffprobe rtsp://<user>:<pw>@<ip>:554/h264Preview_01_sub`** — record the real
      resolution and fps. `detect:` in the Frigate seed is currently `640x360 @ 5fps` from
      the E1 Pro's nominal spec. A mismatch doesn't error; it silently misplaces every
      bounding box.
- [ ] **iGPU probe**, which decides `device:` (see "OpenVINO" below).
- [ ] **`frigate.env` and `go2rtc.env`** filled in on the runner at `~/hawksnest-secrets/`.
- [ ] **`frigate` MQTT user appended** to the mosquitto passwd file — see the warning below.
- [ ] **LM Studio model id** and a Hyper-V firewall rule for its port.

---

## Three things that will bite

### 1. The mosquitto password file is append-only

Frigate needs an MQTT user. mosquitto runs `allow_anonymous false` against a hashed
passwd file sourced from `~/hawksnest-secrets/mosquitto.passwd`. **Regenerating that
file instead of appending drops ring-mqtt's `ring` user and takes every remaining Ring
camera offline at once.**

```sh
mosquitto_passwd -b ~/hawksnest-secrets/mosquitto.passwd frigate '<password>'
grep -q '^ring:' ~/hawksnest-secrets/mosquitto.passwd \
  || echo "STOP — the ring user is gone, do not deploy"
```

### 2. GenAI would have leaked snapshots to OpenAI

**Frigate 0.17 ignores `genai.base_url` for the `openai` provider.** Configuring the
endpoint there looks correct and silently sends camera snapshot images to
`api.openai.com` instead of LM Studio ([maintainer confirmation][genai-disc]; fixed in
0.18). It fails on auth rather than succeeding, but the attempt is the problem — one of
these cameras is in a bedroom.

The endpoint is set via the **`OPENAI_BASE_URL` env var**, declared as a non-optional
`secretKeyRef` so a missing key kills the container instead of falling back to the
public API. `tests/validate_manifests.py` asserts it is present. **Do not "tidy" it into
`config.yml`.** Re-check on the 0.18 upgrade.

### 3. Disk, not the PVC, is the real limit

`frigate-media` is a 200Gi PVC on k3s `local-path` — which is hostPath-backed with **no
quota enforcement**. It will grow past 200Gi and fill the Dragonfly virtual disk, taking
k3s, Home Assistant and the **door locks** down with it.

The actual guardrail is `record.continuous.days` in the Frigate seed (`3` — ≈16 GB for one
camera at ~5.3 GB/day on the sub stream; set to 3 rather than 14 because the first camera is
in a bedroom, decided 2026-07-29). Raise it deliberately, and preferably not before the
DS925+ migration. Add a `df -h` check to the routine.

---

## Architecture

```
                    Reolink E1 Pro (static DHCP)
                    │                        │
        main (2560×1440)              sub (640×360)
                    │                        │
                    ▼                        ▼
      go2rtc (existing pod)           Frigate (new pod)
      └─ WebRTC :8555 (NP 30855)      ├─ detect (OpenVINO)
                    │                 ├─ record 24/7
                    ▼                 ├─ SQLite index + retention
      Hawksnest LIVE view             ├─ CLIP embeddings, GenAI
      /go2rtc/api/ws?src=bedroom      └─ MQTT → HA discovery
      full resolution, sub-second              │
                                    HA + frigate-hass-integration
                                               │
                                  nginx /api/frigate/ (already existed)
                                               │
                                  Hawksnest RECORDED + SEARCH
```

**Live is go2rtc on the main stream; recorded is Frigate on the sub stream. They never
touch each other.** Frigate opens its own connection to the camera rather than pulling
from go2rtc, so a go2rtc restart can't punch a hole in the 24/7 timeline, and go2rtc's
RTSP listener stays off. main and sub are different streams, so nothing is gained by
sharing one connection anyway. Cost: camera credentials appear in two secrets.

**Recorded playback goes through Home Assistant, not a direct Frigate proxy.** Every URL
`Hawksnest/src/lib/cameraEvents.ts` already builds (`/api/frigate/vod/...`,
`/notifications/<id>/clip.mp4`, `/events`) is frigate-hass-integration's scheme exactly —
that code was written against it and has simply never had a Frigate to talk to. Going
direct would mean rewriting working code, losing HA-token auth, and losing the Frigate
entities. `deploy/nginx.conf` already has the `/api/frigate/` location, so **no nginx
change is needed** and `deploy.test.ts`'s five-XFF-clear assertion stays green.

**Naming is load-bearing.** `bedroom` must be identical across the go2rtc stream, the
Frigate camera, the HA entity object id, `src/config/overrides.ts` and the Android
equivalent, because `cameraNameOf()` is `camera.id.split(".")[1]`. Never end a camera
name in `_live`, `_snapshot`, `_event` or `_live_view` — `cameraModel.classify()` splits
on those and would invent a phantom base camera.

---

## Frigate config notes

Schema was verified against **0.17.2**, not written from memory. Two traps:

- **`record.retain` does not exist in 0.17.** It is now `continuous` / `motion` /
  `alerts` / `detections`. A 0.16-style block is rejected outright.
- **`genai.base_url` is ignored** — see above.

The image is **pinned to `0.17.2`, not `:stable`**, and `validate_manifests.py` enforces
that. The config schema moves between minor releases, so a floating tag rolls forward on
any pod recreate and wedges Frigate on a config it can no longer parse. **Re-verify
`kustomize/base/frigate/configmap.yaml` against the release notes before bumping.**

### OpenVINO

Committed default is `device: CPU`, which genuinely handles one camera at 640×360/5fps.
The camera-plan doc names an **RX 9070 XT**, and there is zero GPU precedent anywhere in
this cluster. WSL2 exposes `/dev/dxg` rather than a normal DRI render node, so the Intel
iGPU path is *unproven*, not merely unconfigured. Probe from inside Dragonfly:

```sh
ls -l /dev/dri /dev/dxg && ls /usr/lib/wsl/lib
docker run --rm --device /dev/dri openvino/ubuntu22_runtime \
  python3 -c "from openvino import Core; print(Core().available_devices)"
```

`['CPU','GPU']` → flip `device: GPU` in the seed **and** uncomment the `/dev/dri` +
`/usr/lib/wsl` mounts in `deployment.yaml` (they're commented together on purpose).
Anything else → stay on CPU and move on. `hwaccel_args` is deliberately unset for the
same reason; both flip together if the probe passes.

### Search quality

Detection snapshots come from the `detect` stream, so CLIP and the VLM see 640×360 crops.
Fine for "person in the room", weaker for "man in a red shirt". If semantic search
disappoints, the knob is moving `detect` to the main stream — measure before assuming.
And **tune masks/zones early**: an untuned camera fires on a TV or a ceiling fan, and
every false alert is another junk embedding in the search index. That needs commit 4.

### Bedroom-specific

- `switch.bedroom_privacy_mode` (Reolink integration) physically parks the lens. Commit 6
  is promoted ahead of the search work for this reason.
- ~~Consider whether 14 days of *continuous* is right for this room~~ — **decided 2026-07-29:
  `continuous.days: 3`** (≈16 GB), a scrubbable recent window without two weeks of bedroom
  footage on disk. Alerts stay at 30 days. Retention is per-camera, so a later outdoor camera
  can carry a longer continuous window set under *that* camera, not by raising this one.
- `frigate-config` (the SQLite index, descriptions, embeddings) is on `local-path` and is
  **not** covered by the NFS backup story. Losing it loses all search history.

---

## Remaining work

### 4 — Frigate admin UI (automation)

You can't tune detection without it: masks, zones and object filters are drawn on a live
frame. Follow the ntfy pattern — ClusterIP → NodePort → socat unit in Dragonfly →
Tailscale Serve on its own port, using **8971** (authenticated UI), not 5000. Needs a new
NodePort, a socat unit, a Hyper-V rule, and the NodePort invariant in
`validate_manifests.py` extended (it currently pins 30123/30081/30855).

Interim: `kubectl -n home-automation port-forward svc/frigate 8971:8971`.

### 5 — HA integrations (manual)

1. HACS into `/config/custom_components` on the `ha-config` PVC, then
   frigate-hass-integration. Point it at `http://frigate:5000`.
2. Official **Reolink** integration for PTZ/IR/privacy. **Disable its camera entities** —
   they end in suffixes `cameraModel.classify()` parses and would collide with the Frigate
   camera in `resolveCameras`.
3. Confirm `camera.bedroom` appears with exactly that object id.
4. Add a `recorder.exclude.entity_globs` block for the fps/process sensors. Frigate +
   Reolink add ~25-30 entities per camera, several updating continuously, against
   `purge_keep_days: 30`. Needs the **dual-write ritual** — the seed ConfigMap never
   reaches an already-seeded PVC, so edit the live `/config` too.

### 6-10 — Hawksnest

The web half hinges on one boolean. `CameraPlayer.tsx:78`
(`const isRing = camera.eventSelectId !== null`) gates eight behaviours that are really
three separate capabilities; `CameraPlayer.kt:75` is the identical line gating nine.

**Verified against `main` @ `ffacfc1` on 2026-07-29** — every file/line claim below held except
the token one above. One correction to the framing: the Frigate **event** path is not merely a
seam, it is wired end-to-end on both platforms already (web `CameraPlayer.tsx:114-118` calls
`fetchCameraEvents` in the `!isRing` branch; Android as above). A Frigate camera has no
`eventSelectId`, so it is already `!isRing` and already takes the correct events branch. What
actually breaks for it is narrower than "eight behaviours": `go2rtcSrc` is `undefined` (no live
video at all), `loop={!isRing}` is `true` (recorded playback loops), and `onError`/`onDuration`
are `undefined` (the dead-playlist gap bug). Item 8 is correspondingly smaller than scoped.

- **New `src/lib/frigate.ts`**, mirroring `src/lib/go2rtc.ts` — `primeFrigateCameras()`
  caching `/api/frigate/config`, `frigateHasCamera(name)` with the same circuit-breaker
  semantics. *Verify `/api/frigate/config` proxies through first; the other three routes
  are confirmed, that one is inferred. Fallback is HA entity attributes.*
- **`useRecordedBackend(camera)`** → `"ring" | "frigate" | "none"`, derived rather than
  baked onto `LogicalCamera` so `cameraModel.ts` stays synchronous.
- **`go2rtcSrc` becomes unconditional** on web — `go2rtcMaybeAvailable` already returns
  false for unknown streams.
- **Reuse, don't rebuild, for the gap bug.** `recordingUrlAt` always returns a URL, so a
  Frigate camera scrubbed into a gap mounts a dead playlist with `onError={undefined}`.
  `lib/ringFootage.ts` already solves this (`footageSegmentAt`, `footageSpans`,
  `chooseRecordedSource` with 7 tests). Generalize it to `lib/footage.ts` and normalize
  Frigate's `/recordings` into the same segment shape.
- **Search screen** over `/api/frigate/events` (label/zone/time) and `/events/search`
  (semantic), normalizing into the existing `CameraEvent`. Needs an optional `atMs` on the
  camera overlay store — `CameraPlayer.tsx:81-84` pins a 24h window at mount, so a result
  from last week currently has nowhere to land.

**Two Android divergences that break a naive port:**

1. **Android has no go2rtc stream-list gate.** `CameraPlayer.kt:142` is
   `canGo2rtc = isRing && Go2rtcHealth.maybeAvailable()` — a circuit-breaker only, no
   `/go2rtc/api/streams` check. Dropping the `isRing` gate without porting that check
   gives every camera an 8-second watchdog stall on first open. Same commit, not after.
2. ~~**Android has no token accessor.**~~ **Already solved — verified 2026-07-29 against
   `main` @ `ffacfc1`.** `HaSource.kt:166-178` already does an authenticated
   `/api/frigate/events` read (`Bearer $token`), it is on the `Source` interface
   (`Source.kt:109`), and `CameraPlayerViewModel.kt:133` already calls it through
   `ConnectionManager.fetchCameraEvents`. It went in via the routing-through-`Source` path this
   plan was about to recommend. `RingTimelineClient` still sends no `Authorization`, but that's
   correct — the ring-timeline service doesn't authenticate. **No credential work needed.**

**Lockstep:** `chooseRecordedSource` exists in both `lib/ringFootage.ts` and
`core/logic/RingFootage.kt`. `ARCHITECTURE.md:88-104` states the 1:1 port is deliberate
so the platforms can't drift. Generalize both in the same PR.

**The Ring regression gate:** `CameraPlayerRing.test.tsx` (356 lines, 3 scenarios) and
`e2e/ha/camera-recording.spec.ts` must pass **unedited**. If a Ring test needs changing to
make Frigate pass, the refactor is wrong.

**Docs required in the same PR** (repo convention): `ARCHITECTURE.md` lines 34, 40-52,
53-62, 63-73, 74-86, 88-104, 123-131, 137-145, 302-316 — in particular 53-62's *"a Frigate
seam exists in `cameraEvents.ts`, unused"* and 137-145's *"go2rtc-direct (Ring cams
only)"*, both of which this work makes false. Also `CLAUDE.md`'s camera bullet and the
`deploy/nginx.conf:83` comment claiming there's no Frigate yet.

---

## Verifying

Locally, without a cluster:

```sh
# kustomize v5.4.3 is what CI pins
for ov in prod staging; do
  for ex in kustomize/overlays/$ov/secrets/*.example; do
    r="${ex%.example}"; [ -f "$r" ] || cp "$ex" "$r"
  done
  kustomize build kustomize/overlays/$ov > /tmp/$ov.yaml
  python3 tests/validate_manifests.py $ov /tmp/$ov.yaml
done
```

Both currently pass, and all four new Frigate invariants were negative-tested (each fires
when broken). CI additionally runs kubeconform and HA's own `check_config`.

On the cluster, in order — each gate before the next:

1. **Staging first.** Frigate is parked at 0 there, so this only proves the manifests
   render and nothing collides. This cluster controls door locks.
2. `/go2rtc/api/streams` lists `bedroom` and it plays in Hawksnest. **Then confirm the
   other Ring cameras still play.**
3. `kubectl logs deploy/frigate` — detector initialized, camera connected, no ffmpeg
   restart loop. `curl frigate:5000/api/stats` for inference speed and fps.
4. HA: `camera.bedroom` exists with that exact id; **`ring` still authenticated to
   mosquitto and every Ring camera still online**; the Reolink integration created **no**
   `camera.*` entities.
5. Through the Hawksnest proxy with an HA bearer token:
   `/api/frigate/events?camera=bedroom` and `/api/frigate/config`.
6. Camera wall shows **exactly one** new tile — no `camera.birdseye`, no `image.*` tiles
   (`cards.ts:17-18` maps `image` → `CameraTile`, so they'd render as cameras).
7. Event timestamps match wall-clock local time — the `TZ` check.
8. `df -h` after 24h, extrapolated against `record.continuous.days`.

---

## Out of scope

Phase 2 outdoor/solar/Home Hub (event-driven, won't feed this pipeline). The DS925+
migration — note `spec.nfs` is immutable, so it's a delete-and-recreate, which is exactly
what silently blocked every deploy for four days in July. Retiring ring-mqtt or
ring-timeline; ten Ring cameras are still live.

[genai-disc]: https://github.com/blakeblackshear/frigate/discussions/22224
