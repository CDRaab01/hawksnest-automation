/**
 * ring-timeline — the recorded-footage timeline Hawksnest can't get from Home Assistant.
 *
 * ring-mqtt exposes recorded events as a 40-slot `select` entity ("Motion 1"…) with no event
 * times, and its per-event URL lookup silently produces nothing for several of our cameras — the
 * wired ones show 19-20 real recordings in Ring's own API while HA can play none of them. Ring's
 * `video_search/history` endpoint (what the Ring app's scrub bar uses) has all of it: real event
 * times, durations, thumbnails, person-detection flags, and **pre-signed S3 mp4 URLs a player can
 * load directly**. That endpoint needs Ring account auth, which cannot live in a browser or on a
 * phone, so it lives here.
 *
 * Deliberately small: read-only, no writes to Ring, no media proxying (clients fetch the signed
 * URL straight from S3 — no cluster bandwidth, no second copy of the footage).
 *
 * Auth: its OWN refresh token, never ring-mqtt's — Ring rotates refresh tokens on use, so a shared
 * one would knock the live integration offline. Rotations are persisted to TOKEN_PATH; losing that
 * file means re-authenticating with 2FA (same trade-off as ring-mqtt's /data).
 *
 * Exposure: ClusterIP only, reached through the Hawksnest nginx (tailnet-only), same posture as
 * go2rtc. It hands out signed media URLs, so it must never be published to the LAN.
 */
