"""Tier 0 LIVE 입력 조립: I0 영상 수신 → (링 버퍼) + 디코딩 → 최신성 대기열 → 검출 → 카메라별 추적 (명세 5.1, 12.7, 3.9).

판정·추정 로직은 기능 패키지와 기존 모듈에 있다. 여기서는 잇기만 한다.

    VideoIngestServer(카메라별 TLS, 계약 검증, 손실·노트북 버림 집계)   video_ingest.py (-83)
      └─ I0Bridge.on_codec_info/on_frame: 계약 dict → I0CodecInfo/I0Frame
           ├─ 클립 링 버퍼(EventClipService.receive, 압축 그대로, -57)
           └─ I0Ingest.receive → CameraIngest(epoch·코덱 변경·키프레임 대기·디코딩 실패) → 디코더(pyav_decoder)
                → LatestFrameScheduler(latest-frame-wins, 대기 상한, -21)
    판정 루프 tick: DetectorFeed.poll → I0Ingest.next_batch → 검출기 → ResultGate → MultiCameraLocalTracker → observation_2d

규칙(기존 모듈 규칙을 그대로 따른다):
- 촬영 시각은 프레임 헤더 값만 쓴다. 헤더가 계약과 다르면(촬영 시각이 없으면) video_ingest 가 연결을 끊고 넘기지 않는다.
  디코더 출력은 넣을 때의 열쇠(stream_epoch, frame_sequence)로만 헤더와 묶는다(지어내지 않음).
- 연결이 끊기면(on_disconnected) 그 카메라의 정렬 대기·디코더 안·추론 대기열 프레임을 버리고(STREAM_RESET) 디코더를 다시
  초기화해 키프레임을 기다린다(CameraIngest.connection_lost). 같은 epoch 로 다시 붙으면 번호·손실 기록은 이어 가고, 새 연결의
  codec_info·키프레임이 와야 다시 관측을 낸다. 끊긴 동안 빠진 번호는 손실(gateway_to_runpod_loss)이다.
- I0Ingest 의 보간 창은 0 이다: video_ingest 가 TCP 순서를 지키고 지난 번호를 이미 버리므로(out_of_order) 기다려도 채워질
  번호가 없다. 기다리면 손실 뒤 키프레임의 추론만 늦어진다.
- 카메라 상태(R7·health): 영상 연결이 있고, 그 연결에서 코덱 정보·키프레임이 도착했고(video_ingest CameraStream.ready),
  디코더가 키프레임을 디코딩한(I0Ingest) 카메라만 연결로 본다. 연결은 있는데 아직 아니면 health 의 status 는
  WAITING_KEYFRAME 이다(12.7 관측 없음).
- 시계: LIVE 촬영 시각은 노트북 시계다. I0 제어 채널(시계 교환)이 없으면 service 가 시계 차이를 모름으로 둔다(SAME_CLOCK 아님).

health 집계(cameras[].frames, 보고 구간 = 그 수신자의 직전 health 이후): 세 손실 값과 노트북 버림·구분 불가를 섞지 않는다.
- received: Tier 0 가 받은 FRAME 기록(계약 검증 통과) 전부
- gateway_to_runpod_loss·laptop_skipped·unattributed_gap: video_ingest 의 번호 빈칸 집계(SKIP_NOTICE 반영). I0Ingest 의
  NETWORK_LOSS 는 노트북 버림을 구분하지 못해 health 에 싣지 않는다.
- skipped_by_policy: Tier 0 가 정책으로 버린 프레임 = video_ingest 의 코덱 정보 전·키프레임 전·끝난 epoch FRAME +
  I0Ingest 의 SKIPPED_BY_POLICY(밀림·대기 상한·키프레임 대기·스트림 재설정·디코더 정체 등, LATE_ARRIVAL 제외)
- late_dropped: video_ingest 의 지난 번호(out_of_order) + I0Ingest 의 LATE_ARRIVAL
디코딩 실패·검출기 오류는 계약에 자리가 없어 status()(표시 파일·로그)에만 싣는다.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from services.tier0.video_ingest import VideoConsumer, VideoIngestServer
from simai_ai.tracking.stage_timing import StageTiming
from simai_ai.io import I0Ingest, I0Message
from simai_ai.io.messages import codec_info_from_contract, frame_from_contract

log = logging.getLogger("services.tier0.live_input")

FRAME_FIELDS = ("received", "gateway_to_runpod_loss", "skipped_by_policy", "laptop_skipped", "unattributed_gap",
                "late_dropped")


class I0Bridge:
    """video_ingest 의 소비자: 받은 기록을 클립 링 버퍼와 I0Ingest 에 넘긴다. 카메라별 연결 스레드에서 불린다.

    한쪽 오류가 다른 쪽을 막지 않는다(링 버퍼가 실패해도 디코딩은 하고, 그 반대도 같다). 오류는 카메라별로 센다.
    """

    def __init__(self, ingest: I0Ingest) -> None:
        self._ingest = ingest
        self.timing = StageTiming()
        self._clips: Callable[[I0Message], None] | None = None
        self._lock = threading.Lock()  # 집계만
        self.errors: dict[str, dict[str, int]] = {c: {"ingest": 0, "clips": 0} for c in ingest.camera_ids}
        self.disconnects: dict[str, int] = dict.fromkeys(ingest.camera_ids, 0)

    def attach_clips(self, receive: Callable[[I0Message], None]) -> None:
        """클립 링 버퍼 입력. 영상 수신을 시작하기 전에 붙인다."""
        self._clips = receive

    def _count(self, camera_id: str, where: str, error: Exception) -> None:
        with self._lock:
            self.errors.setdefault(camera_id, {"ingest": 0, "clips": 0})[where] += 1
            n = self.errors[camera_id][where]
        if n == 1 or n % 100 == 0:
            log.warning("%s %s 오류 %d회째 (%s: %s)", camera_id, where, n, type(error).__name__, error)

    def _deliver(self, message: I0Message, camera_id: str, started: float | None = None) -> None:
        if self._clips is not None:
            try:
                self._clips(message)
            except Exception as e:  # noqa: BLE001 - 링 버퍼 오류가 디코딩 경로를 막지 않는다
                self._count(camera_id, "clips", e)
        if started is not None:
            # 수신 처리(I0 기록 → 메시지 변환 → 클립 링 버퍼). 소켓 읽기·TLS 는 넣지 않는다(-662). 디코딩은 아래 구간
            self.timing.record("frame_receive", None, (time.perf_counter() - started) * 1000)
        try:
            with self.timing.measure("frame_receive_decode"):
                self._ingest.receive(message)
        except Exception as e:  # noqa: BLE001 - 다음 기록은 계속 받는다(CameraIngest 가 스스로 복구한다)
            self._count(camera_id, "ingest", e)

    def on_codec_info(self, codec_info: Mapping[str, Any]) -> None:
        self._deliver(codec_info_from_contract(codec_info), codec_info["camera_id"])

    def on_frame(self, header: Mapping[str, Any], data: bytes) -> None:
        started = time.perf_counter()
        self._deliver(frame_from_contract(header, data), header["camera_id"], started)

    def on_disconnected(self, camera_id: str) -> None:
        """연결 끝: 다음 연결의 코덱 정보·키프레임까지 관측 없음(대기 프레임·준비 상태 무효화, 번호·손실 기록은 유지).

        video_ingest 는 같은 카메라의 다음 연결을 이 호출이 끝난 뒤 시작한다(카메라별 lock)."""
        with self._lock:
            self.disconnects[camera_id] = self.disconnects.get(camera_id, 0) + 1
        try:
            self._ingest.connection_lost(camera_id)
        except Exception as e:  # noqa: BLE001
            self._count(camera_id, "ingest", e)


class LiveInput:
    """LIVE 입력 한 묶음: 영상 수신 서버 + I0Ingest + 브리지. start·stop 은 서비스 기동·종료 순서에서 부른다."""

    def __init__(self, ingest: I0Ingest, server_factory: Callable[[VideoConsumer], VideoIngestServer]) -> None:
        self.ingest = ingest
        self.camera_ids = ingest.camera_ids
        self.bridge = I0Bridge(ingest)
        self.server = server_factory(self.bridge)
        served, wanted = set(self.server.streams), set(self.camera_ids)
        if served != wanted:
            raise ValueError(f"영상 포트의 카메라({sorted(served)})가 입력 카메라({sorted(wanted)})와 다르다")
        self._report_lock = threading.Lock()
        self._reported: dict[str, dict[str, dict[str, int]]] = {}
        self.started = False

    def start(self) -> None:
        """영상 포트를 연다. 실패하면 VideoIngestServer 가 이미 연 포트를 닫고 예외를 다시 낸다."""
        self.server.start()
        self.started = True

    def stop(self, timeout_s: float = 0.0) -> list[str]:
        """멈춘다(부분 기동·여러 번 호출 안전). timeout_s 까지 수신 스레드를 기다리고 끝나지 않은 스레드 이름을 돌려준다."""
        self.started = False
        self.server.stop()
        return self.server.join(timeout_s) if timeout_s > 0 else self.server.alive_threads()

    def video_connected(self, camera_id: str) -> bool:
        return camera_id in self.server.connected_cameras()

    def connected(self, camera_id: str) -> bool:
        """관측을 낼 수 있는 입력인지: 영상 연결이 있고, 그 연결에서 코덱 정보·키프레임이 도착했고(video_ingest),
        디코더도 키프레임을 디코딩했다(I0Ingest). 재접속 직후에는 둘 다 다시 기다린다(모듈 설명)."""
        return (self.video_connected(camera_id) and self.server.streams[camera_id].ready
                and self.ingest.camera(camera_id).ready)

    def waiting_keyframe(self, camera_id: str) -> bool:
        return self.video_connected(camera_id) and not self.connected(camera_id)

    # ------------------------------------------------------------------ health 집계

    def frame_totals(self) -> dict[str, dict[str, int]]:
        """카메라별 누적 프레임 집계(모듈 설명의 계약 필드 정의)."""
        out = {}
        for cam in self.camera_ids:
            r = self.server.streams[cam].stats
            counts = self.ingest.log.counts(cam)
            by_reason: dict[str, int] = counts["skipped_by_reason"]  # type: ignore[assignment]
            late = by_reason.get("LATE_ARRIVAL", 0)
            policy = sum(by_reason.values()) - late
            out[cam] = {
                "received": r.frames_received + r.before_codec_info + r.stale_epoch_frames + r.out_of_order,
                "gateway_to_runpod_loss": r.gateway_to_runpod_loss,
                "skipped_by_policy": r.before_codec_info + r.before_keyframe + r.stale_epoch_frames + policy,
                "laptop_skipped": r.laptop_skipped,
                "unattributed_gap": r.unattributed_gap,
                "late_dropped": r.out_of_order + late,
            }
        return out

    def take_frames(self, consumer: str) -> dict[str, dict[str, int]]:
        """그 수신자(I1·노트북)의 직전 호출 뒤 늘어난 집계. 수신자마다 보고 구간이 따로다."""
        totals = self.frame_totals()
        with self._report_lock:
            prev = self._reported.get(consumer, {})
            self._reported[consumer] = totals
        return {cam: {k: max(0, v - prev.get(cam, {}).get(k, 0)) for k, v in t.items()} for cam, t in totals.items()}

    def merge_health(self, payload: dict[str, Any] | None, consumer: str) -> dict[str, Any] | None:
        """health payload 의 cameras[] 에 frames·stream_epoch 를 넣고, 키프레임 대기 카메라는 WAITING_KEYFRAME 으로 적는다."""
        if payload is None:
            return None
        frames = self.take_frames(consumer)
        for entry in payload.get("cameras", ()):
            cam = entry.get("id")
            if cam not in frames:
                continue
            entry["frames"] = frames[cam]
            epoch = self.server.streams[cam].stream_epoch
            if epoch is not None:
                entry["stream_epoch"] = epoch
            if entry.get("status") != "SYNC_INVALID" and self.waiting_keyframe(cam):
                entry["status"] = "WAITING_KEYFRAME"
        return payload

    def status(self) -> dict[str, Any]:
        """표시 파일·로그용 상태(계약 밖 값 포함: 디코딩 실패, 브리지 오류, 재접속)."""
        cams = {}
        totals = self.frame_totals()
        for cam in self.camera_ids:
            counts = self.ingest.log.counts(cam)
            cams[cam] = {
                "video_connected": self.video_connected(cam),
                "ready": self.ingest.camera(cam).ready,
                "stream_epoch": self.server.streams[cam].stream_epoch,
                "decoded": counts["decoded"],
                "inferred": counts["inferred"],
                "decode_failed": counts["decode_failed"],
                "frame_age_ms_p95": counts["frame_age_ms_p95"],
                "queue_wait_ms_max": counts["queue_wait_ms_max"],
                "skipped_by_reason": counts["skipped_by_reason"],
                "bridge_errors": dict(self.bridge.errors.get(cam, {})),
                "disconnects": self.bridge.disconnects.get(cam, 0),
                "protocol_errors": self.server.streams[cam].stats.protocol_errors,
                **totals[cam],
            }
        return {"started": self.started, "cameras": cams}
