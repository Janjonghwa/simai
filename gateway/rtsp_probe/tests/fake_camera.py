"""시험용 RTSP 카메라 흉내 (RTSP over TCP interleaved, H.264 RTP, RTCP Sender Report).

실제 FFmpeg RTSP 클라이언트(PyAV, gateway/recorder 의 RtspSource)가 접속해 받도록 최소한만 구현한다.
영상은 시험 안에서 libx264 로 만든 막대 무늬다(사람 이미지 없음, 파일로 남기지 않음).

장면 조절:
- send_rtcp / sr_every / sr_jitter_ms: RTCP SR 을 보낼지, 몇 프레임마다, 카메라 시각에 얹을 흔들림(±ms, seed 고정)
- drop_frames: 보내지 않을 프레임 번호(카메라가 건너뛴 프레임 → PTS 간격 빈칸)
- drop_fragments: 일부 RTP 패킷만 빼고 보낼 프레임 번호(패킷 손실, 프레임 일부만 도착)
- rtp_ts: 프레임 번호 → RTP 타임스탬프(90 kHz) 함수(PTS 역행 장면)
- skip_keyframes: 키프레임(IDR) 접근 단위를 보내지 않는다(키프레임 없는 스트림)
- max_sessions: 동시에 PLAY 할 수 있는 세션 수(C110 은 2). 넘으면 SETUP 에 453 으로 거절
- auth: "basic" / "digest" / None
"""

from __future__ import annotations

import base64
import hashlib
import random
import socket
import struct
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from fractions import Fraction

import av

NTP_UNIX_OFFSET = 2208988800
MAX_PAYLOAD = 1200


def encode_frames(count: int, fps: int = 25, gop: int = 25, size: tuple[int, int] = (160, 96)) -> list[tuple[bytes, bool]]:
    """막대 무늬 H.264 접근 단위(Annex B) 목록 [(바이트, 키프레임)]. B 프레임 없음."""
    enc = av.CodecContext.create("libx264", "w")
    enc.width, enc.height = size
    enc.pix_fmt = "yuv420p"
    enc.time_base = Fraction(1, fps)
    enc.framerate = Fraction(fps)
    enc.options = {"g": str(gop), "keyint_min": str(gop), "bf": "0", "preset": "ultrafast", "tune": "zerolatency",
                   "sc_threshold": "0", "repeat-headers": "1"}
    enc.open()
    out: list[tuple[bytes, bool]] = []
    w, h = size
    frames = []
    for i in range(count):
        frame = av.VideoFrame(w, h, "yuv420p")
        row = bytearray([30]) * frame.planes[0].line_size
        x = (i * 3) % w
        row[x : x + 8] = bytes([200]) * len(row[x : x + 8])
        frame.planes[0].update(bytes(row) * h)
        for plane in frame.planes[1:]:
            plane.update(bytes([128]) * plane.buffer_size)
        frame.pts = i
        frames.append(frame)
    for frame in [*frames, None]:
        for p in enc.encode(frame):
            out.append((bytes(p), bool(p.is_keyframe)))
    return out


def split_nals(data: bytes) -> list[bytes]:
    out, i = [], data.find(b"\x00\x00\x01")
    while i >= 0:
        j = data.find(b"\x00\x00\x01", i + 3)
        end = len(data) if j < 0 else (j - 1 if data[j - 1] == 0 else j)
        if end > i + 3:
            out.append(data[i + 3 : end])
        i = j
    return out


def _parameter_sets(frames: Iterable[tuple[bytes, bool]]) -> tuple[bytes, bytes]:
    for data, _ in frames:
        nals = split_nals(data)
        sps = next((n for n in nals if n[0] & 0x1F == 7), None)
        pps = next((n for n in nals if n[0] & 0x1F == 8), None)
        if sps and pps:
            return sps, pps
    raise ValueError("SPS/PPS 가 없다")


@dataclass
class CameraScene:
    frames: list[tuple[bytes, bool]]
    fps: float = 25.0
    pace: float = 1.0  # 1 이면 실시간, 0.5 면 두 배 빠르게 보낸다(RTP 시각은 그대로)
    send_rtcp: bool = True
    sr_every: int = 5
    sr_jitter_ms: float = 0.0
    camera_clock_offset_s: float = 0.0  # 카메라 시계가 실제 시각에서 벗어난 정도
    drop_frames: set[int] = field(default_factory=set)
    drop_fragments: set[int] = field(default_factory=set)
    rtp_ts: Callable[[int], int] | None = None
    skip_keyframes: bool = False
    seed: int = 0


