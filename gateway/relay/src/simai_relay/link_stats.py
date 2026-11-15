"""카메라 → 노트북 구간 측정 (명세 12.7 누락, 3.9 촬영 시각 추정, 6.1).

카메라 한 대의 수신 상태와 두 가지 측정을 모은다.

- camera_to_gateway_gap: RTSP over TCP 라 손실이 번호 빈칸이 아니라 도착 간격의 끊김으로 나타난다(12.7). 같은
  stream_epoch 안 프레임 도착 간격(received_at_ms 차이)이 공칭 간격의 1.5배를 넘은 횟수와 최대 간격을 센다. 기준은
  녹화 manifest(gateway/recorder manifest.GAP_FACTOR, -9 측정 도구)와 같다. RTP 타임스탬프(source_pts) 간격이 같은
  기준을 넘은 횟수도 따로 센다(카메라가 아예 보내지 않은 프레임). laptop_heartbeat 의 camera_to_gateway_gap_max_ms 는
  직전 하트비트 이후 도착 간격의 최대값이다.
- receive_delay_ms: 수신 시각 − 추정 촬영 시각. RECEIVE_ESTIMATED 에서 추정은 창 안 가장 덜 늦게 온 프레임에 원점을
  맞추므로(capture_time.py), 이 값은 고정 지연(latency_ms) + 그 프레임이 기준보다 더 늦게 온 만큼이다. Wi-Fi 도착
  흔들림과 순간 정체를 프레임마다 보여 준다.

두 측정 모두 노트북 시계 하나로 잰다(노트북–Pod 시계 차이와 무관). 모르면 null(지어내지 않는다).
"""

from __future__ import annotations

import threading
from collections import deque
from fractions import Fraction
from typing import Any

from simai_recorder.manifest import GAP_FACTOR


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(q / 100 * len(s)))]


SOURCE_KINDS = ("RTSP", "FILE", "SYNTHETIC")  # laptop_heartbeat 1.3.0 cameras[].source_kind


