#!/usr/bin/env python3
"""Behaviour tests for the frigate-health CronJob's decision logic.

WHY THIS EXISTS. On 2026-09-10 the /tmp/cache tmpfs filled and every camera
stopped recording for ~4 hours with nothing alerting — the pod stayed Running and
Ready the whole time, because the readiness probe hits /api/version, which answers
fine while recording is dead. health-cronjob.yaml is the watchdog added in
response. An untested watchdog is the same failure again with extra steps, so this
exercises the paths that matter before they are needed in anger.

WHAT IT RUNS. The EXACT script embedded in the built manifest — extracted from the
CronJob, not a copy that can drift. `urlopen`, `sleep` and `time` are patched on the
real modules (the script does its own `import urllib.request`, so stubs injected via
exec globals get overwritten). The only edit to the source is repointing the state
file at a tempdir, and that substitution is asserted, so renaming the constant fails
here loudly rather than silently skipping the dedup tests.

Nothing here touches a cluster, a camera, or ntfy.

Usage:
  python3 tests/test_frigate_health.py [BUILT_MANIFEST]   # default /tmp/prod.yaml
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
import urllib.request

try:
    import yaml
except ImportError:  # pragma: no cover - guidance only
    sys.exit("PyYAML is required: pip install pyyaml")

CAMS = [
    "big_room", "kitchen", "nursery", "nursery_high", "bedroom",
    "basement", "garage", "front_door_reolink", "first_floor_stairway",
    # On-demand (Home Hub battery) cameras — in ON_DEMAND in the script under test.
    "backyard_patio", "front",
]
CACHE_TOTAL_MB = 2048.0
REAL_URLOPEN = urllib.request.urlopen
REAL_SLEEP = time.sleep
REAL_TIME = time.time

_TMP = tempfile.mkdtemp(prefix="frigate-health-test-")
STATE = os.path.join(_TMP, "state.json")


def load_source(path: str) -> str:
    docs = [d for d in yaml.safe_load_all(open(path)) if d]
    matches = [
        d for d in docs
        if d.get("kind") == "CronJob" and d["metadata"]["name"] == "frigate-health"
    ]
    if not matches:
        sys.exit("no frigate-health CronJob in %s" % path)
    spec = matches[0]["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    src = spec["containers"][0]["command"][2]

    needle = 'STATE = "/ha-config/frigate-health/state.json"'
    if src.count(needle) != 1:
        sys.exit(
            "expected exactly one STATE assignment in the health script; the "
            "constant was renamed or moved, so this test can no longer isolate "
            "its state file. Update `needle` in tests/test_frigate_health.py."
        )
    return src.replace(needle, 'STATE = "%s"' % STATE)


def make_stats(fps=5.0, cache_used_mb=40.0, dead=()):
    """Stats payload shaped like Frigate 0.17's /api/stats."""
    cams = {}
    for c in CAMS:
        cams[c] = {"camera_fps": 0.0 if c in dead else fps, "detection_fps": 0.0}
    return {
        "cameras": cams,
        "service": {
            "storage": {"/tmp/cache": {"total": CACHE_TOTAL_MB, "used": cache_used_mb}}
        },
    }


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Case:
    """One run of the script against a simulated Frigate."""

    def __init__(self, src):
        self.src = src

    def run(self, label, stats, api_down=False, now=1789070000.0, fresh=True):
        if fresh and os.path.exists(STATE):
            os.remove(STATE)
        sent = []

        def fake_urlopen(req, timeout=None):
            url = req if isinstance(req, str) else req.full_url
            if "api/stats" in url:
                if api_down:
                    raise OSError("connection refused")
                return _Resp(json.dumps(stats).encode())
            sent.append({
                "url": url,
                "title": req.headers.get("Title"),
                "priority": req.headers.get("Priority"),
                "body": req.data.decode(),
            })
            return _Resp(b"ok")

        urllib.request.urlopen = fake_urlopen
        time.sleep = lambda s: None
        time.time = lambda: now
        buf, old = io.StringIO(), sys.stdout
        sys.stdout = buf
        try:
            exec(compile(self.src, "health", "exec"), {"__name__": "__main__"})
        finally:
            sys.stdout = old
            urllib.request.urlopen = REAL_URLOPEN
            time.sleep = REAL_SLEEP
            time.time = REAL_TIME

        print("  %-46s -> %d alert(s)" % (label, len(sent)))
        for s in sent:
            print("      [%s] %s :: %s"
                  % (s["priority"], s["title"], s["body"].replace("\n", " / ")))
        return sent


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/prod.yaml"
    case = Case(load_source(path))
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)
            print("      FAIL: %s" % msg)

    print("frigate-health decision logic (%s)" % path)

    got = case.run("healthy: all cameras at 5 fps, cache 2%", make_stats())
    check(not got, "a healthy NVR must not alert")

    # The incident: every camera at 0 fps with the cache pinned near full.
    got = case.run("outage: all 9 cameras at 0 fps, cache 97%",
                   make_stats(dead=CAMS, cache_used_mb=1990.0))
    check(len(got) == 1, "a full outage must alert exactly once")
    if got:
        check("NOT RECORDING" in got[0]["body"], "outage alert must say NOT RECORDING")
        check("big_room" in got[0]["body"], "outage alert must name the dead cameras")
        check(got[0]["priority"] == "urgent", "outage alert must be urgent")

    got = case.run("outage: one camera down, rest fine",
                   make_stats(dead=["big_room"]))
    check(len(got) == 1, "a single dead camera must still alert")
    if got:
        check("big_room" in got[0]["body"], "must name the one dead camera")
        check("kitchen" not in got[0]["body"], "must not implicate healthy cameras")

    # On-demand (battery, Home Hub) cameras are parked OFF most of the day and their
    # camera_fps freezes — at 0 after every Frigate restart. That is the design, not
    # an outage, and it must never page.
    got = case.run("on-demand cameras at 0 fps: parked, not dead",
                   make_stats(dead=["backyard_patio", "front"]))
    check(not got, "a parked on-demand camera must not alert")

    # ...but the allow-list must be exact: a wired camera dead beside a parked one
    # still pages, and names only the wired one.
    got = case.run("on-demand parked AND a wired camera dead",
                   make_stats(dead=["backyard_patio", "big_room"]))
    check(len(got) == 1, "a dead wired camera must still alert beside parked ones")
    if got:
        check("big_room" in got[0]["body"], "must name the dead wired camera")
        check("backyard_patio" not in got[0]["body"], "must not implicate the parked camera")

    # Cache pressure alone, while every camera is still recording: the warning
    # that would have bought hours of notice on 2026-09-10.
    got = case.run("early warning: cache 85%, cameras healthy",
                   make_stats(cache_used_mb=0.85 * CACHE_TOTAL_MB))
    check(len(got) == 1, "cache pressure must alert before recording stops")
    if got:
        check("/tmp/cache" in got[0]["body"], "cache alert must name /tmp/cache")
        check("NOT RECORDING" not in got[0]["body"],
              "cache warning must not claim recording has stopped")

    got = case.run("cache 60%: normal backlog, must stay silent",
                   make_stats(cache_used_mb=0.60 * CACHE_TOTAL_MB))
    check(not got, "ordinary cache use must not alert")

    got = case.run("frigate API unreachable", make_stats(), api_down=True)
    check(len(got) == 1, "an unreachable API must alert")
    if got:
        check("unreachable" in got[0]["body"], "must say the API is unreachable")

    # Throttling and recovery share the state file, so these run in sequence.
    outage = make_stats(dead=CAMS, cache_used_mb=1990.0)
    case.run("outage (first alert, state fresh)", outage)
    got = case.run("same outage 5 min later: suppressed", outage,
                   now=1789070000.0 + 300, fresh=False)
    check(not got, "a repeat inside the 30-min window must be suppressed")

    got = case.run("same outage 31 min later: re-alerts", outage,
                   now=1789070000.0 + 1860, fresh=False)
    check(len(got) == 1, "must re-alert once the throttle window passes")

    got = case.run("recovered: sends recovery notice", make_stats(),
                   now=1789070000.0 + 1900, fresh=False)
    check(len(got) == 1, "recovery must notify")
    if got:
        check("recovered" in got[0]["title"].lower(), "recovery title must say so")
        check(got[0]["priority"] == "default", "recovery must not be urgent")

    got = case.run("still healthy: stays silent", make_stats(),
                   now=1789070000.0 + 2200, fresh=False)
    check(not got, "recovery must be sent once, not on every healthy run")

    print()
    if failures:
        print("FAILED (%d):" % len(failures))
        for f in failures:
            print("  - %s" % f)
        return 1
    print("frigate-health: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
