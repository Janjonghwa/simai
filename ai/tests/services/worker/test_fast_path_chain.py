"""빠른 경로 가짜 사슬 (S15P31A101-25): event_frame_ready → 작업 대여 → 얼굴 처리·덧그림 → 가짜 VLM → 콜백 → 근거 저장.

진짜 코드: Tier 0 EventFrameService·EventFrameBuffer(로컬 사본 포함), 워커 Worker·LeaseClient·VlmRationaleHandler·얼굴 관문·overlay.
가짜: VLM(고정 JSON, 지연 조절), pose, 서명·S3, EC2(conftest.FakeEC2 + 아래 ChainEC2). 사람 사진 없이 무늬 이미지만 쓴다.

ChainEC2 는 VlmRationaleIngestor·VlmRationaleListener 의 규칙을 옮겨 적은 것이라 서버 코드를 시험하지 않는다. 서버 쪽은
server VlmRationaleIntegrationTest(실제 DB)가, 두 쪽의 실제 연결은 GPU 세션 e25(deploy/scripts/e25_fast_path.py)가 맡는다.
저장 행은 실제 표 vlm_assessment 의 칸 이름을 그대로 쓴다. 292(Compose 통합 시험)를 대신하지 않는다.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("cv2")

TIER0_TESTS = Path(__file__).resolve().parents[1] / "tier0"
if str(TIER0_TESTS) not in sys.path:
    sys.path.insert(0, str(TIER0_TESTS))

from clip_stream import transition  # noqa: E402

import services.worker.vlm_rationale as vlm_module  # noqa: E402
from services.tier0.clips.upload import SignedUpload  # noqa: E402
from services.tier0.event_frame_buffer import EventFrameBuffer  # noqa: E402
from services.tier0.event_frames import EventFrameService  # noqa: E402
from services.worker.runner import Outcome  # noqa: E402
from services.worker.vlm_frames import LocalFrames  # noqa: E402
from services.worker.vlm_rationale import VlmRationaleHandler  # noqa: E402
from simai_ai.providers.base import Frame  # noqa: E402
from simai_ai.risk import FixedClock, Severity  # noqa: E402
from simai_ai.vlm.client import VlmResult  # noqa: E402
from simai_ai.vlm.face_blur import Person, VlmFrame, face_region  # noqa: E402

CAMERAS = ("cam_A", "cam_B")
W, H = 320, 240
FACE_KEYPOINTS = ((50.0, 30.0, 0.9), (44.0, 22.0, 0.9), (56.0, 22.0, 0.9), (38.0, 24.0, 0.8), (62.0, 24.0, 0.8))
PERSON = Person(bbox=(20.0, 10.0, 80.0, 120.0), keypoints=FACE_KEYPOINTS + (None,) * 12)
CALIBRATION = "cal-e25"
# 카메라 원점에서 2 m 앞 바닥 평면. 구역(u 200~210, v 150~165)·대상 표시(u 205, v 90)는 얼굴 영역(왼쪽 위)과 겹치지 않는다
ZONE_POLYGON = [[0.8, 0.6], [1.0, 0.6], [1.0, 0.9], [0.8, 0.9], [0.8, 0.6]]
ZONE_PIXEL = (155, 205)  # (행, 열)
ANSWER = {"rationale": "대상자가 위험 구역 안에 서 있다.", "proposed_severity": None}


class ChainEC2:
    """conftest FakeEC2(대여·제출 규칙, 계약 검증)를 감싸 VlmRationaleIngestor(작업 등록)·VlmRationaleListener(근거 저장) 규칙을 더한 가짜.
    등급은 어디서도 쓰지 않는다."""

    def __init__(self, base) -> None:
        base.heartbeat_interval_ms = 1000
        self.base = base
        self.events: dict[str, dict] = {}
        self.assessments: list[dict] = []
        self.ready = threading.Event()

    def __getattr__(self, name):
        return getattr(self.base, name)

    def raise_event(self, state) -> None:
        self.events[state.event_id] = {"severity": "CRITICAL", "rationale": None, "state": state,
                                       "observation_time": state.observation_time}

    def event_frame_ready(self, kind: str, payload: dict) -> None:
        assert kind == "event_frame_ready"
        event = self.events[payload["event_id"]]
        observed, state = event["observation_time"], event["state"]
        if self.now_ms() < observed + 5000 and not any(j.input["event_id"] == payload["event_id"] for j in self.jobs.values()):
            self.add_job("vlm_rationale", {
                "event_id": state.event_id, "event_sequence": state.event_sequence, "rule_id": state.rule_id,
                "rule_version": state.rule_version, "severity": event["severity"], "confidence": "HIGH",
                "needs_check": False, "world_ids": list(state.world_ids), "zone_id": state.zone_id, "metrics": {},
                "observation_time": observed,
                "frames": [{**copy.deepcopy(f), "face_policy": "BLURRED"} for f in payload["frames"]],
                "structured_state": {
                    "calibration_version": CALIBRATION,
                    "tracks": [{"world_id": "w-1", "position_xy": [0.9, -0.6], "head_z": 1.7,
                                "ground_anchor_xyz": [0.9, -0.6, 0.0]}],
                    "zone": {"zone_id": state.zone_id, "judge_type": "GROUND_FOOTPRINT", "polygon_xy": ZONE_POLYGON,
                             "height_m": 0.0}},
            }, priority="VLM_FAST", timeout_ms=5000, deadline_at=observed + 5000)
        self.ready.set()

    def post(self, path, body, timeout_s):
        response = self.base.post(path, body, timeout_s)
        p = body["payload"]
        if path.endswith("/result") and p["status"] == "SUCCEEDED" and response.body["payload"]["status"] == "ACCEPTED":
            job, result = self.jobs[p["job_id"]], p["result"]
            event = self.events[result["event_id"]]
            assert (result["event_sequence"], result["severity"]) == (job.input["event_sequence"], event["severity"])
            assert result["proposed_severity"] is None and result.get("clause_ids", []) == []
            created_at = self.now_ms()
            elapsed = created_at - job.input["observation_time"]
            self.assessments.append({"event_id": result["event_id"], "job_id": p["job_id"], "rule_severity": result["severity"],
                                     "proposed_severity": None, "latency_ms": result["latency_ms"],
                                     "event_observed_at": job.input["observation_time"], "created_at": created_at,
                                     "event_to_rationale_ms": elapsed, "within_deadline": elapsed <= 5000})
            event["rationale"] = result["rationale"]
        return response


@pytest.fixture
def ec2(ec2) -> ChainEC2:
    return ChainEC2(ec2)


class Signer:
    def __init__(self) -> None:
        self.count = 0
        self.lock = threading.Lock()

    def sign(self, request):
        with self.lock:
            self.count += 1
            key = f"event-frames/{request.event_id}/{self.count}.jpg"
        return SignedUpload("https://objects.example/put", key, 9_999_999_999_999, object_uri=f"s3://frames/{key}")


class NoPut:
    def put(self, signed, content, content_type):
        pass


class NoStore:
    """같은 Pod 사본을 읽어야 하므로 불리면 실패한다."""

    def get(self, ctx, uri):
        raise AssertionError("로컬 사본이 있는데 객체 저장소에서 받았다")


class Pose:
    def people(self, image):
        return [PERSON]


class FakeVlm:
    """고정 JSON, 지연 조절. 받은 이미지 사본과 질문을 남긴다."""

    def __init__(self, answer=ANSWER, delay_s: float = 0.0) -> None:
        self.answer, self.delay_s, self.calls = answer, delay_s, []

    def complete(self, prompt, frames, *, system=None, response_format=None):
        self.calls.append((prompt, tuple(frames)))
        time.sleep(self.delay_s)
        return VlmResult(content=json.dumps(self.answer, ensure_ascii=False), model="fake-vlm",
                         latency_ms=round(self.delay_s * 1000), finish_reason="stop")


def _calibration(state_dir: Path) -> None:
    doc = {"calibration_version": CALIBRATION,
           "cameras": [{"camera_id": c, "image_size": [W, H], "R": np.eye(3).tolist(), "t": [0.0, 0.0, 2.0],
                        "K": [[100.0, 0.0, W / 2], [0.0, 100.0, H / 2], [0.0, 0.0, 1.0]],
                        "distortion": {"model": "NONE", "coeffs": []}} for c in CAMERAS]}
    root = state_dir / "calibrations"
    root.mkdir(parents=True)
    (root / (hashlib.sha256(CALIBRATION.encode()).hexdigest() + ".json")).write_text(json.dumps(doc), encoding="utf-8")
    digest = hashlib.sha256(json.dumps(doc, sort_keys=True, allow_nan=False).encode()).hexdigest()
    (root / "active.json").write_text(json.dumps({"versions": [CALIBRATION], "document_hashes": {CALIBRATION: digest}}),
                                      encoding="utf-8")


def _tier0_raises_critical(ec2: ChainEC2, state_dir: Path, observed: int) -> None:
    """진짜 Tier 0 프레임 서비스: 링에 프레임을 넣고 CRITICAL 발생 전이를 주면 event_frame_ready 가 ChainEC2 로 간다."""
    rng = np.random.default_rng(25)
    ring = EventFrameBuffer(CAMERAS)
    for camera in CAMERAS:
        for back_ms in (4000, 3000, 2000, 1000, 0):
            ring.offer(Frame(camera, observed - back_ms, "RECEIVE_ESTIMATED", {"stream_epoch": 0, "frame_sequence": 4000 - back_ms},
                             17.5, rng.integers(0, 255, (H, W, 3), dtype=np.uint8)))
    service = EventFrameService(ring, FixedClock(observed + 100), signer=Signer(), transport=NoPut(),
                                send_event=ec2.event_frame_ready, local_root=state_dir)
    event = transition(severity=Severity.CRITICAL, observation_time=observed, occurred_at=observed + 50)
    ec2.raise_event(event.state)
    service.start()
    tracks = [SimpleNamespace(world_id="w-1", cameras=CAMERAS)]
    service.on_transitions([event], tracks)
    service.on_transitions([event], tracks)  # 같은 병합 이벤트는 1회
    assert ec2.ready.wait(10)
    service.stop(5)


def _handler(vlm: FakeVlm, state_dir: Path) -> VlmRationaleHandler:
    return VlmRationaleHandler(NoStore(), Pose(), client_factory=lambda: vlm, local_frames=LocalFrames(state_dir),
                               env={"SIMAI_STATE_DIR": str(state_dir)})


@pytest.mark.parametrize("opinion", [None, "CRITICAL", "INFO"])
def test_chain_stores_rationale_within_deadline_and_never_touches_severity(ec2, make_worker, tmp_path, monkeypatch, opinion):
    """사슬 한 바퀴. VLM 이 어떤 의견을 내도(같은 등급·하향 포함) 등급은 그대로이고 저장 의견은 null 이다."""
    _calibration(tmp_path)
    real_overlay = vlm_module.overlay
    overlay_inputs = []

    def spy(frame, meta, state, world_ids, camera):
        overlay_inputs.append((frame, np.array(frame.pixels)))  # 덧그림에 들어오는 그림은 이미 얼굴 처리를 마친 것이어야 한다
        return real_overlay(frame, meta, state, world_ids, camera)

    monkeypatch.setattr(vlm_module, "overlay", spy)
    observed = ec2.now_ms() - 200
    _tier0_raises_critical(ec2, tmp_path, observed)

    (job,) = ec2.jobs.values()
    assert (job.priority, job.timeout_ms, job.deadline_at) == ("VLM_FAST", 5000, observed + 5000)
    frames = job.input["frames"]
    assert len(frames) == 4 and all("local_copy" in f for f in frames)
    assert {f["estimated_capture_time_ms"] for f in frames} == {observed, observed - 3000}  # 이벤트 시점 + 직전 3~5초
    assert all(f["estimated_capture_time_ms"] <= observed for f in frames)  # 사후 프레임 없음
    assert not [k for k in job.input if "clip" in k]  # 사후 클립을 기다릴 입력이 없다

    vlm = FakeVlm({**ANSWER, "proposed_severity": opinion}, delay_s=0.05)
    assert make_worker({"vlm_rationale": _handler(vlm, tmp_path)}).run_once() is Outcome.ACCEPTED

    # 저장: 재는 칸은 vlm_assessment.event_observed_at → created_at (event_to_rationale_ms, within_deadline)
    (row,) = ec2.assessments
    assert row["event_observed_at"] == observed and row["event_to_rationale_ms"] == row["created_at"] - observed
    assert row["within_deadline"] is True and row["proposed_severity"] is None
    event = ec2.events[job.input["event_id"]]
    assert event["severity"] == "CRITICAL" and event["rationale"] == ANSWER["rationale"]
    assert ec2.jobs[job.job_id].result["clause_ids"] == []

    # 덧그림은 얼굴 처리 뒤: 들어온 그림의 얼굴 영역이 이미 모자이크(최대 9색)이고, VLM 은 덧그린 VlmFrame 만 받는다
    region = face_region(PERSON, (H, W, 3))
    assert len(overlay_inputs) == 4
    for frame, pixels in overlay_inputs:
        assert isinstance(frame, VlmFrame) and frame.record.regions == 1
        assert len(np.unique(pixels[region.y1:region.y2, region.x1:region.x2].reshape(-1, 3), axis=0)) <= 9
    (prompt, sent), = vlm.calls
    assert len(sent) == 4 and all(isinstance(f, VlmFrame) for f in sent)
    for (_, before), after in zip(overlay_inputs, sent, strict=True):
        assert np.array_equal(after.pixels[region.y1:region.y2, region.x1:region.x2],
                              before[region.y1:region.y2, region.x1:region.x2])  # 덧그림이 얼굴 영역을 되살리지 않는다
        assert not np.array_equal(after.pixels[ZONE_PIXEL], before[ZONE_PIXEL])  # 구역은 실제로 그려졌다
    assert "cam_A" in prompt and "cam_B" in prompt


def test_past_deadline_ends_without_rationale_and_event_unchanged(ec2, make_worker, tmp_path):
    """VLM 이 마감(관측 + 5 s)을 넘기면 워커가 TIMED_OUT 으로 끝내고 근거 없이 이벤트가 그대로다."""
    _calibration(tmp_path)
    observed = ec2.now_ms() - 4500
    _tier0_raises_critical(ec2, tmp_path, observed)
    (job,) = ec2.jobs.values()

    vlm = FakeVlm(delay_s=1.5)
    started = time.monotonic()
    assert make_worker({"vlm_rationale": _handler(vlm, tmp_path)}).run_once() is Outcome.ACCEPTED
    assert time.monotonic() - started < 1.5  # VLM 응답을 기다리지 않고 마감에 끊는다

    assert ec2.jobs[job.job_id].state == "TIMED_OUT" and ec2.jobs[job.job_id].result is None
    assert "마감" in ec2.jobs[job.job_id].error["message"]
    assert ec2.assessments == []
    event = ec2.events[job.input["event_id"]]
    assert event["severity"] == "CRITICAL" and event["rationale"] is None
