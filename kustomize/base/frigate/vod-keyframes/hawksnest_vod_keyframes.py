"""Hawksnest backport: real per-clip keyframe data for Frigate 0.17.x VOD manifests.

WHY THIS EXISTS
---------------
Frigate 0.17.2's VOD mapping (frigate/api/media.py, `vod_ts`) declares every recording file as a
single keyframe run: `clip["keyFrameDurations"] = [duration]`. nginx-vod then cannot cut inside a
file, so each HLS segment is one whole recording file. Our recordings are 60 s (pinned on purpose
2026-07-31 to make the AAC seam dropout 6x rarer — see configmap.yaml), so every seek into
unloaded footage downloads a whole ~1.3-2.2 MB file. Measured 2026-10-30: 10 fps, keyframe every
4 s, one 60 s HLS segment per file on every camera. `vod_segment_duration 10000` is set in
Frigate's nginx.conf, but it cannot split what the mapping says is indivisible.

Upstream fixed exactly this on dev (blakeblackshear/frigate#24011, merged 2026-08-17, not in any
release as of 0.18.2): it stores each file's keyframe offsets at record time and emits real
`keyFrameDurations` + `firstKeyFrameOffset`. Without an index upstream still falls back to one
segment per file, "the only safe cut without keyframe knowledge". This module supplies the index at
playback time instead of record time, by reading the file's own sample tables (stss/stts/ctts)
from the moov box — no subprocess, ~1 ms per file, cached per (path, size, mtime) because a
recording never changes once written. The clip-relative math is a line-for-line port of upstream's
`_clip_key_frames` (frigate/util/recording_coverage.py).

FAILS SAFE
----------
Any problem — unreadable file, no video track, no sync-sample table, fewer than two keyframes in
the served range — returns exactly what 0.17.2 emits today (`{"keyFrameDurations": [duration]}`).
The patch that wires this in (patch_media.py) also leaves media.py untouched if the line it
replaces is not found, so a Frigate upgrade degrades to stock behaviour rather than breaking.

DELETE THIS when Frigate ships #24011 (expected 0.19): the stock manifest then does the same thing
from its own record-time index.
"""

from __future__ import annotations

import logging
import os
import struct
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Any

logger = logging.getLogger(__name__)

_CACHE: OrderedDict[tuple[str, int, float], list[int] | None] = OrderedDict()
_CACHE_MAX = 20_000  # ~3 days x 9 cameras at one file per minute, with headroom
_LOCK = threading.Lock()

# COLD PAGES ARE LATENCY-BOUND, SO INDEX A WHOLE HOUR AT ONCE (2026-10-31). Measured live after
# the first deploy: a never-opened 2 h page took 2.2-4.0 s to build (was ~0.1 s stock) — 120
# files' moov boxes read one after another off the NFS recordings volume, ~25 ms each, almost
# all of it round-trip wait. Repeat builds were 13-74 ms, so only the first touch was slow. On a
# miss, every not-yet-indexed recording in the same camera/hour directory (Frigate's layout:
# recordings/<date>/<hour>/<camera>/<MM.SS>.mp4 — a VOD range is consecutive files from 1-3 of
# these) is indexed concurrently, so the page pays a few parallel batches instead of 120 serial
# reads. Threads, not processes: the work is I/O wait, and it releases the GIL.
_POOL = ThreadPoolExecutor(max_workers=16, thread_name_prefix="hn-vod-kf")
_BATCH_TIMEOUT_S = 5.0
_INDEXED: set[str] = set()  # paths with a cache entry, so a batch skips them without a stat

# A moov this large means something is very wrong with the file; refuse rather than read it.
_MAX_MOOV_BYTES = 16 * 1024 * 1024


def _iter_boxes(buf: bytes, start: int, end: int):
    """Yield (type, payload_start, box_end) for the boxes in buf[start:end]."""
    pos = start
    while pos + 8 <= end:
        size, typ = struct.unpack_from(">I4s", buf, pos)
        hdr = 8
        if size == 1:
            if pos + 16 > end:
                return
            size = struct.unpack_from(">Q", buf, pos + 8)[0]
            hdr = 16
        elif size == 0:
            size = end - pos
        if size < hdr or pos + size > end:
            return
        yield typ, pos + hdr, pos + size
        pos += size


def _child(buf: bytes, start: int, end: int, typ: bytes):
    for t, s, e in _iter_boxes(buf, start, end):
        if t == typ:
            return s, e
    return None


