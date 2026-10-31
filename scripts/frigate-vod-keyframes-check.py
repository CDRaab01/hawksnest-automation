"""Read-only dry run of the VOD keyframe backport against the LIVE Frigate's recordings.

Indexes the newest ~2 h of recording files per camera with the exact code the patch installs
(kustomize/base/frigate/vod-keyframes/hawksnest_vod_keyframes.py) and reports what nginx-vod would
be told. Changes nothing: no file is written, Frigate is not touched.

It is not run on its own — the helper is prepended so the pod needs nothing installed:

  cat kustomize/base/frigate/vod-keyframes/hawksnest_vod_keyframes.py scripts/frigate-vod-keyframes-check.py \
    | kubectl -n home-automation exec -i deploy/frigate -c frigate -- python3 -

What good looks like: every camera "indexed" at or near 100 %, keyframe gaps ~4000 ms, ~6-8 HLS
segments per 60 s file, and a cold index time per 2 h page well under a second (it is cached
after that). A camera at 0 % indexed would simply keep stock whole-file segments.
"""

import glob as _glob
import os as _os
import statistics as _st
import time as _time

_ROOT = "/media/frigate/recordings"
_PAGE = 120  # files in a 2 h page at 60 s per file


def _newest_files(camera_dir_glob):
    files = sorted(_glob.glob(camera_dir_glob), key=_os.path.getmtime)
    return files[-_PAGE:]


_cameras = sorted({p.split("/")[-1] for p in _glob.glob(f"{_ROOT}/*/*/*") if _os.path.isdir(p)})
print(f"{'camera':24} {'files':>5} {'indexed':>8} {'gap ms':>7} {'segs/file':>9} {'cold ms':>8}")
for _cam in _cameras:
    _files = _newest_files(f"{_ROOT}/*/*/{_cam}/*.mp4")
    if not _files:
        continue
    _t = _time.perf_counter()
    _ok, _gaps, _segs = 0, [], []
    for _f in _files:
        _k = video_keyframes_ms(_f)  # noqa: F821 — defined by the prepended helper
        if _k and len(_k) >= 2:
            _ok += 1
            _g = [b - a for a, b in zip(_k, _k[1:])]
            _gaps.extend(_g)
            # nginx-vod cuts at the first keyframe at/after each 10 s boundary
            _n, _last = 0, None
            for _x in _k:
                if _last is None or _x - _last >= 10_000:
                    _n, _last = _n + 1, _x
            _segs.append(_n)
    _ms = (_time.perf_counter() - _t) * 1000
    print(
        f"{_cam:24} {len(_files):>5} {100 * _ok // len(_files):>7}% "
        f"{int(_st.median(_gaps)) if _gaps else '-':>7} "
        f"{_st.median(_segs) if _segs else '-':>9} {_ms:>8.0f}"
    )
