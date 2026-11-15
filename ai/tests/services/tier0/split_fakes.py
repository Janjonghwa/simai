"""검출·판정 프로세스 분리(S15P31A101-728) 시험용 대역. spawn 자식이 이름으로 다시 불러오므로 pytest 를 불러오지 않는다.

- ScriptedLiveInput: LIVE 입력 대역. 시작하면 카메라마다 50 ms 간격으로 새 프레임(촬영 시각 = 검출 프로세스 시계)을 낸다.
- ScriptedDetector: 프레임마다 사람 한 명(bbox·COCO17 keypoint)을 낸다. 식별자·시각은 넣은 프레임 값 그대로.
- OneTrackBackend: 검출 i 에 트랙 i+1 을 붙인다(test_live_input 의 대역과 같다).
"""

from __future__ import annotations

import numpy as np

from simai_ai.providers.base import Frame
from simai_ai.tracking.local_tracker import CameraDetections, PoseDetection

FRAME_EVERY_MS = 50


class ScriptedLiveInput:
    def __init__(self, spec, clock) -> None:
        self.camera_ids = tuple(spec.camera_ids)
        self.clock = clock
        self.seq = dict.fromkeys(self.camera_ids, 0)
        self.last: int | None = None
        self.started = False

    def start(self) -> None:
        self.started = True

    def stop(self, timeout_s: float) -> list[str]:
        self.started = False
        return []

    def connected(self, camera_id: str) -> bool:
        return self.started

    def next_batch(self) -> list[Frame]:
        if not self.started:
            return []
        now = self.clock.now_ms()
        if self.last is not None and now - self.last < FRAME_EVERY_MS:
            return []
        self.last = now
        out = []
        for cam in self.camera_ids:
            self.seq[cam] += 1
            out.append(Frame(camera_id=cam, estimated_capture_time=now, timestamp_source="CAMERA_STREAM",
                             frame_ref={"stream_epoch": 0, "frame_sequence": self.seq[cam]},
                             timestamp_uncertainty_ms=5.0, image=np.zeros((4, 6, 3), np.uint8)))
        return out

    def snapshot(self) -> dict:
        fields = ("received", "gateway_to_runpod_loss", "skipped_by_policy", "laptop_skipped", "unattributed_gap",
                  "late_dropped")
        return {
            "connected": dict.fromkeys(self.camera_ids, self.started),
            "waiting_keyframe": dict.fromkeys(self.camera_ids, False),
            "stream_epoch": dict.fromkeys(self.camera_ids, 0),
            "frame_totals": {c: {**dict.fromkeys(fields, 0), "received": self.seq[c]} for c in self.camera_ids},
            "live_status": {"started": self.started, "cameras": {}},
        }


KEYPOINTS = tuple((100.0 + 3 * i, 200.0 + 5 * i, 0.9) for i in range(17))


class ScriptedDetector:
    def detect(self, batch):
        return [CameraDetections(f.camera_id, f.frame_ref["stream_epoch"], f.frame_ref["frame_sequence"],
                                 f.estimated_capture_time, f.timestamp_source, f.timestamp_uncertainty_ms,
                                 (PoseDetection((90.0, 190.0, 160.0, 290.0), 0.9, KEYPOINTS),))
                for f in batch]


class OneTrackBackend:
    def __init__(self, _config) -> None:
        self._last: set[int] = set()

    def update(self, boxes, scores, features=None):
        out = [(i + 1, i) for i in range(len(boxes))]
        self._last = {t for t, _ in out}
        return out

    def live_track_ids(self):
        return frozenset(self._last), frozenset()


def scripted_spec(camera_ids=("cam_A", "cam_B")):
    from services.tier0.detect_process import DetectorProcessSpec

    return DetectorProcessSpec(mode="live", camera_ids=tuple(camera_ids), detector_factory=ScriptedDetector,
                               tracker_backend_factory=OneTrackBackend, input_factory=ScriptedLiveInput)


class FrameSigner:
    """이벤트 프레임 서명 대역(-738, 검출 프로세스에서 이름으로 불러온다). 객체 주소는 내용 해시로 정해 두 경로가 같게 낸다."""

    def sign(self, request):
        from services.tier0.clips.upload import SignedUpload

        key = f"event-frames/{request.event_id}/{request.camera_id}-{request.sha256[:12]}.jpg"
        return SignedUpload("https://objects.invalid/put", key, 0, object_uri=f"s3://frames/{key}")


class NullTransport:
    """S3 PUT 대역: 받기만 한다."""

    def put(self, signed, content, content_type):
        return None
