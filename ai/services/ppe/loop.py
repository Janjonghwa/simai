"""PPE 서비스 한 주기: Tier 0 입력 공유 파일 → 검출 → 사람 연결 → 누적 → ppe_state → outbox (명세 5.5, S15P31A101-507).

입력은 Tier 0 가 아바타용으로 내보내는 최신 한 건 공유 파일(simai_ai.io.avatar_channel, `simai.avatar-input@1`)을 읽기만 한다.
그 안의 카메라별 영상과 관측은 Tier 0 가 (camera_id, stream_epoch, frame_sequence, 촬영 시각)이 같은 것끼리 짝지은 것이고,
트랙의 local ID → World ID 대응은 그 틱의 다시점 연관 결과다(관측 프레임과 최대 100 ms 차이, 설계 메모의 알려진 한계).

- 카메라마다 추론 시작 사이에 per_camera_period_ms(500 ms = 최대 2 Hz)를 두고, 같은 촬영 칸은 한 번만 처리한다.
- 최신 한 건만 읽으므로 카메라당 대기는 1 이다. NPZ 게시 완료 관측 뒤 max_wait_ms(150 ms)를 넘기면 추론하지 않는다(legacy는 offer 시각).
- 추론은 이 스레드 하나에서 한 장씩(동시 추론 1).
- 접수 문(Tier 0 지연, 3.2)이 막으면 새 추론을 멈춘다. 누적된 상태는 계속 내보내 근거 만료로 UNKNOWN 이 되게 한다.
- Tier 0 가 LIVE 가 아니면(정상 종료) 보내지 않는다.
- 원본 영상은 이 주기 안에서만 쓰고 저장·기록하지 않는다(11장).
"""

from __future__ import annotations

from collections import Counter
from bisect import bisect_right
from collections.abc import Callable, Mapping
import logging
from typing import Any, Protocol
import uuid

from simai_ai.ppe import FrameDetections, FrameObservations, PersonObservation, PpeJudge, associate_frame, build_ppe_state
from simai_ai.ppe.classes import Candidate
from simai_ai.io.channel_timing import ChannelTiming

log = logging.getLogger(__name__)

INPUT_FORMAT = "simai.avatar-input@1"
DEFAULT_PER_CAMERA_PERIOD_MS = 500
DEFAULT_PUBLISH_PERIOD_MS = 500
DEFAULT_MAX_WAIT_MS = 150
UNCERTAIN = "IDENTITY_UNCERTAIN"


class Source(Protocol):
    def latest(self, *, predicate=None): ...


class Detector(Protocol):
    def infer(self, frame_bgr: Any) -> list[Candidate]: ...


class Outbox(Protocol):
    def put(self, message: Mapping[str, Any]) -> bool: ...


class Gate(Protocol):
    def check(self): ...


def frame_observations(doc: Mapping[str, Any], camera_id: str, image_size: tuple[int, int]) -> FrameObservations | None:
    """공유 문서에서 한 카메라·한 프레임의 사람 관측을 만든다. 그 프레임의 관측이 없으면 사람 없는 프레임이다."""
    ref = doc.get("frames", {}).get(camera_id)
    if not isinstance(ref, Mapping):
        return None
    frame_ref = ref.get("frame_ref") or {}
    capture = ref.get("capture_time_ms")
    epoch, seq = frame_ref.get("stream_epoch"), frame_ref.get("frame_sequence")
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in (capture, epoch, seq)):
        return None
    persons = []
    for track in doc.get("world", {}).get("tracks", []):
        hold = bool(track.get("avatar_update_hold"))
        confidence = UNCERTAIN if hold else str(track.get("identity_confidence") or "HIGH")
        for pc in track.get("per_camera", []):
            # 영상과 같은 프레임의 관측만 쓴다(5.5: 같은 카메라·같은 프레임·같은 촬영 시각)
            if pc.get("camera_id") != camera_id or pc.get("estimated_capture_time") != capture:
                continue
            if (pc.get("frame_ref") or {}).get("frame_sequence") not in (None, seq):
                continue
            persons.append(PersonObservation(
                world_id=str(track["world_id"]),
                identity_revision=int(track.get("identity_revision") or 0),
                local_track_id=str(pc["local_track_id"]),
                keypoints_2d=pc.get("keypoints_2d") or [],
                identity_confidence=confidence,
                joint_layout=str(pc.get("joint_layout") or "COCO17"),
            ))
    return FrameObservations(camera_id, epoch, seq, capture, image_size, tuple(persons))


