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

## What it is not

Not 24/7 footage. Ring's continuous recording (CVR) needs a wired *Pro* camera; ours are Indoor
Cams (`stickup_cam_mini_v2`) and an `lpd_v2` doorbell, none of which support it, and
`getPeriodicalFootage` (the periodic-snapshot track) returns `403 AccessDeniedException` on this
account. What this serves is every discrete recording, correctly timed — which is what the Ring app
shows for these cameras too.

## API

| Route | Returns |
|---|---|
| `GET /healthz` | `{ok:true}`. Deliberately does **not** call Ring — readiness means "process up"; a credential problem must surface as an honest error in the app, not an unschedulable pod. |
| `GET /cameras` | `[{id, name, slug, battery, kind}]` — `slug` is ring-mqtt's slugging of the Ring device name, which is how Hawksnest matches a Ring camera to an HA entity. |
| `GET /timeline?device_id=&from=&to=` | `{camera, fromMs, toMs, truncated, events[]}`, oldest-first. |

An event: `{id, startMs, endMs, durationSec, kind, person, url, urlExpiresAtMs, thumbnailUrl}`.

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
