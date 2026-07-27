# ring-timeline

Read-only HTTP service that exposes **Ring's recorded-footage timeline** to Hawksnest.

## Why it exists

Home Assistant can't reach this data. ring-mqtt models recorded playback as a 40-slot `select`
entity (`Motion 1`…`On-demand 5 (Transcoded)`) that carries **no event times**, and its per-event
URL lookup silently produces nothing for several of our cameras — the wired ones show 19–20 real
recordings a day in Ring's own API while HA can play none of them. Everything needed is in Ring's
`video_search/history` endpoint (what the Ring app's own scrub bar uses):

- real event times and durations
- thumbnails and person-detection flags
- **pre-signed S3 mp4 URLs a player can load directly** (verified: ranged `GET` → `HTTP 206`)

That endpoint needs Ring account auth, which can't live in a browser or on a phone. Hence a service.

It is deliberately small: read-only, no writes to Ring, and **no media proxying** — clients fetch
the signed URL straight from S3, so no footage flows through the cluster.

## 24/7 footage — where it actually lives

An earlier version of this file said 24/7 was impossible here, because CVR needs a wired *Pro*
camera and `getPeriodicalFootage` returns `403 AccessDeniedException`. Both facts are true and the
conclusion was still wrong: **seven Indoor Cams on this account do record continuously**, and the
Ring app's scrub bar never uses `video_search` for it. Captured off the live Ring web client, it
calls the Event Video Manager timeline instead:

```
GET https://api.ring.com/evm/v2/timeline/24/devices/{id}
    ?start_time=<ISO>&end_time=<ISO>&order=ASC&visualizations=cloud,local,footage
```

The web client hits this via `account.ring.com/api/cgw/…`, which authenticates with a session
cookie plus a `csrf-token` header — no use to a server. The same route on Ring's bearer-token host
answers instead: `api.ring.com/cgw/…` 404s while `api.ring.com/evm/v2/…` 401s, i.e. it exists there
and only wants auth, so it goes through the same `restClient` bearer as everything else here.

Ring stitches server-side: one request for an arbitrary window returns a **single** `CloudMedia`
item spanning all of it (verified on a 12-hour window — one chunked H264/opus mp4), not a pile of
chunks to concatenate. Four schemas come back and only the first is continuous video:

| Schema | Meaning |
|---|---|
| `CloudMedia` | the stitched 24/7 track — the seven wired cameras only |
| `Event` | motion/person markers, duplicating `/timeline` |
| `Footage` | `ONLINE_PERIODICAL` — an hourly 10-second timelapse of ~20 periodic snapshots. What the battery cameras and doorbell have *instead* of 24/7, and reachable here despite `getPeriodicalFootage` 403ing. |
| `Gap` | spans with no recording |

`/footage` keeps only `CloudMedia`, so it means "continuous video" and nothing else. A non-24/7
camera returns `200` with no `CloudMedia` (not an error), which surfaces as `continuous: false`.

This route is undocumented and versioned (`X-API-VERSION: 1`); a Ring-side change can break it.

## API

| Route | Returns |
|---|---|
| `GET /healthz` | `{ok:true}`. Deliberately does **not** call Ring — readiness means "process up"; a credential problem must surface as an honest error in the app, not an unschedulable pod. |
| `GET /cameras` | `[{id, name, slug, battery, kind}]` — `slug` is ring-mqtt's slugging of the Ring device name, which is how Hawksnest matches a Ring camera to an HA entity. |
| `GET /timeline?device_id=&from=&to=` | `{camera, fromMs, toMs, truncated, events[]}`, oldest-first. Discrete recordings only — a quiet window is legitimately empty. |
| `GET /footage?device_id=&from=&to=` | `{camera, fromMs, toMs, continuous, truncated, segments[]}` — the 24/7 track. `continuous:false` (empty `segments`) for the battery cameras and the doorbell. `from` defaults to one hour back, not 24. |

An event: `{id, startMs, endMs, durationSec, kind, person, url, urlExpiresAtMs, thumbnailUrl}`.

A segment: `{startMs, endMs, url, urlExpiresAtMs, encrypted, chunked, dingId}`. `encrypted` marks an
end-to-end-encrypted span, whose key this service does not hold — don't hand it to a player.

Two behaviors worth knowing:

- **Signed URLs expire in ~15 minutes.** A timeline is perishable — clients must refetch rather
  than hold one open. `urlExpiresAtMs` per event says when.
- **Ring caps a `videoSearch` response at ~20 items** regardless of window width (a 7-day window
  returns the same 20 as a 24-hour one), so this pages backwards to fill the window. Beyond
  `MAX_PAGES` the response sets `truncated: true` rather than quietly looking complete — a busy
  camera really does exceed it (First Floor - Stairway runs ~156 events/day).

## Auth and ops

It holds **its own Ring refresh token — never ring-mqtt's.** Ring rotates refresh tokens on use, so
a shared token would knock the live camera integration offline. Same Ring account is fine; a
separate login is the point.

```
npx -y -p ring-client-api ring-auth-cli     # real terminal: email, password, 2FA
```

The token is ~1.2 KB on one line; terminals wrap it and a short copy is silently truncated (it
base64-decodes to invalid JSON and every request 502s). Seed it into
`kustomize/overlays/prod/secrets/ring-timeline.env`. After first boot the service writes each
rotation to `/data/refresh-token` on the `ring-timeline-data` PVC and the seed goes stale; losing
that PVC means minting a new token (2FA, so a human step).

Exposure is **ClusterIP only** — it hands out signed media URLs, so the only way in is the
Hawksnest nginx pod's `/ring-timeline/` location, itself tailnet-only. Same posture as go2rtc's API.

## Local run

```
TOKEN_PATH=/path/to/token PORT=8099 node server.mjs
curl "localhost:8099/cameras"
curl "localhost:8099/timeline?device_id=<id>"
```