import { createServer } from "node:http";
import { readFileSync, writeFileSync, existsSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { RingApi } from "ring-client-api";

const PORT = Number(process.env.PORT ?? 8080);
const TOKEN_PATH = process.env.TOKEN_PATH ?? "/data/refresh-token";
/** Ring returns at most ~20 items per videoSearch call; page back to fill a window. */
const PAGE_SIZE_HINT = 20;
const MAX_PAGES = 10;
/** Timelines are re-requested on every player open/scrub; don't hammer Ring for each. */
const CACHE_TTL_MS = 30_000;
const CAMERA_TTL_MS = 10 * 60_000;

// ── auth ─────────────────────────────────────────────────────────────────────

function loadToken() {
  if (existsSync(TOKEN_PATH)) {
    const stored = readFileSync(TOKEN_PATH, "utf8").trim();
    if (stored) return stored;
  }
  const seed = (process.env.RING_REFRESH_TOKEN ?? "").trim();
  if (!seed) {
    throw new Error(
      `No Ring refresh token: ${TOKEN_PATH} is empty and RING_REFRESH_TOKEN is unset. ` +
        `Mint one with 'npx -y -p ring-client-api ring-auth-cli' — it must NOT be ring-mqtt's.`,
    );
  }
  return seed;
}

function persistToken(token) {
  try {
    mkdirSync(dirname(TOKEN_PATH), { recursive: true });
    writeFileSync(TOKEN_PATH, token.trim() + "\n", "utf8");
  } catch (e) {
    // Non-fatal: the process keeps working on the in-memory token; only a restart would break.
    console.error(`[ring-timeline] could not persist rotated token: ${e.message}`);
  }
}

const api = new RingApi({
  refreshToken: loadToken(),
  controlCenterDisplayName: "hawksnest-timeline",
});
api.onRefreshTokenUpdated.subscribe(({ newRefreshToken }) => {
  if (newRefreshToken) persistToken(newRefreshToken);
});

// ── camera list ──────────────────────────────────────────────────────────────

/** ring-mqtt's slugging, so a client can match Ring devices to HA entities by name. */
export function slugify(name) {
  return String(name)
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
}

let cameraCache = { at: 0, cameras: [] };

async function getCameras() {
  if (Date.now() - cameraCache.at < CAMERA_TTL_MS && cameraCache.cameras.length) {
    return cameraCache.cameras;
  }
  const cameras = await api.getCameras();
  cameraCache = { at: Date.now(), cameras };
  return cameras;
}

// ── timeline ─────────────────────────────────────────────────────────────────

/** Signed-URL lifetime, so a client can refresh a stale timeline instead of failing playback. */
function urlExpiresAtMs(url) {
  try {
    const p = new URL(url).searchParams;
    const date = p.get("X-Amz-Date"); // 20260727T005508Z
    const expires = Number(p.get("X-Amz-Expires"));
    if (!date || !Number.isFinite(expires)) return null;
    const iso = date.replace(
      /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$/,
      "$1-$2-$3T$4:$5:$6Z",
    );
    const started = Date.parse(iso);
    return Number.isFinite(started) ? started + expires * 1000 : null;
  } catch {
    return null;
  }
}

/**
 * One Ring recording, in the shape Hawksnest's timeline already speaks.
 * `state` is Ring's own ("timed_out" is its normal terminal state for a motion recording that ran
 * to the max length — it does NOT mean the recording is broken; it's on healthy events too).
 */
function normalize(item) {
  const startMs = Number(item.created_at);
  const durationSec = Number(item.duration);
  const url = item.hq_url ?? item.untranscoded_url ?? item.lq_url ?? null;
  return {
    id: String(item.ding_id),
    startMs,
    endMs: Number.isFinite(durationSec) ? startMs + durationSec * 1000 : null,
    durationSec: Number.isFinite(durationSec) ? durationSec : null,
    kind: item.kind ?? "motion",
    person: Boolean(item.cv_properties?.person_detected),
    url,
    urlExpiresAtMs: url ? urlExpiresAtMs(url) : null,
    thumbnailUrl: item.thumbnail_url ?? null,
  };
}

/**
 * Every recording for one camera in `[fromMs, toMs]`, newest-first.
 *
 * Ring caps a videoSearch response at ~20 items regardless of how wide the window is (verified: a
 * 7-day window returns the same 20 as a 24-hour one), so walk backwards a page at a time until a
 * short page, the window start, or MAX_PAGES.
 */
async function fetchTimeline(camera, fromMs, toMs) {
  const events = [];
  let cursor = toMs;
  let truncated = false;
  for (let page = 0; ; page++) {
    if (page >= MAX_PAGES) {
      // Never let a capped result read as "that's all there was" — a busy camera can exceed this
      // (First Floor - Stairway alone runs ~156 events/day).
      truncated = true;
      console.warn(`[ring-timeline] ${camera.name}: stopped at ${MAX_PAGES} pages, window not fully covered`);
      break;
    }
    const res = await camera.videoSearch({ dateFrom: fromMs, dateTo: cursor });
    const items = res?.video_search ?? [];
    if (!items.length) break;
    events.push(...items.map(normalize));
    if (items.length < PAGE_SIZE_HINT) break;
    const oldest = Math.min(...items.map((i) => Number(i.created_at)).filter(Number.isFinite));
    if (!Number.isFinite(oldest) || oldest <= fromMs) break;
    cursor = oldest - 1;
  }
  // Ring can repeat an event across page boundaries; de-dupe and sort oldest-first for the timeline.
  const byId = new Map(events.map((e) => [e.id, e]));
  const list = [...byId.values()]
    .filter((e) => Number.isFinite(e.startMs) && e.startMs >= fromMs && e.startMs <= toMs)
    .sort((a, b) => a.startMs - b.startMs);
  return { events: list, truncated };
}

const timelineCache = new Map(); // key -> { at, payload }

// ── 24/7 continuous footage (CVR) ────────────────────────────────────────────

/**
 * `video_search` only ever returns discrete *events*, which is why a quiet 3-5 AM window comes
 * back empty even on the seven cameras that record continuously. The Ring app's scrub bar does not
 * use it — it calls the Event Video Manager timeline, captured off the live web client:
 *
 *   GET account.ring.com/api/cgw/evm/v2/timeline/24/devices/{id}?start_time&end_time&order&visualizations
 *
 * That host is the web BFF and authenticates with a session cookie + `csrf-token`, which a server
 * cannot hold. The same path on Ring's bearer-token API host answers instead — `api.ring.com/cgw/…`
 * 404s while `api.ring.com/evm/v2/…` 401s, i.e. the route exists there and only wants auth — so it
 * goes through restClient, which attaches the same bearer the rest of this service uses.
 *
 * `getPeriodicalFootage` (the documented 24/7 call) 403s on this account; this one does not.
 */
const CVR_BASE = "https://api.ring.com/evm/v2/timeline/24/devices";

/**
 * Ring stitches the window server-side: a request for an arbitrary span returns ONE `CloudMedia`
 * item covering all of it (verified against a 12-hour window — a single chunked H264/opus mp4),
 * not a pile of chunks to concatenate. So a client can seek anywhere in the span with one URL.
 *
 * The response mixes four schemas and only `CloudMedia` is continuous video:
 *   CloudMedia — cloud-stored video. Only the ones flagged `recording_24x7_enabled` are the
 *                stitched 24/7 track; the same schema also carries ordinary event clips.
 *   Event      — motion/person markers, duplicating what /timeline already serves
 *   Footage    — `ONLINE_PERIODICAL`: an hourly 10-second timelapse built from ~20 periodic
 *                snapshots. It is what the battery cameras and the doorbell have INSTEAD of 24/7,
 *                and notably it is reachable here even though getPeriodicalFootage 403s. Not
 *                continuous video, so it is not served as such — a separate track if ever wanted.
 *   Gap        — spans with no recording at all
 * Only CloudMedia is kept, so /footage means "continuous video" and nothing else.
 */
function normalizeFootage(item) {
  const startMs = Date.parse(item.start_time);
  const endMs = Date.parse(item.end_time);
  const url = item.url ?? null;
  const meta = item.custom_metadata ?? {};
  return {
    startMs: Number.isFinite(startMs) ? startMs : null,
    endMs: Number.isFinite(endMs) ? endMs : null,
    url,
    urlExpiresAtMs: url ? urlExpiresAtMs(url) : null,
    // Ring can mark a segment end-to-end encrypted; those need a key this service does not hold,
    // so say so rather than handing the player a URL it will fail to decode.
    encrypted: Boolean(item.is_e2ee),
    chunked: Boolean(meta.is_chunked),
    dingId: meta.ding_id ? String(meta.ding_id) : null,
  };
}

/** Continuous footage for one camera in `[fromMs, toMs]`, or [] if it isn't a 24/7 camera. */
async function fetchFootage(deviceId, fromMs, toMs) {
  const qs = new URLSearchParams({
    start_time: new Date(fromMs).toISOString(),
    end_time: new Date(toMs).toISOString(),
    order: "ASC",
    // `footage` is the layer that carries the continuous track; `cloud,local` match the web client.
    visualizations: "cloud,local,footage",
  });
  const body = await api.restClient.request({
    url: `${CVR_BASE}/${deviceId}?${qs}`,
    method: "GET",
    responseType: "json",
    // The web client pins this; the endpoint is versioned and older shapes differ.
    headers: { Accept: "application/json", "X-API-VERSION": "1" },
  });
  const slots = Array.isArray(body?.timeline) ? body.timeline : [];
  const segments = slots
    .flatMap((slot) => slot?.items ?? [])
    // `CloudMedia` alone is NOT the 24/7 track — it means "cloud-stored video", which includes
    // ordinary event clips. The doorbell returns 27-second CloudMedia items for its motion
    // recordings, which would otherwise read as continuous footage and duplicate /timeline.
    // `recording_24x7_enabled` is the flag that actually separates them: true on the stitched
    // window (which comes back exactly as long as the request), false on an event clip.
    .filter((i) => i?.schema === "CloudMedia" && i.url && i.custom_metadata?.recording_24x7_enabled === true)
    .map(normalizeFootage)
    .filter((s) => Number.isFinite(s.startMs))
    .sort((a, b) => a.startMs - b.startMs);
  // A camera without 24/7 returns a slot with no CloudMedia rather than an error — an empty list
  // is the honest answer for it, not a failure.
  return { segments, truncated: Boolean(body?.pagination_key) };
}

const footageCache = new Map(); // key -> { at, payload }

// ── http ─────────────────────────────────────────────────────────────────────

function sendJson(res, status, body) {
  const json = JSON.stringify(body);
  res.writeHead(status, {
    "content-type": "application/json",
    "cache-control": "no-store",
    "content-length": Buffer.byteLength(json),
  });
  res.end(json);
}

const server = createServer(async (req, res) => {
  const url = new URL(req.url, "http://localhost");
  try {
    if (url.pathname === "/healthz") {
      return sendJson(res, 200, { ok: true });
    }

    if (url.pathname === "/cameras") {
      const cameras = await getCameras();
      return sendJson(
        res,
        200,
        cameras.map((c) => ({
          id: c.id,
          name: c.name,
          slug: slugify(c.name),
          battery: c.batteryLevel ?? null,
          kind: c.data?.kind ?? null,
        })),
      );
    }

    if (url.pathname === "/timeline") {
      const deviceId = Number(url.searchParams.get("device_id"));
      const toMs = Number(url.searchParams.get("to") ?? Date.now());
      const fromMs = Number(url.searchParams.get("from") ?? toMs - 24 * 3600_000);
      if (!Number.isFinite(deviceId) || !Number.isFinite(fromMs) || !Number.isFinite(toMs)) {
        return sendJson(res, 400, { error: "device_id, from and to must be numbers" });
      }

      // Round the cache key so a scrubbing client (whose `to` moves every frame) still hits it.
      const key = `${deviceId}:${Math.floor(fromMs / CACHE_TTL_MS)}:${Math.floor(toMs / CACHE_TTL_MS)}`;
      const hit = timelineCache.get(key);
      if (hit && Date.now() - hit.at < CACHE_TTL_MS) return sendJson(res, 200, hit.payload);

      const camera = (await getCameras()).find((c) => c.id === deviceId);
      if (!camera) return sendJson(res, 404, { error: `no Ring camera ${deviceId}` });

      const { events, truncated } = await fetchTimeline(camera, fromMs, toMs);
      const payload = {
        camera: { id: camera.id, name: camera.name, slug: slugify(camera.name) },
        fromMs,
        toMs,
        // Ring's signed URLs are good for ~15 minutes. Clients must refetch rather than hold a
        // timeline open; `urlExpiresAtMs` per event says when.
        truncated,
        events,
      };
      timelineCache.set(key, { at: Date.now(), payload });
      if (timelineCache.size > 200) timelineCache.clear();
      return sendJson(res, 200, payload);
    }

    if (url.pathname === "/footage") {
      const deviceId = Number(url.searchParams.get("device_id"));
      const toMs = Number(url.searchParams.get("to") ?? Date.now());
      const fromMs = Number(url.searchParams.get("from") ?? toMs - 3600_000);
      if (!Number.isFinite(deviceId) || !Number.isFinite(fromMs) || !Number.isFinite(toMs)) {
        return sendJson(res, 400, { error: "device_id, from and to must be numbers" });
      }
      if (toMs <= fromMs) {
        return sendJson(res, 400, { error: "to must be after from" });
      }

      const key = `${deviceId}:${Math.floor(fromMs / CACHE_TTL_MS)}:${Math.floor(toMs / CACHE_TTL_MS)}`;
      const hit = footageCache.get(key);
      if (hit && Date.now() - hit.at < CACHE_TTL_MS) return sendJson(res, 200, hit.payload);

      // Unlike /timeline this does not need the camera object, but resolving it keeps the 404 for
      // an unknown device consistent between the two endpoints.
      const camera = (await getCameras()).find((c) => c.id === deviceId);
      if (!camera) return sendJson(res, 404, { error: `no Ring camera ${deviceId}` });

      const { segments, truncated } = await fetchFootage(deviceId, fromMs, toMs);
      const payload = {
        camera: { id: camera.id, name: camera.name, slug: slugify(camera.name) },
        fromMs,
        toMs,
        // False for the battery cameras and the doorbell: they record events only, so a client
        // should keep showing the event timeline and no continuous track.
        continuous: segments.length > 0,
        truncated,
        segments,
      };
      footageCache.set(key, { at: Date.now(), payload });
      if (footageCache.size > 200) footageCache.clear();
      return sendJson(res, 200, payload);
    }

    return sendJson(res, 404, { error: "not found" });
  } catch (e) {
    // Ring auth/network failures are upstream problems, not client errors — say so plainly so the
    // app can show an honest "timeline unavailable" instead of an empty (falsely reassuring) list.
    console.error(`[ring-timeline] ${url.pathname} failed:`, e?.message ?? e);
    return sendJson(res, 502, { error: String(e?.message ?? e) });
  }
});

server.listen(PORT, () => console.log(`[ring-timeline] listening on :${PORT}`));
