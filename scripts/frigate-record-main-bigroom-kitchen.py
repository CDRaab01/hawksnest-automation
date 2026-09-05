#!/usr/bin/env python3
"""Live half of the 2026-09-04 dual write: big_room + kitchen `record` -> MAIN stream.

WHY THIS SCRIPT EXISTS
----------------------
The seed ConfigMap (kustomize/base/frigate/configmap.yaml) is copied to the
frigate-config PVC on FIRST BOOT ONLY. Editing it alone changes nothing on a running
Frigate -- that is precisely what made #64 a merged, green, deployed no-op for two
days in August 2026. Every real change is a dual write: repo seed + live
/config/config.yml. This is the live half, made repeatable and reviewable instead of
typed into a shell once.

WHY IT IS A TEXT EDIT AND NOT A YAML ROUND-TRIP
-----------------------------------------------
The live config carries the same explanatory comments the seed does. `yaml.safe_load`
followed by `yaml.dump` would silently delete every one of them and reorder the keys,
turning a five-line change into an unreviewable rewrite of a 41 KB production file.
So this locates the exact block it expects and refuses to run if it is not there.

SAFETY
------
  * aborts unless each camera's sub-stream block matches EXACTLY once
  * re-parses the result and asserts the new roles are what we intended
  * asserts nothing outside big_room and kitchen changed
  * backs the file up (timestamped, beside the original) before writing

Run as root inside the k3s distro, then restart Frigate:

    wsl -d Dragonfly -u root python3 /mnt/c/Code/hawksnest-automation/scripts/frigate-record-main-bigroom-kitchen.py
    wsl -d Dragonfly -u root kubectl -n home-automation rollout restart deploy/frigate

Verify with scripts/frigate-drift-check.sh (should report no drift once both halves
of the dual write are done).

Idempotent: running it twice is a no-op abort, not a double-patch.
"""

import shutil
import sys
import time

import yaml

PVC = (
    "/var/lib/rancher/k3s/storage/"
    "pvc-a0b6a60b-194d-4152-9f18-1c0920fc2794_home-automation_frigate-config"
)
CFG = PVC + "/config.yml"

# Kept short on purpose: the live file points at the seed rather than duplicating the
# rationale, so there is one place to update when this is revisited.
NOTE = {
    "big_room": (
        "        # MAIN -> record, 2026-09-04. 2560x1440 h264 @20, 5120 kbps.\n"
        "        # Narrower retry of #64: TWO main streams, not seven, and the\n"
        "        # nursery is deliberately excluded and stays on sub. Full rationale\n"
        "        # lives in the repo seed (kustomize/base/frigate/configmap.yaml);\n"
        "        # this file is the live half of that dual write.\n"
    ),
    "kitchen": (
        "        # MAIN -> record, 2026-09-04. 2880x1616 h264 @20, 3072 kbps (its\n"
        "        # ceiling; 0.033 bpp, so high-resolution but under-bitrated). Live\n"
        "        # half of the dual write - see the repo seed for the rationale.\n"
    ),
}

CAMERAS = [
    ("big_room", "FRIGATE_REOLINK_IP_BIG_ROOM"),
    ("kitchen", "FRIGATE_REOLINK_IP_KITCHEN"),
]


def patch(text, cam, ipvar):
    sub = (
        "rtsp://{FRIGATE_REOLINK_USER}:{FRIGATE_REOLINK_PASSWORD}"
        "@{%s}:554/h264Preview_01_sub" % ipvar
    )
    main = sub.replace("_sub", "_main")
    old = (
        "        - path: \n            " + sub + "\n"
        "          roles:\n            - detect\n            - record\n"
    )
    n = text.count(old)
    if n != 1:
        sys.exit(
            "ABORT: sub-stream block for %s matched %d times, expected 1.\n"
            "       Either it is already patched, or the live file has been "
            "reformatted and this script must be re-read against it." % (cam, n)
        )
    new = (
        "        - path: \n            " + sub + "\n"
        "          roles:\n            - detect\n"
        + NOTE[cam]
        + "        - path: \n            " + main + "\n"
        "          roles:\n            - record\n"
    )
    return text.replace(old, new)


def main():
    src = open(CFG, encoding="utf-8").read()
    before = yaml.safe_load(src)

    out = src
    for cam, ipvar in CAMERAS:
        out = patch(out, cam, ipvar)

    after = yaml.safe_load(out)

    for cam, _ in CAMERAS:
        ins = after["cameras"][cam]["ffmpeg"]["inputs"]
        assert len(ins) == 2, "%s: expected 2 inputs, got %d" % (cam, len(ins))
        assert ins[0]["roles"] == ["detect"], "%s: sub stream must be detect-only" % cam
        assert ins[1]["roles"] == ["record"], "%s: main stream must be record-only" % cam
        assert ins[1]["path"].endswith("_main"), "%s: second input is not the main stream" % cam

    # Nothing outside those two cameras may move. This is the assertion that makes a
    # text edit on a production file defensible.
    b, a = dict(before), dict(after)
    bc, ac = b.pop("cameras"), a.pop("cameras")
    assert b == a, "ABORT: config outside `cameras:` changed"
    assert set(bc) == set(ac), "ABORT: the set of cameras changed"
    for name in bc:
        if name not in dict(CAMERAS):
            assert bc[name] == ac[name], "ABORT: camera %s changed unexpectedly" % name

    backup = CFG + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(CFG, backup)
    open(CFG, "w", encoding="utf-8").write(out)

    print("backup :", backup)
    print("bytes  : %d -> %d" % (len(src), len(out)))
    print("roles now:")
    for name, cam in after["cameras"].items():
        streams = [
            "%s=%s"
            % (
                i["path"].rsplit("/", 1)[-1].replace("h264Preview_01_", ""),
                "+".join(i["roles"]),
            )
            for i in cam["ffmpeg"]["inputs"]
        ]
        print("  %-22s %s" % (name, "  ".join(streams)))
    print("\nNow restart Frigate, then confirm with scripts/frigate-drift-check.sh")


if __name__ == "__main__":
    main()
