"""영상 중계 → 현장 경보 단말 상태 보고 (같은 노트북 안, loopback UDP) (S15P31A101-91).

영상 중계(gateway/relay)와 현장 경보 단말은 따로 도는 프로세스다. 노트북 장애 판단(link_status)에는 영상 중계의
Tier 0 영상 연결 상태가, laptop_heartbeat 에는 카메라별 RTSP 상태(cameras[])와 영상 연결이 살아 있는 카메라
(tier0.video_cameras)가 필요하다. 그래서 중계가 1초마다 이 형식의 데이터그램 하나를 127.0.0.1 로 보내고, 단말이 받는다.

- 노트북 안 내부 형식이다(파트 사이 계약이 아니다). 보내는 쪽(build_report)과 받는 쪽(parse_report)이 이 파일 하나에 있다.
  relay 는 simai-site-alert 를 경로 의존으로 쓰므로 같은 함수로 만든다.
- 받는 쪽 단조 시계로 잰다. 3회 연속 오지 않으면(link_status.timeout_s) 영상 상태를 모름으로 본다(정상으로 보지 않음).
- 127.0.0.1 에만 묶는다. 비밀 값은 싣지 않는다(카메라 계정·기기 토큰 없음).
- 형식이 틀린 데이터그램은 통째로 버린다. 같은 중계 프로세스(boot)의 낮은 seq 는 늦게 온 것으로 보고 버린다.
"""

from __future__ import annotations

import json
import logging
import math
import re
import socket
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from simai_site_alert.link_status import RelayReport

log = logging.getLogger(__name__)

KIND = "simai-relay-status/1"
DEFAULT_PORT = 47031
MAX_DATAGRAM = 65_507
MAX_CAMERAS = 64
_CAMERA_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_LINK = ("CONNECTED", "DISCONNECTED")


def _is_uint(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def new_boot_id() -> str:
    return uuid.uuid4().hex


def build_report(
    *, boot: str, seq: int, camera_count: int, video_cameras: Iterable[str], cameras: Iterable[Mapping[str, Any]]
) -> bytes:
    """중계가 보낼 데이터그램. cameras 는 laptop_heartbeat.cameras[] 항목(relay link_stats.heartbeat_entry)."""
    doc = {
        "kind": KIND,
        "boot": boot,
        "seq": seq,
        "camera_count": camera_count,
        "video_cameras": sorted(set(video_cameras)),
        "cameras": [dict(c) for c in cameras],
    }
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _camera_entry(raw: Any) -> dict[str, Any] | None:
    """laptop_heartbeat.cameras[] 항목만 남긴다. 필수 값이 틀리면 None(그 보고를 버린다)."""
    if not isinstance(raw, Mapping):
        return None
    cam, rtsp = raw.get("camera_id"), raw.get("rtsp")
    if not isinstance(cam, str) or not _CAMERA_ID.match(cam) or rtsp not in _LINK:
        return None
    entry: dict[str, Any] = {"camera_id": cam, "rtsp": rtsp}
    for key in ("stream_epoch", "last_frame_sequence"):
        if key in raw:
            if not _is_uint(raw[key]):
                return None
            entry[key] = raw[key]
    if "camera_to_gateway_gap_max_ms" in raw:
        gap = raw["camera_to_gateway_gap_max_ms"]
        if isinstance(gap, bool) or not isinstance(gap, (int, float)) or not math.isfinite(gap) or gap < 0:
            return None
        entry["camera_to_gateway_gap_max_ms"] = gap
    return entry


def parse_report(data: bytes) -> tuple[str, int, RelayReport] | None:
    """데이터그램 → (boot, seq, RelayReport). 형식이 틀리면 None."""
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("kind") != KIND:
        return None
    boot, seq, count = doc.get("boot"), doc.get("seq"), doc.get("camera_count")
    video, cameras = doc.get("video_cameras"), doc.get("cameras")
    if not isinstance(boot, str) or not 0 < len(boot) <= 64 or not _is_uint(seq):
        return None
    if not _is_uint(count) or count > MAX_CAMERAS:
        return None
    if not isinstance(video, list) or len(video) > MAX_CAMERAS or len(set(map(str, video))) != len(video):
        return None
    if not all(isinstance(c, str) and _CAMERA_ID.match(c) for c in video):
        return None
    if not isinstance(cameras, list) or len(cameras) > MAX_CAMERAS:
        return None
    entries = [_camera_entry(c) for c in cameras]
    if any(e is None for e in entries) or len({e["camera_id"] for e in entries if e}) != len(entries):
        return None
    return boot, seq, RelayReport(tuple(sorted(video)), count, tuple(e for e in entries if e is not None))


class RelayStatusReceiver:
    """127.0.0.1:port 로 오는 중계 상태를 받아 on_report(report, 받은 단조 시각)을 부른다. 스레드 하나."""

    def __init__(
        self,
        on_report: Callable[[RelayReport, float], None],
        *,
        port: int = DEFAULT_PORT,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._on_report = on_report
        self._monotonic = monotonic
        self._stop = threading.Event()
        self._last: tuple[str, int] | None = None
        self.accepted = 0
        self.dropped = 0
        self.error: str | None = None
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self._sock.bind(("127.0.0.1", port))
        except OSError:
            self._sock.close()
            raise
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._run, name="relay-status", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(MAX_DATAGRAM)
            except TimeoutError:
                continue
            except OSError as e:  # 닫는 중이거나(Windows 는 ICMP 로 recvfrom 오류가 날 수 있다) 계속 받는다
                if self._stop.is_set():
                    break
                log.debug("중계 상태 수신 오류 (%s)", type(e).__name__)
                continue
            now = self._monotonic()
            if addr[0] != "127.0.0.1":
                self.dropped += 1
                continue
            parsed = parse_report(data)
            if parsed is None:
                self.dropped += 1
                continue
            boot, seq, report = parsed
            if self._last is not None and self._last[0] == boot and seq <= self._last[1]:
                self.dropped += 1  # 같은 중계의 늦게 온 보고
                continue
            self._last = (boot, seq)
            self.accepted += 1
            try:
                self._on_report(report, now)
            except Exception:  # noqa: BLE001 - 상태 반영 오류로 수신을 멈추지 않는다
                log.exception("중계 상태 반영 실패")

    def close(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        finally:
            if self._thread.is_alive():
                self._thread.join(timeout=2)
