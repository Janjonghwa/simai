"""프로세스 분리(-728) 곁가지를 검출 프로세스로 옮긴다 (S15P31A101-738, Re-ID 는 -744). 설계: docs/design/S15P31A101-738-split-side-work.md.

그림·압축 패킷은 검출 프로세스에 있으므로 그것이 필요한 일은 검출 프로세스가 하고, 무엇을 언제 할지(판정 결과)와 I1 송신은 판정
프로세스가 정한다(단일 소유, AGENTS 규칙 1).

| 곁가지 | 검출 프로세스(`ChildSideWork`) | 판정 프로세스(대리 객체) |
|---|---|---|
| 이벤트 프레임(CRITICAL R1~R4) | 프레임 링·JPEG·로컬 사본·서명·S3 PUT | `RemoteEventFrames`: 대상 전이 고르기(seen)·카메라 순서, 결과 `event_frame_ready` 를 I1 로(msg_id·순서는 판정 쪽) |
| 이벤트 클립 | 패킷 링 버퍼(영상 수신에서 바로)·자르기·재다중화 | `RemoteClips`: 클립 대상 전이·poll 을 명령으로 |
| Re-ID(-744) | 크롭·COPE 임베딩(`AsyncReidCollector`), 특징을 캐시에 넣을 때 판정 프로세스로도 보냄 | `RemoteReidCollector`: 특징 사본 → World Track feature_lookup, 수집 주기(update_sampling)는 명령. 보조 비용·DORMANT 재입장·swap 복원·분할 판정은 World Track 그대로 |
| 관제 중 배경 누적(-540) | `BackgroundCleanBackground`(결과 파일·상태 파일·세팅 촬영 요청 그대로) | `RemoteCleanBackground`: 보정 활성화·카메라 리셋·회전 표본 크기 명령, 회전 복구 입력은 검출 프로세스가 쓴 파일을 읽음 |

명령은 판정 → 검출 경계 큐로 간다(`DetectorProcess.send_side`). file 입력은 순서가 결과에 들어가므로 녹화 메시지와 같은 순서 큐에
기다려 넣고(lockstep), LIVE 는 기다리지 않는다(상태를 바꾸는 명령만 짧게 기다림). 검출 프로세스가 다시 뜨면 상태 명령(배경 시작·회전
표본 크기)을 새 세대에 다시 보낸다.
"""

from __future__ import annotations

import logging
import os
import pickle
import queue
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

log = logging.getLogger("services.tier0.split_side_work")

REFERENCE_RESEND_S = 10.0
"""기준 배경 목록이 바뀌지 않아도 이만큼마다 다시 싣는다(상태 하나를 잃어도 판정 쪽 사본이 맞게)"""
ROTATION_DUMP_TIMEOUT_S = 20.0
"""회전 복구 입력 파일 요청 응답 대기 상한(회전 복구 주기 15초보다 길다)"""
RELAY_QUEUE_MAX = 64
"""판정 쪽 event_frame_ready 중계 대기 상한(이벤트 큐 8건의 여러 배)"""
ROTATION_VIEWS_DIRNAME = "rotation-views"


@dataclass(frozen=True)
class SideWorkSpec:
    """검출 프로세스 곁가지 설정(pickle 된다). 없는 칸은 끈다."""

    event_frames: Any = None
    """services.tier0.event_frames.EventFrameIo. None 이면 이벤트 프레임을 만들지 않는다"""
    event_frames_local_root: str | None = None
    clips: bool = False
    """LIVE 영상 수신의 압축 패킷을 클립 링 버퍼에 넣는다"""
    clean_background: Any = None
    """services.tier0.settings.CleanBackgroundSettings. None 이면 배경 누적을 하지 않는다"""
    state_dir: str | None = None
    reid: Any = None
    """simai_ai.reid.collector.ReidCollectorConfig(-744). None 이면 검출 프로세스에 Re-ID 수집기를 만들지 않는다"""
    reid_embedder_factory: Callable[[], Any] | None = None
    """시험 주입: COPE 대신 쓸 임베더(모듈 최상위 이름). None 이면 설정의 COPE 가중치"""


