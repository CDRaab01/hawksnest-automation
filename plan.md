# Reolink + Frigate migration — working plan

**Branch (both repos):** `claude/reolink-frigate-integration-kfvz3h`
**Started:** 2026-07-29 · **Status:** automation side landed, Hawksnest side not started

Replacing Ring with Reolink + Frigate: local RTSP, 24/7 recording, and AI event search,
all on-prem. Phase 1 is the indoor cameras; the first is a **Reolink E1 Zoom** taking
over the **big room**. Design rationale lives in the personal camera-plan doc — this
file is the executable version and the source of truth for status.

> **Retargeted 2026-07-29 — read this before trusting older notes below.** Two of the
> founding assumptions were wrong, and both were caught from a photo of the Reolink app
> rather than from anything in the repo:
>
> - **The room is the BIG ROOM, not the bedroom.** A bedroom Reolink is still planned, but
>   for a later phase. The Reolink therefore claims the `big_room` slug, the **Ring big_room**
>   camera is the one retired, and the **Ring bedroom camera stays live** until its
>   replacement arrives. Commit 1's original direction is inverted.
> - **The camera is an E1 Zoom, not an E1 Pro (E330).** Different model: 5MP main stream
>   (2560×1920, not 2560×1440) and different sub-stream defaults. Every `detect:` number in
>   the seed came from the *E1 Pro* spec sheet and is now doubly unverified.
>
> Consequences that are easy to miss: the `continuous.days: 3` retention was chosen
> *specifically* so two weeks of bedroom footage wouldn't sit on disk. That rationale no
> longer applies to this camera — it stays at 3 for now only because the disk-growth rate
> on an unquotaed volume is still unmeasured. The privacy-mode work (item 6) was promoted
> ahead of search *for the bedroom*, so it can drop back down the order.

Spans two repos:
- **`hawksnest-automation`** (this one) — the cluster: go2rtc, Frigate, storage, secrets.
- **`Hawksnest`** — the app: live view, recorded timeline, search UI, Android.

---

## Status

| # | Repo | Commit | State |
|---|---|---|---|
| 1 | automation | ~~Retire Ring bedroom camera~~ → **retire Ring `big_room`**, free that slug | ✅ `dfee869`, inverted 2026-07-29 |
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

### Resume state — re-verified 2026-07-29 after a host restart

The restart cost nothing. Everything below was measured, not assumed:

- **Cluster healthy.** All 9 pods Running in `home-automation`; `zwave-js-ui` self-recreated ~2 min
  after boot (the S4U watchdog worked — locks were never at risk).
- **Nothing from this branch is deployed, as expected.** Live go2rtc still lists `bedroom` as a
  **Ring** stream, and all **11 Ring cameras are present and healthy**. Commit 1 (retiring the Ring
  bedroom camera) has not landed on the cluster.
- **Both overlays render and validate clean** — prod 47 resources, staging 43, all invariants hold.
  (Checked with `kubectl kustomize`, embedded v5.7.1; CI pins standalone **v5.4.3** — the render
  agreed, but CI remains the authority.)
- **Secrets state unchanged and matches the warnings below.** `mosquitto.passwd` still has exactly
  `ring` + `ratgdo`; `go2rtc.env` still has `RING_REFRESH_TOKEN` + 11 `RING_DEVICE_ID_*` and **no**
  `REOLINK_*`; `frigate.env` still absent. Append, don't regenerate.
- **The camera is not on the network.** All 35 live hosts on `192.168.4.0/24` were port-scanned; none
  has **554 or 8000** open, and no Reolink OUI appears. (One host, `.56`, has `443` open but speaks
  neither TLS nor HTTP and has no 554/8000 — not a camera.) Phase A steps 1-4 (DHCP reservation, RTSP
  user, UID/cloud off, `ffprobe`) are hard-blocked until the E1 Pro is powered on and onboarded.

  > **Scan from Windows, not from WSL.** A ping sweep run *inside* the Dragonfly distro under
  > mirrored networking silently missed **12 live hosts** that the same sweep from the Windows host
  > found — WSL left them `INCOMPLETE` in `ip neigh` rather than reporting them. Scanning only the
  > WSL-visible subset would have produced a confident "not on the network" off an incomplete host
  > list. Enumerate hosts with `arp -a` on the **host**, then port-scan; don't trust `ip neigh` in
  > the distro for discovery.
- **The `ffprobe` measurement is still the one that matters.** `detect:` is `640x360 @ 5fps` from the
  spec sheet and has *not* been confirmed against the real stream. A mismatch misplaces every
  bounding box silently.

Unrelated: `windows/README-windows.md` has an **uncommitted** edit parked on this branch (the Z-Wave
S4U watchdog write-up). It is correct and worth keeping, but it belongs to the Z-Wave work, not the
Reolink migration — commit it separately so it isn't dragged into this PR.

---

## Blocked on physical setup

Commits 1-3 cannot deploy until these exist. All are Phase 0, all need the camera in hand.

