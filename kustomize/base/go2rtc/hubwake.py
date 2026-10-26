"""go2rtc `exec:` source for a battery camera behind the Reolink Home Hub.

    exec:python3 /hubwake/hubwake.py <channel> <main|sub> {output}#starttimeout=50

<channel> is the hub's 0-BASED channel (GetChannelstatus numbering; the RTSP path is 1-based).
go2rtc substitutes {output} with an RTSP URL on its own loopback server; this script publishes
the camera there by relaying the hub's RTP packets byte for byte (no ffmpeg, no transcode).

WHY THIS EXISTS (measured 2026-10-25/26 from inside this pod, docs/adding-a-camera.md): an RTSP
session opened while the camera SLEEPS wakes it — the hub holds DESCRIBE ~3-5 s while it does —
and then never receives a media packet: 0 of 4 single-session trials after a real sleep, 60-75 s
of silence, while the camera fell back asleep with the session still open. That was the whole of
Hawksnest's 36 s+ cold open on `front`. A FRESH session opened once the camera is awake gets
video within ~0.6-3 s — provided the waking session was ended with a real TEARDOWN: killed
without one, the hub keeps it to its 30 s session timeout and often feeds nobody else until then.

  1. if asleep: wake with a throwaway session, poll the hub's GetChannelstatus every 250 ms until
     sleep == 0, TEARDOWN it, settle SETTLE_S (a session opened the instant after a TEARDOWN
     often stalls);
  2. open the PROBE session and wait for real video on it; if none within FIRST_PACKET_S,
     TEARDOWN and open a fresh one (re-waking if the camera slept again), inside BUDGET_S.
     Nothing reaches go2rtc during this phase, so go2rtc never sees a producer that stalls;
  3. announce the hub's own SDP to {output} and relay the probe session's interleaved packets,
     starting with everything buffered during step 2 — so go2rtc gets the camera's opening
     keyframe at once instead of waiting a GOP for the next one.

Why not ffmpeg for step 3 (it was, briefly): it costs a SECOND hub session after the probe plus
its own stream analysis, ~6 s later to first frame (12 s vs ~7 s); and a stalled ffmpeg that has
already connected to {output} can only be replaced by exiting — go2rtc treats it as the producer,
and replacing it from in here made go2rtc launch a second copy of this script.

One process, no children: when go2rtc kills it (last viewer gone), the kernel closes the hub
socket, so nothing can be left holding the camera awake (the orphaned-ffmpeg battery drain, #175).
Credentials come from the pod env (REOLINK_HUB_*, the go2rtc-credentials secret).
"""
import base64
import hashlib
import json
import os
import re
import signal
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request

CH, KIND, OUT = int(sys.argv[1]), sys.argv[2], sys.argv[3]
IP = os.environ["REOLINK_IP_HOME_HUB"]
USER, PW = os.environ["REOLINK_HUB_USER"], os.environ["REOLINK_HUB_PASS"]
URI = f"rtsp://{IP}:554/h264Preview_{CH + 1:02d}_{KIND}"

BUDGET_S = 45        # under go2rtc's #starttimeout=50, so go2rtc sees our exit, not its timeout
FIRST_PACKET_S = 6   # a healthy fresh session delivers video in ~0.6-3 s
SETTLE_S = 1.5       # the gap between TEARDOWN and the next session that measured reliable
POLL_S = 0.25
VIDEO_BYTES = 20000  # 52-byte keepalives arrive on a wedged session; video is ~60 KB/s
KEEPALIVE_S = 15     # the hub's SETUP says timeout=30
MAX_BUFFER = 2_000_000  # a parse that stops making progress must fail, never grow (OOM guard)

T0 = time.time()


def log(msg):
    print(f"hubwake ch{CH} +{time.time() - T0:4.1f}s {msg}", file=sys.stderr, flush=True)


def asleep():
    """The hub's own sleep flag for this channel; None if the API did not answer."""
    req = urllib.request.Request(
        f"http://{IP}/cgi-bin/api.cgi?cmd=GetChannelstatus&user={USER}&password={PW}",
        data=b'[{"cmd":"GetChannelstatus","action":0,"param":{}}]')
    try:
        status = json.load(urllib.request.urlopen(req, timeout=3))[0]["value"]["status"]
        return next(c["sleep"] for c in status if c["channel"] == CH) == 1
    except Exception as e:  # noqa: BLE001 - any failure means "unknown", handled by the caller
        log(f"GetChannelstatus failed: {type(e).__name__}")
        return None