def _plain(value: Any) -> Any:
    """경계를 넘길 사본(읽기 전용 매핑 등 pickle 되지 않는 매핑을 dict 로 바꾼다)."""
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_plain(v) for v in value)
    return value


# ---------------------------------------------------------------------------- 검출 프로세스 쪽


class _SetClock:
    """명령에 실려 온 판정 시계 값을 낸다(클립 대기·마감 판단이 판정 프로세스와 같은 시계를 쓰게)."""

    def __init__(self) -> None:
        self._now = 0

    def set(self, now_ms: int) -> None:
        self._now = max(self._now, int(now_ms))

    def now_ms(self) -> int:
        return self._now


class ChildSideWork:
    """검출 프로세스의 곁가지 묶음. 명령은 주 스레드(명령 수신)가 `handle` 로 넘기고, 그림은 `observe` 로 받는다."""

    def __init__(self, spec: SideWorkSpec, camera_ids: Sequence[str], *, clock: Any,
                 put: Callable[[tuple], bool], put_blocking: Callable[[tuple], None]) -> None:
        self.spec = spec
        self.camera_ids = tuple(camera_ids)
        self._clock = clock
        self._put = put
        self._put_blocking = put_blocking
        self.stop = threading.Event()
        self.event_frames: Any = None
        self.clips: Any = None
        self.clean_background: Any = None
        self._clean_thread: threading.Thread | None = None
        self._clip_clock = _SetClock()
        self.frames_requested = 0
        self.frames_ready_sent = 0
        self.errors = 0
        self._reference_key: Any = None
        self._reference_sent_at = 0.0
        if spec.event_frames is not None:
            from services.tier0.event_frame_buffer import EventFrameBuffer
            from services.tier0.event_frames import EventFrameService

            local_root = None if spec.event_frames_local_root is None else Path(spec.event_frames_local_root)
            try:
                self.event_frames = EventFrameService(
                    EventFrameBuffer(self.camera_ids), clock, signer=spec.event_frames.signer(),
                    transport=spec.event_frames.transport(), send_event=self._send_ready, local_root=local_root)
                self.event_frames.start()
            except Exception as error:  # noqa: BLE001 - 부가 근거 경로 실패가 검출 프로세스 기동을 막지 않는다(단일 경로와 같다)
                self.event_frames = None
                log.warning("이벤트 프레임 기동 실패(검출 프로세스): %s", type(error).__name__)
        if spec.clips:
            from services.tier0.clips.ring_buffer import PacketRingBuffer
            from services.tier0.clips.service import EventClipService

            self.clips = EventClipService(PacketRingBuffer(self.camera_ids), self._clip_clock)
        self.reid: Any = None
        self.feed: Any = None
        self.reid_features_sent = 0
        self.reid_features_dropped = 0
        if spec.reid is not None:
            self.reid = self._build_reid(spec)

    def _build_reid(self, spec: SideWorkSpec) -> Any:
        """크롭·COPE 임베딩은 그림·GPU 가 있는 여기서 한다(-744). 특징은 캐시에 넣을 때 판정 프로세스로도 보낸다."""
        try:
            from simai_ai.reid.collector import AsyncReidCollector, LocalTrackFeatureCache

            forward = self._send_feature

            class ForwardingCache(LocalTrackFeatureCache):
                """같은 캐시(검출 쪽 수집 주기 판단에 쓴다)이고, 넣을 때 판정 프로세스로도 보낸다."""

                def put(self, feature: Any, *, keep_timeline: bool = False) -> None:
                    super().put(feature, keep_timeline=keep_timeline)
                    try:
                        forward(feature)
                    except Exception:  # noqa: BLE001 - 보내기 실패가 임베딩 작업을 멈추지 않는다
                        log.debug("Re-ID 특징 보내기 실패", exc_info=True)

            embedder = None if spec.reid_embedder_factory is None else spec.reid_embedder_factory()
            return AsyncReidCollector(spec.reid, embedder=embedder,
                                      cache=ForwardingCache(feature_ttl_ms=spec.reid.feature_ttl_ms), start_worker=True)
        except Exception as error:  # noqa: BLE001 - Re-ID 는 보조다. 실패해도 검출 프로세스는 뜬다(기하 추적 그대로)
            log.warning("Re-ID 수집기 기동 실패(검출 프로세스): %s", type(error).__name__)
            return None

    def _send_feature(self, feature: Any) -> None:
        """특징 하나(로컬 키·촬영 시각·임베딩)를 판정 프로세스 사본으로 보낸다. 차면 버리고 센다(Re-ID 는 보조)."""
        if self._put(("side", "reid_feature", feature)):
            self.reid_features_sent += 1
        else:
            self.reid_features_dropped += 1

    # --- 그림·패킷 -------------------------------------------------------------------

    @property
    def wants_pictures(self) -> bool:
        return self.event_frames is not None or self.spec.clean_background is not None

    def attach(self, feed: Any, child_input: Any) -> None:
        """검출 결과가 원본 헤더와 일치한 그림을 곁가지에 넘기고(DetectorFeed.observe_decoded), LIVE 패킷을 클립 링에 넣는다."""
        self.feed = feed
        if self.wants_pictures:
            feed.observe_decoded = self.observe
        bridge = getattr(getattr(child_input, "live", None), "bridge", None)
        if self.clips is not None and bridge is not None:
            bridge.attach_clips(self.clips.receive)  # 영상 수신을 열기 전에(-57 링 버퍼)

    def observe(self, frame: Any, detections: Any) -> None:
        """단일 경로 EventFrameService.attach 와 같은 순서(사진 → 배경). 한 콜백 실패가 다른 콜백을 막지 않는다."""
        try:
            if self.event_frames is not None:
                self.event_frames.offer(frame, detections)
        finally:
            background = self.clean_background
            if background is not None:
                background.offer(frame, detections)

    def _send_ready(self, kind: str, payload: Mapping[str, Any]) -> None:
        """EventFrameService 의 I1 송신 자리. 판정 프로세스로 넘기고 그쪽이 I1 신뢰 큐에 넣는다. 차면 QueueFull(같은 재시도)."""
        from services.tier0.i1_client import QueueFull

        if not self._put(("side", kind, dict(payload))):
            raise QueueFull(kind)
        self.frames_ready_sent += 1

    # --- 명령 ---------------------------------------------------------------------

    def handle(self, now_ms: int, kind: str, *args: Any) -> None:
        try:
            self._handle(now_ms, kind, *args)
        except Exception:  # noqa: BLE001 - 곁가지 실패가 검출을 멈추지 않는다
            self.errors += 1
            log.warning("곁가지 명령 %s 실패(검출 프로세스)", kind, exc_info=self.errors <= 3)

    def _handle(self, now_ms: int, kind: str, *args: Any) -> None:
        if kind == "frames":
            if self.event_frames is not None:
                event_id, anchor_ms, occurred_at, cameras = args
                self.frames_requested += 1
                self.event_frames.request(event_id, anchor_ms, occurred_at, tuple(cameras))
        elif kind == "clips":
            if self.clips is not None:
                self._clip_clock.set(now_ms)
                self.clips.on_triggers(args[0], args[1])
        elif kind == "clips_poll":
            if self.clips is not None:
                self._clip_clock.set(now_ms)
                self.clips.poll()
        elif kind == "bg_init":
            self._bg_init(*args)
        elif kind == "bg_activate":
            if self.clean_background is not None:
                self.clean_background.activate(args[0], args[1])
        elif kind == "bg_reset":
            if self.clean_background is not None:
                self.clean_background.reset_camera(args[0])
        elif kind == "bg_rotation_side":
            if self.clean_background is not None:
                self.clean_background.configure_rotation_samples(int(args[0]))
        elif kind == "reid_sampling":
            if self.reid is not None:
                t, stable, people = args
                self.reid.set_sampling(t, frozenset(tuple(k) for k in stable), people)
        elif kind == "bg_dump_rotation":
            seq, path, exclude, *rest = args  # rest: 회전 작업자가 이미 받은 표본 키(-746, 없으면 전부)
            self._put_blocking(("side_reply", seq, *self._dump_rotation(Path(path), exclude, rest[0] if rest else None)))
        else:
            log.warning("모르는 곁가지 명령: %r", kind)

    def _bg_init(self, document: Mapping[str, Any], provenance: Mapping[str, Any]) -> None:
        """배경 누적 시작(판정 쪽 보정 문서·입력 출처가 정해진 뒤 온다). 이미 있으면 활성화로 본다."""
        if self.spec.clean_background is None or self.spec.state_dir is None:
            return
        if self.clean_background is not None:
            self.clean_background.activate(document, provenance)
            return
        from services.tier0.clean_background import BackgroundCleanBackground

        self.clean_background = BackgroundCleanBackground(document, provenance, settings=self.spec.clean_background,
                                                          state_dir=Path(self.spec.state_dir), stop=self.stop)
        self._clean_thread = threading.Thread(target=self.clean_background.run, name="tier0-clean-background",
                                              daemon=True)
        self._clean_thread.start()

    def _dump_rotation(self, path: Path, exclude: Iterable[str], omit: Any = None) -> tuple[bool, str | None]:
        """회전 복구 입력(축소 표본 스냅샷)을 파일로 쓴다(임시 파일 → os.replace). 없으면 None 을 쓴다.
        omit 의 표본(회전 작업자가 이미 받은 것)은 None 으로 비워 파일을 줄인다(-746)."""
        background = self.clean_background
        views = None if background is None else background.rotation_snapshot(exclude_cameras=tuple(exclude),
                                                                            omit_samples=omit)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "wb") as handle:
                pickle.dump(views, handle, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp, path)
            return True, None
        except OSError as e:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return False, f"{type(e).__name__}: {e}"

    # --- 상태 ---------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """하트비트·step 응답에 싣는 값. 기준 배경 목록(bg_reference)은 바뀌었거나 REFERENCE_RESEND_S 가 지났을 때만 싣는다."""
        out: dict[str, Any] = {"side": {"errors": self.errors, "frames_requested": self.frames_requested,
                                        "frames_ready_sent": self.frames_ready_sent}}
        if self.clips is not None:
            out["side"]["clips"] = {**vars(self.clips.stats), "pending": self.clips.pending}
        if self.reid is not None:
            out["reid"] = {**self.reid.status(), "features_sent": self.reid_features_sent,
                           "features_dropped": self.reid_features_dropped}
            feed = self.feed
            if feed is not None and hasattr(feed, "reid_submission_status"):
                out["reid_submission"] = feed.reid_submission_status()
        elif self.spec.reid is not None:
            out["reid"] = {"enabled": bool(self.spec.reid.enabled), "state": "disabled", "reason": "start_failed"}
        background = self.clean_background
        if background is not None:
            out["side"]["clean_background"] = {"dropped": background.dropped, "disabled": background.disabled,
                                               "last_error": background.last_error,
                                               "worker_restarts": background.worker_restarts}
            out["bg_rotation_signatures"] = {cid: background.recent_signature(cid) for cid in self.camera_ids}
            reference = {cid: (str(folder), doc) for cid, (folder, doc) in dict(background._reference).items()}
            key = tuple(sorted((cid, v[0]) for cid, v in reference.items()))
            now = time.monotonic()
            if key != self._reference_key or now - self._reference_sent_at >= REFERENCE_RESEND_S:
                self._reference_key = key
                self._reference_sent_at = now
                out["bg_reference"] = reference
        return out

    def close(self, timeout_s: float = 2.0) -> None:
        self.stop.set()
        if self.reid is not None:
            try:
                self.reid.close()
            except Exception:  # noqa: BLE001
                log.debug("Re-ID 수집기 닫기 실패", exc_info=True)
        if self.event_frames is not None:
            try:
                self.event_frames.stop(timeout_s)
            except Exception:  # noqa: BLE001
                log.debug("이벤트 프레임 정지 실패", exc_info=True)
        if self._clean_thread is not None:
            self._clean_thread.join(timeout_s)
        if self.clean_background is not None:
            try:
                self.clean_background.close()
            except Exception:  # noqa: BLE001
                log.debug("배경 누적 닫기 실패", exc_info=True)


