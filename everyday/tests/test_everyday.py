"""Tests for everyday: pure scoring, plus an end-to-end scan against a fake Frigate.

Run: python3 -m unittest discover -s everyday/tests
The end-to-end tests need ffmpeg on PATH and are skipped without it.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import picker  # noqa: E402

TZ = ZoneInfo("America/Indiana/Indianapolis")


def ts(hh: int, mm: int = 0, date: str = "2026-10-26") -> float:
    return datetime.strptime(f"{date} {hh}:{mm}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ).timestamp()


def ev(id, camera, label, start, dur, score=0.8, sub=None, desc="", fp=False):
    return {
        "id": id, "camera": camera, "label": label, "sub_label": sub,
        "start_time": start, "end_time": start + dur if dur is not None else None,
        "false_positive": fp, "data": {"top_score": score, "description": desc},
    }


class PickerTests(unittest.TestCase):
    def score(self, events, **kw):
        kw.setdefault("exclude_cameras", {"bedroom"})
        kw.setdefault("camera_weights", {"garage": 0.4})
        return picker.score_events(events, tz=TZ, **kw)

    def test_group_of_people_beats_lone_passerby(self):
        t = ts(15)
        events = [
            ev("lone", "kitchen", "person", ts(10), 20),
            ev("a", "big_room", "person", t, 40),
            ev("b", "big_room", "person", t + 5, 30),
            ev("c", "big_room", "person", t + 8, 25),
        ]
        out = self.score(events)
        self.assertIn(out[0].event_id, {"a", "b", "c"})
        self.assertEqual(out[-1].event_id, "lone")

    def test_exclusions_and_unfinished_and_false_positives(self):
        events = [
            ev("bed", "bedroom", "person", ts(12), 30),
            ev("live", "kitchen", "person", ts(12), None),
            ev("fp", "kitchen", "person", ts(12), 30, fp=True),
            ev("ok", "kitchen", "dog", ts(12), 30),
        ]
        self.assertEqual([c.event_id for c in self.score(events)], ["ok"])

    def test_zero_weight_camera_is_dropped(self):
        out = self.score([ev("g", "garage", "person", ts(12), 30)], camera_weights={"garage": 0})
        self.assertEqual(out, [])

    def test_brief_night_car_ranks_below_daytime_pet(self):
        out = self.score([
            ev("car", "front", "car", ts(2), 1),
            ev("dog", "backyard_patio", "dog", ts(14), 25),
        ])
        self.assertEqual(out[0].event_id, "dog")

    def test_recognised_face_bonus(self):
        out = self.score([
            ev("anon", "kitchen", "person", ts(12), 30),
            ev("known", "nursery", "person", ts(13), 30, sub="Elizabeth"),
        ])
        self.assertEqual(out[0].event_id, "known")

    def test_yesterdays_camera_is_discounted(self):
        out = self.score(
            [ev("k", "kitchen", "person", ts(12), 30), ev("b", "big_room", "person", ts(13), 30)],
            previous_camera="kitchen",
        )
        self.assertEqual(out[0].event_id, "b")

    def test_shortlist_varies_cameras_and_moments(self):
        cands = self.score([ev(f"k{i}", "kitchen", "person", ts(12) + i * 60, 30, score=0.9) for i in range(6)]
                           + [ev("b", "big_room", "person", ts(16), 30, score=0.5)])
        short = picker.shortlist(cands, 5)
        cams = [c.camera for c in short]
        self.assertLessEqual(cams.count("kitchen"), 2)
        self.assertIn("big_room", cams)
        kitchen = [c for c in short if c.camera == "kitchen"]
        if len(kitchen) == 2:
            self.assertGreaterEqual(abs(kitchen[0].start - kitchen[1].start), 600)

    def test_peak_time_bounds(self):
        c = self.score([ev("x", "kitchen", "person", 1000.0, 100)])[0]
        self.assertEqual(c.peak_time(), 1006.0)
        c = self.score([ev("y", "kitchen", "person", 1000.0, 2.5)])[0]
        self.assertEqual(c.peak_time(), 1001.0)

    def test_parse_ai_score(self):
        self.assertEqual(picker.parse_ai_score('{"score": 8}'), 8)
        self.assertEqual(picker.parse_ai_score("thinking about 3 kids... final: 7"), 7)
        self.assertEqual(picker.parse_ai_score('```json\n{"score":9.5}\n```'), 9.5)
        self.assertIsNone(picker.parse_ai_score("no idea"))
        self.assertIsNone(picker.parse_ai_score(""))
        self.assertIsNone(picker.parse_ai_score("score: 42"))

    def test_finalize_blends_and_tolerates_missing_ai(self):
        cands = self.score([
            ev("h", "kitchen", "person", ts(12), 30, score=0.95),
            ev("l", "big_room", "dog", ts(13), 30, score=0.6),
        ])
        by = {c.event_id: c for c in cands}
        by["h"].ai_score = 2
        by["l"].ai_score = 10
        self.assertEqual(picker.finalize(cands)[0].event_id, "l")
        for c in cands:
            c.ai_score = None
        self.assertEqual(picker.finalize(cands)[0].event_id, "h")

    def test_parse_weights_and_list(self):
        self.assertEqual(picker.parse_weights("garage=0.4, front=x,bad,kitchen=2"), {"garage": 0.4, "kitchen": 2.0})
        self.assertEqual(picker.parse_list(" bedroom, ,garage"), {"bedroom", "garage"})

    def test_parse_box(self):
        self.assertEqual(picker.parse_box([0.1, 0.2, 0.3, 0.4]), (0.1, 0.2, 0.3, 0.4))
        self.assertIsNone(picker.parse_box([100, 200, 50, 80]))  # pixels, not normalised
        self.assertIsNone(picker.parse_box(None))
        self.assertIsNone(picker.parse_box([0.1, 0.2]))

    def test_video_filter_modes(self):
        crop = picker.video_filter((0.7, 0.2, 0.1, 0.5), "crop", 1920, 1080)
        self.assertTrue(crop.startswith("crop="))
        self.assertIn("0.7500*iw", crop)          # centred on the box
        self.assertIn("scale=1920:1080", crop)
        self.assertIn("0.5000*iw", picker.video_filter(None, "crop", 1920, 1080))
        self.assertIn("pad=1920:1080", picker.video_filter(None, "pad", 1920, 1080))

    def test_choose_anchor_prefers_most_people_then_middle(self):
        w0, w1 = ts(17), ts(19, 30)
        events = [
            ev("solo", "big_room", "person", ts(17, 10), 60),
            ev("g1", "big_room", "person", ts(18, 30), 120),
            ev("g2", "big_room", "person", ts(18, 30) + 2, 100),
            ev("other", "kitchen", "person", ts(18, 30), 300),
            ev("dog", "big_room", "dog", ts(18, 0), 300),
            ev("late", "big_room", "person", ts(21), 60),
        ]
        a = picker.choose_anchor(events, "big_room", w0, w1)
        self.assertEqual(a.people, 2)
        self.assertTrue(ts(18, 30) <= a.time <= ts(18, 33))
        empty = picker.choose_anchor([], "big_room", w0, w1)
        self.assertEqual((empty.time, empty.people), ((w0 + w1) / 2, 0))

    def test_parse_window(self):
        self.assertEqual(picker.parse_window("17:00-19:30"), ((17, 0), (19, 30)))

    def test_edit_plan_groups_months_and_fades_on_camera_change(self):
        moments = [
            ("2026-10-02", "kitchen", "m2"), ("2026-10-01", "kitchen", "m1"),
            ("2026-10-03", "big_room", "m3"), ("2026-11-01", "kitchen", "n1"),
        ]
        anchors = [("2026-10-01", "big_room", "a1"), ("2026-10-02", "big_room", "a2")]
        plan = picker.edit_plan(moments, anchors, anchor_seconds=1.0, moment_seconds=2.5)
        self.assertEqual(len(plan), 2)
        oct_ = plan[0]
        self.assertEqual([s.path for s in oct_], ["a1", "a2", "m1", "m2", "m3"])
        self.assertEqual([s.fade_in for s in oct_], [False, False, True, False, True])
        self.assertEqual(oct_[0].seconds, 1.0)
        self.assertEqual(oct_[2].seconds, 2.5)
        self.assertEqual([(s.path, s.fade_in) for s in plan[1]], [("n1", False)])

    def test_day_bounds_is_local_midnight(self):
        t0, t1 = picker.day_bounds("2026-10-26", TZ)
        self.assertEqual(datetime.fromtimestamp(t0, TZ).hour, 0)
        self.assertEqual(t1 - t0, 86400)


# ── end to end ───────────────────────────────────────────────────────────────

@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        # A 10 s 32:9 test recording stands in for every Frigate clip request.
        cls.rec = cls.tmp / "rec.mp4"
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=1280x360:rate=15",
             "-f", "lavfi", "-i", "sine", "-t", "10", "-c:v", "libx264", "-c:a", "aac", "-shortest", str(cls.rec)],
            check=True,
        )
        date = "2026-10-26"
        cls.date = date
        cls.events = [
            ev("e1", "big_room", "person", ts(15, date=date), 40, desc="Two children building a fort"),
            ev("e2", "big_room", "person", ts(15, date=date) + 3, 30),
            ev("e3", "kitchen", "dog", ts(9, date=date), 20),
            ev("e4", "front", "car", ts(8, date=date), 12),
            ev("e5", "garage", "person", ts(18, date=date), 30),
            ev("e6", "bedroom", "person", ts(22, date=date), 300),
            ev("gone", "nursery", "person", ts(11, date=date), 30, score=1.0, sub="Cedric"),
            ev("din1", "big_room", "person", ts(18, 10, date=date), 90),
            ev("din2", "big_room", "person", ts(18, 10, date=date) + 1, 80),
        ]
        cls.events[-1]["data"]["box"] = [0.6, 0.2, 0.1, 0.5]
        cls.genai_calls = []
        rec_bytes = cls.rec.read_bytes()
        events = cls.events
        calls = cls.genai_calls

        class Fake(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype="application/json"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.startswith("/api/events?"):
                    return self._send(200, json.dumps(events).encode())
                if self.path.endswith("/thumbnail.jpg"):
                    return self._send(200, b"\xff\xd8fakejpeg", "image/jpeg")
                if self.path.startswith("/api/nursery/"):
                    return self._send(200, b"", "video/mp4")  # retention expired
                if "/clip.mp4" in self.path:
                    return self._send(200, rec_bytes, "video/mp4")
                self._send(404, b"{}")

            def do_POST(self):
                n = int(self.headers["Content-Length"])
                req = json.loads(self.rfile.read(n))
                calls.append(req)
                text = req["messages"][0]["content"][0]["text"]
                score = 9 if "fort" in text else (6 if "dog" in text else 2)
                self._send(200, json.dumps({"choices": [{"message": {"content": f"hmm... {{\"score\": {score}}}"}}]}).encode())

        cls.fake = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=cls.fake.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{cls.fake.server_address[1]}"

        os.environ.update({
            "DATA_DIR": str(cls.tmp / "data"), "FRIGATE_URL": base, "GENAI_BASE_URL": base + "/v1",
            "TZ": "America/Indiana/Indianapolis", "SCHEDULER": "0",
        })
        import importlib
        import server
        cls.server = importlib.reload(server)

        cls.http = ThreadingHTTPServer(("127.0.0.1", 0), cls.server.Handler)
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.http.server_address[1]}"
        cls.state = cls.server.scan_day(date, final=True)

    @classmethod
    def tearDownClass(cls):
        cls.fake.shutdown()
        cls.http.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def get(self, path, headers=None):
        req = urllib.request.Request(self.base + path, headers=headers or {})
        with urllib.request.urlopen(req) as r:
            return r.status, dict(r.headers), r.read()

    def post(self, path, obj):
        req = urllib.request.Request(self.base + path, data=json.dumps(obj).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_scan_picks_the_fort(self):
        self.assertEqual(self.state["pick"], "e1")
        self.assertEqual(self.state["status"], "final")
        ids = [c["eventId"] for c in self.state["candidates"]]
        self.assertNotIn("e6", ids)          # bedroom excluded
        self.assertNotIn("gone", ids)        # no footage -> skipped, not fatal
        self.assertLessEqual(len(ids), 5)

    def test_genai_got_image_and_never_called_for_excluded(self):
        self.assertTrue(self.genai_calls)
        content = self.genai_calls[0]["messages"][0]["content"]
        self.assertEqual(content[1]["type"], "image_url")
        texts = " ".join(c["messages"][0]["content"][0]["text"] for c in self.genai_calls)
        self.assertNotIn("bedroom", texts)

    def test_clip_is_normalised_silent_and_short(self):
        clip = self.server.DATA / "picks" / f"{self.date}.mp4"
        probe = json.loads(subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(clip)],
            capture_output=True, text=True, check=True).stdout)
        kinds = [s["codec_type"] for s in probe["streams"]]
        self.assertEqual(kinds, ["video"])
        v = probe["streams"][0]
        self.assertEqual((v["width"], v["height"]), (1920, 1080))
        self.assertAlmostEqual(float(probe["format"]["duration"]), 2.5, delta=0.15)

    def test_anchor_cut_from_the_window(self):
        a = self.state["anchor"]
        self.assertIsNotNone(a)
        self.assertEqual((a["camera"], a["people"]), ("big_room", 2))
        self.assertTrue((self.server.DATA / "anchors" / f"{self.date}.mp4").exists())

    def test_api_days_and_manual_pick_survives_rescan(self):
        code, _, body = self.get("/api/days")
        days = json.loads(body)["days"]
        self.assertEqual(days[0]["date"], self.date)
        self.assertEqual(days[0]["pick"]["eventId"], "e1")

        other = next(c["eventId"] for c in self.state["candidates"] if c["eventId"] != "e1")
        code, st = self.post(f"/api/days/{self.date}/pick", {"eventId": other})
        self.assertEqual((code, st["pick"], st["manual"]), (200, other, True))
        code, _ = self.post(f"/api/days/{self.date}/pick", {"eventId": "nope"})
        self.assertEqual(code, 400)

        st = self.server.scan_day(self.date, final=True)
        self.assertEqual(st["pick"], other)
        self.assertTrue(st["manual"])

    def test_reel_and_media_range(self):
        picks = self.server.DATA / "picks"
        shutil.copyfile(picks / f"{self.date}.mp4", picks / "2026-10-25.mp4")
        reel = self.server.build_reel(2026)
        dur = float(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(reel)],
            capture_output=True, text=True, check=True).stdout)
        self.assertAlmostEqual(dur, 5.0, delta=0.2)  # two stream-copied 2.5 s clips
        # Edited reel: anchor 1.0 + two moments 2.5 each, crossfaded 0.3 into each.
        edit = self.server.DATA / "reel" / "everyday-2026-edit.mp4"
        self.assertTrue(edit.exists())
        dur = float(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(edit)],
            capture_output=True, text=True, check=True).stdout)
        self.assertAlmostEqual(dur, 1.0 + 2.2 + 2.2, delta=0.25)
        code, _, body = self.get("/api/reel")
        reels = json.loads(body)["reels"]
        self.assertEqual([(r["year"], r["kind"]) for r in reels], [(2026, "edit"), (2026, "raw")])
        code, headers, chunk = self.get("/" + reels[0]["url"], {"Range": "bytes=0-99"})
        self.assertEqual(code, 206)
        self.assertEqual(len(chunk), 100)
        self.assertTrue(headers["Content-Range"].startswith("bytes 0-99/"))

    def test_fade_after_a_hard_cut_renders(self):
        # Regression: concat changes the stream timebase; an xfade after it failed with
        # "First input link main timebase ... do not match" until settb ran after fps.
        clip = str(self.server.DATA / "picks" / f"{self.date}.mp4")
        Shot = self.server.picker.Shot
        shots = [Shot("d", "a", clip, "moment", 1.0, False), Shot("d", "a", clip, "moment", 1.0, False),
                 Shot("d", "b", clip, "moment", 1.0, True), Shot("d", "b", clip, "moment", 1.0, False)]
        out = self.server.DATA / "fade-test.mp4"
        self.assertTrue(self.server._render_month(shots, out))
        dur = float(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out)],
            capture_output=True, text=True, check=True).stdout)
        self.assertAlmostEqual(dur, 4.0 - 0.3, delta=0.15)

    def test_media_rejects_traversal(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get("/media/..%2F..%2Fetc%2Fpasswd")
        self.assertEqual(cm.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
