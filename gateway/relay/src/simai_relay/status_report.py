"""중계 상태를 같은 노트북의 현장 경보 단말에 1초마다 알린다 (loopback UDP, S15P31A101-91).

단말(gateway/site_alert)의 장애 상태 기계가 '노트북 → RunPod 영상 단절'(관제 중단)을 판단하고 laptop_heartbeat 의
tier0.video_cameras·cameras[] 를 채우는 데 쓴다. 형식은 단말 쪽 `simai_site_alert.relay_status` 하나에 있고 여기서는 그 함수로
만든다(중복 구현하지 않음).

- 127.0.0.1 로만 보낸다. 비밀 값(카메라 계정·기기 토큰)은 싣지 않는다. camera_id 와 연결 상태·순번·도착 간격뿐이다.
- 받는 쪽이 없어도(단말이 꺼져 있어도) 중계는 계속 돈다. 보내기 실패는 무시한다(UDP).
- 이 보고가 3초 넘게 끊기면 단말은 영상 상태를 모름으로 보고 '관제 중단'을 표시한다(정상으로 보지 않음).
"""

from __future__ import annotations

import logging
import socket
import threading
from collections.abc import Callable
from typing import Any

from simai_site_alert.relay_status import DEFAULT_PORT, build_report, new_boot_id

log = logging.getLogger("simai_relay.status")

__all__ = ["DEFAULT_PORT", "RelayStatusPublisher"]


class RelayStatusPublisher:
    """cameras(): laptop_heartbeat.cameras[] 항목들, video_cameras(): Tier 0 영상 연결이 살아 있는 카메라."""

    def __init__(
        self,
        cameras: Callable[[], list[dict[str, Any]]],
        video_cameras: Callable[[], list[str]],
        *,
        port: int = DEFAULT_PORT,
        interval_s: float = 1.0,
    ) -> None:
        self._cameras = cameras
        self._video = video_cameras
        self._addr = ("127.0.0.1", port)
        self._interval_s = interval_s
        self._boot = new_boot_id()
        self._seq = 0
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._thread: threading.Thread | None = None
        self.sent = 0

    def publish_once(self) -> bool:
        cameras = self._cameras()
        data = build_report(boot=self._boot, seq=self._seq, camera_count=len(cameras),
                            video_cameras=self._video(), cameras=cameras)
        self._seq += 1
        try:
            self._sock.sendto(data, self._addr)
        except OSError as e:  # 받는 쪽이 없으면 Windows 는 다음 송신에서 연결 재설정 오류를 낸다. 중계는 계속 돈다
            log.debug("중계 상태 보고 실패 (%s)", type(e).__name__)
            return False
        self.sent += 1
        return True

    def start(self) -> None:
        def run() -> None:
            while not self._stop.is_set():
                try:
                    self.publish_once()
                except Exception as e:  # noqa: BLE001 - 상태 보고 오류로 중계를 멈추지 않는다
                    log.error("중계 상태 보고 오류 (%s)", type(e).__name__)
                self._stop.wait(self._interval_s)

        self._thread = threading.Thread(target=run, name="relay-status", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._sock.close()