class Rtsp:
    """Just enough RTSP over TCP: requests (optionally Digest-authenticated) and interleaved data."""

    def __init__(self, host, port, user=None, pw=None):
        self.sock = socket.create_connection((host, port), timeout=15)
        self.user, self.pw = user, pw
        self.cseq, self.auth, self.session, self.buf = 0, None, None, b""

    def _authz(self, method, uri):
        if not self.auth:
            return ""
        realm, nonce = self.auth
        ha1 = hashlib.md5(f"{self.user}:{realm}:{self.pw}".encode()).hexdigest()
        ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
        resp = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
        return (f'Authorization: Digest username="{self.user}", realm="{realm}", '
                f'nonce="{nonce}", uri="{uri}", response="{resp}"\r\n')

    def send(self, method, uri, extra="", body=""):
        self.cseq += 1
        sess = f"Session: {self.session}\r\n" if self.session else ""
        length = f"Content-Length: {len(body.encode())}\r\n" if body else ""
        self.sock.sendall((f"{method} {uri} RTSP/1.0\r\nCSeq: {self.cseq}\r\n{self._authz(method, uri)}"
                           f"{sess}{extra}{length}User-Agent: hubwake\r\n\r\n{body}").encode())

    def _response(self):
        """Next RTSP response, keeping any interleaved data that arrived first in self.buf."""
        while True:
            i = self.buf.find(b"RTSP/1.0")
            if i >= 0 and b"\r\n\r\n" in self.buf[i:]:
                break
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("RTSP peer closed the connection")
            self.buf += chunk
        head, rest = self.buf[i:].split(b"\r\n\r\n", 1)
        head = head.decode(errors="replace")
        m = re.search(r"Content-Length:\s*(\d+)", head, re.I)
        n = int(m.group(1)) if m else 0
        while len(rest) < n:
            rest += self.sock.recv(65536)
        self.buf = self.buf[:i] + rest[n:]
        return head, rest[:n].decode(errors="replace")

    def request(self, method, uri, extra="", body=""):
        for _ in range(2):
            self.send(method, uri, extra, body)
            head, resp_body = self._response()
            status = head.split("\r\n")[0]
            if " 401 " in status and self.user and not self.auth:
                w = re.search(r'Digest[^\r\n]*realm="([^"]+)"[^\r\n]*nonce="([^"]+)"', head, re.I)
                if not w:
                    raise ConnectionError("hub asked for an auth scheme other than Digest")
                self.auth = (w.group(1), w.group(2))
                continue
            if " 200 " not in status:
                raise ConnectionError(f"{method} -> {status}")
            m = re.search(r"Session:\s*([^;\r\n]+)", head, re.I)
            if m and not self.session:
                self.session = m.group(1).strip()
            return head, resp_body
        raise ConnectionError(f"{method} -> authentication failed")

    def close(self):
        try:
            if self.session:
                self.send("TEARDOWN", self.uri)
        except OSError:
            pass
        self.sock.close()


class HubSession(Rtsp):
    def __init__(self):
        super().__init__(IP, 554, USER, PW)
        self.uri = URI
        self.sdp = ""

    def start(self):
        """DESCRIBE (the hub holds this while a sleeping camera wakes), SETUP every track, PLAY."""
        _, self.sdp = self.request("DESCRIBE", self.uri, "Accept: application/sdp\r\n")
        controls = re.findall(r"^a=control:(\S+)", self.sdp, re.M)
        for n, ctl in enumerate(c for c in controls if c != "*"):
            track = ctl if ctl.startswith("rtsp") else f"{self.uri}/{ctl}"
            self.request("SETUP", track,
                         f"Transport: RTP/AVP/TCP;unicast;interleaved={2 * n}-{2 * n + 1}\r\n")
        self.request("PLAY", self.uri, "Range: npt=0.000-\r\n")

    def video_within(self, secs):
        """True once real media has arrived (kept in self.buf for the relay), within secs."""
        self.sock.settimeout(0.5)
        end = time.time() + secs
        while time.time() < end:
            if len(self.buf) > VIDEO_BYTES:
                return True
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                return False
            self.buf += chunk
        return len(self.buf) > VIDEO_BYTES


