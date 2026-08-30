# Reolink + Frigate migration — working plan

**Branch (both repos):** `claude/reolink-frigate-integration-kfvz3h`
**Started:** 2026-07-29 · **Status (2026-07-30): LIVE IN PROD.** Three Reolink cameras
(`big_room`, `first_floor_stairway`, `kitchen`) are recording, detecting and generating AI event
descriptions; the Ring fleet is down from 11 to 8. Hawksnest ships paged Frigate VOD, the
stream-list go2rtc gate, and the direct-RTSP live tier. Remaining work is items 4-5 (manual), 6,
9, and the `RecordedBackend.kt` half of 10 (8b done 2026-07-30).

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
| 4 | automation | Frigate admin UI exposure (NodePort + socat + firewall rule) | ✅ `d3c13e1` + `3370f5e` (Serve `:8447`, https+insecure) |
| 5 | HA | HACS + frigate-hass-integration + Reolink integration | ✅ both live (verified 2026-07-30 — see "PTZ: what is actually there") |
| 6 | Hawksnest | PTZ / IR / **privacy mode** controls | ⬜ ready to build — entity surface measured 2026-07-30, no blockers left |
| 7 | Hawksnest | Dev proxy for `/go2rtc/` + `/ring-timeline/` | ✅ `4a56ef2` |
| 8a | Hawksnest | Backend-capability refactor (`recordedBackend.ts`, `frigate.ts`) + tests | ✅ `4a56ef2` |
| 8b | Hawksnest | Footage generalization — Frigate recordings → continuous lane | ✅ 2026-07-30 (via `frigate/recordings/get` WS — the planned REST `/recordings` route never existed, same as events/config) |
| 9 | Hawksnest | AI search screen, mock-ha fixtures, E2E | ⬜ |
| 10 | Hawksnest | Android parity (`RecordedBackend.kt` ✅ 2026-07-30 [feat/reolink-camera-controls]; go2rtc stream-list gate ✅ 2026-07-30) | ✅ |
| 11 | Hawksnest | **Direct-camera RTSP live tier** (Android-only; `/32` tailnet routes) | ✅ 2026-07-30 |
| — | both | Remove the Ring bedroom camera from the Ring account for good | ⬜ after verify |

~~Nothing is deployed.~~ **Deployed and live since 2026-07-29/30** — see the status line at the top;
the notes further down that say "nothing is deployed" or "the camera is not on the network" are
the pre-deployment record, kept for the reasoning, not the current state.

**The two repos are still out of lockstep on one point:** web derives its recorded backend, Android
still derives its own `isRing` for the RECORDED path. The go2rtc **live** gate half of item 10 is
done (2026-07-30); `RecordedBackend.kt` remains and should not be left to drift.

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
- [ ] **Dedicated non-admin RTSP user, created ON THE CAMERA.** Two traps here, both hit
      2026-07-29:
      - **The Reolink CLOUD account is not a camera account.** Handing RTSP the cloud login
        email (`…@gmail.com`) fails with `401 Unauthorized`, and the local HTTP API rejects it
        too (`"detail": "login failed", "rspCode": -7`) — proving it's a credential problem, not
        URL parsing. Reolink cameras keep their own local user database; the app's cloud identity
        has no standing with RTSP or `/cgi-bin/api.cgi`. Create the user in the camera's own
        **User Management** (web UI at `http://<ip>` now that HTTP is on, or the app), logging in
        as the local `admin` with the device password set during camera setup.
      - **The USERNAME must be URL-safe too, not just the password.** An earlier version of this
        line only warned about the password. Both halves land in `rtsp://user:pass@ip:554/…`
        unencoded, so an `@` in the *username* — which any email address has — breaks the URL
        exactly the same way. Use a plain alphanumeric name like `frigate`.
      - Password rules unchanged: no `/ @ : # ?`.
      - **Do not brute-force this.** The API returns `auth_warning_info.remain_times` (10 at the
        time of writing) and an `unlock_time` — repeated wrong guesses lock the account out.
- [ ] **UID / cloud disabled** in the Reolink app, and the camera blocked from WAN egress
      at the router. The UID is Reolink's P2P relay identity; turning it off is most of
      the point of leaving Ring.
