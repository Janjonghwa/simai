"""G0 전송 비교 측정: 카메라별 TLS 연결 vs 단일 연결 다중화 (명세 12.7 "한 카메라의 지연이 다른 카메라를 막지 않도록",
15.1 G0, 부록 D-G0-01·D-G0-10·D-G0-11). 이 컴퓨터 안(127.0.0.1)에서만 돈다. 결과를 제품 성능으로 쓰지 않는다.

    uv run simai-relay bench-transport --seconds 20                       # 두 방식, cam_A 수신 5초 멈춤
    uv run simai-relay bench-transport --mode per-camera --slow-camera none --seconds 1800 --json soak.json   # 30분 연속

- 송신: 카메라마다 합성 프레임(25fps, GOP 마다 키프레임, 크기 고정 — 영상 내용 없음)을 만든다. 헤더는 I0 frame_header 형식.
  - per-camera: 제품 코드 `TlsCameraSink` 그대로(카메라별 TLS 연결, 송신 큐 상한, 다음 키프레임까지 버림).
  - mux: 비교용 단일 TLS 연결. 큐 하나(상한 = 카메라 수 × 카메라별 상한)에 네 카메라를 섞고, 넘치면 그 카메라만 다음
    키프레임까지 버린다. 이 측정 안에만 있다.
- 수신: 이 모듈의 TLS 수신기. 느린 카메라(--slow-camera)의 프레임을 받는 동안 수신을 멈추거나(stall) 느리게(slow) 한다.
  카메라별 연결이면 그 카메라 연결만 멈추고, 다중화면 그 연결 하나를 읽는 쪽이 멈추므로 모든 카메라가 기다린다.
- 지표(카메라별): 보낸 프레임, 받은 프레임, 송신 큐에 넣은 시각 → 수신 시각 지연(p50·p95·p99·max), 최대 수신 간격,
  노트북 skipped(큐 넘침), dropped_link(연결 없음), 수신 쪽 frame_sequence 빈칸.

인증서를 주지 않으면 임시 폴더에 자체 서명 인증서를 만든다(cryptography, 개발 의존성). 측정이 끝나면 지운다.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import socket
import ssl
import sys
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from simai_site_alert.pinning import PinnedPeerVerifier, pinned_client_context

from simai_relay import wire
from simai_relay.sink import I0CodecInfo, I0Frame
from simai_relay.tls_sink import TlsCameraSink, VideoTarget

BENCH_TOKEN = "bench-device-token"  # 이 컴퓨터 안 측정용(비밀 값 아님)


# ---------------------------------------------------------------------------- 인증서


@dataclass(frozen=True)
class SelfSigned:
    cert_file: Path
    key_file: Path
    fingerprint: str  # sha256:<hex>, SubjectPublicKeyInfo DER 의 SHA-256


def make_self_signed(directory: Path, name: str = "tier0") -> SelfSigned:
    """시험·측정용 자체 서명 인증서(EC P-256, 1일). 저장소에 두지 않는다."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"{name}-test")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=1)).sign(key, hashes.SHA256()))
    directory.mkdir(parents=True, exist_ok=True)
    cert_file, key_file = directory / f"{name}.crt", directory / f"{name}.key"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    spki = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return SelfSigned(cert_file, key_file, "sha256:" + hashlib.sha256(spki).hexdigest())


# ---------------------------------------------------------------------------- 합성 프레임


def codec_info(camera_id: str, epoch: int = 0, fps: float = 25.0) -> I0CodecInfo:
    return I0CodecInfo({"camera_id": camera_id, "stream_epoch": epoch, "codec": "H264", "profile": "High",
                        "sps": "Z2QAKqw0yAoA9sBEAAADAAQAAAMA8jxgykA=", "pps": "aO48sA==", "pts_timebase": "1/90000",
                        "width": 2304, "height": 1296, "nominal_fps": fps})


def frame(camera_id: str, seq: int, keyframe: bool, data: bytes, *, epoch: int = 0, fps: float = 25.0) -> I0Frame:
    now = time.time_ns() // 1_000_000
    return I0Frame({"camera_id": camera_id, "stream_epoch": epoch, "frame_sequence": seq,
                    "source_pts": round(seq * 90000 / fps), "pts_timebase": "1/90000",
                    "estimated_capture_time_ms": now, "timestamp_source": "RECEIVE_ESTIMATED",
                    "timestamp_uncertainty_ms": 20, "keyframe": keyframe, "received_at_ms": now}, data)


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return round(s[min(len(s) - 1, int(q / 100 * len(s)))], 1)