def wake():
    """Wake the camera with a throwaway session; return once the hub says it is up."""
    w = None
    try:
        w = HubSession()
        w.start()
    except (OSError, ConnectionError) as e:
        log(f"wake session: {e}; polling anyway")
    while time.time() - T0 < BUDGET_S:
        state = asleep()
        if state is False:
            break
        if state is None:          # API down: fall back to the measured typical wake time
            time.sleep(5)
            break
        time.sleep(POLL_S)
    log("camera awake")
    if w:
        w.close()
    time.sleep(SETTLE_S)


def h264_parameter_sets(data):
    """(SPS, PPS) NAL units from the first video frames in `data` (interleaved channel 0), or None.

    The camera repeats SPS+PPS+IDR in-band every 2 s, but the hub's SDP declares NO
    sprop-parameter-sets. A bare rtsp:// source let go2rtc catch them itself from the first
    packet; announced without them, viewers sat on `non-existing PPS 0 referenced` for ~28 s
    (production, 2026-10-26). So they are lifted from the buffered opening and declared.
    """
    sps = pps = None
    try:
        frames, _, _ = split_frames(data, 2)
    except ConnectionError:
        return None
    i = 0
    while i + 4 <= len(frames) and not (sps and pps):
        ch, n = frames[i + 1], int.from_bytes(frames[i + 2:i + 4], "big")
        rtp = frames[i + 4:i + 4 + n]
        i += 4 + n
        if ch != 0 or len(rtp) < 13:
            continue
        payload = rtp[12 + 4 * (rtp[0] & 0x0F):]          # skip the RTP header + CSRCs
        nals = [payload]
        if payload and payload[0] & 0x1F == 24:             # STAP-A: several NALs in one packet
            nals, j = [], 1
            while j + 2 <= len(payload):
                size = int.from_bytes(payload[j:j + 2], "big")
                nals.append(payload[j + 2:j + 2 + size])
                j += 2 + size
        for nal in nals:
            kind = nal[0] & 0x1F if nal else 0
            sps = nal if kind == 7 and not sps else sps
            pps = nal if kind == 8 and not pps else pps
    return (sps, pps) if sps and pps else None


def publish_sdp(hub_sdp, params=None):
    """The hub's SDP, minus the session-level lines that only describe the hub's own server, with
    the H.264 parameter sets declared on the video track when we have them."""
    keep, video_pt = [], None
    for line in hub_sdp.replace("\r\n", "\n").split("\n"):
        if line.startswith(("a=control:", "a=range:", "a=recvonly", "a=type:", "a=x-qt", "a=tool:")):
            continue
        if params and video_pt and line.startswith(f"a=fmtp:{video_pt} "):
            continue                                       # replaced by ours below
        if line:
            keep.append(line)
        if line.startswith("m="):
            keep.append(f"a=control:trackID={sum(1 for k in keep if k.startswith('m=')) - 1}")
        m = re.match(r"a=rtpmap:(\d+) H264/", line)
        if m and params:
            video_pt = m.group(1)
            sps, pps = params
            keep.append(f"a=fmtp:{video_pt} packetization-mode=1;profile-level-id={sps[1:4].hex()};"
                        f"sprop-parameter-sets={base64.b64encode(sps).decode()},"
                        f"{base64.b64encode(pps).decode()}")
    return "\r\n".join(keep) + "\r\n"


def split_frames(buf, tracks):
    """Split the hub's TCP stream into (complete $-frames to forward, unparsed rest, statuses).

    The stream is RTP/RTCP in RFC 2326 interleaved frames ($, channel, 16-bit length) with the
    hub's replies to our keepalives inline between them. A PARTIAL frame header or reply is not
    junk — it stays in `rest` until more bytes arrive. Treating one as junk misaligned every later
    frame and fed go2rtc garbage until its container was OOMKilled (2026-10-26), so anything that
    is neither now raises instead of guessing, and `rest` can never grow past MAX_BUFFER.
    """
    out, statuses = bytearray(), []
    while buf:
        if buf[0] == 0x24:
            if len(buf) < 4:
                break
            if buf[1] >= 2 * tracks:
                raise ConnectionError(f"lost RTP framing (channel {buf[1]})")
            size = 4 + int.from_bytes(buf[2:4], "big")
            if len(buf) < size:
                break
            out += buf[:size]
            buf = buf[size:]
        elif buf.startswith(b"RTSP/") or b"RTSP/".startswith(bytes(buf[:5])):
            end = buf.find(b"\r\n\r\n")
            if end < 0:
                break
            m = re.search(rb"Content-Length:\s*(\d+)", buf[:end], re.I)
            skip = end + 4 + (int(m.group(1)) if m else 0)
            if len(buf) < skip:
                break
            statuses.append(bytes(buf[:buf.find(b"\r\n")]).decode(errors="replace"))
            buf = buf[skip:]
        else:
            raise ConnectionError(f"lost RTP framing (byte {buf[0]:#x})")
    if len(buf) > MAX_BUFFER:
        raise ConnectionError("hub stream did not parse; refusing to buffer more")
    return bytes(out), buf, statuses


