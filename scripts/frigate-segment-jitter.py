#!/usr/bin/env python3
"""
frigate-segment-jitter.py — how evenly are a camera's recorded frames spaced?

Reads the mp4 segments Frigate wrote for one or more cameras over the last N hours
and prints, per segment, the effective frame rate, the frame-spacing standard
deviation, the share of frames that arrived in <10 ms bursts, and the longest gap.
It answers "does this camera's footage play smoothly?" without opening a stream —
so it is safe for the Home Hub battery cameras, which any RTSP request would wake.

Why it exists (2026-09-13): the two Argus 4 Pros behind the Reolink Home Hub played
"jumpy / speeds up / slows down" in Hawksnest. Recorded segments showed why: the
hub delivers frames in bursts over a starved Wi-Fi path, and the cameras' Frigate
input args stamped every frame with its ARRIVAL time (`-use_wallclock_as_timestamps 1`)
while record was `-c copy`, so the bursts were baked into the file. A wired wall
camera reads ~38 ms stdev / 3 % bursts; the hub cameras read 170-190 ms / 40 %
(`backyard_patio`) and 2.5 fps with 17 s gaps (`front`). Run this before and after
any change to those cameras' input args, stream settings, or radio path.

Usage (from the repo root, Dragonfly WSL distro, root):
    python3 scripts/frigate-segment-jitter.py                 # front + backyard_patio, 24 h
    python3 scripts/frigate-segment-jitter.py -c basement -H 2  # a wired control, 2 h
    python3 scripts/frigate-segment-jitter.py --audio           # also check AAC packet spacing

It runs ffprobe INSIDE the frigate pod (the binary is /usr/lib/ffmpeg/7.0/bin/ffprobe,
not on PATH there) by shipping the analysis half of itself over `kubectl exec -i`.

Reading the numbers: healthy is stdev under ~60 ms, bursts under ~10 %, max gap under
~1 s, and eff_fps within a frame or two of the stream's configured rate. stdev high
with fps normal = jittered timestamps (arrival time stamped, no loss). fps far below
the configured rate = frames actually missing (radio path / camera).
"""
import argparse
import subprocess
import sys

NAMESPACE = "home-automation"
DEFAULT_CAMERAS = ["front", "backyard_patio"]

# Runs inside the pod. Kept as a string so this file is the single source; argv is
# "<hours> <audio 0|1> <camera...>".
IN_POD = r'''
import glob, os, statistics, subprocess, sys, time
FF = "/usr/lib/ffmpeg/7.0/bin/ffprobe"
hours = float(sys.argv[1]); audio = sys.argv[2] == "1"; cams = sys.argv[3:]

def pts(path, sel):
    out = subprocess.run([FF, "-v", "error", "-select_streams", sel, "-show_entries",
                          "packet=pts_time", "-of", "csv=p=0", path],
                         capture_output=True, text=True).stdout
    v = []
    for line in out.splitlines():
        try:
            v.append(float(line.split(",")[0]))
        except ValueError:
            pass
    v.sort()
    return v

def describe(p):
    if len(p) < 3:
        return None
    d = [b - a for a, b in zip(p, p[1:])]
    span = p[-1] - p[0]
    return dict(frames=len(p), span=span, fps=(len(p) - 1) / span if span else 0.0,
                stdev=statistics.pstdev(d) * 1000,
                burst=100.0 * sum(1 for x in d if x < 0.010) / len(d),
                maxgap=max(d) * 1000)

cutoff = time.time() - hours * 3600
for cam in cams:
    files = sorted(f for f in glob.glob("/media/frigate/recordings/*/*/%s/*.mp4" % cam)
                   if os.path.getmtime(f) > cutoff)
    print("== %s: %d segments in the last %g h" % (cam, len(files), hours))
    print("   %-42s %6s %7s %6s %8s %6s %8s" % ("segment (UTC dir/hour)", "frames", "span_s", "fps", "stdev_ms", "burst%", "maxgap"))
    rows = []
    for f in files:
        r = describe(pts(f, "v:0"))
        name = f.split("recordings/")[1]
        if r is None:
            print("   %-42s   (fewer than 3 frames)" % name)
            continue
        rows.append(r)
        print("   %-42s %6d %7.2f %6.2f %8.1f %6.1f %8.0f" % (name, r["frames"], r["span"], r["fps"], r["stdev"], r["burst"], r["maxgap"]))
        if audio:
            a = describe(pts(f, "a:0"))
            if a:
                print("   %-42s %6d %7.2f %6s %8.1f %6.1f %8.0f  (audio; AAC @16k = 64 ms grid)" % ("", a["frames"], a["span"], "", a["stdev"], a["burst"], a["maxgap"]))
    if rows:
        bad = sum(1 for r in rows if r["stdev"] > 100 or r["maxgap"] > 1000)
        print("   SUMMARY %s: %d analysable segments, %d with stdev>100 ms or a >1 s gap, median fps %.1f, median stdev %.0f ms, median burst %.0f%%" % (
            cam, len(rows), bad, statistics.median(r["fps"] for r in rows),
            statistics.median(r["stdev"] for r in rows), statistics.median(r["burst"] for r in rows)))
'''


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("-c", "--camera", action="append", help="camera name (repeatable); default: the hub cameras")
    ap.add_argument("-H", "--hours", type=float, default=24.0, help="look-back window in hours (default 24)")
    ap.add_argument("--audio", action="store_true", help="also report AAC packet spacing per segment")
    ap.add_argument("-n", "--namespace", default=NAMESPACE)
    args = ap.parse_args()
    cams = args.camera or DEFAULT_CAMERAS
    cmd = ["kubectl", "-n", args.namespace, "exec", "-i", "deploy/frigate", "-c", "frigate", "--",
           "python3", "-", str(args.hours), "1" if args.audio else "0", *cams]
    return subprocess.run(cmd, input=IN_POD, text=True).returncode


if __name__ == "__main__":
    sys.exit(main())