# ---------------------------------------------------------------------------- 수신기


@dataclass
class SlowPlan:
    camera_id: str | None = "cam_A"
    mode: str = "stall"  # stall: 창 시작에서 한 번 for_s 동안 읽지 않음, slow: 창 안에서 프레임마다 slow_ms 쉼
    at_s: float = 5.0
    for_s: float = 5.0
    slow_ms: float = 200.0


class BenchReceiver:
    """이 컴퓨터 안 TLS 수신기. HELLO 토큰 확인 → HELLO_ACK → 레코드를 읽어 카메라별로 기록한다."""

    def __init__(self, cert: SelfSigned, slow: SlowPlan, sent_at: dict[tuple[str, int], float]) -> None:
        self._ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._ctx.load_cert_chain(cert.cert_file, cert.key_file)
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        self._slow = slow
        self._sent_at = sent_at
        self._t0 = time.perf_counter()
        self._stalled: set[str] = set()
        self._stop = threading.Event()
        self.latency_ms: dict[str, list[float]] = {}
        self.received: dict[str, int] = {}
        self.max_interval_ms: dict[str, float] = {}
        self.seq_gaps: dict[str, int] = {}
        self._last_at: dict[str, float] = {}
        self._last_seq: dict[str, int] = {}
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        t = threading.Thread(target=self._accept, daemon=True)
        t.start()
        self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        self._sock.close()

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except (TimeoutError, OSError):
                continue
            t = threading.Thread(target=self._handle, args=(conn,), daemon=True)
            t.start()
            self._threads.append(t)

    def _maybe_slow(self, camera_id: str) -> None:
        plan = self._slow
        if plan.camera_id != camera_id:
            return
        t = time.perf_counter() - self._t0
        if not plan.at_s <= t < plan.at_s + plan.for_s:
            return
        if plan.mode == "stall":
            if camera_id not in self._stalled:
                self._stalled.add(camera_id)
                time.sleep(plan.at_s + plan.for_s - t)
        else:
            time.sleep(plan.slow_ms / 1000)

    def _handle(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(None)
            with self._ctx.wrap_socket(conn, server_side=True) as tls:
                hello = wire.read_record(tls, max_meta=wire.MAX_HELLO_META, max_data=0)
                if hello is None or hello[0] != wire.HELLO or hello[1].get("device_token") != BENCH_TOKEN:
                    tls.sendall(wire.encode(wire.HELLO_ACK, {"ok": False, "reason": "unauthorized"}))
                    return
                tls.sendall(wire.encode(wire.HELLO_ACK, {"ok": True}))
                while not self._stop.is_set():
                    rec = wire.read_record(tls)
                    if rec is None:
                        return
                    kind, meta, _ = rec
                    if kind != wire.FRAME:
                        continue
                    now = time.perf_counter()
                    cam, seq = meta["camera_id"], meta["frame_sequence"]
                    sent = self._sent_at.pop((cam, seq), None)
                    if sent is not None:
                        self.latency_ms.setdefault(cam, []).append((now - sent) * 1000)
                    self.received[cam] = self.received.get(cam, 0) + 1
                    if cam in self._last_at:
                        iv = (now - self._last_at[cam]) * 1000
                        self.max_interval_ms[cam] = max(self.max_interval_ms.get(cam, 0.0), iv)
                    if cam in self._last_seq and seq > self._last_seq[cam] + 1:
                        self.seq_gaps[cam] = self.seq_gaps.get(cam, 0) + seq - self._last_seq[cam] - 1
                    self._last_at[cam], self._last_seq[cam] = now, seq
                    self._maybe_slow(cam)
        except (OSError, ssl.SSLError, wire.WireError):
            return


# ---------------------------------------------------------------------------- 비교용 단일 연결 다중화


class MuxSender:
    """비교용: 네 카메라를 TLS 연결 하나에 섞어 보낸다. 큐 하나, 카메라별로 넘치면 다음 키프레임까지 버림."""

    def __init__(self, target: VideoTarget, cameras: list[str], max_frames: int, sndbuf: int = 64 * 1024) -> None:
        # 제품 송신과 같은 서버 확인(공개키 고정)을 거친 뒤에만 HELLO 를 보낸다
        verifier = PinnedPeerVerifier(target.spki_sha256, frozenset({target.spki_sha256}))
        raw = socket.create_connection((target.host, target.port))
        raw.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, sndbuf)
        raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._tls = pinned_client_context().wrap_socket(raw, server_hostname=None)
        verifier(self._tls)
        self._tls.sendall(wire.encode(wire.HELLO, {"protocol": wire.PROTOCOL, "camera_id": "mux",
                                                   "laptop_id": "bench", "device_token": BENCH_TOKEN}))
        wire.read_record(self._tls, max_meta=wire.MAX_HELLO_META, max_data=0)
        self.max_frames = max_frames
        self._cond = threading.Condition()
        self._items: deque[bytes] = deque()
        self._frames = 0
        self._skipping = dict.fromkeys(cameras, False)
        self._need_key = dict.fromkeys(cameras, True)
        self.skipped = dict.fromkeys(cameras, 0)
        self._closed = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def send_codec_info(self, record: I0CodecInfo) -> None:
        with self._cond:
            self._items.append(wire.encode(wire.CODEC_INFO, record.codec_info))
            self._cond.notify()

    def send_frame(self, record: I0Frame) -> None:
        cam, key = record.header["camera_id"], record.header["keyframe"]
        with self._cond:
            if self._need_key[cam] and not key:
                return
            self._need_key[cam] = False
            if self._skipping[cam]:
                if not key or self._frames >= self.max_frames:
                    self.skipped[cam] += 1
                    return
                self._skipping[cam] = False
            elif self._frames >= self.max_frames:
                self._skipping[cam] = True
                self.skipped[cam] += 1
                return
            self._items.append(wire.encode(wire.FRAME, record.header, record.data))
            self._frames += 1
            self._cond.notify()

    def _run(self) -> None:
        try:
            while True:
                with self._cond:
                    while not self._items and not self._closed:
                        self._cond.wait(0.2)
                    if self._closed:
                        return
                    payload = self._items.popleft()
                    if payload[0] == wire.FRAME:
                        self._frames -= 1
                self._tls.sendall(payload)
        except OSError:
            return

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()
        try:
            socket.socket.shutdown(self._tls, socket.SHUT_RDWR)
        except OSError:
            pass
        self._thread.join(5)
        self._tls.close()


