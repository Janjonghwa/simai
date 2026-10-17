"""이벤트 클립 시험용 합성 I0 패킷 스트림과 이벤트 전이."""

from __future__ import annotations

from dataclasses import dataclass

from simai_ai.io.messages import I0CodecInfo, I0Frame, I0FrameHeader
from simai_ai.risk.common import Origin, Severity
from simai_ai.risk.lifecycle import Change, EventKey, EventState, EventTransition, Lifecycle, Verification

T0 = 1_760_342_400_000
FRAME_MS = 50  # 20 fps
GOP = 20  # 1초마다 키프레임
SPS = b"\x67\x64\x00\x2a\xac"
PPS = b"\x68\xee\x3c\xb0"


def codec(camera_id: str, epoch: int = 0) -> I0CodecInfo:
    return I0CodecInfo(camera_id=camera_id, stream_epoch=epoch, codec="H264", pts_timebase="1/90000", sps=SPS, pps=PPS)


def payload(camera_id: str, epoch: int, seq: int, keyframe: bool) -> bytes:
    nal = b"\x65" if keyframe else b"\x41"
    return b"\x00\x00\x00\x01" + nal + f"|{camera_id}|{epoch}|{seq:06d}|".encode() + bytes(32)


def frame(camera_id: str, seq: int, t_ms: int, *, epoch: int = 0, keyframe: bool | None = None) -> I0Frame:
    key = seq % GOP == 0 if keyframe is None else keyframe
    header = I0FrameHeader(camera_id=camera_id, stream_epoch=epoch, frame_sequence=seq, estimated_capture_time_ms=t_ms,
                           timestamp_source="CAMERA_STREAM", keyframe=key)
    return I0Frame(header, payload(camera_id, epoch, seq, key))


@dataclass
class Stream:
    """카메라 여러 대의 20 fps 스트림. advance 로 앞으로 보낸 만큼 프레임을 낸다."""

    cameras: tuple[str, ...]
    t_ms: int = T0
    seq: int = 0
    epoch: int = 0

    def start(self) -> list:
        return [codec(c, self.epoch) for c in self.cameras]

    def advance(self, duration_ms: int) -> list[I0Frame]:
        out = []
        for _ in range(duration_ms // FRAME_MS):
            out.extend(frame(c, self.seq, self.t_ms, epoch=self.epoch) for c in self.cameras)
            self.seq += 1
            self.t_ms += FRAME_MS
        return out


def transition(
    *,
    origin: Origin = Origin.LIVE,
    severity: Severity = Severity.HIGH,
    lifecycle: Lifecycle = Lifecycle.RAISED,
    changes: tuple[Change, ...] = (Change.RAISED,),
    observation_time: int = T0,
    event_id: str = "0192a0a0-0000-7000-8000-000000000001",
    event_sequence: int = 1,
    resume_count: int = 0,
    occurred_at: int | None = None,
) -> EventTransition:
    state = EventState(
        event_id=event_id,
        gateway_id="tier0-01",
        origin=origin,
        event_sequence=event_sequence,
        lifecycle=lifecycle,
        key=EventKey("R1", ("w-1",), None),
        rule_version=1,
        severity=severity,
        needs_check=False,
        verification=Verification.VERIFIED,
        world_ids=("w-1",),
        origin_world_ids=("w-1",),
        zone_id="zone-a",
        metrics={},
        reasons=(),
        raised_at=observation_time,
        evaluated_at=observation_time,
        observation_time=observation_time,
        config_version="cfg-1",
        calibration_version="cal-1",
        scene_version="scene-1",
        identity_revisions={"w-1": 0},
        resume_count=resume_count,
    )
    return EventTransition(state=state, transition=lifecycle, changes=changes,
                           occurred_at=observation_time + 150 if occurred_at is None else occurred_at)
