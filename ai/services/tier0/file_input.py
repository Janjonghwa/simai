"""Tier 0 파일 입력: 녹화 세션 재생과 world_state·risk_event JSONL 출력 (S15P31A101-215, 명세 5.1 입력 추상화, 21.3).

판정·추정 로직은 기능 패키지와 기존 모듈에 있다. 여기서는 잇기만 한다. 구조와 근거는 docs/design/tier0-recording-replay.md.

    RecordingReplay(녹화 manifest·세그먼트 확인, simai_ai.io)
      └─ FileInput.pump(재생 시계): release_ms 가 된 I0 메시지 → I0Ingest(LIVE 와 같은 디코딩·최신성 대기열)
    판정 루프 tick: DetectorFeed.poll → I0Ingest.next_batch → 검출기 → MultiCameraLocalTracker → ... → 판정(REPLAY 출처)
      └─ JsonlOutput: world_state(봉투 그대로, I1 과 별개)·risk_event(뷰어 projection, source REPLAY) 한 줄씩

규칙:
- 시계는 녹화 시각이다(ReplayClock, 명세 21.3 "시계는 주입식"). speed 가 수면 실제 시간의 그 배속, None 이면 최대 속도:
  판정 tick 마다 tick_ms 씩만 나아가므로 같은 녹화·설정이면 결과가 매번 같다(실제 시각에 기대지 않는다).
- 이 입력의 판정 출처는 REPLAY 다(명세 7장·부록 E). 현장 경보·I1 로 나가지 않는다(AGENTS 규칙 5): 설정이 I1·I0 제어 채널을
  만들지 않고, 7.3 표가 REPLAY 에는 채널을 주지 않으며(local_alert 신호를 만들지 않음), 저널(LIVE 전용)에도 넣지 않는다.
- 카메라 연결 상태: 세그먼트의 코덱 정보가 들어오면 연결, 다음 세그먼트가 있는 세그먼트가 끝나면 끊김(I0Ingest.connection_lost).
  녹화가 끝난 카메라는 끊김으로 바꾸지 않는다(녹화 끝은 카메라 장애가 아니다. 프레임이 오지 않으면 perception 의
  camera_stall_ms 가 공통 시각에서 뺀다).
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import IO, Any

from services.tier0.i1_client import Envelope
from simai_ai.io import I0CodecInfo, I0Ingest, RecordingReplay

log = logging.getLogger("services.tier0.file_input")

WORLD_STATE_FILE = "world_state.jsonl"
RISK_EVENT_FILE = "risk_event.jsonl"
FAILURE_FILE = "replay_failed.json"  # JSONL 기록 실패 지점(쓸 수 있으면 남긴다)
RISK_EVENT_SCHEMA_VERSION = "2.1.0"  # contracts/messages/risk_event (2.1.0 escalations, 이 출력은 싣지 않는다)


class ReplayClock:
    """녹화 시각을 내는 주입 시계. Tier0Service 의 mono_ns·wall_ms 로 넘긴다(Tier0Clock 이 둘을 묶는다).

    speed None(최대 속도): advance_ms 로만 나아간다. 수(배속): 실제 단조 시계 경과 × speed.
    """

    def __init__(self, origin_ms: int, speed: float | None, monotonic_ns: Callable[[], int] = time.monotonic_ns) -> None:
        if speed is not None and not speed > 0:
            raise ValueError("speed 는 양수 또는 None(최대 속도)이다")
        self.origin_ms = origin_ms
        self.speed = speed
        self._real = monotonic_ns
        self._real0 = monotonic_ns()
        self._virtual_ns = 0
        self._lock = threading.Lock()

    @property
    def max_speed(self) -> bool:
        return self.speed is None

    def mono_ns(self) -> int:
        if self.speed is None:
            with self._lock:
                return self._virtual_ns
        return int((self._real() - self._real0) * self.speed)

    def wall_ms(self) -> int:
        return self.origin_ms + self.mono_ns() // 1_000_000

    def advance_ms(self, ms: int) -> None:
        if self.speed is not None:
            raise RuntimeError("실시간 배속 재생 시계는 advance_ms 로 옮기지 않는다")
        with self._lock:
            self._virtual_ns += ms * 1_000_000


class FileInput:
    """녹화 재생 한 묶음: RecordingReplay → I0Ingest. pump·drained 는 판정 스레드에서만 부른다(모듈 설명)."""

    def __init__(self, replay: RecordingReplay, ingest: I0Ingest, clock: ReplayClock) -> None:
        self.replay = replay
        self.ingest = ingest
        self.clock = clock
        self.camera_ids = replay.camera_ids
        self._connected = dict.fromkeys(self.camera_ids, False)
        self.closed = False  # 녹화를 다 넣고 디코더에 남은 그림까지 대기열로 내보냈다
        self.errors = 0
        # 재생을 끝까지 하지 못한 이유(녹화 읽기·디코더 비우기 실패). 있으면 재생을 거기서 끝내고 진입점은 종료 코드 4
        self.failed: str | None = None

    def fail(self, reason: str) -> None:
        """재생을 여기서 끝낸다(남은 구간을 지어내지 않는다). 판정 루프는 drained 로 보고 끝낸다."""
        if self.failed is None:
            self.failed = reason
            log.error("녹화 재생 실패: %s (여기서 재생을 끝낸다)", reason)
        self.replay.close()
        self.closed = True

    def pump(self) -> int:
        """재생 시계가 지난 메시지를 I0Ingest 에 넣는다. 다 넣었으면 디코더를 비운다(I0Ingest.close). 넣은 수를 돌려준다."""
        if self.failed is not None:
            return 0
        try:
            due = self.replay.pop_due(self.clock.wall_ms())
        except (OSError, ValueError) as e:  # 확인한 뒤 파일이 바뀌거나 디스크가 실패함
            self.fail(f"녹화를 끝까지 읽지 못했다 ({type(e).__name__}: {e})")
            return 0
        for item in due:
            cam = item.camera_id
            try:
                if item.message is None:
                    self._connected[cam] = False
                    self.ingest.connection_lost(cam)
                    continue
                if isinstance(item.message, I0CodecInfo):  # 새 세그먼트 연결
                    self._connected[cam] = True
                self.ingest.receive(item.message)
            except Exception as e:  # noqa: BLE001 - 한 프레임 오류가 재생을 멈추지 않는다(CameraIngest 가 스스로 복구한다)
                self.errors += 1
                if self.errors == 1 or self.errors % 100 == 0:
                    log.warning("%s 재생 입력 오류 %d회째 (%s: %s)", cam, self.errors, type(e).__name__, e)
        if self.replay.finished and not self.closed:
            try:
                self.ingest.close()  # 디코더에 남은 그림(PyAV flush 는 DecodeError 를 낼 수 있다)
            except Exception as e:  # noqa: BLE001 - 끝 처리 실패는 재생 실패로 남긴다(정상 완료로 보지 않음)
                self.fail(f"디코더를 비우지 못했다 ({type(e).__name__}: {e})")
                return len(due)
            self.closed = True
        return len(due)

    def connected(self, camera_id: str) -> bool:
        """관측을 낼 수 있는 입력인지: 세그먼트 연결 중이고 디코더가 키프레임을 디코딩했다(LIVE 의 LiveInput.connected 와 같은 뜻)."""
        return self._connected.get(camera_id, False) and self.ingest.camera(camera_id).ready

    def drained(self) -> bool:
        """녹화를 다 넣었고 추론 대기열에 남은 프레임이 없다(마지막 프레임까지 판정에 넘겼다)."""
        return self.closed and all(self.ingest.scheduler.pending(c) == 0 for c in self.camera_ids)

    def close(self) -> None:
        self.replay.close()

    def status(self) -> dict[str, Any]:
        return {"delivered": self.replay.delivered, "finished": self.replay.finished, "closed": self.closed,
                "failed": self.failed,
                "errors": self.errors, "now_ms": self.clock.wall_ms(),
                "cameras": {c: {"connected": self.connected(c), **self.ingest.log.counts(c)} for c in self.camera_ids}}


# ---------------------------------------------------------------------------- JSONL 출력


def risk_event_payload(transition: Any) -> dict[str, Any]:
    """확정된 전이(lifecycle EventTransition) → risk_event 뷰어 projection payload (contracts/messages/risk_event).

    전이 뒤 상태를 싣는다. rationale·proposed_severity 는 VLM 결과가 없으므로 null, 확인·운영자 종료·상향 기록은 없다
    (이 출력은 REPLAY 출처라 받는 채널이 없다). delivery 는 LIVE 에만 있는 필드라 싣지 않는다.
    """
    s = transition.state
    lifecycle = str(s.lifecycle)
    payload: dict[str, Any] = {
        "event_id": s.event_id,
        "event_sequence": s.event_sequence,
        "rule_id": s.rule_id,
        "rule_version": s.rule_version,
        "source": str(s.origin),
        "lifecycle": lifecycle,
        "verification": str(s.verification),
        "severity": str(s.severity),
        "confidence": str(s.confidence),
        "needs_check": s.needs_check,
        "confidence_reasons": [str(r) for r in s.confidence_reasons],
        "resume_count": s.resume_count,
        "world_ids": list(s.world_ids),
        "zone_id": s.zone_id,
        "metrics": dict(s.metrics),
        "rationale": None,
        "proposed_severity": None,
        "gateway_id": s.gateway_id,
        "config_version": s.config_version,
        "calibration_version": s.calibration_version,
        "scene_version": s.scene_version,
        "raised_at": s.raised_at,
        "updated_at": transition.occurred_at,
        "resolved_at": transition.occurred_at if lifecycle == "RESOLVED" else None,
        "acks": [],
    }
    if s.related_event_id is not None:
        payload["related_event_id"] = s.related_event_id
    return payload


class JsonlOutput:
    """world_state·risk_event 를 한 줄에 메시지 하나씩 쓴다(봉투 포함, 계약 그대로). 판정 스레드에서만 쓴다.

    있는 파일은 덮어쓰지 않는다(AGENTS 규칙 6): 같은 이름 파일이 있으면 FileExistsError 로 시작하지 않는다.
    줄마다 flush 한다(뷰어 재생 도구가 실행 중에도 읽을 수 있게). fsync 는 하지 않는다(재생은 다시 돌릴 수 있다).
    쓰기·flush 가 실패하면(디스크·권한) failed 에 실패 지점(파일·줄 번호·이유)을 남기고 그 뒤로는 어느 파일에도 쓰지 않는다.
    이미 쓴 줄은 그대로 두고, 쓸 수 있으면 replay_failed.json 에 같은 내용을 남긴다. 서비스는 재생을 실패로 끝낸다.
    """

    def __init__(self, directory: Path, envelope: Envelope, new_msg_id: Callable[[], str] = lambda: str(uuid.uuid4())):
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory
        self._envelope = envelope
        self._new_msg_id = new_msg_id
        self._world: IO[str] = (directory / WORLD_STATE_FILE).open("x", encoding="utf-8", newline="\n")
        try:
            self._events: IO[str] = (directory / RISK_EVENT_FILE).open("x", encoding="utf-8", newline="\n")
        except BaseException:
            self._world.close()
            raise
        self.world_state_lines = 0
        self.risk_event_lines = 0
        self.failed: dict[str, Any] | None = None

    def _write(self, f: IO[str], name: str, line_no: int, message: Mapping[str, Any]) -> None:
        if self.failed is not None:
            raise OSError(f"JSONL 기록이 이미 실패했다: {self.failed['file']} {self.failed['line']}번째 줄")
        try:
            f.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
            f.flush()
        except (OSError, ValueError) as e:  # ValueError: 닫힌 파일
            self.failed = {"file": name, "line": line_no, "error": f"{type(e).__name__}: {e}",
                           "msg_type": message.get("msg_type"), "msg_id": message.get("msg_id"),
                           "world_state_lines": self.world_state_lines, "risk_event_lines": self.risk_event_lines}
            log.error("JSONL 기록 실패: %s", self.failed)
            try:
                (self.directory / FAILURE_FILE).write_text(json.dumps(self.failed, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass  # 같은 디스크가 실패했으면 남길 수 없다(로그·진입점 오류에는 남는다)
            raise

    def world_state(self, payload: Mapping[str, Any], *, msg_id: str) -> None:
        """judge 의 offer_world_state 자리. msg_id 는 그 tick 의 evidence_ref 와 같다."""
        self._write(self._world, WORLD_STATE_FILE, self.world_state_lines + 1,
                    self._envelope.make("world_state", payload, msg_id=msg_id))
        self.world_state_lines += 1

    def transitions(self, transitions: Iterable[Any]) -> None:
        for t in transitions:
            self._write(self._events, RISK_EVENT_FILE, self.risk_event_lines + 1, {
                "schema_version": RISK_EVENT_SCHEMA_VERSION,
                "msg_type": "risk_event",
                "msg_id": self._new_msg_id(),
                "sent_at": t.occurred_at,
                "source_id": t.state.gateway_id,
                "payload": risk_event_payload(t),
            })
            self.risk_event_lines += 1

    def close(self) -> None:
        for f in (self._world, self._events):
            try:
                f.close()
            except OSError:
                log.exception("JSONL 파일을 닫지 못했다")