class PpeLoop:
    def __init__(
        self,
        source: Source,
        gate: Gate,
        detector: Detector,
        outbox: Outbox,
        *,
        source_id: str,
        stream_epoch: int,
        model_version: str,
        checkpoint_sha256: str,
        clock_ms: Callable[[], int],
        monotonic_ms: Callable[[], int] | None = None,
        judge: PpeJudge | None = None,
        per_camera_period_ms: int = DEFAULT_PER_CAMERA_PERIOD_MS,
        publish_period_ms: int = DEFAULT_PUBLISH_PERIOD_MS,
        max_wait_ms: int = DEFAULT_MAX_WAIT_MS,
        timing: ChannelTiming | None = None,
    ) -> None:
        self.source, self.gate, self.detector, self.outbox = source, gate, detector, outbox
        self.source_id = source_id
        self.stream_epoch = stream_epoch
        self.model_version = model_version
        self.checkpoint_sha256 = checkpoint_sha256
        self.clock_ms = clock_ms
        self.monotonic_ms = monotonic_ms or clock_ms
        self.judge = judge or PpeJudge()
        self.per_camera_period_ms = per_camera_period_ms
        self.publish_period_ms = publish_period_ms
        self.max_wait_ms = max_wait_ms
        self.state_sequence = 0
        self._last_frame: dict[str, tuple[int, int]] = {}  # camera → (stream_epoch, frame_sequence)
        self._last_infer_at: dict[str, int] = {}  # camera → 추론 시작 시각(운영은 단조 시계)
        self._last_bin: dict[str, tuple[int, int]] = {}  # camera → (stream_epoch, 촬영 칸)
        self._last_publish: int | None = None
        self._last_camera: str | None = None  # 마지막 실제 검출 시작. 미처리 입력은 순서를 소비하지 않는다.
        self._provenance: dict[str, Any] | None = None
        self.counts: Counter[str] = Counter()  # 계측(판정에 쓰지 않음)
        self.timing = timing or ChannelTiming()

    def diagnostics(self):
        source_timing = getattr(self.source, "timing", None)
        return {"loop": self.timing.summary(),
                "source": None if source_timing is None else source_timing.summary()}

    def _skip_reason(self, camera_id, ref, produced, *, published=False):
        if published and (not isinstance(produced, int) or isinstance(produced, bool)
                          or produced < 0 or produced > self.clock_ms()):
            return "publication_invalid"
        frame_ref = ref.get("frame_ref") or {}
        key = (frame_ref.get("stream_epoch"), frame_ref.get("frame_sequence"))
        capture = ref.get("capture_time_ms")
        if self._last_frame.get(camera_id) == key:
            return "duplicate"
        infer_at = self.monotonic_ms()
        last = self._last_infer_at.get(camera_id)
        bin_key = (key[0], capture // self.judge.cfg.bin_ms) if isinstance(capture, int) else None
        if (last is not None and infer_at - last < self.per_camera_period_ms) or (
            bin_key is not None and self._last_bin.get(camera_id) == bin_key
        ):
            return "rate_skip"
        if isinstance(produced, int) and self.clock_ms() - produced > self.max_wait_ms:
            return "wait_exceeded"
        return None

    def _metadata_eligible(self, doc):
        """모든 카메라가 불가할 때만 영상 읽기를 생략한다. 처리 기록은 소비하지 않는다."""
        eligible = False
        for camera_id, ref in doc.get("frames", {}).items():
            reason = self._skip_reason(camera_id, ref or {}, self._wait_since(doc),
                                       published="published_at_ms" in doc)
            if reason is None:
                eligible = True
            else:
                self.counts["metadata_" + reason] += 1
                self.timing.record("metadata_skip_" + reason, camera=camera_id)
        if not eligible:
            self.counts["metadata_rejected"] += 1
        return eligible

    @staticmethod
    def _wait_since(doc):
        return doc.get("published_at_ms", doc.get("produced_at_ms"))

    def _camera_order(self, images):
        cameras = sorted(images)
        start = 0 if self._last_camera is None else bisect_right(cameras, self._last_camera)
        return cameras[start:] + cameras[:start]

    def step(self) -> None:
        admission = self.gate.check()
        if not getattr(admission, "live", True):
            self.counts["not_live"] += 1
            return
        if admission.admit:
            if not self._infer_latest():
                return  # 카메라 묶음 도중 Tier 0가 정상 종료했다.
        else:
            self.counts["gate_" + (getattr(admission, "code", "") or "closed")] += 1
        if not getattr(self.gate.check(), "live", True):
            self.counts["not_live"] += 1
            return  # 마지막 추론 중 종료한 경우도 상태를 보내지 않는다.
        self._publish_due()

    def _infer_latest(self) -> bool:
        """최신 카메라 묶음을 처리한다. 도중 NOT_LIVE면 False로 송신도 중단한다."""
        with self.timing.measure("source_latest"):
            if getattr(self.source, "supports_predicate", False):
                got = self.source.latest(predicate=self._metadata_eligible)
            else:
                got = self.source.latest()
        if got is None:
            self.counts["no_input"] += 1
            return True
        doc, images = got
        if doc.get("format") != INPUT_FORMAT:
            self.counts["bad_input"] += 1
            return True
        provenance = doc.get("world", {}).get("input_provenance")
        if isinstance(provenance, Mapping) and "tracks" in provenance:
            self._provenance = dict(provenance)
        produced = self._wait_since(doc)
        batch_ready = self.timing.clock()
        cameras = self._camera_order(images)
        if cameras:
            self.timing.record("camera_first", camera=cameras[0])
        for camera_id in cameras:
            self.timing.record("camera_batch_wait", (self.timing.clock() - batch_ready) * 1000, camera=camera_id)
            image = images[camera_id]
            ref = doc.get("frames", {}).get(camera_id) or {}
            frame_ref = ref.get("frame_ref") or {}
            key = (frame_ref.get("stream_epoch"), frame_ref.get("frame_sequence"))
            if isinstance(produced, int):
                self.timing.record("camera_source_age_at_check", self.clock_ms() - produced, camera=camera_id)
            reason = self._skip_reason(camera_id, ref, produced, published="published_at_ms" in doc)
            if reason is not None:
                if reason != "duplicate":
                    self.counts[reason] += 1
                self.timing.record("camera_skip_" + reason, camera=camera_id)
                continue
            height, width = image.shape[:2]
            with self.timing.measure("camera_observations", camera=camera_id):
                obs = frame_observations(doc, camera_id, (int(width), int(height)))
            if obs is None:
                self.counts["bad_frame"] += 1
                continue
            if not obs.persons:
                self._last_frame[camera_id] = key
                self.counts["empty_persons"] += 1
                self.timing.record("camera_skip_empty_persons", camera=camera_id)
                dets = FrameDetections(camera_id, obs.stream_epoch, obs.frame_sequence, obs.capture_time_ms,
                                       obs.image_size, ())
                with self.timing.measure("camera_association_ingest", camera=camera_id):
                    self.judge.ingest(associate_frame(obs, dets, self.judge.cfg))
                continue
            # 한 카메라 추론 중 부하 신호가 바뀔 수 있다. 관측 구성 뒤, 새 검출 직전에 다시 확인한다.
            with self.timing.measure("camera_gate", camera=camera_id):
                admission = self.gate.check()
            if not getattr(admission, "live", True):
                self.counts["not_live"] += 1
                return False
            if not admission.admit:
                self.counts["gate_" + (getattr(admission, "code", "") or "closed")] += 1
                break  # 남은 프레임·촬영 칸·처리율 기록은 소비하지 않는다.
            if isinstance(produced, int):
                age = self.clock_ms() - produced
                self.timing.record("camera_source_age_at_infer", age, camera=camera_id)
                if age < 0 and "published_at_ms" in doc:
                    self.counts["publication_invalid"] += 1
                    self.timing.record("camera_skip_publication_invalid", camera=camera_id)
                    continue
                if age > self.max_wait_ms:
                    self.counts["wait_exceeded"] += 1
                    self.timing.record("camera_skip_wait_exceeded", camera=camera_id)
                    continue  # 관측 구성·gate 재확인에 걸린 시간도 150ms 대기에 포함한다.
            self._last_frame[camera_id] = key
            self._last_infer_at[camera_id] = self.monotonic_ms()  # 관측 구성 비용 뒤의 실제 추론 시작을 기록한다.
            self._last_bin[camera_id] = (obs.stream_epoch, obs.capture_time_ms // self.judge.cfg.bin_ms)
            self._last_camera = camera_id
            with self.timing.measure("camera_inference", camera=camera_id):
                candidates = self.detector.infer(image)
            dets = FrameDetections(camera_id, obs.stream_epoch, obs.frame_sequence, obs.capture_time_ms, obs.image_size,
                                   tuple(candidates))
            with self.timing.measure("camera_association_ingest", camera=camera_id):
                self.judge.ingest(associate_frame(obs, dets, self.judge.cfg))
            self.counts["inferred"] += 1
            self.timing.record("camera_inferred", camera=camera_id)
        return True

    def _publish_due(self) -> None:
        now = self.clock_ms()
        if self._provenance is None:
            return  # Tier 0 입력을 아직 한 번도 못 받았다
        if self._last_publish is not None and now - self._last_publish < self.publish_period_ms:
            return
        self._last_publish = now
        # 처리 UTC로 칸을 닫으면 허용된 촬영→공유 지연도 LATE_FRAME이 된다(-572).
        # 촬영 진행과 실제 근거 만료를 분리한다. gate 차단·입력 중단 중에도 만료는 현재 UTC로 돈다.
        views = self.judge.evaluate(now, capture_time_ms=self.judge.latest_capture_time_ms)
        try:
            message = build_ppe_state(
                self.judge, views, msg_id=str(uuid.uuid4()), sent_at=now, source_id=self.source_id,
                stream_epoch=self.stream_epoch, state_sequence=self.state_sequence, model_version=self.model_version,
                checkpoint_sha256=self.checkpoint_sha256, input_provenance=self._provenance)
        except ValueError:  # 64명 초과(계약 상한). 잘라 보내지 않고 이번 주기를 건너뛴다
            self.counts["too_many_people"] += 1
            log.warning("ppe_state 를 만들지 못했다: 사람 %d명(계약 상한 64)", len(views))
            return
        self.state_sequence += 1  # 쓰기 실패도 잃은 메시지로 친다(순번 빈 구간)
        if self.outbox.put(message):
            self.counts["published"] += 1
        else:
            self.counts["publish_failed"] += 1