# ---------------------------------------------------------------------------- 실행


@dataclass
class BenchConfig:
    seconds: float = 20.0
    cameras: int = 4
    fps: float = 25.0
    gop: int = 50
    key_bytes: int = 60_000
    p_bytes: int = 8_000
    queue_frames: int = 25
    slow: SlowPlan | None = None


def run_mode(mode: str, cfg: BenchConfig, cert: SelfSigned) -> dict[str, Any]:
    cams = [f"cam_{chr(ord('A') + i)}" for i in range(cfg.cameras)]
    sent_at: dict[tuple[str, int], float] = {}
    receiver = BenchReceiver(cert, cfg.slow or SlowPlan(camera_id=None), sent_at)
    receiver.start()
    target = VideoTarget("127.0.0.1", receiver.port, cert.fingerprint)
    pins = frozenset({cert.fingerprint})
    if mode == "per-camera":
        sinks: dict[str, Any] = {c: TlsCameraSink(c, lambda: target, device_token=BENCH_TOKEN, pins=pins,
                                                  laptop_id="bench", max_queue_frames=cfg.queue_frames)
                                 for c in cams}
        deadline = time.monotonic() + 10
        while not all(s.connected for s in sinks.values()) and time.monotonic() < deadline:
            time.sleep(0.01)
    else:
        mux = MuxSender(target, cams, cfg.queue_frames * cfg.cameras)
        sinks = dict.fromkeys(cams, mux)
    key, p = b"\x00" * cfg.key_bytes, b"\x00" * cfg.p_bytes
    generated = dict.fromkeys(cams, 0)
    stop = threading.Event()

    def produce(cam: str) -> None:
        sink = sinks[cam]
        sink.send_codec_info(codec_info(cam, fps=cfg.fps))
        t0 = time.perf_counter()
        i = 0
        while not stop.is_set():
            wait = t0 + i / cfg.fps - time.perf_counter()
            if wait > 0:
                time.sleep(wait)
            if time.perf_counter() - t0 >= cfg.seconds:
                return
            is_key = i % cfg.gop == 0
            record = frame(cam, i, is_key, key if is_key else p, fps=cfg.fps)
            sent_at[(cam, i)] = time.perf_counter()
            sink.send_frame(record)
            generated[cam] += 1
            i += 1

    threads = [threading.Thread(target=produce, args=(c,), daemon=True) for c in cams]
    for t in threads:
        t.start()
    for t in threads:
        t.join(cfg.seconds + 30)
    time.sleep(min(2.0, (cfg.slow.for_s if cfg.slow else 0) + 1.0))  # 큐에 남은 프레임이 도착할 시간
    stop.set()
    result: dict[str, Any] = {"mode": mode, "cameras": {}}
    for c in cams:
        lat = receiver.latency_ms.get(c, [])
        if mode == "per-camera":
            snap = sinks[c].snapshot()
            skipped, dropped = snap["skipped"], snap["dropped_link"]
        else:
            skipped, dropped = sinks[c].skipped[c], None
        result["cameras"][c] = {
            "generated": generated[c], "received": receiver.received.get(c, 0),
            "latency_ms": {"p50": _pct(lat, 50), "p95": _pct(lat, 95), "p99": _pct(lat, 99),
                           "max": round(max(lat), 1) if lat else None},
            "max_receive_interval_ms": round(receiver.max_interval_ms.get(c, 0.0), 1),
            "laptop_skipped": skipped, "dropped_link": dropped, "receiver_sequence_gaps": receiver.seq_gaps.get(c, 0),
        }
    for s in {id(s): s for s in sinks.values()}.values():
        s.close()
    receiver.stop()
    return result