# ---------------------------------------------------------------------------- 판정 프로세스 쪽 대리 객체


class RemoteEventFrames:
    """EventFrameService 대역(판정 프로세스). 대상 전이와 카메라 순서는 여기서 고르고(seen 은 판정 lock 안), 그림·JPEG·업로드는 검출
    프로세스가 한다. 검출 프로세스가 보낸 event_frame_ready 는 중계 스레드가 I1 신뢰 큐에 넣는다(msg_id·순서는 판정 쪽 I1)."""

    def __init__(self, process: Any, camera_ids: Sequence[str], *, send_event: Callable[..., Any]) -> None:
        self._process = process
        self.camera_ids = tuple(camera_ids)
        self._send_event = send_event
        self._seen: set[str] = set()
        self._relay: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=RELAY_QUEUE_MAX)
        self._closing = threading.Event()
        self._thread: threading.Thread | None = None
        self.requested = 0
        self.request_dropped = 0
        self.relayed = 0
        self.relay_dropped = 0
        process.on_side("event_frame_ready", self.deliver)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="tier0-event-frames", daemon=True)
        self._thread.start()

    def attach(self, _feed: Any) -> None:
        return None  # 그림은 검출 프로세스에서 받는다

    def offer(self, _frame: Any, _detections: Any = None) -> None:
        return None

    def on_transitions(self, transitions: Iterable[Any], tracks: Iterable[Any] = ()) -> None:
        from services.tier0.event_frames import frame_requests

        if self._closing.is_set():
            return
        for event_id, anchor_ms, occurred_at, cameras in frame_requests(transitions, tracks, self.camera_ids, self._seen):
            if self._process.send_side(("frames", event_id, anchor_ms, occurred_at, cameras)):
                self.requested += 1
            else:
                self.request_dropped += 1
                log.warning("event_frames_failed event_id=%s reason=queue_full(detect_process)", event_id)

    def deliver(self, payload: Mapping[str, Any]) -> None:
        """검출 프로세스 받기 스레드에서 불린다. 기다리지 않는다(차면 버리고 남긴다)."""
        try:
            self._relay.put_nowait(dict(payload))
        except queue.Full:
            self.relay_dropped += 1
            log.warning("event_frames_failed event_id=%s reason=relay_full", payload.get("event_id"))

    def _run(self) -> None:
        from services.tier0.i1_client import QueueFull

        while not self._closing.is_set():
            try:
                payload = self._relay.get(timeout=0.05)
            except queue.Empty:
                continue
            frames = len(payload.get("frames") or ())
            while not self._closing.is_set():
                try:
                    self._send_event("event_frame_ready", payload)
                    self.relayed += 1
                    log.info("event_frame_ready_queued event_id=%s frames=%d", payload.get("event_id"), frames)
                    break
                except QueueFull:
                    self._closing.wait(0.05)  # 같은 객체 주소를 붙든 채 신뢰 큐 자리만 기다린다
                except Exception as error:  # noqa: BLE001 - 근거 사진 실패가 판정·경보로 새지 않는다
                    log.warning("event_frames_failed event_id=%s reason=%s", payload.get("event_id"),
                                type(error).__name__)
                    break

    def stop(self, timeout_s: float) -> bool:
        self._closing.set()
        if self._thread is not None:
            self._thread.join(timeout_s)
        return self._thread is None or not self._thread.is_alive()

    def status(self) -> dict[str, Any]:
        return {"mode": "detect_process", "requested": self.requested, "request_dropped": self.request_dropped,
                "relayed": self.relayed, "relay_dropped": self.relay_dropped,
                "child": (self._process.snapshot().get("side") or {}).get("frames_ready_sent")}


