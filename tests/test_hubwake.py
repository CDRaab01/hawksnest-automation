#!/usr/bin/env python3
"""Behaviour tests for go2rtc's Home Hub wake wrapper (kustomize/base/go2rtc/hubwake.py).

WHY THIS EXISTS. The wrapper relays the hub's RTP stream into go2rtc by parsing RFC 2326
interleaved frames itself. Its first version dropped a frame header that happened to straddle two
TCP reads, which misaligned every later frame; go2rtc was fed garbage until its container was
OOMKilled (2026-10-26) — and that container also carries Ring two-way talk. A TCP read boundary
can fall anywhere, so the parser is exercised here with the same stream cut at every offset.

WHAT IT RUNS. The EXACT script shipped in the built manifest's go2rtc-hubwake ConfigMap, not a
copy that can drift. It is executed as a module (not __main__), so nothing opens a socket.
Nothing here touches a cluster, the hub, or a camera.

Usage:
  python3 tests/test_hubwake.py [BUILT_MANIFEST]   # default /tmp/prod.yaml
"""

from __future__ import annotations

import os
import random
import sys

try:
    import yaml
except ImportError:  # pragma: no cover - guidance only
    sys.exit("PyYAML is required: pip install pyyaml")


def load_module(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        docs = [d for d in yaml.safe_load_all(f) if isinstance(d, dict)]
    cm = next((d for d in docs if d.get("kind") == "ConfigMap"
               and d["metadata"]["name"].startswith("go2rtc-hubwake")), None)
    if cm is None:
        sys.exit(f"no go2rtc-hubwake ConfigMap in {path}")
    os.environ.update(REOLINK_IP_HOME_HUB="192.0.2.1", REOLINK_HUB_USER="u", REOLINK_HUB_PASS="p")
    sys.argv = ["hubwake.py", "1", "sub", "rtsp://127.0.0.1:8554/test"]
    mod: dict = {"__name__": "hubwake"}
    exec(compile(cm["data"]["hubwake.py"], "hubwake.py", "exec"), mod)
    return mod


def frame(channel: int, payload: bytes) -> bytes:
    return b"$" + bytes([channel]) + len(payload).to_bytes(2, "big") + payload


def main() -> int:
    hw = load_module(sys.argv[1] if len(sys.argv) > 1 else "/tmp/prod.yaml")
    split = hw["split_frames"]
    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    rng = random.Random(7)
    frames = [frame(rng.choice([0, 1, 2, 3]), rng.randbytes(rng.randint(0, 1500))) for _ in range(60)]
    # Payload bytes that look like a frame start or a reply must not confuse the parser.
    frames.insert(5, frame(0, b"$\x00\x05\xdcRTSP/1.0 200 OK\r\n\r\n"))
    reply = b"RTSP/1.0 200 OK\r\nCSeq: 9\r\nSession: 4B3B1A99\r\nContent-Length: 4\r\n\r\nabcd"
    bad_reply = b"RTSP/1.0 454 Session Not Found\r\nCSeq: 10\r\n\r\n"
    stream = b"".join(frames[:20]) + reply + b"".join(frames[20:40]) + bad_reply + b"".join(frames[40:])
    want = b"".join(frames)

    # 1. The whole stream at once.
    out, rest, statuses = split(stream, 2)
    check(out == want and rest == b"", "whole stream: forwarded bytes differ from the frames")
    check(statuses == ["RTSP/1.0 200 OK", "RTSP/1.0 454 Session Not Found"],
          f"whole stream: inline replies not reported, got {statuses}")

    # 2. Cut into two reads at EVERY offset — the bug was a header split across reads.
    bad_cut = None
    for cut in range(1, len(stream)):
        try:
            o1, r1, _ = split(stream[:cut], 2)
            o2, r2, _ = split(r1 + stream[cut:], 2)
        except ConnectionError:
            o1 = o2 = r2 = None
        if o1 is None or o1 + o2 != want or r2:
            bad_cut = cut
            break
    check(bad_cut is None, f"two reads cut at byte {bad_cut}: frames lost or misaligned")

    # 3. Many small random reads, as a socket delivers them.
    buf, got = b"", b""
    i = 0
    try:
        while i < len(stream):
            n = rng.randint(1, 300)
            o, buf, _ = split(buf + stream[i:i + n], 2)
            got += o
            i += n
    except ConnectionError as e:
        got = None
        failures.append(f"random small reads: parser lost framing ({e})")
    check(got is None or (got == want and buf == b""), "random small reads: frames lost or misaligned")

    # 4. Garbage must raise, never be guessed past or forwarded.
    for junk, name in ((b"\x80\x60\x12\x34garbage", "a stray RTP byte"),
                       (frame(7, b"x"), "an interleaved channel the hub never set up")):
        try:
            split(junk, 2)
            failures.append(f"{name} was accepted instead of raising")
        except ConnectionError:
            pass

    # 5. A stream that never completes must not be buffered without bound.
    try:
        split(b"RTSP/1.0 200 OK\r\n" + b"x" * (hw["MAX_BUFFER"] + 1), 2)
        failures.append("an unterminated reply was buffered past MAX_BUFFER")
    except ConnectionError:
        pass

    # 6. The SDP announced to go2rtc keeps both tracks, renumbers their controls to the SETUP
    #    URLs the relay uses, and drops the hub's server-only session lines.
    hub_sdp = ("v=0\r\ns=Session streamed by \"preview\"\r\nt=0 0\r\na=tool:Reolink\r\na=control:*\r\n"
               "a=range:npt=now-\r\nm=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\na=control:track1\r\n"
               "m=audio 0 RTP/AVP 97\r\na=rtpmap:97 MPEG4-GENERIC/16000\r\na=recvonly\r\n"
               "a=control:track2\r\n")
    sdp = hw["publish_sdp"](hub_sdp)
    lines = sdp.split("\r\n")
    check(lines.count("a=control:trackID=0") == 1 and lines.count("a=control:trackID=1") == 1,
          f"publish SDP controls wrong:\n{sdp}")
    check(lines.index("a=control:trackID=0") > lines.index("m=video 0 RTP/AVP 96")
          and lines.index("a=control:trackID=1") > lines.index("m=audio 0 RTP/AVP 97"),
          "publish SDP: a track's control is not inside its own media section")
    check(not any(l.startswith(("a=control:track1", "a=range", "a=tool", "a=recvonly")) for l in lines),
          f"publish SDP kept hub-only lines:\n{sdp}")

    # 7. SPS/PPS are lifted from the opening frames and declared on the video track. Without them
    #    viewers sat ~28 s on "non-existing PPS 0 referenced" in production (2026-10-26); the hub's
    #    SDP declares none. Real values from backyard_patio's sub stream.
    import base64
    sps = base64.b64decode("Z2QAM6wVFKBgDehAAAE9gAAlNQE=")
    pps = base64.b64decode("aO48sA==")
    rtp_header = bytes([0x80, 96, 0, 1]) + bytes(8)
    single = frame(2, rtp_header + b"\x00" * 20) + frame(0, rtp_header + sps) + frame(0, rtp_header + pps)
    stap = frame(0, rtp_header + bytes([24]) + len(sps).to_bytes(2, "big") + sps
                 + len(pps).to_bytes(2, "big") + pps)
    for data, name in ((single, "single-NAL packets"), (stap, "a STAP-A packet")):
        check(hw["h264_parameter_sets"](data) == (sps, pps), f"SPS/PPS not found in {name}")
    check(hw["h264_parameter_sets"](frame(0, rtp_header + b"\x41" + bytes(30))) is None,
          "parameter sets invented from a frame that carries none")
    sdp = hw["publish_sdp"](hub_sdp, (sps, pps))
    check("a=fmtp:96 packetization-mode=1;profile-level-id=640033;"
          "sprop-parameter-sets=Z2QAM6wVFKBgDehAAAE9gAAlNQE=,aO48sA==" in sdp.split("\r\n"),
          f"publish SDP does not declare the parameter sets on the video track:\n{sdp}")

    if failures:
        print(f"FAIL — {len(failures)} hubwake check(s) failed:\n")
        for f in failures:
            print(f"  ✗ {f}")
        return 1
    print("OK — hubwake relay parser and publish SDP behave (7 checks).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