- [x] **`ffprobe` done 2026-07-29.** Confirmed by two independent sources (ffprobe through the
      go2rtc pod, and the camera's own `GetEnc`). Device: **E1 Zoom, itemNo E340**, firmware
      `v3.2.0.4741`, already named "Big Room" on the device.

      | | Configured | Actual | Verdict |
      |---|---|---|---|
      | sub resolution | 640×360 | **640×360** | ✅ matches — bounding boxes are safe |
      | sub codec | h264 | **h264** High | ✅ |
      | sub fps | 5 | **10** | ⚠️ deliberate split, see below |
      | sub bitrate | (assumed) | **256 kbps** → ~2.8 GB/day | disk budget was ~2× too pessimistic |
      | main resolution | 2560×1440 | **3840×2160** | ❌ wrong assumption |
      | main codec | assumed h264 | **h265 / HEVC** | ❌ **breaks WebRTC live view** |

      `detect.fps: 5` against a 10 fps stream is **kept on purpose**: `detect.fps` is the rate
      Frigate runs detection at, not a claim about the stream, and Frigate decimates to it. The
      same sub stream also carries the `record` role, so 10 fps makes the 24/7 timeline smoother
      to scrub while detection gains nothing from the extra frames.
- [x] **Main stream switched to H.264 — done 2026-07-29, ffprobe-verified.** It shipped as
      3840×2160 **h265**, which WebRTC largely cannot play. Fixed at the camera via `SetEnc`
      (not a go2rtc transcode — 4K HEVC transcoding alongside Frigate's detector was never
      viable). Final state, confirmed by ffprobe *after* the change:

      | stream | codec | resolution | fps | bitrate |
      |---|---|---|---|---|
      | main (go2rtc live view) | **h264** High | **2560×1440** | 20 | ~~4096~~ → **5120 kbps** |
      | sub (Frigate detect+record) | h264 High | 640×360 | 10 | 256 kbps |

      2560×1440 is what the design assumed from the start, so nothing downstream changed.

      **Bitrate raised to 5120 on both E1 Zooms, 2026-07-30.** Leaving 4096 after the
      h265→h264 switch was an oversight: h264 is markedly less efficient, and 2560×1440@20 at
      4096 kbps is 0.056 bits/pixel/frame against the ~0.10–0.15 h264 wants — visibly soft and
      blocky on motion. 5120 is the hardware maximum at this resolution (6144 and 7168 are
      silently rejected — the same `SetEnc` trap below, caught by re-reading `GetEnc`).
      The kitchen **E1 Pro (E330, 4 MP)** caps lower and stays at **3072 kbps**; it rejected
      6144. **Its main stream is 2880×1616, not 1440p** (measured by ffprobe 2026-07-30 — don't
      assume it matches the Zooms). That makes it the worst-off camera by a distance: 3072 kbps
      over 2880×1616@20 is **0.033 bpp**, half the Zooms' 0.069 and a fifth of what h264 wants.
      Since its bitrate ceiling is already reached, the only remaining levers are *fewer pixels*
      or *fewer frames* — dropping it to 15 fps would buy ~33 % more bits per frame, and a lower
      main resolution would buy more still. Untouched for now: worth judging on the RTSP/go2rtc
      tier before changing anything. Note this is a *quality* fix and was never the cause of the jerkiness the owner
      reported — that was Hawksnest's Android client falling back to segmented HLS
      (item 10 divergence 1). Judge encoder quality only on a client using the go2rtc tier.

      > **Two traps, both worth remembering.**
      >
      > 1. **`SetEnc` lies about unsupported combinations.** Asking for 4K + h264 returned
      >    `code: 0, rspCode: 200` — a clean success — and left the camera on h265. It does not
      >    reject invalid combos, it ignores them. **Always re-read `GetEnc` or ffprobe after
      >    writing encode settings; never trust the 200.** On this camera 4K is H.265-only, so
      >    raising the resolution back to 4K silently breaks live view again.
      > 2. **The RTSP path name is not the codec.** `h264Preview_01_main` served H.265 quite
      >    happily. The path is a fixed label, not a description.

      Requires **admin**; the read-only `frigate` account gets `enc: permit 4` and cannot write
      (`permit` is a bitmask — 4 = read, 6 = read+write).
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
- [x] **Pods can reach LM Studio — done 2026-07-29 via `lmstudio-fwd.service`**, a socat bridge in
      the distro (`10.42.0.1:21234` → `127.0.0.1:1234`). **Not** a firewall fix; firewall rules
      were a dead end, see "4. LM Studio" below. Verified end-to-end from a pod with a real
      chat-completion call.

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
2. ~~Firewall rules.~~ **A DEAD END — do not repeat this.** Several hours went into firewall
   rules (a scoped `New-NetFirewallRule`, then a `New-NetFirewallHyperVRule` on the WSL
   `VMCreatorId`) on the theory that `192.168.4.34:1234` was being blocked. **Nothing was ever
   being blocked.** Under WSL **mirrored** networking the Dragonfly VM *owns* `192.168.4.34` —
   `ip route get 192.168.4.34` inside the distro returns `local … dev lo`. A pod connecting to
   that address is talking to the VM itself, where nothing listens on 1234. The packets never
   reached Windows, so no firewall rule could ever have helped. Both rules are harmless; delete
   them if you like.

   The tell, if this shape recurs: Caddy on 80/443 *was* reachable from WSL while everything else
   was not — because Caddy actually listens **inside** the VM's reachable path, not because its
   firewall rules were better. Comparing rule configurations sent me the wrong way for a while.

3. **The bridge — `lmstudio-fwd.service`, the actual solution.** Installed and enabled in the
   Dragonfly distro 2026-07-29, on disk at `/etc/systemd/system/lmstudio-fwd.service` (**not**
   `systemd-run` — transient units do not survive a reboot, the same lesson as the Hawksnest
   forwarders).

   ```
   pod -> 10.42.0.1:21234 -> socat -> 127.0.0.1:1234 -> LM Studio on Windows
   ```

   - The distro *can* reach Windows on `127.0.0.1` (mirrored loopback, `LoopbackEnabled: True`).
     A pod cannot, because its `127.0.0.1` is the pod. socat is the only bridge.
   - **Port 21234, not 1234.** Mirrored networking mirrors every Windows *listening* socket into
     the VM, so `bind()` on 1234 fails with `Address already in use` even though `ss` shows
     nothing. 11434 fails too — that is Ollama's port on Windows. Pick a port free **on Windows**.
   - **Bound to `10.42.0.1` (cni0) only**, so it is unreachable from the LAN. That is a better
     answer to the unauthenticated-API problem than the firewall scoping ever was.
   - `Restart=always`, because cni0 does not exist until k3s is up and early starts fail by design.

   Verified end-to-end from a pod: `/v1/models` lists the models, and a real
   `/v1/chat/completions` against `google/gemma-4-e4b` returned correctly (LM Studio JIT-loads
   the model). Confirmed *not* reachable on `192.168.4.34:21234`.
3. `OPENAI_BASE_URL` then points at the host's LAN IP (`192.168.4.34`), **not** `127.0.0.1` or
   `host.docker.internal` — both measured blocked from a pod.

Re-verify from a pod, not from the distro, or you will get a false pass.

**Model id is settled.** All three vision models report `"type": "vlm"` via `/api/v0/models`:
`google/gemma-4-31b-qat`, `google/gemma-4-26b-a4b-qat`, `google/gemma-4-e4b` (all `not-loaded`;
LM Studio JIT-loads on first request). The `qwen3-coder-30b*` models are text-only and would fail
on every event. Pick one of the gemma-4 ids.

## Going back to 4K later (deliberately deferred, 2026-07-29)

**The camera is 4K-capable (3840×2160, 8MP) and is currently running its main stream at
2560×1440.** That is a downgrade, made knowingly, and it is reversible. Recording this so the
choice doesn't calcify into an unexamined default.

> **SCOPE CORRECTION, 2026-08-30.** This whole section is about the **E1 Zoom**, and the
> "4K is H.265-only" claim was measured there. It is **not a fleet law** — the E1 Outdoor Pro
> (`nursery_high`) runs 4K **h264** today, ffprobe-verified: `h264, 3840, 2160, 20/1`. So on a
> new model, re-probe rather than assuming the downgrade is required. Two things measured on
> the way that sharpen the trade below: libwebrtc does not merely fail on HEVC, it **null-derefs
> and kills the app** (SIGSEGV, 2026-08-29); and the RTSP-direct tier that would sidestep HEVC
> **has never been configured on the phone** — `rtspUser`/`rtspPass` are blank, so every camera
> streams over go2rtc/WebRTC today, making WebRTC codec support the binding constraint rather
> than a fallback concern.

**Why it was downgraded:** on this camera 4K is **H.265-only**, and browser WebRTC support for
HEVC is absent-to-marginal (Chrome/Edge especially). 4K therefore meant either no web live view
at all, or go2rtc transcoding 4K HEVC on the same CPU as Frigate's detector — not viable, and
there is no GPU path (the OpenVINO probe found no `/dev/dri`).

**What it actually costs:** *live view sharpness only.* Frigate reads the **sub** stream for both
`detect` and `record`, so 24/7 footage, detection, snapshots, search embeddings and event clips
are all completely unaffected by the main stream's resolution. 1440p is also already more than a
phone display resolves.

**To revert** (needs the camera's **admin** account — `frigate` is read-only):

```sh
# vType MUST be h265; 4K+h264 is silently ignored, see the trap below
curl -X POST "http://192.168.4.37/cgi-bin/api.cgi?cmd=SetEnc&user=admin&password=<pw>" \
  -H "Content-Type: application/json" \
  -d '[{"cmd":"SetEnc","param":{"Enc":{"channel":0,"audio":1,
       "mainStream":{"size":"3840*2160","frameRate":20,"bitRate":4096,
                     "profile":"High","vType":"h265","gop":2},
       "subStream":{"size":"640*360","frameRate":10,"bitRate":256,
                    "profile":"High","vType":"h264","gop":4}}}}]'
# then WAIT ~20s (the encoder restarts; the HTTP API 502s meanwhile) and VERIFY with GetEnc
```

**Revisit it when any of these becomes true** — don't just flip it and hope:

1. **Chrome/Edge ship usable WebRTC HEVC.** This is the real unblock; support has been landing
   incrementally behind flags. Test in the actual Hawksnest web view, not a codec-support table.
2. **Live view moves off WebRTC** to MSE/HLS in go2rtc. Browser HEVC support in MSE is better
   than in WebRTC, though still not universal — worth measuring before committing.
3. **Android-only 4K.** ExoPlayer handles HEVC fine, so the Android app could take a 4K H.265
   stream today while web stays on 1440p. That means two go2rtc stream entries for one camera and
   a client-side choice — real complexity, only worth it if 4K on the phone actually matters.
4. **A GPU appears** for transcoding. Currently disproven — see the OpenVINO section.

Whatever the route, **re-verify with `ffprobe` afterwards**, because `SetEnc` reports success on
changes it silently declines (below).

## Architecture

```
     3× Reolink: big_room + first_floor_stairway (E1 ZOOM), kitchen (E1 Pro)
                    │                        │
        main (2560×1440 / 2880×1616)  sub (640×360 / 896×512)
                    │                        │
                    ▼                        ▼
      go2rtc (existing pod)           Frigate (new pod)
      └─ WebRTC :8555 (NP 30855)      ├─ detect (OpenVINO)
                    │                 ├─ record 24/7
                    ▼                 ├─ SQLite index + retention
      Hawksnest LIVE view             ├─ CLIP embeddings, GenAI
      /go2rtc/api/ws?src=<cam>        └─ MQTT → HA discovery
      full resolution, sub-second              │
      (`<cam>_sub` = Low quality)   HA + frigate-hass-integration
                                               │
                                  nginx /api/frigate/ (already existed)
                                               │
                                  Hawksnest RECORDED + SEARCH
```

**Live is go2rtc on the main stream; recorded is Frigate on the sub stream. They never
touch each other.** Frigate opens its own connection to the camera rather than pulling
from go2rtc, so a go2rtc restart can't punch a hole in the 24/7 timeline, and go2rtc's
RTSP listener stays off. Cost: camera credentials appear in two secrets.

Recorded playback is therefore the 10 fps detect stream, and looks it. Splitting `record`
onto the main stream was tried (#64, 2026-08-24) and **reverted 2026-08-26** — it never
reached the running Frigate (the seed initContainer is first-boot-only), and the nursery
collapses from 3272 kbps to 236 kbps once the other cameras also stream main. The full
measurements, the cause (signal x demand, not signal — big_room is fine at the same
-60 dBm because it only asks for 937 kbps), and what would unblock it are in the record
comment of `kustomize/base/frigate/configmap.yaml`. **Do not re-attempt this FLEET-WIDE
without first fixing the nursery's RF or lowering its main bitrate.**

**One camera now deviates: `nursery_high` records from main as of 2026-08-30** (4K H.265,
4370 kbps measured → ~47 GB/day). That is not a partial rollback of the revert above — the
revert's blocker is seven simultaneous main streams (~24 Mbps of contention), and this is one
(~4.4 Mbps) from the stronger of the two radios in that room. A `nursery` baseline was captured
immediately before the change so a regression is provable rather than remembered. Two facts from
it that generalise: a per-camera main-record split needs **no camera-side change** (SetEnc is only
needed to *downgrade* to h264), and it originally made that camera's recordings **HEVC**, which
turned out to be worse than a compatibility annoyance: libwebrtc **null-derefs on HEVC**, so going
live on it killed the Android app (SIGSEGV in `libjingle_peerconnection_so.so`, 2026-08-29). Fixed
2026-08-30 by re-encoding the camera to **4K h264** — which this model supports, unlike the E1
Zooms that the "4K forces h265" rule below was measured on. Recordings and live view are h264
everywhere now, with no client split at all.

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

1. ~~**Android has no go2rtc stream-list gate.**~~ **DONE 2026-07-30** — `core/net/Go2rtcStreams`
   ports web's cache (60 s TTL, fail-to-EMPTY-set, hosts `Go2rtcHealth`), and `CameraPlayer.kt`'s
   `canGo2rtc` now asks it instead of `isRing`; both landed in one commit as required. This was
   not just parity: the `isRing` gate was dropping every Reolink camera to segmented HLS, which
   is what the owner reported as jumpy live video. The gate is tri-state — while the list is in
   flight the ladder holds the WebRTC arms and must NOT resolve HLS (waking a battery camera's
   stream pipeline is exactly what the lazy-HLS rule prevents).
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

**Carry the `big_room` rename into Hawksnest when items 8b-10 start.** Checked 2026-07-29: the
only coupled file is `src/components/camera/__tests__/CameraPlayerFrigate.test.tsx`, whose fixture
uses `camera.bedroom` as the Frigate camera. It is **not broken** — the fixture defines both the
Frigate config and the go2rtc stream list as `bedroom`, so it is self-consistent and passes. But it
now describes a camera that won't exist, so rename it with that work rather than leaving a test that
documents the wrong room. Every other `bedroom` hit in Hawksnest is unrelated (room icons,
`fan.bedroom` fixtures).

**The Ring regression gate:** `CameraPlayerRing.test.tsx` (356 lines, 3 scenarios) and
`e2e/ha/camera-recording.spec.ts` must pass **unedited**. If a Ring test needs changing to
make Frigate pass, the refactor is wrong.

**Docs required in the same PR** (repo convention): `ARCHITECTURE.md` lines 34, 40-52,
53-62, 63-73, 74-86, 88-104, 123-131, 137-145, 302-316 — in particular 53-62's *"a Frigate
seam exists in `cameraEvents.ts`, unused"* and 137-145's *"go2rtc-direct (Ring cams
only)"*, both of which this work makes false. Also `CLAUDE.md`'s camera bullet and the
`deploy/nginx.conf:83` comment claiming there's no Frigate yet.

---

## Deployed to prod 2026-07-29 — what the rollout actually taught us

Merged as PR #22 (`9111202`); prod deploy green. Verified against the cluster, not the workflow's
green tick. Two things the plan did not predict:

### The mosquitto passwd file needs a POD RESTART, not just a deploy

This is the sharp one, and the plan's whole append-don't-regenerate section missed it. Appending
`frigate` to `~/hawksnest-secrets/mosquitto.passwd` and deploying is **not sufficient**: Frigate came
up and sat in a reconnect loop logging `Unable to connect to MQTT server: MQTT Not authorized`.

The Secret was correct the whole time — `kubectl get secret mosquitto-credentials` showed
`frigate ratgdo ring`. The problem is the mount:

```
passwd  path=/mosquitto/config/passwd  subPath=passwd
```

**Kubernetes never propagates updates to `subPath`-mounted Secrets.** The running pod kept the old
two-user file indefinitely. `kubectl rollout restart deployment/mosquitto` fixed it immediately.
There is no reload path — mosquitto can SIGHUP-reload a passwd file, but it cannot reload one the
kubelet has not updated. **Any future MQTT user change needs the same restart.** Cost is small:
ring-mqtt and HA reconnect within seconds, and the locks are Z-Wave so they are untouched.

Note the check order that made this quick to find: Secret contents first, *then* the pod's mounted
copy. They disagreed, which pointed straight at the mount rather than at the deploy or the append.

### `ratgdo` is a placeholder account, not a live device

The plan (and the guard script) warn that dropping `ratgdo` takes "the garage opener offline at
once". **There is no garage opener.** No retained `ratgdo/#` topics exist, no ratgdo client has ever
connected, and CLAUDE.md defers Ratgdo from V1. Keep preserving the account — it costs nothing and
the guard is still correct — but the stated consequence was overstated, and overstated warnings get
ignored.

### Verified end state

| | |
|---|---|
| go2rtc streams | 11 — `big_room` → **rtsp** (Reolink), `bedroom` → **ring**, no duplicate slug |
| Frigate | camera + capture process up, **no ffmpeg restart loop**, no MQTT errors |
| Detector | OpenVINO CPU, **10.0 ms inference**, camera_fps 5.0 — CPU is comfortably adequate |
| Recording | mp4 segments writing; 44 MB in the first minutes |
| Disk | 55 G used of 1007 G, **902 G available** — the fill-the-disk risk is remote at 3-day retention |
| mosquitto | `frigate ratgdo ring` all intact |
| Locks / Ring | unaffected; every other pod's restart count unchanged by the deploy |

### Unrelated: the host crashed mid-verification

The suite went down during this rollout and it was **not Frigate** — it was unexpected shutdown #22
from the known RAM fault (see the host-RAM-fault memory + `HARDWARE-RMA.md`). Symptom was
`wsl.exe … 0x8007274c` with the VM alive but workloads unresponsive. Worth stating plainly because
the timing invites the wrong conclusion: Frigate was suspected first, and the host was *not*
resource-starved at the time (CPU 55%, 7 GB RAM free, 686 GB disk free). Everything came back on its
own after recovery, restart counts +1 across the board.

One real contribution from this work, though: verifying GenAI made LM Studio JIT-load a model that
stayed resident, pushing host memory to 93%. **Consider a TTL on the GenAI model** so an idle
description model does not hold ~6 GB indefinitely on a box that also hosts an 18 GB coding model.

## Two post-deploy traps found while tuning (2026-07-29)

### `detect.enabled` defaults to FALSE in 0.17 — Frigate recorded for hours and detected nothing

The seed set `detect.width/height/fps` but never `detect.enabled`, and **Frigate 0.17 treats an
absent `enabled` as false**. The result is the worst shape of failure: pod healthy, ffmpeg healthy,
`camera_fps` correct, recordings accumulating on disk — and **zero detection**. No events, no CLIP
embeddings, no alerts, no GenAI descriptions. Nothing in `kubectl logs` says so.

It was caught only by pulling a camera snapshot, seeing a person plainly in frame, and noticing
`detection_fps: 0.0`. The two reliable tells:

```sh
curl .../api/stats            # -> "detection_enabled": false
mosquitto_sub -t 'frigate/<camera>/detect/state'   # -> OFF
```

Fixed by adding `enabled: true`. After a restart: `detection_enabled: true`, `detection_fps: 5.0`,
`inference_speed: 2.09 ms`, `skipped_fps: 0.0`. **2 ms inference settles the CPU-vs-GPU question for
good** — the OpenVINO CPU detector is not remotely stressed by one 640×360 camera.

### The Frigate seed is FIRST-BOOT ONLY — the ConfigMap is decorative once deployed

`seed-config` is `if [ ! -f /config/config.yml ]`, unlike go2rtc's init which re-seeds on every
start. Two consequences, and the second one bites:

- Good: masks, zones and passwords set in Frigate's UI **survive restarts**.
- **Editing `kustomize/base/frigate/configmap.yaml` and deploying does NOT change the running
  Frigate.** The file already exists on the PVC, so the seed is skipped silently.

Every Frigate config change therefore needs a **dual write** — the repo seed *and* the live
`/config/config.yml` — the same ritual the plan already calls for on HA's `recorder.exclude`. The
`detect.enabled` fix above was applied both ways. To force a full re-seed instead, move
`/config/config.yml` aside and restart, but that discards anything drawn in the UI.

## BLOCKER: Frigate recorded playback 401s — the integration requires signed segment URLs

**Found 2026-07-29 by testing in the app: live view works, Ring works, Frigate scrubbing is a
black screen.** This is an app-side gap, not a misconfiguration, and it blocks item 8b/9 testing.

frigate-hass-integration **v5.15.4 requires an `authSig` query parameter on every VOD *segment*
request**. Playlists do not need it. `VodSegmentProxyView._async_validate_signed_manifest()` is
unconditional — there is no config option to disable it, and a valid Bearer token is not enough.

Measured, isolating each hop:

| request | result |
|---|---|
| Frigate direct `:5000` → segment | 200 |
| HA proxy → `master.m3u8` | 200 |
| HA proxy → `index-v1-a1.m3u8` | 200 |
| HA proxy → `seg-1-v1-a1.m4s` | **401** |
| HA proxy → same segment **with `authSig`** | **200** (`video/mp4`) |

The HA log line is the giveaway and is easy to miss:
`Missing authSig query parameter on VOD segment request.`

### Why it fails

`recordingUrlAt()` (`src/lib/cameraEvents.ts:62`) builds a plain
`/api/frigate/vod/<cam>/start/<s>/end/<e>/master.m3u8`. The manifest loads fine, hls.js then
requests segments *relative to it* — and relative resolution **drops the query string**, so no
segment ever carries a signature.

### The fix, verified end to end

`authSig` is a JWT signed with HA's `DATA_SIGN_SECRET`, obtained from the **`auth/sign_path`**
WebSocket command. Crucially the validator only checks
`claims["path"].startswith(request.path.rsplit("/", 1)[0])` — so **one signature for the manifest
path covers every segment in that directory**. There is no need to sign each segment.

1. `auth/sign_path` on the manifest path (`expires` ~600s), extract `authSig`.
2. Load that signed URL as the HLS source.
3. Configure hls.js `xhrSetup` to append the same `authSig` to segment requests.

### ANDROID IS WORSE, AND ANDROID IS WHERE THIS WAS FOUND

The above describes the **web** path. On **Android every request 401s, including the manifest** —
measured with no auth header, exactly what the app sends today:

```
master.m3u8       -> 401
index-v1-a1.m3u8  -> 401
seg-1-v1-a1.m4s   -> 401
```

`VideoPlayer.kt` builds a bare `ExoPlayer.Builder(context).build()` and `Uri.parse(url)` — **no
DataSource.Factory, no Authorization header anywhere.** That has never mattered before because
Frigate VOD is the first thing Android plays that needs HA auth: live view goes through
go2rtc/WebRTC, and Ring recorded goes through ring-timeline, which deliberately does not
authenticate (see the note on `RingTimelineClient`). So this is a gap the Ring-only design never
exposed, not a regression.

### The design this points to (verified)

**Signed URLs need no Bearer token at all** — measured, with no auth header:

```
master.m3u8 ?authSig=...  -> 200
segment     ?authSig=...  -> 200
```

So both platforms want the same shape, and it is simpler than adding token plumbing:

1. `auth/sign_path` on the manifest path → one `authSig`.
2. Use the signed manifest URL as the source.
3. Append that same `authSig` to every subsequent request — Android via a
   `DataSource.Factory` wrapper, web via hls.js `xhrSetup`. Needed because both resolve segment
   URLs relative to the manifest, which **drops the query string**.

No Authorization header is required on either platform. Note the signature expires, so a long
scrub session needs re-signing — pick `expires` accordingly and handle 401-on-refresh.

### Remaining caveat, web only

`xhrSetup` does not exist on native HLS. `HlsPlayer.tsx:106-115` loads hls.js only when the browser
lacks native HLS, so Safari/iOS would still 401 on segments. Either force hls.js for Frigate VOD,
rewrite the manifest in nginx to append `authSig` per line, or document native HLS as unsupported
for Frigate recorded playback. **Android is unaffected by this particular choice.**

## A single VOD manifest caps at ~3 hours — "one continuous VOD" does not scale

**Measured 2026-07-29.** Frigate's `/vod/` endpoint 503s past roughly 3 hours:

```
 60min -> 200 (330 segments)      190min -> 200 (778)
120min -> 200                     220min -> 200 (940)
180min -> 200                     230min -> 503
```

The cause is in Frigate's bundled nginx, not Frigate itself:

```
media_set_parse_durations: invalid number of elements in the durations array 1108
```

That is **nginx-vod-module's hard segment-count ceiling (~1024)**. At the ~11s segments these
cameras produce it lands at ~3 hours. It is a compile-time constant, so it cannot be raised from
config — only by rebuilding the module, which is not worth doing on a pinned upstream image.

**This invalidates an assumption in the design, not just a nice-to-have.** ARCHITECTURE.md says the
Frigate path is "one continuous VOD spanning the window", and `CameraPlayer` pins a **24h** window —
which is already 8× over the limit. That path has never worked for a full window and never could;
it only appeared to work in testing because short recent windows fall under the cap.

### What scrubbing the full retention actually requires

The owner's requirement (2026-07-29) is to scrub the whole retention period — 3 days today, and
whatever `record.continuous.days` says later. That needs the timeline and the media to decouple:

- **Timeline UI spans the full retention.** Purely presentational, cheap, and it is what makes the
  3 days feel reachable. Today it is hardcoded `DAY_MS` (`CameraPlayer.kt:49,77`) with
  `Timeline24h` clamping zoom to a 24h maximum, so both need to take the retention span instead.
- **The VOD manifest becomes a bounded window that follows the playhead** — around 1-2h, safely
  under the 1024-segment cap — refetched (and re-signed) when the playhead scrubs outside it.
  This is the normal shape for long-retention NVR scrubbing; loading three days of segments up
  front was never viable.
- **Retention should be discovered, not hardcoded.** `/api/config` exposes
  `record.continuous.days`, so the window can follow the Frigate config rather than drifting from
  it the way a constant would.

Note the interaction with signing: each window is a distinct path, so a new window needs a new
`authSig`. Paging the window and re-signing are the same event, which keeps that simple.

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

---

## 11. Direct-camera RTSP live tier (Hawksnest Android) — built 2026-07-30

**Why, when go2rtc already carries these streams.** go2rtc re-packages the camera's RTSP into
WebRTC: continuous, but with a relay hop and (here) TCP transport. Playing the camera's own RTSP is
what the Reolink app does, and it is the shortest path that exists. It is the **top** tier, not a
replacement — it needs credentials, a routable camera IP, and one of the camera's few RTSP
sessions, so anything that can't satisfy all three steps down to go2rtc.

**Android-only, permanently.** Browsers cannot play RTSP at any level, so the web client's ceiling
is and stays WebRTC. This is the one place the two ladders legitimately differ; `ReolinkRtsp.kt`
has no web twin on purpose. Don't file it as a parity gap.

Ladder is now: recorded VOD → **RTSP-direct** → go2rtc WebRTC → HA WebRTC → HLS → MJPEG → snapshot.

**Setup is per-phone, in the app** (Settings → *Camera direct stream*): camera username, password,
and a camera-name→IP list. Password is Keystore-wrapped like the HA token; the whole DataStore file
is already excluded from cloud backup and device transfer. Nothing is baked into the repo — it is
public, and the camera account is a house credential.

**Network prerequisite:** per-camera `/32` Tailscale subnet routes, advertised by this host and
approved in the admin console. Full runbook, including the `set --advertise-routes` replace-not-
append trap, in `windows/README-windows.md`. Approved 2026-07-30 for `.37`, `.53`, `.64`.

**New-camera runbook gains two steps:**
1. Re-issue `tailscale set --advertise-routes=…` with **every** camera `/32` (it replaces the list),
   then approve the new route in the admin console.
2. Add the camera's name→IP row in the app's Settings on each phone that should use the tier.

### PTZ: what is actually there (measured 2026-07-30, supersedes the guesses above)

The pre-deployment notes in this file say ONVIF port 8000 is closed, only 9000 is open, and
the Reolink integration is not installed. **All three are now false** — measured against the
live cameras and the running HA, not inferred:

- **Open ports on all three cameras: 80, 554, 8000, 9000.** The HTTP API and ONVIF are both
  reachable; nothing needs enabling on the cameras for PTZ.
- **The official Reolink integration is installed and its entities exist.** The earlier
  "not installed" reading came from the HA *seed* ConfigMap, which cannot show it: UI-installed
  integrations live in `.storage/core.config_entries`, never in `configuration.yaml`. Don't
  conclude an integration is absent from the seed alone.
- **The `camera.*` collision trap never fired** — the integration's own camera entities are not
  present, so `camera.big_room` / `first_floor_stairway` / `kitchen` remain the Frigate ones.
  Keep it that way if the integration is ever re-added.

Entity surface, per camera (from `/api/states`):

| | big_room (E1 Zoom) | stairway (E1 Zoom) | kitchen (E1 Pro) |
|---|---|---|---|
| `button.<n>_ptz_{up,down,left,right,stop,calibrate}` | ✅ | ✅ | ✅ |
| `number.<n>_zoom` (0–32) / `number.<n>_focus` (0–285) | ✅ | ✅ | ✖ none |
| `switch.<n>_auto_focus` | ✅ | ✅ | ✖ none |
| `sensor.<n>_ptz_{pan,tilt}_position` | ✅ | ✅ | ✅ |
| `select.<n>_ptz_preset` | ✖ | ✖ | ✖ |

Three consequences the client work must respect:

1. **The Reolink device name is NOT always the camera base.** The stairway's PTZ entities are
   `button.stairway_ptz_*` / `number.stairway_zoom`, while its Frigate camera is
   `camera.first_floor_stairway`. Deriving PTZ entity ids from the camera base would silently
   drop PTZ on that camera — capability detection must tolerate an alias, not assume equality.
2. **No preset select exists, and that is a camera-side fact, not a bug.** `GetPtzPreset`
   returns six slots (`pos1`…`pos6`) all with `enable: 0` — nothing is saved, so the
   integration creates no select. Save a preset in the Reolink app and the entity appears.
3. **Custom PTZ speed is unsupported on this hardware.** The camera's own `GetAbility` reports
   `supportPtzSpeed: {permit: 0, ver: 0}`, so `reolink.ptz_move` with a speed argument is out —
   plain button presses are the whole vocabulary. (Same probe confirms what IS supported:
   `ptzCtrl` ver 2, `supportPt`, `supportZoom`, `supportFocus`, `disableAutoFocus`, `ptzPreset`,
   `ptzPatrol`, and `aiTrack` — the E1 Zooms can auto-track, which Frigate's own autotracking
   cannot drive.) `supportDigitalZoom: permit 0` — the zoom is optical only.

**Still unmeasured: press semantics.** Whether a direction press moves continuously until
`ptz_stop` or advances one step was not tested, because the test physically re-aims a recording
camera. The dedicated stop button and the absence of a speed parameter both point to
continuous, but it is unverified. The client design does not depend on the answer: press on
touch-down, `ptz_stop` on release/unmount covers both (continuous → hold-to-move; step → one
step per tap, with the stop a harmless no-op). Confirm it during the on-device smoke test.

Baseline aim, if a test ever needs restoring: big_room pan 5345 / tilt 0 / zoom 15 / focus 187;
stairway pan 3314 / tilt 750 / zoom 0 / focus 36; kitchen pan 3682 / tilt 382. Autofocus on for
both Zooms.

### Two things to keep in mind

0. **In-cluster API exposure is an ACCEPTED RISK, and NetworkPolicy cannot narrow it today**
   (audited 2026-07-30). Frigate `:5000` and go2rtc `:1984` are unauthenticated ClusterIP
   surfaces — by upstream design for `:5000` (frigate-hass-integration expects it) — reachable
   by anything in the namespace. The obvious fix, a NetworkPolicy restricting them to the HA
   and Hawksnest pods, was written and then withdrawn: **this k3s runs `--disable-network-policy`**,
   so the policy would sit in the cluster enforcing nothing — protection that reads as present
   but isn't, which is worse than a recorded gap. Enabling enforcement means removing the flag
   and restarting k3s (a full cluster restart on the node that runs the door locks), so it is
   deliberate scheduled work, not a drive-by: do it at the next planned k3s maintenance window,
   then land the policies. Until then the guards are: single-tenant cluster, the split
   frigate/frigate-ui Services (5000 is never NodePort-exposed), and Frigate's authenticated
   UI on `:8971`.

1. **RTSP session budget.** Reolink cameras allow only a handful of concurrent sessions.
   Frigate holds ONE per camera (the sub stream, carrying `detect` + `record`), go2rtc opens
   the main only while someone is watching, and **each viewing phone takes another main** — so
   the idle baseline is one session per camera and the first viewer makes two. (Verified
   2026-08-26 by decoding `/proc/net/tcp` in both pods: exactly one ESTABLISHED :554 session
   per camera from Frigate, none from go2rtc while idle.) The nursery served three concurrent
   main streams plus its sub without complaint, so the cap is not the practical limit here —
   airtime is; see the record comment in the frigate ConfigMap. An
   over-budget open is rejected by the camera, which the app treats as a fail-fast → go2rtc. That
   is the designed behaviour, but it means "live view got slower when two people watched at once"
   has a real cause. If it becomes common, point the phone at the sub stream instead.
2. **Fixed bitrate has no adaptation.** The main stream is ~5 Mbps regardless of link quality, so a
   weak cellular connection degrades to a *stall*, not to lower quality. The player treats a
   7-second post-play stall as a failure and steps down to go2rtc, which does adapt. The stall is
   deliberately NOT recorded against the camera's circuit-breaker — the camera was fine.

Failure handling is per-camera (`core/net/RtspHealth`), unlike go2rtc's process-wide breaker:
go2rtc is one shared service so one failure predicts all, whereas each camera is its own server and
a global verdict would let one powered-off camera downgrade the whole fleet for the session.

**Cleartext invariant is intact and was verified, not assumed:** `media3-exoplayer-rtsp` 1.10.1 has
zero references to `NetworkSecurityPolicy`, and `RtspClient`/`RtspMessageChannel` use raw
`java.net.Socket` — the policy only binds cooperating HTTP stacks. Hawksnest's
`cleartextTrafficPermitted="false"` needed no exception. If a future media3 changes that, stop and
discuss: a scoped `<domain-config>` cannot match bare IPs.