class RemoteClips:
    """EventClipService 대역(판정 프로세스). 링 버퍼·자르기는 검출 프로세스가 하고, 클립 대상 전이·poll 만 명령으로 보낸다."""

    export_enabled = False  # 업로더는 단일 경로와 같이 없다(service._assemble_and_run)

    def __init__(self, process: Any) -> None:
        self._process = process
        self.sent = 0
        self.dropped = 0
        self.ignored_transitions = 0

    def receive(self, _message: Any) -> None:
        return None  # 패킷은 검출 프로세스의 영상 수신이 바로 넣는다

    def on_transitions(self, transitions: Iterable[Any]) -> list[Any]:
        """전이에서 클립 대상(ClipTrigger)·보존 표시 열쇠만 뽑아 보낸다(전이 객체는 읽기 전용 매핑을 품어 pickle 되지 않는다).

        반환값은 단일 경로(EventClipService.on_transitions: 중복·대기 목록 가득 참을 뺀 새로 넣은 대상)와 다르다: 여기는 전이에서 뽑은
        클립 대상 전부다(중복 제거는 검출 프로세스 쪽이 한다). 지금 호출자(JudgmentLoop)는 반환값을 쓰지 않는다."""
        from services.tier0.clips.service import critical_hold_key
        from services.tier0.clips.trigger import clip_trigger_for

        triggers, holds = [], []
        for t in transitions:
            hold = critical_hold_key(t)
            if hold is not None:
                holds.append(hold)
            trigger = clip_trigger_for(t)
            if trigger is None:
                self.ignored_transitions += 1
            else:
                triggers.append(trigger)
        if triggers or holds:
            self._send(("clips", triggers, holds))
        return triggers

    def poll(self) -> None:
        self._send(("clips_poll",))

    def _send(self, cmd: tuple) -> None:
        if self._process.send_side(cmd, required=cmd[0] == "clips"):
            self.sent += 1
        else:
            self.dropped += 1

    @property
    def stats(self) -> SimpleNamespace:
        child = dict((self._process.snapshot().get("side") or {}).get("clips") or {})
        child.pop("pending", None)
        return SimpleNamespace(**child, cmd_sent=self.sent, cmd_dropped=self.dropped,
                               parent_ignored_transitions=self.ignored_transitions)

    @property
    def pending(self) -> int:
        return int(((self._process.snapshot().get("side") or {}).get("clips") or {}).get("pending") or 0)


