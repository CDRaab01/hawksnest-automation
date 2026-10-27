"""
everyday — picks ~1.5 s of each day from Frigate for a "one second every day" family video.

Nightly it reads the day's Frigate events, scores them (picker.py), asks the local model
(LM Studio, the same endpoint Frigate's GenAI uses) to rate a shortlist, cuts the top
candidates out of Frigate's continuous recordings with ffmpeg, auto-picks the best, and
appends it to a per-year reel. Hawksnest shows the candidates and can swap the pick.

Runs twice per day: a PROVISIONAL scan of today in the evening (so there is something to
review before bed) and a FINAL scan of yesterday just after midnight (so the evening is
included). A manual pick is never overwritten by a rescan.

Timing matters: Frigate's continuous retention is 1-3 days per camera, so a day must be
scanned within ~24 h or its footage is gone. Startup backfills any unscanned recent day.

Storage (/data, the everyday-media PVC on the NAS):
  days/YYYY-MM-DD.json             state: candidates + pick
  candidates/YYYY-MM-DD/<id>.mp4   normalised 1.5 s clip per candidate (+ .jpg thumb)
  picks/YYYY-MM-DD.mp4             the chosen clip (kept forever)
  reel/everyday-YYYY.mp4           every pick of the year, concatenated in date order

Exposure: ClusterIP only, reached through the Hawksnest nginx `/everyday/` (tailnet only),
same posture as ring-timeline. It serves interior-camera footage, so never a NodePort.

Stdlib + ffmpeg only, on purpose: nothing to pin, nothing for dependabot to churn.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import picker

# ── config ───────────────────────────────────────────────────────────────────

ENV = os.environ
PORT = int(ENV.get("PORT", "8080"))
DATA = Path(ENV.get("DATA_DIR", "/data"))
FRIGATE = ENV.get("FRIGATE_URL", "http://frigate:5000").rstrip("/")
TZ = ZoneInfo(ENV.get("TZ", "America/Indiana/Indianapolis"))
CLIP_SECONDS = float(ENV.get("CLIP_SECONDS", "1.5"))
CANDIDATES = int(ENV.get("CANDIDATES", "5"))
AI_SHORTLIST = int(ENV.get("AI_SHORTLIST", "12"))
# The bedroom is excluded by default — nobody wants that camera choosing the day.
EXCLUDE = picker.parse_list(ENV.get("EXCLUDE_CAMERAS", "bedroom"))
WEIGHTS = picker.parse_weights(ENV.get("CAMERA_WEIGHTS", "garage=0.4"))
PROVISIONAL_AT = ENV.get("PROVISIONAL_AT", "21:00")
FINAL_AT = ENV.get("FINAL_AT", "00:20")
BACKFILL_DAYS = int(ENV.get("BACKFILL_DAYS", "2"))
KEEP_CANDIDATE_DAYS = int(ENV.get("KEEP_CANDIDATE_DAYS", "14"))
OUT_W, OUT_H = (int(x) for x in ENV.get("OUTPUT_SIZE", "1920x1080").split("x"))

# GenAI: same LM Studio endpoint Frigate uses (OPENAI_BASE_URL from frigate-credentials).
# FAIL CLOSED: no URL -> heuristics only. Never fall back to a public endpoint — these
# are interior-camera frames of children.
GENAI_URL = (ENV.get("GENAI_BASE_URL") or "").rstrip("/")
GENAI_MODEL = ENV.get("GENAI_MODEL", "google/gemma-4-e4b")
if "openai.com" in GENAI_URL or "anthropic.com" in GENAI_URL:
    print("[everyday] refusing public GENAI_BASE_URL; GenAI disabled", flush=True)
    GENAI_URL = ""

JOB_LOCK = threading.Lock()  # one scan / reel build at a time
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def log(msg: str) -> None:
    print(f"[everyday] {datetime.now(TZ):%Y-%m-%d %H:%M:%S} {msg}", flush=True)


# ── state ────────────────────────────────────────────────────────────────────

def day_path(date: str) -> Path:
    return DATA / "days" / f"{date}.json"


def load_day(date: str) -> dict | None:
    p = day_path(date)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def save_day(date: str, state: dict) -> None:
    p = day_path(date)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(p)  # atomic on the same filesystem, so a reader never sees half a file


def media_url(rel: Path | str) -> str:
    return "media/" + str(rel).replace(os.sep, "/")


# ── Frigate ──────────────────────────────────────────────────────────────────

def http_get(url: str, timeout: float = 30) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def frigate_events(t0: float, t1: float) -> list[dict]:
    """All finished events in [t0, t1). Paged backwards by `before`, since /api/events
    caps `limit` and returns newest first."""
    out: list[dict] = []
    before = t1
    for _ in range(20):
        q = urllib.parse.urlencode(
            {"after": t0, "before": before, "limit": 500, "include_thumbnails": 0}
        )
        page = json.loads(http_get(f"{FRIGATE}/api/events?{q}"))
        if not page:
            break
        out.extend(page)
        oldest = min(e["start_time"] for e in page)
        if len(page) < 500 or oldest <= t0:
            break
        before = oldest
    seen: set[str] = set()
    uniq = []
    for e in out:
        if e["id"] not in seen:
            seen.add(e["id"])
            uniq.append(e)
    return uniq


def cut_clip(c: picker.Candidate, dest: Path) -> bool:
    """Pull a window around the event's peak from Frigate's continuous recordings and
    normalise it: CLIP_SECONDS, no audio, OUTPUT_SIZE letterboxed (panoramas and 4:3
    cameras keep their framing), 30 fps h264 — identical params on every clip so the
    yearly reel can be concatenated with -c copy."""
    peak = c.peak_time()
    a, b = int(peak - CLIP_SECONDS), int(peak + CLIP_SECONDS) + 2
    url = f"{FRIGATE}/api/{urllib.parse.quote(c.camera)}/start/{a}/end/{b}/clip.mp4"
    with tempfile.TemporaryDirectory() as td:
        raw = Path(td) / "raw.mp4"
        try:
            raw.write_bytes(http_get(url, timeout=120))
        except (urllib.error.URLError, TimeoutError) as e:
            log(f"clip fetch failed {c.event_id} ({c.camera}): {e}")
            return False
        if raw.stat().st_size < 1024:
            log(f"no recording for {c.event_id} ({c.camera}) — retention or sleeping camera")
            return False
        offset = max(0.0, peak - a - CLIP_SECONDS / 2)
        vf = (
            f"scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=decrease,"
            f"pad={OUT_W}:{OUT_H}:(ow-iw)/2:(oh-ih)/2,fps=30,format=yuv420p,setsar=1"
        )
        tmp_out = Path(td) / "out.mp4"
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{offset:.2f}", "-i", str(raw), "-t", f"{CLIP_SECONDS:.2f}",
            "-an", "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-profile:v", "high", "-video_track_timescale", "30000",
            "-movflags", "+faststart", str(tmp_out),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0 or not tmp_out.exists() or tmp_out.stat().st_size < 1024:
            log(f"ffmpeg failed {c.event_id}: {r.stderr.strip()[:300]}")
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp_out), dest)
        # Poster frame for the review grid.
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", "0.5",
             "-i", str(dest), "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "4",
             str(dest.with_suffix(".jpg"))],
            capture_output=True,
        )
    return True


# ── GenAI ────────────────────────────────────────────────────────────────────

def ai_rate(c: picker.Candidate) -> float | None:
    if not GENAI_URL:
        return None
    content: list[dict] = [{
        "type": "text",
        "text": (
            f"{picker.AI_PROMPT}\n\nCamera: {c.camera}. Detected: {c.label}"
            f"{' (' + c.sub_label + ')' if c.sub_label else ''}. "
            f"Time: {datetime.fromtimestamp(c.start, TZ):%A %I:%M %p}. "
            f"Duration: {c.duration:.0f}s. "
            f"Description: {c.description or 'none'}"
        ),
    }]
    try:
        thumb = http_get(f"{FRIGATE}/api/events/{c.event_id}/thumbnail.jpg", timeout=15)
        content.append({
            "type": "image_url",
            "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(thumb).decode()},
        })
    except (urllib.error.URLError, TimeoutError):
        pass
    body = json.dumps({
        "model": GENAI_MODEL,
        "messages": [{"role": "user", "content": content}],
        "temperature": 0.2,
    }).encode()
    req = urllib.request.Request(
        f"{GENAI_URL}/chat/completions", data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer local"},
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            reply = json.loads(r.read())
        text = reply["choices"][0]["message"].get("content") or ""
        return picker.parse_ai_score(text)
    except Exception as e:  # model down/unloaded: heuristics carry the day
        log(f"genai rating failed {c.event_id}: {e}")
        return None


# ── the daily job ────────────────────────────────────────────────────────────

def previous_pick_camera(date: str) -> str | None:
    prev = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    st = load_day(prev) or {}
    pid = st.get("pick")
    for c in st.get("candidates", []):
        if c["eventId"] == pid:
            return c["camera"]
    return None


def scan_day(date: str, *, final: bool) -> dict:
    with JOB_LOCK:
        t0, t1 = picker.day_bounds(date, TZ)
        log(f"scan {date} ({'final' if final else 'provisional'})")
        events = frigate_events(t0, min(t1, time.time()))
        cands = picker.score_events(
            events, tz=TZ, exclude_cameras=EXCLUDE, camera_weights=WEIGHTS,
            previous_camera=previous_pick_camera(date),
        )
        short = picker.shortlist(cands, AI_SHORTLIST)
        for c in short:
            c.ai_score = ai_rate(c)
        ranked = picker.finalize(short)

        prev = load_day(date) or {}
        out_dir = DATA / "candidates" / date
        kept: list[dict] = []
        for c in ranked:
            if len(kept) >= CANDIDATES:
                break
            dest = out_dir / f"{c.event_id}.mp4"
            if not dest.exists() and not cut_clip(c, dest):
                continue  # no footage (retention / asleep): try the next one
            j = c.to_json()
            j["clip"] = media_url(dest.relative_to(DATA))
            j["thumb"] = media_url(dest.with_suffix(".jpg").relative_to(DATA))
            kept.append(j)

        # A manual pick survives a rescan, even if it has fallen out of the new top N.
        manual = bool(prev.get("manual"))
        pick = prev.get("pick") if manual else (kept[0]["eventId"] if kept else None)
        if manual and pick and not any(k["eventId"] == pick for k in kept):
            old = next((c for c in prev.get("candidates", []) if c["eventId"] == pick), None)
            if old:
                kept.append(old)

        state = {
            "date": date,
            "status": "final" if final else "provisional",
            "scannedAt": time.time(),
            "eventCount": len(events),
            "genai": bool(GENAI_URL),
            "candidates": kept,
            "pick": pick,
            "manual": manual,
        }
        save_day(date, state)
        if pick:
            _install_pick(date, pick)
        log(f"scan {date}: {len(events)} events -> {len(kept)} candidates, pick={pick}")
    if final:
        build_reel(int(date[:4]))
    return state


def _install_pick(date: str, event_id: str) -> None:
    src = DATA / "candidates" / date / f"{event_id}.mp4"
    if src.exists():
        dst = DATA / "picks" / f"{date}.mp4"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        if src.with_suffix(".jpg").exists():
            shutil.copyfile(src.with_suffix(".jpg"), dst.with_suffix(".jpg"))


def set_pick(date: str, event_id: str) -> dict:
    with JOB_LOCK:
        st = load_day(date)
        if not st or not any(c["eventId"] == event_id for c in st.get("candidates", [])):
            raise KeyError(event_id)
        st["pick"], st["manual"] = event_id, True
        save_day(date, st)
        _install_pick(date, event_id)
    threading.Thread(target=build_reel, args=(int(date[:4]),), daemon=True).start()
    return st


def build_reel(year: int) -> Path | None:
    """Concatenate every pick of `year` in date order. Clips share encoding params
    (cut_clip), so this is a stream copy: seconds, not a re-encode."""
    with JOB_LOCK:
        picks = sorted((DATA / "picks").glob(f"{year}-*.mp4"))
        if not picks:
            return None
        out = DATA / "reel" / f"everyday-{year}.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            for p in picks:
                f.write(f"file '{p.as_posix()}'\n")
            listfile = f.name
        tmp = out.with_suffix(".tmp.mp4")
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat",
             "-safe", "0", "-i", listfile, "-c", "copy", "-movflags", "+faststart", str(tmp)],
            capture_output=True, text=True,
        )
        os.unlink(listfile)
        if r.returncode != 0:
            log(f"reel {year} failed: {r.stderr.strip()[:300]}")
            return None
        tmp.replace(out)
        log(f"reel {year}: {len(picks)} days")
        return out


def prune() -> None:
    """Drop candidate clips older than KEEP_CANDIDATE_DAYS. Picks and day JSON are kept."""
    cutoff = (datetime.now(TZ) - timedelta(days=KEEP_CANDIDATE_DAYS)).strftime("%Y-%m-%d")
    for d in (DATA / "candidates").glob("*"):
        if d.is_dir() and d.name < cutoff:
            shutil.rmtree(d, ignore_errors=True)


# ── scheduler ────────────────────────────────────────────────────────────────

def _hm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def scheduler() -> None:
    """Checks once a minute. Marker files make each run idempotent across restarts."""
    markers = DATA / "runs"
    markers.mkdir(parents=True, exist_ok=True)

    def once(key: str, fn) -> None:
        m = markers / key
        if m.exists():
            return
        try:
            fn()
            m.touch()
        except Exception:
            log(f"job {key} failed:\n{traceback.format_exc()}")

    # Backfill: footage expires in 1-3 days, so catch up on anything missed while down.
    today = datetime.now(TZ).date()
    for i in range(BACKFILL_DAYS, 0, -1):
        d = (today - timedelta(days=i)).isoformat()
        st = load_day(d)
        if not st or st.get("status") != "final":
            once(f"final-{d}", lambda d=d: scan_day(d, final=True))

    while True:
        now = datetime.now(TZ)
        today = now.date().isoformat()
        yesterday = (now.date() - timedelta(days=1)).isoformat()
        if (now.hour, now.minute) >= _hm(FINAL_AT):
            once(f"final-{yesterday}", lambda: (scan_day(yesterday, final=True), prune()))
        if (now.hour, now.minute) >= _hm(PROVISIONAL_AT):
            once(f"prov-{today}", lambda: scan_day(today, final=False))
        time.sleep(60)


# ── HTTP ─────────────────────────────────────────────────────────────────────

def day_summary(st: dict) -> dict:
    pick = next((c for c in st.get("candidates", []) if c["eventId"] == st.get("pick")), None)
    return {
        "date": st["date"],
        "status": st.get("status"),
        "manual": st.get("manual", False),
        "candidateCount": len(st.get("candidates", [])),
        "pick": pick,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "everyday/1"

    def log_message(self, fmt, *args):  # quiet: the review screen polls
        pass

    def _json(self, code: int, obj) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(min(n, 65536)))
        except json.JSONDecodeError:
            return {}

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if path == "/healthz":
            return self._json(200, {"ok": True})
        if path == "/api/days":
            limit = int((q.get("limit") or ["30"])[0])
            files = sorted((DATA / "days").glob("*.json"), reverse=True)[:limit]
            days = [day_summary(st) for f in files if (st := load_day(f.stem))]
            return self._json(200, {"days": days, "genai": bool(GENAI_URL)})
        m = re.fullmatch(r"/api/days/(\d{4}-\d{2}-\d{2})", path)
        if m:
            st = load_day(m.group(1))
            return self._json(200, st) if st else self._json(404, {"error": "not scanned"})
        if path == "/api/reel":
            reels = sorted((DATA / "reel").glob("everyday-*.mp4"))
            out = []
            for r in reels:
                year = r.stem.split("-")[1]
                out.append({
                    "year": int(year),
                    "days": len(list((DATA / "picks").glob(f"{year}-*.mp4"))),
                    "url": media_url(r.relative_to(DATA)),
                    "updatedAt": r.stat().st_mtime,
                })
            return self._json(200, {"reels": out})
        if path.startswith("/media/"):
            return self._media(path[len("/media/"):])
        self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        m = re.fullmatch(r"/api/days/(\d{4}-\d{2}-\d{2})/(pick|scan)", path)
        if not m:
            return self._json(404, {"error": "not found"})
        date, action = m.groups()
        if action == "pick":
            eid = str(self._body().get("eventId") or "")
            try:
                return self._json(200, set_pick(date, eid))
            except KeyError:
                return self._json(400, {"error": "eventId is not a candidate for that day"})
        # scan: async, the model can take minutes
        final = date < datetime.now(TZ).date().isoformat()
        threading.Thread(target=scan_day, args=(date,), kwargs={"final": final}, daemon=True).start()
        self._json(202, {"queued": date})

    def _media(self, rel: str) -> None:
        """Static files under DATA with Range support — Safari won't play mp4 without it."""
        try:
            p = (DATA / urllib.parse.unquote(rel)).resolve()
            p.relative_to(DATA.resolve())  # no ../ escapes
        except (ValueError, OSError):
            return self._json(404, {"error": "not found"})
        if not p.is_file() or p.suffix not in (".mp4", ".jpg"):
            return self._json(404, {"error": "not found"})
        size = p.stat().st_size
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        code = 200
        if rng and (mm := re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip())):
            s, e = mm.groups()
            if s:
                start, end = int(s), int(e) if e else size - 1
            elif e:
                start = max(0, size - int(e))
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            code = 206
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "private, max-age=300")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with p.open("rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(1 << 16, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                left -= len(chunk)


def main() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    log(f"frigate={FRIGATE} genai={'on' if GENAI_URL else 'off'} exclude={sorted(EXCLUDE)}")
    if ENV.get("SCHEDULER", "1") != "0":
        threading.Thread(target=scheduler, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
