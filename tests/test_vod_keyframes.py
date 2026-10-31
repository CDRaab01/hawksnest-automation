#!/usr/bin/env python3
"""Tests for the Frigate VOD keyframe backport (kustomize/base/frigate/vod-keyframes/).

The patch runs only at Frigate pod start and changes what nginx-vod is told about every recording,
so a mistake would surface as broken playback across every camera. These pin:

  * the moov parser agrees with ffprobe on the RAW sample timeline (edit lists ignored — Frigate's
    nginx runs `vod_ignore_edit_list on`), with and without B-frames, on files cut by the same
    ffmpeg segment muxer args Frigate uses;
  * the clip-relative math matches upstream `_clip_key_frames` (blakeblackshear/frigate#24011);
  * every failure path returns exactly the stock 0.17.2 declaration;
  * the startup patcher edits only when both anchors match, is idempotent, and never raises.

Needs ffmpeg/ffprobe on PATH (CI installs them). Run: python3 -m unittest tests/test_vod_keyframes.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "kustomize" / "base" / "frigate" / "vod-keyframes"))

import hawksnest_vod_keyframes as kf  # noqa: E402
import patch_media  # noqa: E402

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _segments(tmp: Path, bframes: int) -> list[Path]:
    """130 s of 10 fps video (keyframe every 4 s, like the Reolinks) + AAC, cut into 60 s files
    with Frigate's own record output args."""
    src = tmp / f"src{bframes}.mp4"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=10",
         "-f", "lavfi", "-i", "sine=f=440:sample_rate=16000",
         "-t", "130", "-c:v", "libx264", "-g", "40", "-bf", str(bframes), "-c:a", "aac", str(src)],
        check=True,
    )
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-i", str(src),
         "-f", "segment", "-segment_time", "60", "-segment_format", "mp4",
         "-reset_timestamps", "1", "-strftime", "0", "-c", "copy", str(tmp / f"b{bframes}_%03d.mp4")],
        check=True,
    )
    return sorted(tmp.glob(f"b{bframes}_*.mp4"))