class RemoteCleanBackground:
    """BackgroundCleanBackground 대역(판정 프로세스). 누적·합성·내보내기·상태 파일은 검출 프로세스가 한다.

    회전 복구(판정 프로세스)가 쓰는 면만 갖는다: settings, _reference(기준 배경 목록, 검출 프로세스 보고값), reset_camera,
    configure_rotation_samples, rotation_snapshot(검출 프로세스가 쓴 파일을 읽음), recent_signature(보고값)."""

    def __init__(self, process: Any, settings: Any, state_dir: Path, document: Mapping[str, Any],
                 provenance: Mapping[str, Any]) -> None:
        self._process = process
        self.settings = settings
        self._views_dir = Path(state_dir) / "clean_background" / ROTATION_VIEWS_DIRNAME
        self._document = _plain(document)
        self._provenance = _plain(provenance)
        self._rotation_side: int | None = None
        self.dump_failures = 0
        self.last_error: str | None = None
        # 상태 갱신과 명령 넣기를 묶는다: 재시작 재전송(관측 받기 스레드)과 보정 활성화(판정 스레드)가 겹쳐도 새 세대 큐에
        # 옛 문서의 bg_init 이 새 문서의 bg_activate 뒤에 들어가지 않게(넣은 순서 = lock 잡은 순서)
        self._state_lock = threading.Lock()
        process.on_new_generation(self._replay_state)
        with self._state_lock:
            self._send(("bg_init", self._document, self._provenance))

    def _send(self, cmd: tuple) -> bool:
        ok = self._process.send_side(cmd, required=True)
        if not ok:
            log.warning("배경 누적 명령 %s 를 검출 프로세스에 넣지 못했다", cmd[0])
        return ok

    def _replay_state(self) -> None:
        """검출 프로세스가 다시 떴다: 새 세대에 지금 보정 문서·회전 표본 크기를 다시 보낸다(표본은 새로 모은다)."""
        with self._state_lock:
            self._send(("bg_init", self._document, self._provenance))
            if self._rotation_side is not None:
                self._send(("bg_rotation_side", self._rotation_side))

    def activate(self, document: Mapping[str, Any], provenance: Mapping[str, Any]) -> None:
        document, provenance = _plain(document), _plain(provenance)
        with self._state_lock:
            self._document, self._provenance = document, provenance
            self._send(("bg_activate", document, provenance))

    def reset_camera(self, cid: str) -> None:
        with self._state_lock:
            self._send(("bg_reset", cid))

    def configure_rotation_samples(self, max_side: int) -> None:
        with self._state_lock:
            self._rotation_side = int(max_side)
            self._send(("bg_rotation_side", self._rotation_side))

    @property
    def _reference(self) -> dict[str, tuple[Path, dict[str, Any]]]:
        ref = self._process.snapshot().get("bg_reference") or {}
        return {cid: (Path(folder), doc) for cid, (folder, doc) in ref.items()}

    def recent_signature(self, cid: str) -> tuple[int, int, int] | None:
        sig = (self._process.snapshot().get("bg_rotation_signatures") or {}).get(cid)
        return None if sig is None else tuple(sig)  # type: ignore[return-value]

    def rotation_snapshot(self, exclude_cameras: Iterable[str] = (), omit_samples: Any = None) -> dict | None:
        """검출 프로세스에 축소 표본 스냅샷을 파일로 쓰게 하고 읽는다(회전 복구 스레드, 판정 lock 밖). 모르면 None.
        omit_samples(카메라별 표본 키)는 회전 작업자가 이미 받은 표본이라 파일에서 비운다(-746)."""
        path = self._views_dir / f"views-{os.getpid()}-{threading.get_ident()}.pkl"
        omit = {cid: sorted(keys) for cid, keys in (omit_samples or {}).items() if keys}
        cmd = ("bg_dump_rotation", str(path), tuple(exclude_cameras or ())) + ((omit,) if omit else ())
        reply = self._process.side_request(cmd, timeout_s=ROTATION_DUMP_TIMEOUT_S)
        if reply is None or not reply[0]:
            self.dump_failures += 1
            self.last_error = None if reply is None else reply[1]
            return None
        try:
            with open(path, "rb") as handle:
                return pickle.load(handle)
        except (OSError, pickle.UnpicklingError, EOFError) as e:
            self.dump_failures += 1
            self.last_error = f"{type(e).__name__}: {e}"
            return None
        finally:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def status(self) -> dict[str, Any]:
        return {"mode": "detect_process", "dump_failures": self.dump_failures, "last_error": self.last_error,
                "child": (self._process.snapshot().get("side") or {}).get("clean_background")}

    def close(self) -> None:
        return None  # 검출 프로세스가 닫는다