def relay(hub, tracks):
    """Publish to go2rtc and copy the hub's interleaved frames until either side goes away."""
    out = urllib.parse.urlsplit(OUT)
    pub = Rtsp(out.hostname, out.port or 554)
    pub.uri = OUT
    params = h264_parameter_sets(hub.buf)
    log("SPS/PPS found in the opening frames" if params else "no SPS/PPS in the opening frames")
    pub.request("ANNOUNCE", OUT, "Content-Type: application/sdp\r\n", publish_sdp(hub.sdp, params))
    for n in range(tracks):
        pub.request("SETUP", f"{OUT}/trackID={n}",
                    f"Transport: RTP/AVP/TCP;unicast;interleaved={2 * n}-{2 * n + 1};mode=record\r\n")
    pub.request("RECORD", OUT, "Range: npt=0.000-\r\n")
    log("publishing to go2rtc")

    closed = threading.Event()

    def drain_go2rtc():            # go2rtc may send RTCP; reading it is also how we see it leave
        # go2rtc sends a publisher NOTHING in normal operation, so this read must block forever:
        # with the connect timeout still set, the silence read as "go2rtc left" after exactly
        # 15 s and every live view was cut and restarted (measured 2026-10-26).
        pub.sock.settimeout(None)
        try:
            while pub.sock.recv(65536):
                pass
        except OSError:
            pass
        closed.set()

    threading.Thread(target=drain_go2rtc, daemon=True).start()
    hub.sock.settimeout(1)
    buf, next_keepalive = hub.buf, time.time() + KEEPALIVE_S
    hub.buf = b""
    while not closed.is_set():
        if time.time() > next_keepalive:
            hub.send("GET_PARAMETER", hub.uri)      # its 200 arrives inline; dropped below
            next_keepalive = time.time() + KEEPALIVE_S
        out_bytes, buf, statuses = split_frames(buf, tracks)
        for status in statuses:
            if " 200 " not in status:
                log(f"hub answered the keepalive with: {status}")
        if out_bytes:
            try:
                pub.sock.sendall(out_bytes)
            except OSError as e:
                log(f"go2rtc write failed: {e}")
                break
        try:
            chunk = hub.sock.recv(65536)
        except socket.timeout:
            continue
        except OSError:
            chunk = b""
        if not chunk:
            log("hub ended the session")     # its 5-minute cap, or the camera dropped
            break
        buf += chunk
    if closed.is_set():
        log("go2rtc closed the publish connection")
    pub.sock.close()


def main():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    state = asleep()
    log(f"start, asleep={state}")
    if state is not False:
        wake()
    n, hub = 0, None
    while time.time() - T0 < BUDGET_S:
        n += 1
        try:
            hub = HubSession()
            hub.start()
            if hub.video_within(FIRST_PACKET_S):
                log(f"video on session {n}")
                break
        except (OSError, ConnectionError) as e:
            log(f"session {n}: {e}")
        if hub:
            hub.close()
            hub = None
        state = asleep()
        log(f"session {n} gave no video, asleep={state}")
        if state:
            wake()
        else:
            time.sleep(SETTLE_S)
    if hub is None:
        log("gave up")
        return 1
    tracks = len([c for c in re.findall(r"^a=control:(\S+)", hub.sdp, re.M) if c != "*"])
    try:
        relay(hub, tracks)
    except (OSError, ConnectionError) as e:
        log(f"relay stopped: {e}")
        return 1
    finally:
        hub.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
