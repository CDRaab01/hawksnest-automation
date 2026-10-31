"""Wire hawksnest_vod_keyframes into Frigate's VOD mapping, at container start.

Run by the frigate container's command before it execs s6's /init (deployment.yaml). It edits
the image's own frigate/api/media.py in the container's writable layer, so every pod start
re-applies it to a pristine file and nothing persists across image changes.

FAILS SAFE, by design: unless BOTH anchors below are found exactly once and the patched source
compiles, media.py is left untouched and Frigate runs stock 0.17.2 VOD (one HLS segment per
recording file — slower seeks, nothing broken). The script never exits non-zero: a patch problem
must never keep the NVR from starting. Grep the pod log for "hawksnest-vod-keyframes".

See hawksnest_vod_keyframes.py for why, and when to delete this.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

TAG = "hawksnest-vod-keyframes"
FRIGATE = Path("/opt/frigate/frigate")
MEDIA = FRIGATE / "api" / "media.py"
HELPER_SRC = Path(__file__).with_name("hawksnest_vod_keyframes.py")
HELPER_DST = FRIGATE / "hawksnest_vod_keyframes.py"

# 0.17.2 frigate/api/media.py, vod_ts: the whole-file keyframe declaration.
TARGET = '            clip["keyFrameDurations"] = [duration]\n'
REPLACEMENT = (
    "            # hawksnest-vod-keyframes: real keyframe gaps so nginx-vod can cut inside a file\n"
    '            clip.update(_hn_keyframe_fields(recording.path, clip.get("clipFrom"), duration))\n'
)
IMPORT_ANCHOR = "from frigate.util.media import get_keyframe_before\n"
IMPORT_LINE = "from frigate.hawksnest_vod_keyframes import keyframe_fields as _hn_keyframe_fields\n"


def log(msg: str) -> None:
    print(f"[{TAG}] {msg}", flush=True)


def patch(media: Path = MEDIA, helper_src: Path = HELPER_SRC, helper_dst: Path = HELPER_DST) -> bool:
    try:
        src = media.read_text()
    except OSError as e:
        log(f"cannot read {media}: {e} — leaving Frigate stock")
        return False
    if IMPORT_LINE in src:
        log("already patched")
        return True
    if src.count(TARGET) != 1 or src.count(IMPORT_ANCHOR) != 1:
        log(
            f"anchors not found exactly once (target={src.count(TARGET)}, "
            f"import={src.count(IMPORT_ANCHOR)}) — Frigate changed; leaving it stock. "
            "If this Frigate ships blakeblackshear/frigate#24011, delete this patch."
        )
        return False
    patched = src.replace(TARGET, REPLACEMENT).replace(IMPORT_ANCHOR, IMPORT_ANCHOR + IMPORT_LINE)
    try:
        compile(patched, str(media), "exec")
        compile(helper_src.read_text(), str(helper_dst), "exec")
    except (SyntaxError, OSError) as e:
        log(f"patched source does not compile ({e}) — leaving Frigate stock")
        return False
    try:
        shutil.copyfile(helper_src, helper_dst)
        media.write_text(patched)
    except OSError as e:
        log(f"write failed ({e}) — leaving Frigate stock")
        try:
            media.write_text(src)
        except OSError:
            pass
        return False
    log("patched frigate/api/media.py: VOD manifests carry real keyframe gaps")
    return True


if __name__ == "__main__":
    try:
        patch()
    except Exception as e:  # never block startup
        log(f"unexpected error ({e!r}) — leaving Frigate stock")
    sys.exit(0)