def _read_moov(path: str) -> bytes | None:
    """Read the top-level moov box. ffmpeg's segment muxer writes it at the END of the file, so
    top-level box headers are walked with seeks rather than reading the mdat."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        file_end = f.tell()
        pos = 0
        while pos + 8 <= file_end:
            f.seek(pos)
            hdr = f.read(16)
            if len(hdr) < 8:
                return None
            size, typ = struct.unpack_from(">I4s", hdr, 0)
            hlen = 8
            if size == 1:
                if len(hdr) < 16:
                    return None
                size = struct.unpack_from(">Q", hdr, 8)[0]
                hlen = 16
            elif size == 0:
                size = file_end - pos
            if size < hlen:
                return None
            if typ == b"moov":
                if size > _MAX_MOOV_BYTES:
                    return None
                f.seek(pos)
                data = f.read(size)
                return data if len(data) == size else None
            pos += size
    return None


def video_keyframes_ms(path: str) -> list[int] | None:
    """Presentation times (ms from file start) of the first video track's sync samples.

    Times are the raw sample-table timeline (dts + ctts), deliberately WITHOUT the edit list —
    Frigate's nginx runs `vod_ignore_edit_list on`, so that is the timeline nginx-vod cuts on.
    None when the file has no usable index (the caller then serves the whole file).
    """
    moov = _read_moov(path)
    if moov is None:
        return None
    for typ, ts, te in _iter_boxes(moov, 8, len(moov)):
        if typ != b"trak":
            continue
        mdia = _child(moov, ts, te, b"mdia")
        if not mdia:
            continue
        hdlr = _child(moov, *mdia, b"hdlr")
        if not hdlr or moov[hdlr[0] + 8 : hdlr[0] + 12] != b"vide":
            continue
        mdhd = _child(moov, *mdia, b"mdhd")
        minf = _child(moov, *mdia, b"minf")
        stbl = _child(moov, *minf, b"stbl") if minf else None
        if not mdhd or not stbl:
            return None
        version = moov[mdhd[0]]
        timescale = struct.unpack_from(">I", moov, mdhd[0] + (20 if version == 1 else 12))[0]
        if timescale <= 0:
            return None

        stss = _child(moov, *stbl, b"stss")
        stts = _child(moov, *stbl, b"stts")
        if not stss or not stts:
            # No stss means "every sample is a sync sample" — legal, but not what a camera
            # produces; don't guess, serve whole files.
            return None
        n_sync = struct.unpack_from(">I", moov, stss[0] + 4)[0]
        sync = [struct.unpack_from(">I", moov, stss[0] + 8 + 4 * i)[0] for i in range(n_sync)]

        # sample number (1-based) -> dts, via the stts runs
        runs = []
        n = struct.unpack_from(">I", moov, stts[0] + 4)[0]
        for i in range(n):
            count, delta = struct.unpack_from(">II", moov, stts[0] + 8 + 8 * i)
            runs.append((count, delta))

        ctts_runs = []
        ctts = _child(moov, *stbl, b"ctts")
        if ctts:
            cver = moov[ctts[0]]
            m = struct.unpack_from(">I", moov, ctts[0] + 4)[0]
            fmt = ">Ii" if cver == 1 else ">II"
            for i in range(m):
                ctts_runs.append(struct.unpack_from(fmt, moov, ctts[0] + 8 + 8 * i))

        def lookup(sample: int, table: list[tuple[int, int]], cumulative: bool) -> int:
            acc = 0
            remaining = sample - 1  # samples before this one
            for count, value in table:
                if remaining < count:
                    return acc + remaining * value if cumulative else value
                if cumulative:
                    acc += count * value
                remaining -= count
            return acc if cumulative else 0

        out = []
        for s in sync:
            dts = lookup(s, runs, cumulative=True)
            off = lookup(s, ctts_runs, cumulative=False) if ctts_runs else 0
            out.append(int((dts + off) * 1000 / timescale))
        out.sort()
        return out
    return None


def _index_one(path: str) -> tuple[tuple[str, int, float], list[int] | None] | None:
    """Index one file into the cache. Returns (key, value), or None if it cannot be stat'd."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (path, st.st_size, st.st_mtime)
    with _LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return key, _CACHE[key]
    try:
        value = video_keyframes_ms(path)
    except Exception:  # a malformed file must never take the manifest down
        logger.debug("hawksnest vod keyframes: could not index %s", path, exc_info=True)
        value = None
    with _LOCK:
        _CACHE[key] = value
        _CACHE.move_to_end(key)
        _INDEXED.add(path)
        while len(_CACHE) > _CACHE_MAX:
            old_key, _ = _CACHE.popitem(last=False)
            _INDEXED.discard(old_key[0])
    return key, value


def _index_siblings(path: str) -> None:
    """Index every not-yet-indexed .mp4 in path's directory concurrently, then return."""
    directory = os.path.dirname(path)
    try:
        names = os.listdir(directory)
    except OSError:
        return
    with _LOCK:
        todo = [
            p
            for p in (os.path.join(directory, n) for n in names if n.endswith(".mp4"))
            if p not in _INDEXED
        ]
    if path not in todo:
        todo.append(path)
    wait([_POOL.submit(_index_one, p) for p in todo], timeout=_BATCH_TIMEOUT_S)


def cached_keyframes_ms(path: str) -> list[int] | None:
    if path not in _INDEXED:
        _index_siblings(path)
    result = _index_one(path)  # a cache hit after the batch; re-validates size/mtime always
    return result[1] if result else None


def clip_key_frames(
    keyframes: list[int] | None, clip_from: int | None, duration: int
) -> tuple[list[int] | None, int]:
    """Clip-relative keyframe gaps and first offset. Port of upstream `_clip_key_frames`:
    a keyframe exactly on the clip end is excluded (it would declare a zero-length tail), and
    fewer than two keyframes returns (None, 0) — one cut point cannot split anything."""
    if not keyframes:
        return None, 0
    clip_start = clip_from if clip_from is not None else 0
    relative = [int(k) - clip_start for k in keyframes if clip_start <= k < clip_start + duration]
    if len(relative) < 2:
        return None, 0
    return [b - a for a, b in zip(relative, relative[1:])], relative[0]


def keyframe_fields(path: str, clip_from: int | None, duration: int) -> dict[str, Any]:
    """The keyframe keys for one nginx-vod clip. Stock 0.17.2 output on any failure."""
    try:
        gaps, first = clip_key_frames(cached_keyframes_ms(path), clip_from, duration)
    except Exception:
        gaps, first = None, 0
    if gaps is None:
        return {"keyFrameDurations": [duration]}
    fields: dict[str, Any] = {"keyFrameDurations": gaps}
    if first > 0:
        fields["firstKeyFrameOffset"] = first
    return fields