class FakeRtspCamera:
    def __init__(self, scene: CameraScene, user: str = "", password: str = "", auth: str | None = "digest",
                 max_sessions: int = 2, path: str = "stream1") -> None:
        self.scene = scene
        self.user, self.password, self.auth = user, password, auth
        self.max_sessions = max_sessions
        self.path = path
        self.requests: list[str] = []
        self.playing = 0
        self.rejected = 0
        self._lock = threading.Lock()
        self._sps, self._pps = _parameter_sets(scene.frames)
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen()
        self.port = self._sock.getsockname()[1]
        self._closed = False
        threading.Thread(target=self._accept, daemon=True).start()

    # ------------------------------------------------------------------ 서버
    def close(self) -> None:
        self._closed = True
        self._sock.close()

    def __enter__(self) -> FakeRtspCamera:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _accept(self) -> None:
        while not self._closed:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, conn: socket.socket) -> None:
        state = {"session": None, "playing": False, "nonce": hashlib.md5(str(random.random()).encode()).hexdigest()}
        send_lock = threading.Lock()
        stop = threading.Event()
        buf = b""
        try:
            conn.settimeout(30)
            while not stop.is_set():
                while True:
                    if buf[:1] == b"$":
                        if len(buf) >= 4:
                            n = struct.unpack("!H", buf[2:4])[0]
                            if len(buf) >= 4 + n:
                                buf = buf[4 + n :]  # 클라이언트 RTCP RR 은 버린다
                                continue
                    elif b"\r\n\r\n" in buf:
                        break
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    buf += chunk
                head, buf = buf.split(b"\r\n\r\n", 1)
                text = head.decode("utf-8", "replace")
                self.requests.append(text)
                self._handle(conn, text, state, send_lock, stop)
        except OSError:
            pass
        finally:
            stop.set()
            if state["playing"]:
                with self._lock:
                    self.playing -= 1
            try:
                conn.close()
            except OSError:
                pass

    def _authorized(self, method: str, headers: dict[str, str], state: dict) -> bool:
        if self.auth is None:
            return True
        got = headers.get("authorization", "")
        if self.auth == "basic":
            return got == "Basic " + base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        if not got.startswith("Digest "):
            return False
        fields = {}
        for part in got[7:].split(","):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                fields[k] = v.strip('"')
        ha1 = hashlib.md5(f"{self.user}:cam:{self.password}".encode()).hexdigest()
        ha2 = hashlib.md5(f"{method}:{fields.get('uri', '')}".encode()).hexdigest()
        return fields.get("response") == hashlib.md5(f"{ha1}:{state['nonce']}:{ha2}".encode()).hexdigest()

    def _reply(self, conn: socket.socket, lock: threading.Lock, cseq: str, status: str,
               headers: dict[str, str] | None = None, body: str = "") -> None:
        h = {"CSeq": cseq, **(headers or {})}
        if body:
            h["Content-Length"] = str(len(body.encode()))
        msg = f"RTSP/1.0 {status}\r\n" + "".join(f"{k}: {v}\r\n" for k, v in h.items()) + "\r\n" + body
        with lock:
            conn.sendall(msg.encode())

    def _handle(self, conn: socket.socket, text: str, state: dict, lock: threading.Lock,
                stop: threading.Event) -> None:
        lines = text.split("\r\n")
        method, url = lines[0].split(" ")[:2]
        headers = {}
        for ln in lines[1:]:
            if ":" in ln:
                k, v = ln.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        cseq = headers.get("cseq", "0")
        if method == "OPTIONS":
            self._reply(conn, lock, cseq, "200 OK",
                        {"Public": "OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN, GET_PARAMETER"})
            return
        if not self._authorized(method, headers, state):
            www = ('Basic realm="cam"' if self.auth == "basic"
                   else f'Digest realm="cam", nonce="{state["nonce"]}"')
            self._reply(conn, lock, cseq, "401 Unauthorized", {"WWW-Authenticate": www})
            return
        base = f"rtsp://127.0.0.1:{self.port}/{self.path}"
        if method == "DESCRIBE":
            sprop = f"{base64.b64encode(self._sps).decode()},{base64.b64encode(self._pps).decode()}"
            sdp = ("v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=simai fake camera\r\nt=0 0\r\n"
                   "m=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\n"
                   f"a=fmtp:96 packetization-mode=1;sprop-parameter-sets={sprop}\r\n"
                   f"a=framerate:{self.scene.fps:g}\r\na=control:track1\r\n")
            self._reply(conn, lock, cseq, "200 OK", {"Content-Base": base + "/", "Content-Type": "application/sdp"}, sdp)
        elif method == "SETUP":
            with self._lock:
                if self.playing >= self.max_sessions:
                    self.rejected += 1
                    full = True
                else:
                    self.playing += 1
                    full = False
            if full:
                self._reply(conn, lock, cseq, "453 Not Enough Bandwidth")
                return
            state["playing"] = True
            state["session"] = f"{random.randrange(10**8):08d}"
            self._reply(conn, lock, cseq, "200 OK", {"Transport": "RTP/AVP/TCP;unicast;interleaved=0-1",
                                                     "Session": f"{state['session']};timeout=60"})
        elif method == "PLAY":
            self._reply(conn, lock, cseq, "200 OK", {"Session": state["session"] or "", "Range": "npt=0.000-"})
            threading.Thread(target=self._stream, args=(conn, lock, stop), daemon=True).start()
        elif method == "GET_PARAMETER":
            self._reply(conn, lock, cseq, "200 OK", {"Session": state["session"] or ""})
        elif method == "TEARDOWN":
            self._reply(conn, lock, cseq, "200 OK")
            stop.set()
        else:
            self._reply(conn, lock, cseq, "405 Method Not Allowed")

    # ------------------------------------------------------------------ RTP/RTCP 송신
    def _stream(self, conn: socket.socket, lock: threading.Lock, stop: threading.Event) -> None:
        sc = self.scene
        rng = random.Random(sc.seed)
        ssrc, seq, base_ts = 0x51A1, 1000, 123456
        packets = octets = 0
        t0_mono, t0_wall = time.monotonic(), time.time()
        ts_of = sc.rtp_ts or (lambda i: round(i * 90000 / sc.fps))

        def send(channel: int, payload: bytes) -> None:
            with lock:
                conn.sendall(b"$" + struct.pack("!BH", channel, len(payload)) + payload)

        try:
            for i, (data, key) in enumerate(sc.frames):
                if stop.is_set():
                    return
                due = t0_mono + i / sc.fps * sc.pace
                if (wait := due - time.monotonic()) > 0:
                    time.sleep(wait)
                ts = (base_ts + ts_of(i)) & 0xFFFFFFFF
                if sc.send_rtcp and i % sc.sr_every == 0:
                    # 카메라 시각 = 이 프레임의 실제 촬영 시각(+시계 오프셋, +흔들림). 같은 시계면 RTP 와 일정하게 대응한다.
                    cam = t0_wall + ts_of(i) / 90000 + sc.camera_clock_offset_s
                    if sc.sr_jitter_ms:
                        cam += rng.uniform(-sc.sr_jitter_ms, sc.sr_jitter_ms) / 1000
                    ntp = cam + NTP_UNIX_OFFSET
                    msw = int(ntp)
                    lsw = int((ntp - msw) * 2**32) & 0xFFFFFFFF
                    sr = struct.pack("!BBHIIIIII", 0x80, 200, 6, ssrc, msw, lsw, ts, packets, octets)
                    send(1, sr)
                if i in sc.drop_frames or (sc.skip_keyframes and key):
                    continue
                nals = split_nals(data)
                rtp_payloads: list[bytes] = []
                for nal in nals:
                    if len(nal) <= MAX_PAYLOAD:
                        rtp_payloads.append(nal)
                        continue
                    hdr, body = nal[0], nal[1:]
                    chunks = [body[k : k + MAX_PAYLOAD] for k in range(0, len(body), MAX_PAYLOAD)]
                    for c, chunk in enumerate(chunks):
                        fu_ind = (hdr & 0xE0) | 28
                        fu_hdr = (0x80 if c == 0 else 0) | (0x40 if c == len(chunks) - 1 else 0) | (hdr & 0x1F)
                        rtp_payloads.append(bytes([fu_ind, fu_hdr]) + chunk)
                for k, payload in enumerate(rtp_payloads):
                    marker = 0x80 if k == len(rtp_payloads) - 1 else 0
                    pkt = struct.pack("!BBHII", 0x80, marker | 96, seq & 0xFFFF, ts, ssrc) + payload
                    seq += 1
                    if i in sc.drop_fragments and 0 < k == len(rtp_payloads) - 1:
                        continue  # 마지막 조각을 잃는다(번호는 건너뜀)
                    send(0, pkt)
                    packets += 1
                    octets += len(payload)
        except OSError:
            return
        # 영상이 끝나면 연결을 끊는다(실제 카메라의 스트림 끊김)
        stop.set()
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