class CameraLinkStats:
    """카메라 한 대의 수신 상태·측정. 수신 스레드가 쓰고 다른 스레드(하트비트·로그)가 읽으므로 잠근다."""

    def __init__(self, camera_id: str, delay_window: int = 250, *, source_kind: str | None = None) -> None:
        self.camera_id = camera_id
        # 중계가 연 입력 종류(RTSP / FILE / SYNTHETIC). 하트비트 cameras[].source_kind 로 싣는다(S15P31A101-724).
        # 모르는 값은 싣지 않는다(받는 쪽은 없으면 RTSP 로 본다).
        self.source_kind = source_kind if source_kind in SOURCE_KINDS else None
        self._lock = threading.Lock()
        self._connected = False
        self._streaming = False  # 이번 epoch 에 codec_info 와 키프레임을 보냈다
        self._epoch: int | None = None
        self._last_seq: int | None = None
        self._connects = 0
        self._open_failures = 0
        self._frames = 0
        self._dropped_before_keyframe = 0
        # 도착 간격 (epoch 안에서만 잰다. 재접속 공백은 끊김·재접속 기록으로 따로 본다)
        self._threshold_ms: float | None = None
        self._prev_received: int | None = None
        self._prev_pts: int | None = None
        self._tb_ms: Fraction | None = None
        self._gap_count = 0
        self._pts_gap_count = 0
        self._arrival_max: float | None = None
        self._arrival_last: float | None = None
        self._pts_interval_max: float | None = None
        self._window_arrival_max: float | None = None  # 직전 하트비트 이후
        self._delays: deque[float] = deque(maxlen=delay_window)
        self._delay_max: float | None = None

    # ------------------------------------------------------------------ 수신 루프가 부른다

    def on_open_failed(self) -> None:
        with self._lock:
            self._open_failures += 1
            self._connected = False

    def on_connected(self, epoch: int) -> None:
        with self._lock:
            self._connected = True
            self._streaming = False
            self._epoch = epoch
            self._last_seq = None
            self._connects += 1
            self._prev_received = self._prev_pts = None

    def on_stream_started(self, codec_info: dict[str, Any], dropped_before_keyframe: int) -> None:
        fps = codec_info.get("nominal_fps")
        tb = codec_info["pts_timebase"]
        with self._lock:
            self._streaming = True
            self._dropped_before_keyframe += dropped_before_keyframe
            self._threshold_ms = round(1000 / fps * GAP_FACTOR, 1) if fps else None
            self._tb_ms = Fraction(1, int(tb.split("/", 1)[1])) * 1000

    def on_frame(self, header: dict[str, Any]) -> None:
        received = header["received_at_ms"]
        pts = header["source_pts"]
        with self._lock:
            self._frames += 1
            self._last_seq = header["frame_sequence"]
            if self._prev_received is not None:
                iv = float(received - self._prev_received)
                self._arrival_last = iv
                self._arrival_max = iv if self._arrival_max is None else max(self._arrival_max, iv)
                self._window_arrival_max = iv if self._window_arrival_max is None else max(self._window_arrival_max, iv)
                if self._threshold_ms is not None and iv > self._threshold_ms:
                    self._gap_count += 1
            if pts is not None and self._prev_pts is not None and self._tb_ms is not None:
                piv = float((pts - self._prev_pts) * self._tb_ms)
                self._pts_interval_max = piv if self._pts_interval_max is None else max(self._pts_interval_max, piv)
                if self._threshold_ms is not None and piv > self._threshold_ms:
                    self._pts_gap_count += 1
            self._prev_received = received
            self._prev_pts = pts
            delay = float(received - header["estimated_capture_time_ms"])
            self._delays.append(delay)
            self._delay_max = delay if self._delay_max is None else max(self._delay_max, delay)

    def on_disconnected(self) -> None:
        with self._lock:
            self._connected = False
            self._streaming = False

    # ------------------------------------------------------------------ 읽기

    def heartbeat_entry(self, reset_window: bool = True) -> dict[str, Any]:
        """laptop_heartbeat payload.cameras[] 한 항목. 모르는 값은 넣지 않는다.

        camera_to_gateway_gap_max_ms 는 직전 호출 이후 도착 간격의 최대값이고, reset_window 면 창을 비운다.
        """
        with self._lock:
            entry: dict[str, Any] = {"camera_id": self.camera_id,
                                     "rtsp": "CONNECTED" if self._connected else "DISCONNECTED"}
            if self._epoch is not None:
                entry["stream_epoch"] = self._epoch
            if self._last_seq is not None:
                entry["last_frame_sequence"] = self._last_seq
            if self._window_arrival_max is not None:
                entry["camera_to_gateway_gap_max_ms"] = self._window_arrival_max
            if self.source_kind is not None:
                entry["source_kind"] = self.source_kind
            if reset_window:
                self._window_arrival_max = None
            return entry

    def snapshot(self) -> dict[str, Any]:
        """로그·점검용 전체 상태. 비밀 값은 들지 않는다."""
        with self._lock:
            delays = list(self._delays)
            return {
                "camera_id": self.camera_id,
                "rtsp": "CONNECTED" if self._connected else "DISCONNECTED",
                "streaming": self._streaming,
                "stream_epoch": self._epoch,
                "last_frame_sequence": self._last_seq,
                "connects": self._connects,
                "open_failures": self._open_failures,
                "frames": self._frames,
                "dropped_before_keyframe": self._dropped_before_keyframe,
                "camera_to_gateway_gap": {
                    "threshold_ms": self._threshold_ms,
                    "count": self._gap_count,
                    "max_ms": self._arrival_max,
                    "arrival_interval_ms_last": self._arrival_last,
                    "pts_gap_count": self._pts_gap_count,
                    "pts_interval_max_ms": self._pts_interval_max,
                },
                "receive_delay_ms": {
                    "last": delays[-1] if delays else None,
                    "p50": _pct(delays, 50),
                    "p95": _pct(delays, 95),
                    "max": self._delay_max,
                },
            }