class RemoteReidCollector:
    """AsyncReidCollector 대역(판정 프로세스, -744). 크롭·COPE 임베딩은 검출 프로세스가 하고, 여기는 받은 특징의 사본
    ((camera, local_id[-g세대]) → 특징)을 두고 World Track 의 feature_lookup 에 돌려준다. Re-ID 보조 비용·DORMANT 재입장·swap 복원·
    분할 판정은 World Track(판정 프로세스)이 그대로 한다(기하 게이트 뒤 보조 비용만, AGENTS 규칙 1·8).

    수집 주기 정책(update_sampling)은 World 정본 스냅샷으로 여기서 계산해 바뀐 때만 명령으로 보낸다. 검출 프로세스가 다시 뜨면 사본을
    비운다(새 세대 로컬 ID 는 -g<세대> 라 옛 특징과 겹치지 않지만, 옛 세대 특징을 남기지 않는다)."""

    def __init__(self, process: Any, config: Any) -> None:
        from simai_ai.reid.collector import LocalTrackFeatureCache
        from simai_ai.tracking.stage_timing import StageTiming

        self._process = process
        self.config = config
        self.cache = LocalTrackFeatureCache(feature_ttl_ms=config.feature_ttl_ms)
        self.timing = StageTiming()
        self.people = 0
        self._last_sampling: tuple[int, frozenset] | None = None
        self.features_received = 0
        self.sampling_sent = 0
        self.sampling_dropped = 0
        self.resets = 0
        process.on_side("reid_feature", self._receive)
        process.on_new_generation(self._reset)

    def _child(self) -> dict[str, Any]:
        snap = self._process.snapshot()
        child = snap.get("reid") if snap else None
        if child is None:
            child = (getattr(self._process, "built_side", None) or {}).get("reid")
        return dict(child or {})

    @property
    def state(self) -> str:
        return str(self._child().get("state") or "disabled")

    @property
    def reason(self) -> str | None:
        return self._child().get("reason")

    @property
    def active(self) -> bool:
        return self.config.enabled and self.state == "active"

    def _receive(self, feature: Any) -> None:
        """받기 스레드에서 불린다. 같은 키는 촬영 시각이 늘 때만 바꾼다(LocalTrackFeatureCache.put)."""
        self.cache.put(feature)
        self.features_received += 1

    def _reset(self) -> None:
        self.cache.clear()
        self._last_sampling = None
        self.resets += 1

    def lookup(self, camera_id: str, local_track_id: str, now_ms: int | None = None) -> Any:
        if not self.config.enabled:
            return None
        with self.timing.measure("reid_lookup", self.people):
            return self.cache.lookup(camera_id, local_track_id, now_ms)

    def update_sampling(self, frame: Any) -> None:
        from simai_ai.reid.collector import sampling_snapshot

        people, t, stable = sampling_snapshot(frame)
        self.people = people
        key = (people, stable)
        if self._last_sampling is not None and self._last_sampling == key and self._process.spec.mode == "live":
            # LIVE: 같은 정책이면 보내지 않는다(검출 쪽은 시각 차 500 ms 안에서만 정책을 쓰므로 0.4초마다는 다시 보낸다)
            if t - self._sent_t < 400:
                return
        if self._process.send_side(("reid_sampling", t, sorted(stable), people)):
            self.sampling_sent += 1
            self._last_sampling = key
            self._sent_t = t
        else:
            self.sampling_dropped += 1

    _sent_t = 0

    def status(self) -> dict[str, Any]:
        """단일 경로 AsyncReidCollector.status 와 같은 키(검출 프로세스 보고값) + 판정 쪽 사본 계수."""
        child = self._child()
        base = {"enabled": bool(self.config.enabled), "state": self.state, "reason": self.reason}
        return {**child, **base, "mode": "detect_process", "parent_cached_tracks": len(self.cache),
                "features_received": self.features_received, "sampling_sent": self.sampling_sent,
                "sampling_dropped": self.sampling_dropped, "resets": self.resets,
                "parent_timing": self.timing.summary()}

    def close(self, timeout_s: float = 1.0) -> None:
        return None  # 수집기는 검출 프로세스가 닫는다