def _table(results: list[dict[str, Any]]) -> str:
    lines = ["| 방식 | 카메라 | 보냄 | 받음 | 지연 p50 | p95 | p99 | max | 최대 수신 간격 | skipped | 수신 빈칸 |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        for c, m in r["cameras"].items():
            lat = m["latency_ms"]
            lines.append(f"| {r['mode']} | {c} | {m['generated']} | {m['received']} | {lat['p50']} | {lat['p95']} | "
                         f"{lat['p99']} | {lat['max']} | {m['max_receive_interval_ms']} | {m['laptop_skipped']} | "
                         f"{m['receiver_sequence_gaps']} |")
    return "\n".join(lines)


def add_parser(sub: Any) -> None:
    b = sub.add_parser("bench-transport", help="G0: 카메라별 TLS 연결 vs 단일 연결 다중화 지연·차단 비교 (로컬)")
    b.add_argument("--mode", choices=["both", "per-camera", "mux"], default="both")
    b.add_argument("--seconds", type=float, default=20.0, help="방식마다 송신 시간 (30분 연속은 1800)")
    b.add_argument("--cameras", type=int, default=4)
    b.add_argument("--fps", type=float, default=25.0)
    b.add_argument("--gop", type=int, default=50, help="키프레임 간격(프레임)")
    b.add_argument("--key-bytes", type=int, default=60_000)
    b.add_argument("--p-bytes", type=int, default=8_000)
    b.add_argument("--queue-frames", type=int, default=25, help="카메라별 송신 큐 상한(프레임)")
    b.add_argument("--slow-camera", default="cam_A", help="수신이 멈추는 카메라 (none 이면 없음)")
    b.add_argument("--slow-mode", choices=["stall", "slow"], default="stall")
    b.add_argument("--slow-at", type=float, default=5.0)
    b.add_argument("--slow-for", type=float, default=5.0)
    b.add_argument("--slow-ms", type=float, default=200.0)
    b.add_argument("--cert", help="TLS 인증서 PEM (없으면 임시 자체 서명)")
    b.add_argument("--key", help="TLS 비밀 키 PEM")
    b.add_argument("--json", help="결과 JSON 을 쓸 경로")


def cmd_bench(args: argparse.Namespace) -> int:
    slow = None if args.slow_camera == "none" else SlowPlan(args.slow_camera, args.slow_mode, args.slow_at,
                                                             args.slow_for, args.slow_ms)
    cfg = BenchConfig(args.seconds, args.cameras, args.fps, args.gop, args.key_bytes, args.p_bytes, args.queue_frames,
                      slow)
    modes = ["per-camera", "mux"] if args.mode == "both" else [args.mode]
    with tempfile.TemporaryDirectory() as tmp:
        if args.cert and args.key:
            from simai_site_alert.pinning import spki_sha256

            pem = Path(args.cert).read_text(encoding="ascii")
            cert = SelfSigned(Path(args.cert), Path(args.key), spki_sha256(ssl.PEM_cert_to_DER_cert(pem)))
        else:
            cert = make_self_signed(Path(tmp))
        results = [run_mode(m, cfg, cert) for m in modes]
    out = {"config": {**{k: v for k, v in vars(cfg).items() if k != "slow"},
                      "slow": vars(slow) if slow else None}, "results": results}
    print(_table(results))
    if args.json:
        Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    sys.stdout.flush()
    return 0