- [x] **Camera is on the LAN — `192.168.4.37`** (MAC `14:14:16:f8:4b:e1`), confirmed 2026-07-29.
      Still needs a **DHCP reservation** so it can't drift.
- [ ] **Enable RTSP (and HTTP) on the camera — it is currently reachable but mute.** Port scan of
      `.37` shows **only port 9000 open** (Reolink's proprietary app protocol). `554` (RTSP), `80`,
      `443` and `8000` (ONVIF) are all **closed**, which is why every discovery sweep and the ONVIF
      WS-Discovery probe found nothing. Recent Reolink firmware ships these disabled. Turn RTSP on
      in the camera's settings (Reolink app or web UI → Network → Advanced → Port Settings); the
      `ffprobe` step and Frigate both depend on it and will fail identically until then.
- [ ] **Dedicated non-admin RTSP user** on the camera. Password must be URL-safe
      (no `/ @ : # ?`) — it goes into an `rtsp://` URL unencoded in two configs.
- [ ] **UID / cloud disabled** in the Reolink app, and the camera blocked from WAN egress
      at the router. The UID is Reolink's P2P relay identity; turning it off is most of
      the point of leaving Ring.
- [ ] **`ffprobe rtsp://<user>:<pw>@<ip>:554/h264Preview_01_sub`** — record the real
      resolution and fps. `detect:` in the Frigate seed is currently `640x360 @ 5fps` from
      the E1 Pro's nominal spec. A mismatch doesn't error; it silently misplaces every
      bounding box.
- [x] **iGPU probe** — **done 2026-07-29, answer is CPU.** `/dev/dri` does not exist in the
      Dragonfly distro; only `/dev/dxg` plus the WSL d3d12 shims (`libd3d12.so`,
      `libd3d12core.so`, `libdxcore.so`). OpenVINO's GPU plugin needs a real render node, so
      the container probe can't even bind `--device /dev/dri`. **Stay on `device: CPU`** — which
      is already the committed default, so *no config change is needed*. Leave the `/dev/dri` +
      `/usr/lib/wsl` mounts in `deployment.yaml` commented and `hwaccel_args` unset.
- [ ] **`frigate.env` created** at `/home/sonic/hawksnest-secrets/` (it does not exist yet) and
      **`go2rtc.env` APPENDED to** — verified 2026-07-29, the live file already holds
      `RING_REFRESH_TOKEN` + 11 `RING_DEVICE_ID_*` and has none of the `REOLINK_*` keys. Rewriting
      it from the example costs the Ring refresh token and every device id. Same append-don't-
      regenerate hazard as the mosquitto file below. (It also still carries a now-unused
      `RING_DEVICE_ID_BEDROOM` from before commit 1 retired that camera — harmless, leave it.)
- [ ] **`frigate` MQTT user appended** to the mosquitto passwd file — see the warning below.
- [x] **LM Studio bound to `0.0.0.0:1234`** — done 2026-07-29 via
      `lms server start --bind 0.0.0.0 --port 1234`. The GUI toggle is hard to find; the CLI
      flag is the reliable route, and it persists in
      `~/.lmstudio/.internal/http-server-config.json` as `"networkInterface"`.
- [ ] **Firewall rule for 1234 — now the actual blocker.** With the bind fixed, the host firewall
      is what's left: `192.168.4.34:1234` is still refused from both the distro and a pod. Needs an
      **elevated** shell (see "4. LM Studio" below for the exact command). Scope it to
      `192.168.4.0/22` + `10.42.0.0/16` rather than Any — the LM Studio API has **no
      authentication**, so a wide-open 1234 hands every device on the LAN a free GPU.

---

## Four things that will bite

### 1. The mosquitto password file is append-only

Frigate needs an MQTT user. mosquitto runs `allow_anonymous false` against a hashed
passwd file sourced from `~/hawksnest-secrets/mosquitto.passwd`. **Regenerating that
file instead of appending drops ring-mqtt's `ring` user and takes every remaining Ring
camera offline at once.**

**The file has TWO existing users, not one** — verified on the runner 2026-07-29: `ring` AND
`ratgdo` (the garage opener). An earlier version of this guard only checked for `ring` and would
have passed a file that had silently dropped `ratgdo`.

```sh
# -b appends (or updates just that user); it does NOT rewrite the file.
mosquitto_passwd -b /home/sonic/hawksnest-secrets/mosquitto.passwd frigate '<password>'

# Every pre-existing user must still be there.
for u in ring ratgdo frigate; do
  grep -q "^$u:" /home/sonic/hawksnest-secrets/mosquitto.passwd \
    || echo "STOP — '$u' is gone, do not deploy"
done
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

### 4. LM Studio is loopback-only — the firewall rule is not the fix

**Verified on the host 2026-07-29.** LM Studio listens on **`127.0.0.1:1234` only**:

```
LocalAddress LocalPort OwningProcess
127.0.0.1         1234         21684
```

The plan previously said "you'll also need a Hyper-V firewall rule for port 1234". That is
necessary but **not sufficient, and on its own does nothing** — no firewall rule can expose a
socket bound to loopback. Measured reachability:

| From | `1234` |
|---|---|
| Windows host | reachable |
| Dragonfly distro (mirrored WSL maps host loopback) | reachable |
| **A k3s pod** (`exec` into `go2rtc`, tried `127.0.0.1`, `192.168.4.34`, `10.42.0.1`) | **all blocked** |

A pod has its own netns, so it never inherits mirrored-WSL's loopback mapping — `127.0.0.1`
inside the pod is the pod. **Frigate is a pod.** Order of operations:

1. ✅ **Done 2026-07-29.** Don't hunt for the GUI toggle — it is easy to confuse with
   "Enable Local LLM Service (headless)" in App Settings, which is a *different* setting and does
   nothing for binding. Use the CLI:
   ```powershell
   & "$env:USERPROFILE\.lmstudio\bin\lms.exe" server start --bind 0.0.0.0 --port 1234
   ```
   It persists to `~/.lmstudio/.internal/http-server-config.json` (`"networkInterface": "0.0.0.0"`),
   so it survives restarts. Revert with `--bind 127.0.0.1`.
2. **Then the firewall rule — still outstanding, and now the only thing in the way.** Re-measured
   after the rebind: `192.168.4.34:1234` is *still* refused from the distro and from a pod, so the
   host firewall is genuinely blocking. Needs an **elevated** PowerShell:
   ```powershell
   New-NetFirewallRule -DisplayName "LMStudio-1234" -Name "LMStudio-1234" `
     -Direction Inbound -Action Allow -Protocol TCP -LocalPort 1234 `
     -RemoteAddress @("192.168.4.0/22","10.42.0.0/16") -Profile Any
   ```
   **Scope it, don't use `-RemoteAddress Any`.** The LM Studio API is unauthenticated; an open
   1234 is a free GPU for anything on the LAN. Note the LAN is a **/22**, not a /24.
   May also need the Hyper-V rule, same shape as `HomeAssistant-8123` (on
   `VMCreatorId {40E0AC32-46A5-438A-A0B2-2B479E8F2E90}`) — add it only if the plain rule
   isn't enough. Note **`Go2rtc-8555` does not actually exist** despite
   `windows/README-windows.md` claiming it; go2rtc gets by via the socat units.
3. `OPENAI_BASE_URL` then points at the host's LAN IP (`192.168.4.34`), **not** `127.0.0.1` or
   `host.docker.internal` — both measured blocked from a pod.

Re-verify from a pod, not from the distro, or you will get a false pass.

**Model id is settled.** All three vision models report `"type": "vlm"` via `/api/v0/models`:
`google/gemma-4-31b-qat`, `google/gemma-4-26b-a4b-qat`, `google/gemma-4-e4b` (all `not-loaded`;
LM Studio JIT-loads on first request). The `qwen3-coder-30b*` models are text-only and would fail
on every event. Pick one of the gemma-4 ids.

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

> **Probe run 2026-07-29 — the answer is CPU; this is closed.** `/dev/dri` does not exist in the
> distro (only `/dev/dxg` + the WSL d3d12 shims), so the `docker run --device /dev/dri` step can't
> even start. The WSL2 iGPU path is now *disproven*, not merely unproven. **No change required** —
> `device: CPU` is already committed, the mounts stay commented, `hwaccel_args` stays unset. Don't
> re-litigate this without a real render node appearing.

### Search quality

Detection snapshots come from the `detect` stream, so CLIP and the VLM see 640×360 crops.
Fine for "person in the room", weaker for "man in a red shirt". If semantic search
disappoints, the knob is moving `detect` to the main stream — measure before assuming.
And **tune masks/zones early**: an untuned camera fires on a TV or a ceiling fan, and
every false alert is another junk embedding in the search index. That needs commit 4.

### Room-specific

**Superseded by the 2026-07-29 retarget — this camera is the big room.** Kept because all of
it applies to the bedroom Reolink planned for a later phase:

- `switch.<camera>_privacy_mode` (Reolink integration) physically parks the lens. Commit 6 was
  promoted ahead of the search work *because the first camera was going in a bedroom*. With the
  big room first, that urgency is gone — **it can drop back behind the search work**. It becomes
  a hard prerequisite again when the bedroom camera lands.
- ~~Consider whether 14 days of *continuous* is right for this room~~ — the `continuous.days: 3`
  decision was made for a **bedroom**, and that reasoning does not transfer to a living space.
  It stays at 3 for now purely as a disk guardrail (first Frigate camera, unquotaed hostPath,
  growth rate still unmeasured), **not** for privacy. Once `df -h` after 24h gives a real number,
  14 days is reasonable for this room. Retention is per-camera, so the future bedroom camera
  should carry its own shorter window set under *that* camera — don't raise this one and assume
  it covers both.
- Alerts/detections stay at 30 days either way.
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
