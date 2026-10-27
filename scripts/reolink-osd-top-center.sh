#!/usr/bin/env bash
# Move every Reolink camera's OSD timestamp to top-center, then report what
# Frigate sees (live motion masks + detection_fps) so masks can be checked.
#
# Run inside the Dragonfly WSL distro (needs kubectl + python3 + LAN access).
#   ./reolink-osd-top-center.sh            # apply
#   DRY_RUN=1 ./reolink-osd-top-center.sh  # read-only: show current OSD only
#
# Wall cameras: prompts ONCE for the camera ADMIN password (the fleet
# FRIGATE_REOLINK_USER account is non-admin and cannot SetOsd). One login
# attempt per camera, and the run aborts if the FIRST camera rejects it, so a
# typo cannot burn the lockout budget across the fleet.
# Home Hub: uses FRIGATE_REOLINK_HUB_* from the Frigate pod (already admin).
set -euo pipefail
NS="${NS:-home-automation}"
WAIT="${WAIT:-90}"

envdump="$(kubectl -n "$NS" exec deploy/frigate -c frigate -- printenv 2>/dev/null \
  || kubectl -n "$NS" exec deploy/frigate -- printenv)"
export CAM_ENV="$(printf '%s\n' "$envdump" | grep -E '^FRIGATE_REOLINK_(IP_|HUB_)')"

read -r -p "Camera admin username [admin]: " ADMIN_USER
export ADMIN_USER="${ADMIN_USER:-admin}"
read -r -s -p "Camera admin password (wall cameras): " ADMIN_PASS; echo
export ADMIN_PASS DRY_RUN="${DRY_RUN:-0}"

python3 - <<'PY'
import json, os, sys, urllib.request

env = dict(l.split("=", 1) for l in os.environ["CAM_ENV"].splitlines() if "=" in l)
dry = os.environ["DRY_RUN"] == "1"

def call(ip, cmd, body, token=None):
    url = f"http://{ip}/cgi-bin/api.cgi?cmd={cmd}" + (f"&token={token}" if token else "")
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())[0]

def login(ip, user, pw):
    r = call(ip, "Login", [{"cmd": "Login", "param": {"User": {"Version": "0", "userName": user, "password": pw}}}])
    if r.get("code") != 0:
        left = r.get("error", {})
        raise RuntimeError(f"login failed: {left}")
    return r["value"]["Token"]["name"]

def top_center(choices):
    for want in ("top center", "upper center", "top middle", "upper middle"):
        for c in choices:
            if c.lower().replace("_", " ") == want:
                return c
    for c in choices:
        cl = c.lower()
        if ("top" in cl or "upper" in cl) and ("center" in cl or "middle" in cl):
            return c
    return None

def do_channel(label, ip, tok, ch):
    r = call(ip, "GetOsd", [{"cmd": "GetOsd", "action": 1, "param": {"channel": ch}}], tok)
    if r.get("code") != 0:
        print(f"  {label}: GetOsd failed {r.get('error')}"); return
    cur = r["value"]["Osd"]["osdTime"]
    choices = r.get("range", {}).get("Osd", {}).get("osdTime", {}).get("pos", [])
    tgt = top_center(choices)
    print(f"  {label}: now enable={cur.get('enable')} pos={cur.get('pos')!r}; options={choices}")
    if tgt is None:
        print(f"  {label}: no top-center option, skipped"); return
    if cur.get("enable") == 1 and cur.get("pos") == tgt:
        print(f"  {label}: already top-center"); return
    if dry:
        print(f"  {label}: would set pos={tgt!r}"); return
    s = call(ip, "SetOsd", [{"cmd": "SetOsd", "param": {"Osd": {"channel": ch, "osdTime": {"enable": 1, "pos": tgt}}}}], tok)
    after = call(ip, "GetOsd", [{"cmd": "GetOsd", "action": 0, "param": {"channel": ch}}], tok)["value"]["Osd"]["osdTime"]
    print(f"  {label}: SetOsd code={s.get('code')} -> enable={after.get('enable')} pos={after.get('pos')!r}")

def logout(ip, tok):
    try: call(ip, "Logout", [{"cmd": "Logout", "param": {}}], tok)
    except Exception: pass

wall = sorted((k[len("FRIGATE_REOLINK_IP_"):].lower(), v) for k, v in env.items()
              if k.startswith("FRIGATE_REOLINK_IP_") and k != "FRIGATE_REOLINK_IP_HOME_HUB")
print("== Wall cameras")
first = True
for name, ip in wall:
    try:
        tok = login(ip, os.environ["ADMIN_USER"], os.environ["ADMIN_PASS"])
    except Exception as e:
        print(f"  {name} ({ip}): {e}")
        if first:
            sys.exit("Aborting: first camera rejected the password (protecting lockout budget).")
        continue
    first = False
    try: do_channel(f"{name} ({ip})", ip, tok, 0)
    except Exception as e: print(f"  {name}: error {e}")
    finally: logout(ip, tok)

hub = env.get("FRIGATE_REOLINK_IP_HOME_HUB")
if hub:
    print("== Home Hub battery cameras (asleep ones may fail; re-run later)")
    try:
        tok = login(hub, env["FRIGATE_REOLINK_HUB_USER"], env["FRIGATE_REOLINK_HUB_PASSWORD"])
        st = call(hub, "GetChannelstatus", [{"cmd": "GetChannelstatus", "action": 0, "param": {}}], tok)
        for c in st["value"]["status"]:
            if c.get("uid"):
                try: do_channel(f"hub ch{c['channel']} {c.get('name')!r}", hub, tok, c["channel"])
                except Exception as e: print(f"  hub ch{c['channel']}: error {e}")
        logout(hub, tok)
    except Exception as e:
        print(f"  hub: {e}")
PY

[ "${DRY_RUN:-0}" = 1 ] && exit 0
echo "== Waiting ${WAIT}s for Frigate motion stats to settle..."
sleep "$WAIT"
kubectl -n "$NS" exec -i deploy/frigate -c frigate -- python3 - <<'PY'
import json, urllib.request
g = lambda p: json.load(urllib.request.urlopen("http://127.0.0.1:5000" + p, timeout=15))
stats, cfg = g("/api/stats"), g("/api/config")
print("== Frigate: camera | enabled | detection_fps | live motion masks")
for name, c in cfg["cameras"].items():
    s = stats.get("cameras", {}).get(name, {})
    m = (c.get("motion") or {}).get("mask") or []
    if isinstance(m, dict): m = list(m)
    print(f"  {name:22} {str(c.get('enabled')):5} fps={s.get('detection_fps')!s:5} masks={m}")
PY