def _ffprobe_raw_keyframes(path: Path) -> list[int]:
    out = subprocess.run(
        ["ffprobe", "-loglevel", "error", "-ignore_editlist", "1", "-select_streams", "v:0",
         "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    return sorted(
        round(float(ts) * 1000)
        for ts, flags in (line.split(",")[:2] for line in out.splitlines() if line)
        if "K" in flags
    )


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe not installed")
class ParserMatchesFfprobe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.files = _segments(cls.tmp, 0) + _segments(cls.tmp, 2)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_keyframes_match_ffprobe_on_the_raw_timeline(self):
        for f in self.files:
            with self.subTest(file=f.name):
                mine = kf.video_keyframes_ms(str(f))
                ref = _ffprobe_raw_keyframes(f)
                self.assertIsNotNone(mine)
                self.assertEqual(len(mine), len(ref))
                # ms truncation vs rounding: never more than 1 ms apart
                self.assertLessEqual(max(abs(a - b) for a, b in zip(mine, ref)), 1)

    def test_a_full_file_splits_on_four_second_keyframes(self):
        f = self.files[0]
        fields = kf.keyframe_fields(str(f), None, 60_000)
        self.assertGreater(len(fields["keyFrameDurations"]), 10)
        self.assertTrue(all(3_000 <= g <= 5_000 for g in fields["keyFrameDurations"]))

    def test_the_cache_returns_the_same_answer(self):
        f = str(self.files[1])
        self.assertEqual(kf.cached_keyframes_ms(f), kf.cached_keyframes_ms(f))


class ClipKeyFramesMatchesUpstream(unittest.TestCase):
    """Cases mirror upstream frigate/test/test_recording_coverage.py semantics."""

    def test_no_index_or_one_keyframe_cannot_split(self):
        self.assertEqual(kf.clip_key_frames(None, None, 60_000), (None, 0))
        self.assertEqual(kf.clip_key_frames([], None, 60_000), (None, 0))
        self.assertEqual(kf.clip_key_frames([0], None, 60_000), (None, 0))

    def test_whole_file(self):
        self.assertEqual(kf.clip_key_frames([0, 4000, 8000], None, 10_000), ([4000, 4000], 0))

    def test_clip_from_makes_offsets_relative(self):
        self.assertEqual(
            kf.clip_key_frames([0, 4000, 8000, 12000], 4000, 8_500), ([4000, 4000], 0)
        )
        self.assertEqual(
            kf.clip_key_frames([0, 4000, 8000, 12000], 3000, 10_000), ([4000, 4000], 1000)
        )

    def test_a_keyframe_on_the_clip_end_is_excluded(self):
        self.assertEqual(kf.clip_key_frames([0, 4000, 8000], None, 8_000), ([4000], 0))


class FailsSafeToStock(unittest.TestCase):
    def test_missing_file(self):
        self.assertEqual(kf.keyframe_fields("/nonexistent.mp4", None, 60_000), {"keyFrameDurations": [60_000]})

    def test_garbage_file(self):
        with tempfile.NamedTemporaryFile(suffix=".mp4") as f:
            f.write(b"\x00\x00\x00\x08ftyp" + b"\xff" * 64)
            f.flush()
            self.assertEqual(kf.keyframe_fields(f.name, None, 1234), {"keyFrameDurations": [1234]})

    def test_first_offset_only_when_positive(self):
        orig = kf.cached_keyframes_ms
        try:
            kf.cached_keyframes_ms = lambda _p: [500, 4500, 8500]
            self.assertEqual(
                kf.keyframe_fields("x", None, 10_000),
                {"keyFrameDurations": [4000, 4000], "firstKeyFrameOffset": 500},
            )
            kf.cached_keyframes_ms = lambda _p: [0, 4000, 8000]
            self.assertEqual(kf.keyframe_fields("x", None, 10_000), {"keyFrameDurations": [4000, 4000]})
        finally:
            kf.cached_keyframes_ms = orig


class ColdHourIsIndexedInParallel(unittest.TestCase):
    """A cold page used to read its 120 files' moov boxes one after another off NFS (~3 s live).
    The first miss in a camera/hour directory must index the whole directory concurrently."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.files = []
        for i in range(48):
            f = self.tmp / f"{i:02d}.00.mp4"
            f.write_bytes(b"x")
            self.files.append(str(f))
        self.orig = kf.video_keyframes_ms
        self.calls = 0

        def slow(path):
            import time
            self.calls += 1
            time.sleep(0.05)  # an NFS round trip or three
            return [0, 4000, 8000]

        kf.video_keyframes_ms = slow

    def tearDown(self):
        kf.video_keyframes_ms = self.orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_first_miss_indexes_the_directory_concurrently_then_everything_hits(self):
        import time
        t = time.perf_counter()
        self.assertEqual(kf.cached_keyframes_ms(self.files[0]), [0, 4000, 8000])
        first = time.perf_counter() - t
        # 48 x 50 ms serially would be 2.4 s; 16 workers make it ~0.15 s.
        self.assertLess(first, 1.0)
        self.assertEqual(self.calls, 48)
        t = time.perf_counter()
        for f in self.files:
            kf.cached_keyframes_ms(f)
        self.assertLess(time.perf_counter() - t, 0.5)
        self.assertEqual(self.calls, 48, "every sibling was already indexed")


# The two anchors exactly as they appear in Frigate 0.17.2 frigate/api/media.py.
FIXTURE = (
    "import json\n"
    "from frigate.util.media import get_keyframe_before\n"
    "\n"
    "def vod_ts(recordings, duration=1):\n"
    "    clips = []\n"
    "    for recording in recordings:\n"
    "        clip = {}\n"
    "        if True:\n"
    '            clip["keyFrameDurations"] = [duration]\n'
    "            clips.append(clip)\n"
    "    return clips\n"
)


class Patcher(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "api").mkdir()
        self.media = self.tmp / "api" / "media.py"
        self.helper = self.tmp / "hawksnest_vod_keyframes.py"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_patch(self):
        return patch_media.patch(self.media, patch_media.HELPER_SRC, self.helper)

    def test_patches_both_anchors_and_copies_the_helper(self):
        self.media.write_text(FIXTURE)
        self.assertTrue(self.run_patch())
        out = self.media.read_text()
        self.assertIn(patch_media.IMPORT_LINE, out)
        self.assertIn("_hn_keyframe_fields(recording.path", out)
        self.assertNotIn(patch_media.TARGET, out)
        self.assertTrue(self.helper.exists())
        compile(out, "media.py", "exec")

    def test_is_idempotent(self):
        self.media.write_text(FIXTURE)
        self.run_patch()
        once = self.media.read_text()
        self.assertTrue(self.run_patch())
        self.assertEqual(once, self.media.read_text())

    def test_leaves_a_changed_frigate_untouched(self):
        changed = FIXTURE.replace('clip["keyFrameDurations"] = [duration]', 'clip["keyFrameDurations"] = plan')
        self.media.write_text(changed)
        self.assertFalse(self.run_patch())
        self.assertEqual(self.media.read_text(), changed)
        self.assertFalse(self.helper.exists())

    def test_missing_media_never_raises(self):
        self.assertFalse(self.run_patch())


def _check_rendered(manifest: Path) -> None:
    """Invariant: Frigate runs the patcher, then execs /init, with the generated ConfigMap mounted."""
    import yaml

    docs = [d for d in yaml.safe_load_all(manifest.read_text()) if d]
    dep = next(d for d in docs if d.get("kind") == "Deployment" and d["metadata"]["name"] == "frigate")
    spec = dep["spec"]["template"]["spec"]
    c = next(c for c in spec["containers"] if c["name"] == "frigate")
    cmd = " ".join(c.get("command", []))
    assert "patch_media.py" in cmd and cmd.rstrip().endswith("exec /init"), cmd
    vol = next(v for v in spec["volumes"] if v["name"] == "vod-keyframes")
    cm_name = vol["configMap"]["name"]
    assert any(d.get("kind") == "ConfigMap" and d["metadata"]["name"] == cm_name for d in docs), cm_name
    assert any(m["name"] == "vod-keyframes" and m["mountPath"] == "/hawksnest/vod-keyframes" for m in c["volumeMounts"])


if __name__ == "__main__":
    # `python3 tests/test_vod_keyframes.py /tmp/prod.yaml` also checks the rendered wiring.
    if len(sys.argv) > 1 and sys.argv[1].endswith(".yaml"):
        _check_rendered(Path(sys.argv.pop(1)))
        print("OK — Frigate VOD keyframe backport is wired in the rendered manifest")
    unittest.main(verbosity=2)
