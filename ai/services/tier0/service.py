"""Tier 0 서비스 조립: 기동 순서, 스레드, 종료 (명세 3.2 재시작 복구·저널 계약·구성 묶음, 3.10, 3.12, 18.2 시작 순서).

스레드·lock 구조와 순서 불변식은 docs/design/tier0-service.md 에 그림과 함께 있다. 요약:

기동(start, 한 스레드에서 차례로):
 1. 상태 디렉터리·표시 파일(state/tier0.live): 이전 실행이 남긴 표시 파일이 있으면 정상 종료하지 않은 것으로 로그에 남긴다.
    그다음 표시 파일을 phase=starting 으로 바로 쓰고(GPU 실험 큐·분석 워커가 기동 중에도 LIVE 로 본다) GPU 메모리를 먼저
    잡는다(GpuReservation, 명세 3.2). 기동이 실패하면 이 표시 파일을 지운다(이전 실행이 남긴 것이었으면 start_failed 로 남긴다).
 2. 보정(CalibrationProvider) → CameraRig, 시계(Tier0Clock → DecisionClock: 판정 시각은 앞으로만 간다).
 3. 저널을 쓰기 모드로 열고 recover(체크포인트 → 저널 재적용 → 활성 이벤트 UNVERIFIABLE 복원, 저널에 기록). 쓰기 스레드·
    체크포인트·I0·I1 어느 것도 시작하기 전이다(단일 작성자, 복원 전 현장 경보 목록 금지).
 4. 구성 묶음 복구: 체크포인트의 config_version 을 BundleArchive 에서 같은 검증기로 다시 만든다. 결과(MATCHED·없음·거부)를
    비교해 남긴다(config_check). 체크포인트가 없으면 보관소의 가장 최근 묶음, 그것도 없으면 설정의 시작 묶음(bootstrap).
 5. 인지·판정·스냅샷·health·I1 연결·체크포인트·클립을 만든다. 스냅샷 seed = 복원한 활성 이벤트, start_offset = 저널 끝.
    인지를 만든 직후(첫 World Track update 전) 체크포인트 Track 을 복원 후보로 넘기고 이전 이벤트 연결을 만든다
    (RestoreLink: manager.restore → RestoredEventLinker, -178). 체크포인트 재료는 첫 인지 tick 전까지 복구 결과로 채운다
    (그 전에 쓰는 체크포인트가 복구가 알던 config_version·Track 을 잃지 않게).
 6. 시작: 저널 쓰기 스레드 → Checkpointer(정리는 SnapshotOutbox.compact 로만) → I1 → I0 제어 채널 → I0 영상 수신(LIVE)
    → 검출·Local Tracker 루프(tier0-detect, LIVE, S15P31A101-383) → 정리 주기 → 판정 루프.

file 입력(녹화 재생, S15P31A101-215, file_input.py): 보정보다 먼저 녹화(manifest·세그먼트 파일)를 확인하고(어긋나면 종료 코드 4)
시계를 녹화 시각(ReplayClock)으로 바꾼다. 판정 출처는 REPLAY 이고 I1·I0 제어 채널을 열지 않는다(설정이 막는다). 판정 루프가
tick 마다 재생 메시지를 I0Ingest 에 넣고(최대 속도면 tick_ms 씩 시계를 옮긴다), 녹화를 다 판정에 넘기면 finished 를 켠다.
world_state·확정 전이는 설정하면 JSONL 로 남긴다(I1 과 별개).

LIVE 입력(S15P31A101-182, live_input.py): 검출기(TensorRT 엔진 → 없으면 .pt 가중치, 둘 다 없으면 종료 코드 4) → 디코더
(`input.decoder`: CPU 기본, NVDEC 선택. 열지 못하면 4) → 영상 포트 설정(모자라면 2) 순서로 확인한다. 영상 수신은 클립 링 버퍼를 붙인 뒤 연다.
검출과 카메라별 Local Tracker 는 10 Hz 판정 루프에서 분리된 `tier0-detect` 스레드(기본 `detect_tick_ms=50` ms, 20 Hz)가
판정 lock 밖에서 돌려 카메라당 12 FPS 이상 관측을 내고, 유계 큐(`detect_queue_depth=8`)를 통해 판정 루프로 넘긴다(S15P31A101-383).

종료(stop, 종료 신호): 검출·판정 루프 → 정리 주기 → I0(영상 수신·제어 채널) → I1(더는 저널을 읽지 않는다) → 저널 쓰기 스레드 비우기(넣은 단계를 모두
fsync) → 마지막 체크포인트 → 저널 닫기 → 표시 파일 지우기. 제한 시간 안에 끝나지 않은 스레드는 이름을 로그에 남긴다.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import json
import logging
import os
from pathlib import Path
import threading
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit
import uuid

from services.tier0.clips.ring_buffer import PacketRingBuffer
from services.tier0.clips.service import EventClipService
from services.tier0.clips.upload import HttpPutTransport
from services.tier0.config_fetch import HttpsFetcher
from services.tier0.drift_watch import BackgroundDriftWatch
from services.tier0.endpoint_registration import EndpointRegistrar, read_cert_pem
from services.tier0.event_frames import EventFrameService, EventFrameSigner
from services.tier0.event_frame_buffer import EventFrameBuffer
from services.tier0.event_snapshot import EventStateSnapshot
from services.tier0.file_input import FileInput, JsonlOutput, ReplayClock
from services.tier0.health import HealthReporter, WarningSources
from services.tier0.i0_control import ControlServer
from services.tier0.i1_client import Envelope, WebSocketConnector
from services.tier0.i1_publisher import I1Link, build_i1_link, with_world_state_drops
from services.tier0.judge import JudgmentLoop
from services.tier0.live_input import LiveInput
from services.tier0.perception import DetectorFeed, FrameFeed, Perception, PoseDetector, SimObservationFeed
from services.tier0.restore_link import RestoreLink
from services.tier0.settings import Tier0Settings
from services.tier0.site_alert_sync import SiteAlertResync
from services.tier0.video_ingest import TokenVerifier, build_server, parse_token_hashes, server_context
from simai_ai.calibration.live_safety import SwapProbation
from simai_ai.calibration.monitor import BaselineRecord, MonitorReport, MonitorStatus
from simai_ai.calibration.runtime import RuntimeCalibrationStore
from simai_ai.config import BundleArchive, BundleRejected, BundleVerifier, ConfigBundle, ConfigSync
from simai_ai.eventlog import ACKED_NAME, AckedOffsetStore, EventJournal, JournalWriter, Outbox, TransportError
from simai_ai.geometry.rig import CameraRig
from simai_ai.gpu.admission import RUNNING, STARTING, LoadWindow, clean_stop_path, clean_stop_record, marker_section
from simai_ai.gpu.admission import AVATAR_DETECT_BUDGET_MS, AVATAR_JUDGE_BUDGET_MS
from simai_ai.gpu.processing import ProcessingWindow
from simai_ai.gpu.reserve import GpuReservation
from simai_ai.io import DecoderUnavailable, FreshnessConfig, I0Ingest, NvdecDecoder, PyAvDecoder, RecordingReplay
from simai_ai.io.freshness import SkipReason
from simai_ai.providers.files import FileCalibrationProvider
from simai_ai.providers.provenance import build_input_provenance
from simai_ai.recovery import Checkpointer, CheckpointStore, RecoveryHealthGate, RecoveryResult, recover
from simai_ai.risk import LifecycleTracker, ObservedTracksPolicy, Origin, ack_from_contract, default_registry
from simai_ai.risk._parse import RiskInputError
from simai_ai.tracking.clock_sync import Tier0Clock, Tier0ClockSync
from simai_ai.tracking.decision_time import ClockAwareVerification, DecisionClock
from simai_ai.tracking.gateway_link import GatewayLinkMonitor
from simai_ai.tracking.local_tracker import BackendFactory, MultiCameraLocalTracker
from simai_ai.tracking.system_health import ProgressWatchdog
from simai_ai.tracking.world_track import session_prefix_from_ms

log = logging.getLogger("services.tier0")

HOUSEKEEPING_S = 0.1
JUDGE_OVERRUN_YIELD_S = 0.01  # 비공정한 판정 lock 의 즉시 재획득을 쉬어 정리 주기에 기회를 준다(9차 Pod 검증값, -375)
DETECT_IDLE_WAIT_MAX_S = 0.005  # 새 프레임이 아직 없을 때 검출 루프가 짧게 쉬는 상한(-383)
STATUS_EVERY_S = 1.0
MARKER_EVERY_S = 5.0
QUEUE_WAIT_EXCEEDED = str(SkipReason.QUEUE_WAIT_EXCEEDED)  # LIVE 입력 추론 대기 상한 초과(지연 표본에 싣는다)

# 입력별 판정 출처. 녹화 재생은 REPLAY: 실제 알림 채널로 나가지 않는다(AGENTS 규칙 5, 명세 7장·부록 E). 없으면 LIVE
INPUT_ORIGINS: dict[str, Origin] = {"file": Origin.REPLAY}


class Tier0StartError(RuntimeError):
    """시작할 수 없다. exit_code 로 진입점 종료 코드를 정한다(2 설정, 3 복구 거부, 4 입력·검출기)."""

    def __init__(self, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


# ---------------------------------------------------------------------------- 스레드 안전 어댑터


class SharedTier0Clock(Tier0Clock):
    """Tier0Clock.now 는 기준점·마지막 단조 시각을 바꾸므로 여러 스레드(판정·I0·I1·체크포인트)에서 부르려면 lock 이 필요하다."""

    def __init__(self, monotonic_ns: Callable[[], int], wall_ms: Callable[[], int]) -> None:
        super().__init__(monotonic_ns, wall_ms)
        self._now_lock = threading.Lock()

    def now(self):  # type: ignore[override]
        with self._now_lock:
            return super().now()


class SharedDecisionClock(DecisionClock):
    """DecisionClock(앞으로만 가는 판정 시각)을 여러 스레드에서 쓰게 lock 으로 감싼다(lock 순서: 이것 → Tier0Clock)."""

    def __init__(self, clock: Tier0Clock) -> None:
        super().__init__(clock)
        self._now_lock = threading.Lock()

    def now_ms(self) -> int:
        with self._now_lock:
            return super().now_ms()


class LockedClockSync:
    """Tier0ClockSync 는 스레드 안전하지 않다(요청은 정리 주기, 응답은 I0 수신, 추정은 판정·health). 한 lock 으로 감싼다."""

    def __init__(self, sync: Tier0ClockSync) -> None:
        self._sync = sync
        self._lock = threading.Lock()

    def request_due(self) -> bool:
        with self._lock:
            return self._sync.request_due()

    def send_request(self) -> str:
        with self._lock:
            return self._sync.send_request()

    def handle_response(self, message: Mapping[str, Any]) -> Any:
        with self._lock:
            return self._sync.handle_response(message)

    def link_lost(self) -> None:
        with self._lock:
            self._sync.link_lost()

    def capture_clock(self, gateway_link_ok: bool):
        with self._lock:
            return self._sync.capture_clock(gateway_link_ok)

    def health_field(self, gateway_link_ok: bool):
        with self._lock:
            return self._sync.health_field(gateway_link_ok)

    @property
    def generation(self) -> int:
        with self._lock:
            return self._sync.generation


class _ControlChannel:
    """Tier0ClockSync 의 송신 채널(ClockSyncChannel) → I0 제어 채널 송신 대기열."""

    def __init__(self, control: ControlServer) -> None:
        self._control = control

    def send(self, message: dict[str, Any]) -> None:
        if not self._control.send_message(message):
            raise ConnectionError("I0 제어 채널이 연결돼 있지 않다")


class _NoTransport:
    """엔드포인트 등록은 I1 메시지로만 한다(EndpointRegistrar.build 만 쓴다). HTTPS 임시안 전송은 쓰지 않는다."""

    def post(self, path: str, body: Mapping[str, Any], timeout_s: float) -> int:  # pragma: no cover - 쓰지 않는다
        raise RuntimeError("HTTPS 엔드포인트 등록은 쓰지 않는다(I1 tier0_endpoint)")


def _quiet(fn: Callable[[], Any]) -> Callable[[], None]:
    def run() -> None:
        try:
            fn()
        except Exception:  # noqa: BLE001
            pass

    return run


def _offline_transport(_messages: Any) -> None:
    raise TransportError("EC2(I1) 설정이 없다: 오프라인")


# ---------------------------------------------------------------------------- 보정 감시 기준선 저장(-117)


def baselines_to_json(baselines: Mapping[str, BaselineRecord]) -> dict[str, Any]:
    return {
        version: {
            "values_px": dict(rec.values_px),
            "verified_camera_ids": list(rec.verified_camera_ids),
            "sources": dict(rec.sources),
        }
        for version, rec in sorted(baselines.items())
    }


def baselines_from_json(doc: Any) -> dict[str, BaselineRecord]:
    if not isinstance(doc, dict):
        raise ValueError("기준선 파일은 객체다")
    out = {}
    for version, rec in doc.items():
        out[version] = BaselineRecord(
            calibration_version=version,
            values_px={str(k): float(v) for k, v in rec["values_px"].items()},
            verified_camera_ids=tuple(str(c) for c in rec.get("verified_camera_ids", ())),
            sources={str(k): str(v) for k, v in rec.get("sources", {}).items()},
        )
    return out


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_baselines(path: Path) -> dict[str, BaselineRecord]:
    """저장한 보정 감시 기준선(검증 여부 포함). 없으면 빈 dict, 깨졌으면 로그를 남기고 빈 dict(검증된 것으로 지어내지 않는다)."""
    if not path.exists():
        return {}
    try:
        return baselines_from_json(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError):
        log.error("보정 감시 기준선 파일을 읽지 못했다: %s (기준선 없이 다시 배운다, 미확정)", path)
        return {}


def save_baselines(path: Path, baselines: Mapping[str, BaselineRecord]) -> None:
    _atomic_write(path, json.dumps(baselines_to_json(baselines), ensure_ascii=False, sort_keys=True).encode("utf-8"))


# ---------------------------------------------------------------------------- 서비스


class Tier0Service:
    """Tier 0 한 프로세스. start → (실행) → stop."""

    def __init__(
        self,
        settings: Tier0Settings,
        *,
        mono_ns: Callable[[], int] = time.monotonic_ns,
        wall_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
        feed: FrameFeed | None = None,
        drift_watch_factory: Callable[[dict[str, BaselineRecord]], Any] | None = None,
        new_msg_id: Callable[[], str] = lambda: str(uuid.uuid4()),
        journal_fsync: Callable[[int], None] = os.fsync,
        detector_factory: Callable[[], PoseDetector] | None = None,
        tracker_backend_factory: BackendFactory | None = None,
        gpu_torch: Any | None = None,
        on_camera_drifted: Callable[[str], None] | None = None,
    ) -> None:
        """detector_factory·tracker_backend_factory: LIVE 검출기·Local Tracker 백엔드를 바꿔 끼운다(ultralytics 가 없는
        노트북 시험의 대역). 없으면 설정의 엔진·가중치와 ultralytics 추적기를 쓴다."""
        self.settings = settings
        self._detector_factory = detector_factory
        self._tracker_backend_factory = tracker_backend_factory
        self.live_input: LiveInput | None = None
        self.file_input: FileInput | None = None  # file 입력(녹화 재생)
        self.jsonl: JsonlOutput | None = None
        self._replay: RecordingReplay | None = None
        self.finished = threading.Event()  # file 입력: 재생이 끝났다(끝까지 넘겼거나 file_input.failed)
        self.output_failed: dict[str, Any] | None = None  # file 입력 JSONL 기록 실패 지점(진입점 종료 코드 5)
        self._clock_unknown = False  # LIVE 인데 시계 교환 채널이 없다(F1): SAME_CLOCK 으로 보지 않는다
        # GPU 공유(명세 3.2, simai_ai.gpu): 선확보와 분석 워커 접수 문에 싣는 지연 표본. gpu_torch 는 시험용 가짜 torch
        self.gpu = GpuReservation(settings.gpu_reserve_mb, device=settings.gpu_device, torch_module=gpu_torch)
        self.load = LoadWindow(settings.lag_budget_ms)
        processing_clock = lambda: mono_ns() / 1_000_000_000
        self.judge_processing = ProcessingWindow(AVATAR_JUDGE_BUDGET_MS, monotonic=processing_clock)
        self.detect_processing = ProcessingWindow(AVATAR_DETECT_BUDGET_MS, monotonic=processing_clock)
        self._qwe_total = 0  # LIVE 입력의 QUEUE_WAIT_EXCEEDED 누계(직전 지연 표본 때)
        self.marker_error: str | None = None  # 표시 파일 쓰기 실패(분석 워커·GPU 큐는 표시 파일이 없으면 모름으로 본다)
        self._journal_fsync = journal_fsync
        self._mono_ns = mono_ns
        self._wall_ms = wall_ms
        self._feed_override = feed
        self._drift_watch_factory = drift_watch_factory
        self.drift_watch: Any | None = None
        self.live_calibration: Any | None = None
        self.clean_background: Any | None = None
        self.rotation_recovery: Any | None = None
        self._rotation_recovery_error: str | None = None
        self.reid_collector: Any | None = None
        self.event_frames: EventFrameService | None = None
        self._live_calibration_error: str | None = None
        self._on_camera_drifted = on_camera_drifted
        self._drift_report: MonitorReport | None = None
        self._pending_drift_report: MonitorReport | None = None
        self._drift_notified: set[tuple[str, str]] = set()
        self._live_change_points: dict[tuple[str, str], int] = {}
        self._swap_probation: SwapProbation | None = None
        self._new_msg_id = new_msg_id
        self.judge_lock = threading.RLock()  # 판정 lock: 추적기·인지·클립·현장 경보 송신 순서
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._restored = False
        self.started = False
        self.previous_unclean = False
        self.events: list[str] = []  # 기동·종료 순서 기록(시험·로그)
        self.config_check: dict[str, Any] = {}
        self.recovery: RecoveryResult | None = None
        self.link: I1Link | None = None
        self.control: ControlServer | None = None
        self.clock_sync: LockedClockSync | None = None
        self._warned: dict[str, Any] = {}
        self._pending_calibration: tuple[dict[str, Any], CameraRig] | None = None
        self.calibration_status: dict[str, Any] = {"last_rejection": None, "effective_at": None}
        self._re_setup_cameras: dict[str, dict[str, Any]] = {}
        """재세팅이 필요한 카메라(camera_id → reason·detected_at, judge_lock 안에서만 바꾼다). health.re_setup 이 싣는다(-480)"""
        self._calibration_message_cursor = 0

    def _event(self, name: str) -> None:
        self.events.append(name)
        log.info("tier0: %s", name)

    def mono_ms(self) -> float:
        return self._mono_ns() / 1e6

    # --- 기동 ------------------------------------------------------------------------

    def start(self) -> None:
        s = self.settings
        s.state_dir.mkdir(parents=True, exist_ok=True)
        if s.live_marker.exists():
            self.previous_unclean = True
            log.warning("이전 Tier 0 실행이 정상 종료하지 않았다(%s 가 남아 있다). 저널·체크포인트로 복구한다", s.live_marker)
        self._clear_clean_stop()
        self._write_phase_marker(STARTING)
        try:
            self.gpu.acquire()  # 실패해도 예외를 내지 않는다(상태로 남김). 판정이 먼저다
            self._start()
        except BaseException:
            self._start_failed_marker()
            raise

    def _start(self) -> None:
        s = self.settings
        if s.file is not None:
            self._open_recording()  # 시계를 녹화 시각으로 바꾸므로 시계보다 먼저
        self.tier0_clock = SharedTier0Clock(self._mono_ns, self._wall_ms)
        self.clock = SharedDecisionClock(self.tier0_clock)
        now = self.clock.now_ms()

        # 보정
        try:
            self.calibration = RuntimeCalibrationStore(
                s.state_dir / "calibrations", lambda: FileCalibrationProvider(s.calibration_file).current())
            self.rig = CameraRig.from_provider(self.calibration)
            self._scene_site_id = self.calibration.current()["coordinate_frame"].get("site_id")
        except (OSError, ValueError, KeyError, TypeError) as e:
            raise Tier0StartError(f"보정 문서를 읽지 못했다: {s.calibration_file} ({type(e).__name__}: {e})", 2) from e

        # 노트북 쪽(I0 제어 채널·시계 교환·연결 감시)
        self.gateway_link = GatewayLinkMonitor()
        if s.control is not None:
            try:
                tokens = TokenVerifier(parse_token_hashes(s.control.token_hashes))
                context = server_context(s.control.cert_file, s.control.key_file)
            except (OSError, ValueError) as e:
                raise Tier0StartError(f"I0 제어 채널 TLS·토큰 설정 오류 ({type(e).__name__})", 2) from e
            self.control = ControlServer(
                host=s.control.host, port=s.control.port, ssl_context=context, tokens=tokens, source_id=s.gateway_id,
                clock=self.clock, on_message=self._on_laptop_message, on_connected=self._on_laptop_connected,
                on_disconnected=self._on_laptop_disconnected,
            )
            self.clock_sync = LockedClockSync(Tier0ClockSync(self.tier0_clock, _ControlChannel(self.control),
                                                             source_id=s.gateway_id))
            verification: Any = ClockAwareVerification(
                offset_source=lambda: self.clock_sync.capture_clock(self._gateway_ok()),  # type: ignore[union-attr]
                generation_source=lambda: self.clock_sync.generation,  # type: ignore[union-attr]
            )
        elif s.input_mode == "live":
            # LIVE 촬영 시각은 노트북 시계다. 제어 채널(시계 교환)이 없으면 시계 차이를 모른다: SAME_CLOCK 으로 보지 않고
            # 해소 확인을 막고(offset 모름) health 는 UNSAFE_UNKNOWN 이다(명세 3.9·6.1 미상 시계 정책, -160)
            log.warning("LIVE 입력인데 I0 제어 채널(SIMAI_TIER0_CONTROL_PORT)이 없다: 노트북 시계 차이를 몰라 해소를 확정하지 "
                        "않고 health 는 UNSAFE_UNKNOWN 이다")
            self._clock_unknown = True
            verification = ClockAwareVerification(offset_source=lambda: None)
        else:
            verification = ObservedTracksPolicy()  # 노트북 없는 조립(SIM 오프라인): 촬영 시각이 판정 시계와 같다

        # 3) 저널 → 복구 (쓰기 스레드·체크포인트·I0·I1 시작 전)
        self.journal = EventJournal.open(s.journal_dir, clock=self.clock, fsync=self._journal_fsync)
        try:
            self.store = CheckpointStore(s.checkpoint_dir, gateway_id=s.gateway_id)
            self.tracker = LifecycleTracker(origin=INPUT_ORIGINS.get(s.input_mode, Origin.LIVE), gateway_id=s.gateway_id,
                                            clock=self.clock, policy=s.lifecycle, verification=verification)
            self.recovery = recover(journal=self.journal, store=self.store, tracker=self.tracker)
        except Exception as e:
            self.journal.close()
            raise Tier0StartError(f"재시작 복구를 거부했다: {type(e).__name__}: {e} (docs/design/tier0-recovery.md 운영 절차)",
                                  3) from e
        self._restored = True
        r = self.recovery
        self._event(f"recovered:{r.source}:{len(r.restored)}")
        for rej in r.rejected:
            log.warning("체크포인트를 쓰지 않았다: %s (%s)", rej.name, rej.reason)
        if r.stats.gaps or r.stats.unknown_updates or r.stats.duplicates or r.stats.stale:
            log.warning("저널 재적용 이상: %s", r.stats)

        try:
            self._assemble_and_run(now)
        except BaseException:
            if not self.started:
                self._abort_start()
            raise

    def _assemble_and_run(self, now: int) -> None:
        s = self.settings
        r = self.recovery
        assert r is not None
        # 4) 구성 묶음
        registry = default_registry()
        known = self.calibration.known
        self.archive = BundleArchive(s.archive_dir)
        restored = self._restore_config(r, registry, known)
        verifier = BundleVerifier(HttpsFetcher(ca_file=(s.env.get("SIMAI_EC2_CA_FILE") or None)), registry, known,
                                  self.clock, setup_calibration=self.calibration.setup_candidate_problem)
        self.config_sync = ConfigSync(verifier, self.clock, source_id=s.gateway_id, store=self.archive,
                                      restored=restored, restored_applied_at=None if restored is None else now,
                                      before_apply=self._adopt_setup_calibration)

        # 5) 인지·판정
        feed = self._feed_override or self._build_feed(now)
        provenance = build_input_provenance(feed.provenance(), calibration=self.rig.calibration_provenance)
        avatar_writer = None
        if s.env.get("SIMAI_AVATAR_SHARE") == "1":
            from simai_ai.io.avatar_channel import LatestChannelWriter, avatar_channel_path
            root = Path(s.env.get("SIMAI_ROOT") or "/workspace/simai")
            avatar_writer = LatestChannelWriter(avatar_channel_path(s.env, root), clock_ms=self._wall_ms)
        self.perception = Perception(self.rig, feed, session=session_prefix_from_ms(now), scene_version=s.scene_version,
                                     input_provenance=provenance, avatar_writer=avatar_writer)
        # 복원 Track 후보·이전 이벤트 연결: 첫 World Track update 전(판정 스레드 시작 전, -178)
        self.restore_link = RestoreLink(self.perception.manager, r)
        self.writer = JournalWriter(self.journal)
        self.watchdog = ProgressWatchdog()
        self.clips = EventClipService(PacketRingBuffer(feed.camera_ids), self.clock)
        if self.live_input is not None:
            self.live_input.bridge.attach_clips(self.clips.receive)  # 영상 수신을 열기 전에(-57 링 버퍼)
        self.snapshot = EventStateSnapshot(
            self.journal, gateway_id=s.gateway_id, clock=self.clock, config_version=self._config_version,
            start_offset=self.journal.next_offset, seed=self.tracker.active(),
            current_state=lambda eid: self.judge.current_state(eid),
            risk_observed_at=lambda eid: self.judge.risk_observed_at(eid),
            resume_window_ms=s.lifecycle.resume_window_ms,
        )
        self.gate = RecoveryHealthGate()
        self.health = HealthReporter(
            perception=self.perception, now_ms=self.clock.now_ms, mono_ns=self._mono_ns, gate=self.gate,
            watchdog=self.watchdog, ec2_connected=self._ec2_ok, gateway_ok=self._gateway_ok,
            capture_clock=self._health_capture_clock(),
            laptop_clock_offset=self._health_laptop_offset(),
            config_fields=self._config_health_fields, config_active=lambda: self.config_sync.active is not None,
            calibration_state=self._calibration_state, calibration_version=lambda: self.rig.calibration_version,
            calibration_issue=lambda: (self.calibration_status["last_rejection"] is not None
                                       or bool(self.calibration_status.get("cumulative_warning"))
                                       or bool(self.calibration_status.get("re_setup_required"))),
            re_setup=self.re_setup_field,
            lock=self.judge_lock,
        )
        self.resync = SiteAlertResync(
            active_states=lambda: self.judge.active_states(), send=self._send_laptop_health, clock=self.clock,
            lock=self.judge_lock, ready=lambda: self._restored,
        )
        self.link = self._build_link() if s.i1 is not None else None
        if s.event_frames_enabled and s.input_mode == "live" and self.link is not None:
            try:
                parts = urlsplit(s.i1.url)
                base = s.env.get("SIMAI_EC2_BASE_URL") or urlunsplit(("https", parts.netloc, "", "", ""))
                self.event_frames = EventFrameService(
                    EventFrameBuffer(feed.camera_ids), self.clock,
                    signer=EventFrameSigner(base, s.i1.token, ca_file=s.i1.ca_file),
                    # S3 PUT 은 판단용 로컬 사본을 알린 뒤 뒤에서 한다(-408). 경보·VLM 경로가 기다리지 않으니 2 s 에서 5 s 로 늘린다
                    transport=HttpPutTransport(timeout_s=5.0), send_event=self.link.publisher.send_event,
                    local_root=s.state_dir,
                )
                self.event_frames.start()
            except Exception as error:  # noqa: BLE001 - 부가 근거 경로 실패가 관제 기동을 막지 않는다
                self.event_frames = None
                log.warning("이벤트 프레임 기동 실패: %s", type(error).__name__)
        if s.file is not None and s.file.jsonl_dir is not None:
            try:
                self.jsonl = JsonlOutput(s.file.jsonl_dir, Envelope(s.gateway_id, self.clock, new_msg_id=self._new_msg_id),
                                         self._new_msg_id)
            except OSError as e:  # 같은 이름 파일이 있으면(FileExistsError) 덮어쓰지 않는다
                raise Tier0StartError(f"JSONL 출력 파일을 만들지 못했다: {s.file.jsonl_dir} ({type(e).__name__})", 2) from e
        if self.link is not None:
            self.snapshot.attach(self.link.client, self.link.publisher.envelope)
            self.outbox = self.link.outbox
        else:
            self.outbox = self.snapshot.bind(Outbox(
                self.journal, AckedOffsetStore(s.journal_dir / ACKED_NAME, gateway_id=s.gateway_id),
                gateway_id=s.gateway_id, transport=_offline_transport, clock=self.clock))
        # 정리는 SnapshotOutbox.compact 로만(스냅샷이 접지 않은 기록을 지우지 않게, -81)
        self.checkpointer = Checkpointer(self.journal, self.store, r.projection, clock=self.clock,
                                         compact=self.outbox.compact, interval_s=s.checkpoint_interval_s)
        # 첫 인지 tick 전에 쓰는 체크포인트도 복구가 알던 값을 잇는다(Track 시각은 원래 값 그대로라 30 s 규칙이 그대로 선다)
        self.checkpointer.update_inputs(config_version=self._config_version(), last_capture_time=r.last_capture_time,
                                        tracks_t=r.tracks_t, tracks=r.tracks)
        self.judge = JudgmentLoop(
            lock=self.judge_lock, now_ms=self.clock.now_ms, config_sync=self.config_sync, perception=self.perception,
            tracker=self.tracker, journal=self.journal, writer=self.writer, calibration_state=self._calibration_state,
            send_i0=None if self.control is None else self.control.send,
            offer_world_state=(self.jsonl.world_state if self.jsonl is not None
                               else None if self.link is None else self.link.publisher.offer_world_state),
            record_transitions=None if self.jsonl is None else self.jsonl.transitions,
            checkpoint_inputs=self.checkpointer.update_inputs, clips=self.clips, event_frames=self.event_frames,
            snapshot=self.snapshot,
            observe_progress=lambda t: self.watchdog.observe(t, self._mono_ns()), restore_link=self.restore_link,
            new_msg_id=self._new_msg_id,
            begin_tick=self._begin_calibration_tick,
        )
        if not s.human_calibration_enabled:
            log.info("사람 관절 보정 꺼짐")
        if s.human_calibration_enabled and s.drift_watch_enabled and self._drift_watch_factory is not None:
            # 보정 감시(-117): 저장한 기준선을 검증 여부 그대로 돌려주고(CalibrationDriftWatch(baselines=...)), 정리 주기마다 저장한다
            self.drift_watch = self._drift_watch_factory(load_baselines(s.baselines_file))
        elif s.human_calibration_enabled and s.drift_watch_enabled:
            self.drift_watch = BackgroundDriftWatch(self._apply_drift_report,
                                                   baselines=load_baselines(s.baselines_file))
            self._drift_document = self.calibration.current()
            self.perception.observe_association = lambda assoc: self.drift_watch.offer(
                self._drift_document, assoc, tuple(getattr(self.perception.feed, "camera_streams", {}).values()))

        # 6) 시작
        if s.human_calibration_enabled and s.live_calibration.enabled:
            try:
                from services.tier0.live_calibration import BackgroundLiveCalibration

                self.live_calibration = BackgroundLiveCalibration(
                    self.calibration.current(), provenance, settings=s.live_calibration,
                    now_ms=self.clock.now_ms, stop=self._stop, propose=self._propose_live_calibration,
                    can_run=lambda: (self.started and self._calibration_state() != "CALIBRATED"
                                     and self._swap_probation is None),
                    active_report=self.calibration.current_report,
                    safety_reference=self.calibration.safety_reference,
                )
                active_report = self.calibration.current_report() or {}
                deviation = active_report.get("cumulative_deviation") or {}
                self.calibration_status.update(cumulative_deviation=deviation,
                                               cumulative_warning=deviation.get("warnings", []))
                if s.live_calibration.safety_enabled and active_report.get("rollback_of"):
                    self.live_calibration.block("되돌림 뒤 자동 보정 보류: 운영자 확인 필요")
                elif s.live_calibration.safety_enabled and active_report.get("consecutive_confirmation"):
                    last = next((r["payload"] for r in reversed(self.calibration.transitions)
                                 if r["payload"]["reason"] == "RECALIBRATED"
                                 and r["payload"]["calibration_version"] == self.rig.calibration_version), None)
                    if last is not None and self.clock.now_ms() <= last["effective_at"] + 60_000:
                        self._swap_probation = SwapProbation(last["previous_calibration_version"],
                            last["calibration_version"], last["effective_at"],
                            fusion_before=((active_report.get("holdout") or {}).get("after") or {}).get("fusion_3plus_ratio"))
                self.perception.observe_frame = self.live_calibration.buffer.append_frame
                self._spawn("tier0-live-calibration", self.live_calibration.run)
            except Exception as exc:
                self._live_calibration_error = str(exc)
                log.exception("후보 보정 기동 실패(판정은 계속한다)")
        if s.clean_background.enabled and isinstance(feed, DetectorFeed):
            try:
                from services.tier0.clean_background import BackgroundCleanBackground

                self.clean_background = BackgroundCleanBackground(
                    self.calibration.current(), provenance, settings=s.clean_background,
                    state_dir=s.state_dir, stop=self._stop)
                feed.observe_decoded = self.clean_background.offer
                self._spawn("tier0-clean-background", self.clean_background.run)
            except Exception:
                log.exception("배경 수집 기동 실패(판정은 계속한다)")
        if self.clean_background is not None and s.rotation_recovery.enabled:
            try:
                from services.tier0.rotation_recovery import BackgroundRotationRecovery

                self.rotation_recovery = BackgroundRotationRecovery(
                    self.calibration.current(), provenance, self.clean_background,
                    settings=s.rotation_recovery, now_ms=self.clock.now_ms, stop=self._stop,
                    propose=self._propose_rotation_calibration,
                    on_drift_report=self._apply_drift_report,
                    on_requires_setup=self._record_requires_setup,
                    in_probation=lambda: self._swap_probation is not None,
                    reference_document=lambda: self.calibration.safety_reference()[0],
                )
                active_report = self.calibration.current_report() or {}
                if s.rotation_recovery.safety_enabled and active_report.get("rollback_of"):
                    self.rotation_recovery.block("되돌림 뒤 자동 보정 보류: 운영자 확인 필요")
                elif (s.rotation_recovery.safety_enabled and active_report.get("consecutive_confirmation")
                      and self._swap_probation is None):
                    last = next((r["payload"] for r in reversed(self.calibration.transitions)
                                 if r["payload"]["reason"] == "RECALIBRATED"
                                 and r["payload"]["calibration_version"] == self.rig.calibration_version), None)
                    if last is not None and self.clock.now_ms() <= last["effective_at"] + 60_000:
                        self._swap_probation = SwapProbation(last["previous_calibration_version"],
                                                             last["calibration_version"], last["effective_at"])
                self._spawn("tier0-rotation-recovery", self.rotation_recovery.run)
            except Exception as exc:
                self._rotation_recovery_error = str(exc)
                log.exception("회전 복구 기동 실패(판정은 계속한다)")
        if self.event_frames is not None and isinstance(feed, DetectorFeed):
            self.event_frames.attach(feed)
        if isinstance(self.drift_watch, BackgroundDriftWatch):
            self._spawn("tier0-drift-watch", lambda: self.drift_watch.run(self._stop))
        self.writer.start()
        self._event("journal_writer")
        self.checkpointer.start()
        self._event("checkpointer")
        if self.link is not None:
            self.link.start()
            self._event("i1")
        if self.control is not None:
            self.control.start()
            self._event("i0_control")
        if self.live_input is not None:
            try:
                self.live_input.start()
            except OSError as e:
                raise Tier0StartError(f"LIVE 입력: I0 영상 포트를 열지 못했다 ({type(e).__name__}: {e})", 4) from e
            self._event("i0_video")
        if isinstance(feed, DetectorFeed) and self.file_input is None and (
            self.live_input is not None or s.input_mode == "live" or feed.async_mode
        ):
            feed.async_mode = True
            self._spawn("tier0-detect", self._run_detect)
        self._spawn("tier0-housekeeping", self._run_housekeeping)
        self._spawn("tier0-judge", self._run_judge)
        self._event("judge")
        self.started = True
        self._write_marker()

    def _abort_start(self) -> None:
        """기동이 중간에 실패했다: 이미 시작한 스레드를 멈추고 저널을 닫는다. stop 과 같은 deadline 원칙(디스크가 멈춰도
        기다리지 않는다)을 따르고, 마지막 체크포인트는 쓰지 않는다(시작하지 못한 상태다). 정리 주기를 멈춘 뒤라 표시 파일을
        다시 쓰지 않으며, 표시 파일 정리는 이어서 start() 가 한다(_start_failed_marker: 이번 것은 지우고 이전 비정상 종료면
        start_failed 로 남김)."""
        failures = self._shutdown(time.monotonic() + self.settings.drain_s, final_checkpoint=False)
        if failures:
            log.error("기동 실패 정리 중 끝나지 않은 단계: %s", failures)

    def _restore_config(self, r: RecoveryResult, registry: Any, known: Callable[[str], bool]) -> ConfigBundle | None:
        s = self.settings
        check: dict[str, Any] = {"checkpoint_config_version": r.config_version, "result": None, "restored": None}
        bundle: ConfigBundle | None = None
        if r.config_version is not None:
            try:
                bundle = self.archive.restore(r.config_version, registry=registry, calibration_known=known, clock=self.clock)
                check["result"] = "MATCHED"
            except KeyError as e:
                check["result"] = "MISSING_IN_ARCHIVE"
                log.error("체크포인트의 구성 묶음 %s 를 보관소에서 찾지 못했다: %s", r.config_version, e)
            except BundleRejected as e:
                check["result"] = "ARCHIVE_REJECTED"
                log.error("보관한 구성 묶음 %s 가 검증을 통과하지 못했다: %s", r.config_version, e)
        else:
            notices = self.archive.notices()
            if notices:
                newest = notices[-1]["payload"]
                try:
                    bundle = self.archive.restore(newest["config_version"], registry=registry, calibration_known=known,
                                                  clock=self.clock, bundle_hash=newest["bundle_hash"])
                    check["result"] = "ARCHIVE_NEWEST"
                except (KeyError, BundleRejected) as e:
                    check["result"] = "ARCHIVE_REJECTED"
                    log.error("보관소의 최근 구성 묶음을 복구하지 못했다: %s", e)
        if bundle is None and s.bootstrap_config_version is not None and s.bootstrap_archive is not None:
            try:
                bundle = BundleArchive(s.bootstrap_archive).restore(
                    s.bootstrap_config_version, registry=registry, calibration_known=known, clock=self.clock)
                check["result"] = (check["result"] or "NO_CHECKPOINT_BUNDLE") + "+BOOTSTRAP"
                if r.config_version is not None and r.config_version != bundle.config_version:
                    log.warning("체크포인트 묶음(%s)과 다른 시작 묶음(%s)으로 판정한다", r.config_version, bundle.config_version)
                self.archive.save(bundle)
            except (KeyError, BundleRejected, OSError) as e:
                log.error("시작 구성 묶음 %s 를 복구하지 못했다: %s", s.bootstrap_config_version, e)
        check["restored"] = None if bundle is None else bundle.config_version
        if bundle is None:
            log.error("적용할 구성 묶음이 없다: EC2 의 config_bundle 을 받기 전까지 위험 판정을 하지 않는다(health UNSAFE_UNKNOWN)")
        self.config_check = check
        self._event(f"config:{check['result']}:{check['restored']}")
        return bundle

    def _open_recording(self) -> None:
        """file 입력: 녹화를 열고(manifest 계약·세그먼트 파일 크기·sha256·프레임 줄 확인) 시계를 녹화 시각으로 바꾼다."""
        f = self.settings.file
        assert f is not None
        try:
            self._replay = RecordingReplay(f.session_dir, f.camera_ids)
        except (OSError, ValueError, KeyError, TypeError) as e:
            raise Tier0StartError(f"녹화를 열지 못했다: {f.session_dir} ({type(e).__name__}: {e})", 4) from e
        for r in self._replay.capture_time_repairs:  # 깨진 PTS 시각선 → 프레임 시계(S15P31A101-315). 녹화 파일은 그대로다
            if r.broken_since is not None:
                log.warning("녹화 재생: %s epoch %d 의 PTS 시각선이 깨져 frame_sequence %d 부터 촬영 시각을 프레임 시계로 "
                            "다시 매긴다(바꾼 프레임 %d 장)", r.camera_id, r.stream_epoch, r.broken_since, r.repaired)
            elif r.skipped is not None and r.skipped != "disabled":
                log.info("녹화 재생: %s epoch %d 의 촬영 시각은 녹화 헤더 그대로다(깨짐 감지 불가: %s)",
                         r.camera_id, r.stream_epoch, r.skipped)
            if r.raised:  # 단조 보장(S15P31A101-316): 뒤로 가거나 같은 촬영 시각을 직전 + 1 ms 로 올림
                log.warning("녹화 재생: %s epoch %d 의 촬영 시각 %d 장이 직전 값보다 뒤로 가거나 같아 직전 + 1 ms 로 올린다"
                            "(최대 %d ms, 불확실성도 그만큼 키움)", r.camera_id, r.stream_epoch, r.raised, r.max_raise_ms)
        self.replay_clock = ReplayClock(self._replay.start_ms, f.speed)
        self._mono_ns, self._wall_ms = self.replay_clock.mono_ns, self.replay_clock.wall_ms

    def _build_feed(self, now: int) -> FrameFeed:
        s = self.settings
        if s.file is not None:
            return self._build_file_feed()
        if s.input_mode == "sim":
            assert s.sim is not None
            anchor = s.sim.anchor_wall_ms if s.sim.anchor_wall_ms is not None else now
            try:
                return SimObservationFeed(s.sim.run_dir, anchor_wall_ms=anchor, arrival_lag_ms=s.sim.arrival_lag_ms,
                                          max_backlog_ms=s.sim.max_backlog_ms)
            except (OSError, ValueError) as e:
                raise Tier0StartError(f"SIM 입력을 열지 못했다: {e}", 4) from e
        return self._build_live_feed()

    def _build_live_feed(self) -> FrameFeed:
        """LIVE: I0 영상 수신 → 디코딩 → 최신성 대기열 → YOLO → Local Tracker (live_input.py). 영상 수신은 step 6 에서 연다."""
        s = self.settings
        assert s.live is not None
        detector = self._build_detector()
        decoder_factory = self._decoder_factory(s.live.decoder)
        config = FreshnessConfig(queue_depth=s.live.queue_depth, max_queue_wait_ms=s.live.max_queue_wait_ms,
                                 reorder_window_ms=0)  # 보간 창 0: live_input 모듈 설명
        try:
            ingest = I0Ingest(s.camera_ids, decoder_factory, self.clock, config)
        except DecoderUnavailable as e:
            raise Tier0StartError(f"LIVE 입력: 디코더({s.live.decoder})를 열지 못했다: {e}", 4) from e
        try:
            live = LiveInput(ingest, lambda consumer: build_server(s.env, consumer))
        except (ValueError, OSError) as e:
            raise Tier0StartError(f"LIVE 입력: I0 영상 수신 설정 오류: {e}", 2) from e
        tracker_kwargs: dict[str, Any] = {"camera_ids": s.camera_ids}
        if self._tracker_backend_factory is not None:
            tracker_kwargs["backend_factory"] = self._tracker_backend_factory
        tracker = MultiCameraLocalTracker(**tracker_kwargs)
        self.live_input = live
        self.reid_collector = self._build_reid_collector()
        return DetectorFeed(camera_ids=s.camera_ids, tickets=ingest.next_batch, detector=detector, tracker=tracker,
                            connected=live.connected, source="LIVE", on_close=live.stop,
                            max_queued_per_camera=s.detect_queue_depth, async_mode=True,
                            reid_collector=self.reid_collector)

    def _build_file_feed(self) -> FrameFeed:
        """file: 녹화 I0 메시지 → I0Ingest(LIVE 와 같은 디코딩·최신성 대기열) → YOLO → Local Tracker (file_input.py)."""
        s = self.settings
        f, replay = s.file, self._replay
        assert f is not None and replay is not None
        missing = [c for c in replay.camera_ids if c not in self.rig.cameras]
        if missing:
            raise Tier0StartError(f"보정 {self.rig.calibration_version} 에 없는 녹화 카메라: {missing}", 2)
        detector = self._build_detector()
        config = FreshnessConfig(queue_depth=f.queue_depth, max_queue_wait_ms=f.max_queue_wait_ms,
                                 reorder_window_ms=0)  # 녹화 프레임은 frame_sequence 가 이어진다(RecordingReplay 가 확인)
        try:
            ingest = I0Ingest(replay.camera_ids, self._decoder_factory(f.decoder), self.clock, config)
        except DecoderUnavailable as e:
            raise Tier0StartError(f"file 입력: 디코더({f.decoder})를 열지 못했다: {e}", 4) from e
        tracker_kwargs: dict[str, Any] = {"camera_ids": replay.camera_ids}
        if self._tracker_backend_factory is not None:
            tracker_kwargs["backend_factory"] = self._tracker_backend_factory
        self.file_input = FileInput(replay, ingest, self.replay_clock)
        self.reid_collector = self._build_reid_collector()
        return DetectorFeed(camera_ids=replay.camera_ids, tickets=ingest.next_batch, detector=detector,
                            tracker=MultiCameraLocalTracker(**tracker_kwargs), connected=self.file_input.connected,
                            source="FILE", on_close=self.file_input.close, provenance=replay.provenance(),
                            max_queued_per_camera=s.detect_queue_depth,
                            reid_collector=self.reid_collector)

    def _build_reid_collector(self) -> Any:
        from simai_ai.reid.collector import AsyncReidCollector, ReidCollectorConfig

        r = self.settings.reid
        cfg = ReidCollectorConfig(
            enabled=r.enabled,
            batch_size=r.batch_size,
            update_interval_ms=r.update_interval_ms,
            stable_update_interval_ms=r.stable_update_interval_ms,
            max_crops_per_second=r.max_crops_per_second,
            max_queue_size=r.max_queue_size,
            cope_root=r.cope_root,
            weights_path=r.weights_path,
            clip_model_path=r.clip_model_path,
            use_face_aux=r.use_face_aux,
        )
        return AsyncReidCollector(cfg, start_worker=True)

    def _build_detector(self) -> PoseDetector:
        """TensorRT 엔진(SIMAI_TIER0_ENGINE) → 없으면 .pt 가중치(SIMAI_TIER0_WEIGHTS, 경고) → 둘 다 없으면 종료 코드 4."""
        if self._detector_factory is not None:
            return self._detector_factory()
        from services.tier0.yolo_detector import DetectorUnavailable, UltralyticsPoseDetector

        s = self.settings
        model = s.engine_path if s.engine_path and Path(s.engine_path).exists() else None
        if model is None and s.weights_path and Path(s.weights_path).exists():
            log.warning("TensorRT 엔진이 없어(SIMAI_TIER0_ENGINE=%r) 가중치 %s 를 ultralytics 로 연다(느리다, 운영 기준 아님)",
                        s.engine_path, s.weights_path)
            model = s.weights_path
        if model is None:
            raise Tier0StartError(f"{s.input_mode.upper()} 입력: 검출기가 없다(SIMAI_TIER0_ENGINE={s.engine_path!r}, "
                                  f"SIMAI_TIER0_WEIGHTS={s.weights_path!r} 둘 다 없음)", 4)
        try:
            return UltralyticsPoseDetector(model, imgsz=s.imgsz)
        except DetectorUnavailable as e:
            raise Tier0StartError(f"{s.input_mode.upper()} 입력: {e}", 4) from e
        except Exception as e:  # noqa: BLE001 - 엔진·가중치를 읽지 못함(손상·GPU 불일치)
            raise Tier0StartError(f"{s.input_mode.upper()} 입력: 검출기 {model} 를 열지 못했다 ({type(e).__name__}: {e})",
                                  4) from e

    @staticmethod
    def _decoder_factory(kind: str) -> Callable[[str], Any]:
        """카메라마다 디코더 하나. nvdec 은 CPU 로 몰래 떨어지지 않는다(pyav_decoder)."""
        if kind == "nvdec":
            return lambda _camera_id: NvdecDecoder()
        return lambda _camera_id: PyAvDecoder()

    def _build_link(self) -> I1Link:
        s = self.settings
        assert s.i1 is not None
        connector = WebSocketConnector.from_settings(s.i1)
        envelope = Envelope(s.gateway_id, self.clock)
        registrar = EndpointRegistrar(_NoTransport(), clock_ms=self.clock.now_ms, sleep=lambda _s: None)
        env = dict(s.env)
        link_ref: dict[str, I1Link] = {}

        def health_payload() -> dict[str, Any] | None:
            link = link_ref.get("link")
            drops = None if link is None else link.publisher.world_state_drops()
            return with_world_state_drops(self._live_health(self.health.payload(), "i1"), drops)

        link = build_i1_link(
            connector,
            envelope,
            health_payload=health_payload,
            health_fields=[self._site_fields],
            make_outbox=lambda transport: self.snapshot.bind(Outbox(
                self.journal, AckedOffsetStore(s.journal_dir / ACKED_NAME, gateway_id=s.gateway_id),
                gateway_id=s.gateway_id, transport=transport, clock=self.clock)),
            config_sync=self.config_sync,
            on_event_ack=lambda ack: self.judge.acknowledge(ack),
            ack_seen=self.journal.has_ack_id,
            on_event_close=lambda request: self.judge.close(request),
            on_event_escalate=lambda request: self.judge.escalate(request),
            endpoint_build=lambda: registrar.build(env, read_cert_pem(env)),
        )
        link.dispatcher.register("scene_reference", self._accept_scene_reference)
        link_ref["link"] = link
        self.calibration.ensure_scene_snapshot(effective_at=self.clock.now_ms(), msg_id=self._new_msg_id())
        link.publisher.add_source(self._calibration_messages)
        return link

    # --- 검증된 보정 후보(-344). 후보 계산은 -345의 백그라운드 실행기가 한다. ----------------

    def _record_requires_setup(self, camera_id: str, reason: str) -> None:
        with self.judge_lock:
            self.calibration_status["re_setup_required"] = True
            self.calibration_status["re_setup_reason"] = f"현장 세팅 다시 하기: {camera_id} ({reason})"
            self.calibration_status["re_setup_camera_id"] = camera_id
            # 처음 감지한 시각은 해소되기 전에는 바뀌지 않는다(계약 re_setup_camera.detected_at). 사유만 최신으로 고친다
            known = self._re_setup_cameras.get(camera_id)
            self._re_setup_cameras[camera_id] = {
                "reason": reason or "사유 미상",
                "detected_at": self.clock.now_ms() if known is None else known["detected_at"],
            }

    def re_setup_field(self) -> list[dict[str, Any]]:
        """health.re_setup 값(계약 2.2.0): 재세팅이 필요한 카메라 목록(camera_id 오름차순), 없으면 빈 배열. 판정은 건드리지 않는다."""
        with self.judge_lock:
            return [{"camera_id": cid, "reason": item["reason"], "detected_at": item["detected_at"]}
                    for cid, item in sorted(self._re_setup_cameras.items())]

    def _propose_rotation_calibration(self, document, report, generation: int) -> bool:
        with self.judge_lock:
            rot = self.rotation_recovery
            if rot is None or generation != rot.generation or rot.blocked_reason is not None:
                return False
            if rot.safety_enabled and self._swap_probation is not None:
                return False
            accepted = self.propose_calibration(document, report)
            if accepted:
                self._live_pending_version = document["calibration_version"]
            return accepted

    def _propose_live_calibration(self, document, report, generation: int) -> bool:
        with self.judge_lock:
            live = self.live_calibration
            if live is None or generation != live.generation or live.blocked_reason is not None:
                log.warning("보정 후보 거부: 재수집/활성 버전 변경 또는 자동 보정 보류")
                return False
            if live.safety_enabled:
                if self._swap_probation is not None:
                    return False
                if not live.confirmation.observe(report):
                    self.calibration_status["last_decision"] = {
                        "accepted": False, "reason": "새 관측에 의한 연속 개선 확인 대기"}
                    return False
            accepted = self.propose_calibration(document, report)
            if accepted:
                self._live_pending_version = document["calibration_version"]
            return accepted

    def propose_calibration(self, document: Mapping[str, Any], report: Mapping[str, Any]) -> bool:
        """다른 스레드에서 호출 가능. True는 대기 수락이며 활성화는 다음 판정 tick이다."""
        with self.judge_lock:
            try:
                if not self.started or self._stop.is_set():
                    raise ValueError("실행 중인 서비스가 아니다")
                if self._pending_calibration is not None:
                    raise ValueError("이미 대기 중인 보정 후보가 있다")
                doc = self.calibration.prepare(document, report)
                rig = CameraRig.from_provider(FileCalibrationProvider(self.calibration.path(doc["calibration_version"])))
                self._pending_calibration = doc, rig
                self.calibration_status["pending_version"] = rig.calibration_version
                self.calibration_status["last_decision"] = {
                    "accepted": True, "reason": "PROVISIONAL 후보 검증 통과, 다음 tick 전환 대기",
                    "comparison": self.calibration.last_validation}
                log.info("보정 후보 채택: %s, 비교=%s", rig.calibration_version, self.calibration.last_validation)
                return True
            except (OSError, ValueError, KeyError, TypeError) as exc:
                clock = getattr(self, "clock", None)
                self.calibration_status["last_rejection"] = {
                    "at_ms": None if clock is None else clock.now_ms(), "reason": str(exc)}
                self.calibration_status["last_decision"] = {
                    "accepted": False, "reason": str(exc), "comparison": getattr(exc, "comparison", None)}
                log.warning("보정 후보 거부: %s, 비교=%s", exc, getattr(exc, "comparison", None))
                return False

    def _adopt_setup_calibration(self, bundle) -> str | None:
        """구성 묶음 적용 직전(판정 경계, 판정 lock 안)에 세팅 보정을 채택한다(-467). None 이면 묶음 적용, 문자열이면 거부 사유.

        세팅 보정은 운영 중 후보와 달리 2회 연속 확인·교체 뒤 감시(SwapProbation)를 거치지 않고 새 World 계보의 시작으로 바로
        기준이 된다. 채택 조건(계보·content_hash·계약·현장·카메라)을 다시 보고, 하나라도 어긋나면 저장·전환하지 않고 묶음을
        거부해 이전 보정·묶음으로 판정을 계속한다. 전환은 calibration_status.setup_adoption·로그·calibration_update 로 남긴다.
        """
        if not getattr(bundle, "setup_calibration", False):
            return None
        ref = bundle.notice.components["calibration"]
        at_ms = self.clock.now_ms()
        try:
            doc = json.loads(bundle.blobs[ref.sha256])
            version = doc["calibration_version"]
            if self.calibration.known(version) and self.rig.calibration_version == version:
                return None  # 이미 채택했다(같은 묶음을 다시 적용하는 경우)
            self.calibration.prepare_setup(doc, received_at=at_ms, config_version=bundle.config_version)
            rig = CameraRig.from_provider(FileCalibrationProvider(self.calibration.path(version)))
            previous_version = self.rig.calibration_version
            self.calibration.activate(doc, effective_at=at_ms, msg_id=self._new_msg_id(), new_world=True)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.calibration_status["last_rejection"] = {"at_ms": at_ms, "reason": f"세팅 보정 채택 거부: {exc}"}
            log.error("세팅 보정 채택 거부(묶음 %s, 이전 보정 유지): %s", bundle.config_version, exc)
            return f"세팅 보정 채택 거부: {exc}"
        provenance = build_input_provenance(self.perception.feed.provenance(), calibration=rig.calibration_provenance)
        self.perception.replace_rig(rig, provenance)
        self.rig = rig
        # 세팅 보정은 감시·되돌림 대상이 아니다. 이전 World 기준의 대기 후보·감시·근거는 버린다
        self._swap_probation = None
        self._live_pending_version = None
        self._pending_calibration = None
        if self.live_calibration is not None:
            self.live_calibration.activate(doc, provenance)
        if self.rotation_recovery is not None:
            self.rotation_recovery.activate(doc, provenance)
        self._drift_document = doc
        if self.clean_background is not None:
            self.clean_background.activate(doc, provenance)
        self._drift_report = self._pending_drift_report = None
        adoption = {"version": version, "previous_version": previous_version, "setup_session_id": doc.get("setup_session_id"),
                    "config_version": bundle.config_version, "at_ms": at_ms}
        self.calibration_status.update(pending_version=None, last_rejection=None, effective_at=at_ms, probation=None,
                                       re_setup_required=False, re_setup_reason=None, re_setup_camera_id=None,
                                       setup_adoption=adoption)
        self._re_setup_cameras.clear()  # 재세팅이 끝났다: health.re_setup 을 비운다(-480)
        log.info("세팅 보정 채택: %s → %s (세션 %s, 묶음 %s, 새 World 기준)", previous_version, version,
                 doc.get("setup_session_id"), bundle.config_version)
        self._event(f"calibration_switched:{version}:{at_ms}")
        return None

    def _begin_calibration_tick(self) -> None:
        if self._swap_probation is not None and self.clock.now_ms() > self._swap_probation.started_ms + self._swap_probation.window_ms:
            self.calibration_status["probation"] = "감시 기간 종료(관측 없음은 정상 확인을 뜻하지 않음)"
            self._swap_probation = None
        pending = self._pending_calibration
        if pending is None:
            return
        doc, rig = pending
        previous_version = self.rig.calibration_version
        at_ms = self.clock.now_ms()
        provenance = build_input_provenance(self.perception.feed.provenance(), calibration=rig.calibration_provenance)
        try:
            self.calibration.activate(doc, effective_at=at_ms, msg_id=self._new_msg_id())
        except (OSError, ValueError) as exc:
            self.calibration_status["last_rejection"] = {"at_ms": at_ms, "reason": f"활성화 저장 실패: {exc}"}
            log.error("보정 전환 보류: %s", exc)
            return  # 같은 후보를 다음 tick에 다시 저장하며 직전 rig로 판정을 계속한다
        self.perception.replace_rig(rig, provenance)
        self.rig = rig
        if (((self.live_calibration is not None and self.live_calibration.safety_enabled)
                or (self.rotation_recovery is not None and self.rotation_recovery.safety_enabled))
                and getattr(self, "_live_pending_version", None) == doc["calibration_version"]):
            report = self.calibration.current_report() or {}
            self._swap_probation = SwapProbation(previous_version, rig.calibration_version, at_ms,
                fusion_before=((report.get("holdout") or {}).get("after") or {}).get("fusion_3plus_ratio"))
            self.calibration_status["probation"] = "교체 뒤 감시 중"
            deviation = report.get("cumulative_deviation") or {}
            self.calibration_status["cumulative_deviation"] = deviation
            self.calibration_status["cumulative_warning"] = deviation.get("warnings", [])
            if deviation.get("warnings"):
                log.warning("최초 기준 대비 누적 편차 초과: %s", deviation)
        else:
            self._swap_probation = None
        self._live_pending_version = None
        if self.live_calibration is not None:
            self.live_calibration.activate(doc, provenance)
            if self.live_calibration.safety_enabled and (self.calibration.current_report() or {}).get("rollback_of"):
                self.live_calibration.block("되돌림 뒤 자동 보정 보류: 운영자 확인 필요")
        if self.rotation_recovery is not None:
            self.rotation_recovery.activate(doc, provenance)
            if self.rotation_recovery.safety_enabled and (self.calibration.current_report() or {}).get("rollback_of"):
                self.rotation_recovery.block("되돌림 뒤 자동 보정 보류: 운영자 확인 필요")
        self._drift_document = doc
        if self.clean_background is not None:
            self.clean_background.activate(doc, provenance)
        self._drift_report = self._pending_drift_report = None
        self._pending_calibration = None
        self.calibration_status.update(pending_version=None, last_rejection=None, effective_at=at_ms,
                                       re_setup_required=False, re_setup_reason=None, re_setup_camera_id=None)
        self._re_setup_cameras.clear()  # 새 보정이 활성화됐다: health.re_setup 을 비운다(-480)
        self._event(f"calibration_switched:{rig.calibration_version}:{at_ms}")

    def _apply_drift_report(self, report: MonitorReport, changed_streams: tuple[str, ...]) -> None:
        """보조 스레드에서 계산한 근거만 반영한다. 새 rig에 옛 근거를 적용하지 않는다."""
        notify = []
        with self.judge_lock:
            if self._stop.is_set() or report.calibration_version != self.rig.calibration_version:
                return
            self._drift_report = report
            if (self._pending_calibration is not None
                    and self.calibration_status.get("probation") == "악화 지속: 되돌림 대기"):
                return
            if (not changed_streams and self._swap_probation is not None
                    and self._swap_probation.observe(report, report.fusion_3plus_ratio)):
                try:
                    doc = self.calibration.prepare_rollback(self._swap_probation.previous_version,
                                                           created_at=self.clock.now_ms())
                    rig = CameraRig.from_provider(FileCalibrationProvider(self.calibration.path(doc["calibration_version"])))
                    self._pending_calibration = doc, rig
                    self.calibration_status.update(pending_version=rig.calibration_version,
                        probation="악화 지속: 되돌림 대기", rollback_from=self.rig.calibration_version)
                    self._swap_probation = None
                    if self.live_calibration is not None:
                        self.live_calibration.block("교체 뒤 악화: 되돌림 대기")
                    if self.rotation_recovery is not None:
                        self.rotation_recovery.block("교체 뒤 악화: 되돌림 대기")
                    log.warning("보정 교체 뒤 악화 지속: 새 버전 %s로 되돌림 대기", rig.calibration_version)
                    return
                except (OSError, ValueError) as exc:
                    log.error("되돌림 보관 실패(다음 근거에서 재시도): %s", exc)
            if report.findings:
                self._pending_drift_report = report
            pending = self._pending_drift_report
            if pending is not None:
                # 저장 실패 시 근거를 유지해 다음 표본 때 다시 저장한다(감시기는 확정 근거를 한 번만 낸다).
                self.calibration.report_drift(pending, effective_at=self.clock.now_ms(), msg_id=self._new_msg_id())
                self._pending_drift_report = None
            if self.calibration.state == "DRIFTED" and not changed_streams:
                for camera in report.cameras:
                    key = report.calibration_version, camera.camera_id
                    cp = camera.change_point_ms
                    newer = cp is not None and cp > self._live_change_points.get(key, -1)
                    if camera.status is MonitorStatus.DRIFTED and (key not in self._drift_notified or newer):
                        self._drift_notified.add(key)
                        self._live_change_points[key] = report.t_ms if cp is None else cp
                        notify.append(camera.camera_id)
            if self.live_calibration is not None:
                if changed_streams:
                    if self.live_calibration.blocked_reason != "스트림 변경: 내부 파라미터 재추정 필요":
                        self.live_calibration.block("스트림 변경: 내부 파라미터 재추정 필요")
                elif self.calibration.state == "DRIFTED":
                    if notify and self.live_calibration.blocked_reason == "틀어짐 귀속 불확실: 운영자 확인 필요":
                        self.live_calibration.blocked_reason = None
                    elif not any(c.status is MonitorStatus.DRIFTED for c in report.cameras):
                        if self.live_calibration.blocked_reason is None:
                            self.live_calibration.block("틀어짐 귀속 불확실: 운영자 확인 필요")
                for cid in notify:
                    cp = next((c.change_point_ms for c in report.cameras if c.camera_id == cid), None)
                    self.live_calibration.reset_camera(cid, cp)
                # reset 이전에 만들어 대기하던 자동 후보도 폐기한다. 수동 후보(-344)는 기준 검증을 유지한다.
                if (notify or self.live_calibration.blocked_reason) and getattr(self, "_live_pending_version", None):
                    if self.calibration_status.get("pending_version") == self._live_pending_version:
                        self._pending_calibration = None
                        self.calibration_status["pending_version"] = None
            if self.rotation_recovery is not None:
                if changed_streams:
                    self.rotation_recovery.block("스트림 변경: 내부 파라미터 재추정 필요")
                for cid in notify:
                    self.rotation_recovery.reset_camera(cid)
            if self.clean_background is not None:
                for cid in set(notify) | set(changed_streams):
                    self.clean_background.reset_camera(cid)
        # -345 reset_camera의 연결점. 무거운 후보 계산/콜백은 판정 lock 밖이다.
        for cid in notify:
            if self._on_camera_drifted is not None:
                try:
                    self._on_camera_drifted(cid)
                except Exception:
                    log.exception("카메라 재수집 콜백 실패: %s", cid)

    def _accept_scene_reference(self, message):
        """자산 조회 없이 EC2의 현재 Scene 참조만 다음 world 프레임에 적용한다."""
        payload = message.get("payload", {})
        version = payload.get("scene_version")
        schema_version = message.get("schema_version")
        if not isinstance(schema_version, str) or schema_version.split(".")[0] != "1":
            return
        with self.judge_lock:
            site = self._scene_site_id
            if not site or payload.get("site_id") != site:
                return
            if isinstance(version, str) and version.strip():
                self.perception.scene_version = version

    def _calibration_messages(self):
        """I1 신뢰 대기열 source. 재시작 때 같은 msg_id의 전환 이력을 다시 보내 유실을 막는다."""
        with self.judge_lock:
            records = self.calibration.transitions[self._calibration_message_cursor:]
            messages = [self.link.publisher.envelope.make("calibration_update", r["payload"], msg_id=r["msg_id"])
                        for r in records]
            self._calibration_message_cursor += len(records)
            return messages

    def _config_health_fields(self) -> dict[str, Any]:
        fields = dict(self.config_sync.health_fields())
        active = self.config_sync.active
        if active is not None and active.calibration_version != self.rig.calibration_version:
            fields["config_sync"] = "STALE"
        return fields

    # --- 상태 조회 ------------------------------------------------------------------

    def _gateway_ok(self) -> bool:
        return self.control is not None and self.gateway_link.ok(self.mono_ms())

    def _ec2_ok(self) -> bool:
        return self.link is not None and self.link.client.connected

    def _config_version(self) -> str | None:
        active = self.config_sync.active
        return None if active is None else active.config_version

    def _calibration_state(self) -> str:
        if self.drift_watch is not None and hasattr(self.drift_watch, "store"):
            snap = self.drift_watch.store.snapshot()
            if snap is not None and snap.calibration_version == self.rig.calibration_version:
                return str(snap.state)
        return str(self.calibration.state)

    def _health_capture_clock(self) -> Callable[[bool], Any] | None:
        """health 의 촬영 시계 입력. None 이면 SAME_CLOCK(노트북 없는 SIM 만). LIVE 에 제어 채널이 없으면 늘 모름(None 반환)."""
        if self.clock_sync is not None:
            return self.clock_sync.capture_clock
        return (lambda _gateway_ok: None) if self._clock_unknown else None

    def _health_laptop_offset(self) -> Callable[[bool], Any] | None:
        if self.clock_sync is not None:
            return self.clock_sync.health_field
        return (lambda _gateway_ok: None) if self._clock_unknown else None

    def _live_health(self, payload: dict[str, Any], consumer: str) -> dict[str, Any]:
        """LIVE 입력이면 cameras[] 에 프레임 집계·키프레임 대기 상태를 더한다(live_input.LiveInput.merge_health)."""
        if self.live_input is None:
            return payload
        return self.live_input.merge_health(payload, consumer) or payload

    def _site_fields(self) -> dict[str, Any]:
        """I1 health 의 active_site_alerts·as_of. 복원 전에는 싣지 않는다(빈 목록은 '경보 없음'이라 지어내지 않는다)."""
        if not self._restored:
            return {}
        return self.resync.fields()

    def _send_laptop_health(self, fields: dict[str, Any]) -> None:
        """SiteAlertResync 의 송신: health 전체를 만들어 노트북 제어 채널 대기열에 넣는다(판정 lock 안에서 불린다)."""
        if self.control is None:
            return
        drops = None if self.link is None else self.link.publisher.world_state_drops()
        payload = with_world_state_drops(self._live_health(self.health.payload(), "laptop"), drops) or {}
        payload.update(fields)
        if not self.control.send("health", payload):
            raise ConnectionError("I0 제어 채널이 끊겼다")

    # --- 노트북 제어 채널 훅 ----------------------------------------------------------

    def _on_laptop_connected(self, generation: int) -> None:
        self.gateway_link.control_connected(self.mono_ms())
        if self.clock_sync is not None:
            self.clock_sync.link_lost()  # 이전 연결의 표본을 쓰지 않는다
            try:
                self.clock_sync.send_request()  # 접속 직후 바로 교환
            except ConnectionError:
                pass
        self.resync.control_connected()  # 주기를 기다리지 않고 활성 현장 경보 목록(복원 뒤에만)

    def _on_laptop_disconnected(self, generation: int) -> None:
        self.gateway_link.control_disconnected(generation)
        if self.clock_sync is not None:
            self.clock_sync.link_lost()
        self.resync.control_disconnected()

    def _on_laptop_message(self, message: Mapping[str, Any], generation: int) -> None:
        kind = message.get("msg_type")
        if kind == "laptop_heartbeat":
            self.gateway_link.heartbeat(self.mono_ms(), generation)
        elif kind == "clock_sync_response":
            if self.clock_sync is not None:
                self.clock_sync.handle_response(message)
        elif kind == "site_ack":
            try:
                ack = ack_from_contract("site_ack", message["payload"])
            except RiskInputError as e:
                log.warning("site_ack 를 받지 않았다: %s", e)
                return
            self.judge.acknowledge(ack)

    # --- 스레드 ----------------------------------------------------------------------

    def _spawn(self, name: str, target: Callable[[], None]) -> None:
        t = threading.Thread(target=target, name=name, daemon=True)
        self._threads.append(t)
        t.start()

    def _run_detect(self) -> None:
        """LIVE 검출·Local Tracker 루프(판정 lock 밖, -383). 유계 큐에 Observation2DFrame 을 적재한다."""
        feed = self.perception.feed
        if not isinstance(feed, DetectorFeed):
            return
        period = self.settings.detect_tick_ms / 1000.0
        idle_wait = min(period / 4.0, DETECT_IDLE_WAIT_MAX_S)
        next_at = time.monotonic()
        while not self._stop.is_set():
            try:
                processed = self.detect_processing.run(feed.detect_once)
            except Exception:  # noqa: BLE001 - 한 묶음 실패로 검출 루프를 멈추지 않는다
                log.exception("검출 루프 단계 실패")
                processed = 0
            if processed <= 0:
                self._stop.wait(idle_wait)
                next_at = time.monotonic()
                continue
            next_at += period
            delay = next_at - time.monotonic()
            if delay < 0:
                next_at = time.monotonic()
                continue
            self._stop.wait(delay)

    def _run_judge(self) -> None:
        period = self.settings.tick_ms / 1000.0
        next_at = time.monotonic()
        replay = self.file_input
        while not self._stop.is_set():
            if replay is not None:  # file 입력: 재생 시계가 지난 메시지를 넣는다(최대 속도면 시계를 tick_ms 옮긴 뒤)
                if replay.clock.max_speed:
                    replay.clock.advance_ms(self.settings.tick_ms)
                try:
                    replay.pump()
                except Exception as e:  # noqa: BLE001 - 재생 입력 실패는 재생 실패로 남기고 끝낸다(LIVE 와 다름)
                    replay.fail(f"재생 입력 실패 ({type(e).__name__}: {e})")
            try:
                self.judge_processing.run(self.judge.tick)
            except Exception:  # noqa: BLE001 - 한 tick 실패로 판정 루프를 멈추지 않는다(로그·통계)
                self.judge.stats.errors += 1
                log.exception("판정 tick 실패")
            try:
                self.resync.tick()  # local_alert 송신 뒤 같은 판정 스레드에서 1Hz health 를 보낸다(-375)
            except Exception:  # noqa: BLE001 - health 실패로 판정 루프를 멈추지 않는다
                log.exception("판정 스레드 health 송신 실패")
            if replay is not None:
                if self.jsonl is not None and self.jsonl.failed is not None:
                    self.output_failed = dict(self.jsonl.failed)  # 결과를 잃었다: 정상 완료로 보지 않는다
                    replay.fail(f"JSONL 기록 실패: {self.output_failed['file']} {self.output_failed['line']}번째 줄 "
                                f"({self.output_failed['error']})")
                if replay.failed is not None:
                    self._event("replay_failed")
                    self.finished.set()
                    return
                feed = self.perception.feed
                if replay.drained() and (not isinstance(feed, DetectorFeed) or feed.drained()):
                    self._event("replay_finished")
                    self.finished.set()
                    return
                if replay.clock.max_speed:
                    continue  # 실제 시간을 기다리지 않는다
            next_at += period
            delay = next_at - time.monotonic()
            if delay < 0:
                next_at = time.monotonic()
                delay = JUDGE_OVERRUN_YIELD_S
            self._stop.wait(delay)

    def _run_housekeeping(self) -> None:
        last_status = last_marker = last_timing = time.monotonic()
        while not self._stop.wait(HOUSEKEEPING_S):
            try:
                if self.clock_sync is not None and self.control is not None and self.control.connected:
                    if self.clock_sync.request_due():
                        try:
                            self.clock_sync.send_request()
                        except ConnectionError:
                            pass
                now = time.monotonic()
                if now - last_status >= STATUS_EVERY_S:
                    last_status = now
                    self._sample_load()
                    self.health.update_fps()
                    with self.judge_lock:
                        self.clips.poll()
                    self._check_warnings()
                    if self.drift_watch is not None:
                        save_baselines(self.settings.baselines_file, self.drift_watch.baselines())
                if now - last_timing >= 30.0:
                    last_timing = now
                    log.info("[tier0] stage_timing %s", json.dumps({
                        "judge": self.perception.timing.summary(),
                        "detector": (self.perception.feed.timing.summary()
                                     if isinstance(self.perception.feed, DetectorFeed) else None),
                        "input": None if self.live_input is None else self.live_input.bridge.timing.summary(),
                        "reid": None if self.reid_collector is None else self.reid_collector.status(),
                    }))
                if now - last_marker >= MARKER_EVERY_S:
                    last_marker = now
                    self._write_marker()
            except Exception:  # noqa: BLE001
                log.exception("정리 주기 실패")

    def _warn_change(self, key: str, value: Any, message: str) -> None:
        if self._warned.get(key) != value:
            self._warned[key] = value
            if value:
                log.warning(message, value)
            else:
                log.info("해소: %s", key)

    def _check_warnings(self) -> None:
        cp = self.checkpointer
        self._warn_change("checkpointer_stalled", cp.stalled(), "체크포인트를 쓰지 못하고 있다(정체): %s")
        self._warn_change("checkpointer_error", None if cp.last_error is None else repr(cp.last_error),
                          "체크포인트 쓰기 오류: %s")
        self._warn_change("journal_stalled", self.writer.stalled(), "저널 쓰기 정체: %s")
        self._warn_change("journal_failed", None if self.writer.failed is None else repr(self.writer.failed),
                          "저널 쓰기 실패(재시작으로 복구): %s")
        blocked = self.outbox.blocked
        self._warn_change("outbox_blocked", None if blocked is None else f"{blocked.journal_offset}:{blocked.reason}",
                          "EC2 전송 보류(outbox blocked): %s")
        snap = self.snapshot.status()
        self._warn_change("snapshot_broken", snap["broken"], "event_state_snapshot 접기 중단: %s")
        # 같은 상태를 health.tier0_warnings 로도 알린다(읽기만, 판정 lock 밖에서 읽어 기억, -179). 표시 파일 쓰기 실패도(-197)
        self.health.refresh_warnings(WarningSources(cp, self.writer, self.outbox, self.snapshot, self.marker_error))

    def status(self) -> dict[str, Any]:
        """운영 상태(표시 파일·로그). 체크포인트·저널·outbox·스냅샷의 세부 상태를 싣는다(켜진 경고는 health.tier0_warnings 에도)."""
        cp = self.checkpointer
        blocked = self.outbox.blocked
        snap_health = self.health.last
        avatar_writer = getattr(getattr(self, "perception", None), "avatar_writer", None)
        drift_report = self._drift_report
        return {
            "gateway_id": self.settings.gateway_id,
            "pid": os.getpid(),
            "previous_unclean": self.previous_unclean,
            "recovery": None if self.recovery is None else {
                "source": str(self.recovery.source), "restored": len(self.recovery.restored),
                "rejected": [r.name for r in self.recovery.rejected]},
            "restore_link": dict(self.restore_link.status()) if getattr(self, "restore_link", None) is not None else None,
            "config_check": self.config_check,
            "calibration": {**self.calibration_status, "active_version": self.rig.calibration_version,
                            "live_candidate": ({"enabled": (self.settings.human_calibration_enabled and
                                                            self.settings.live_calibration.enabled),
                                                "last_error": self._live_calibration_error}
                                               if self.live_calibration is None else self.live_calibration.status()),
                            "rotation_recovery": ({"enabled": (self.settings.clean_background.enabled and
                                                               self.settings.rotation_recovery.enabled),
                                                   "last_error": self._rotation_recovery_error}
                                                  if self.rotation_recovery is None else self.rotation_recovery.status()),
                            "state": self._calibration_state(),
                            "drift_watch": None if drift_report is None else {
                                "t_ms": drift_report.t_ms, "evidence": str(drift_report.evidence),
                                "cameras": {c.camera_id: {"status": str(c.status), "baseline_verified": c.baseline_verified}
                                            for c in drift_report.cameras}},
                            "config_mismatch": (self.config_sync.active is not None
                                and self.config_sync.active.calibration_version != self.rig.calibration_version)},
            "config_sync": self.config_sync.health_fields(),
            "system_health": None if snap_health is None else str(snap_health.report.system_health),
            "health_reasons": [] if snap_health is None else [str(x) for x in snap_health.report.reasons],
            "checkpointer": {"stalled": cp.stalled(), "last_error": None if cp.last_error is None else repr(cp.last_error),
                             "compacted": cp.compacted},
            "journal": {"next_offset": self.journal.next_offset, "backlog": self.writer.backlog(),
                        "stalled": self.writer.stalled(), "failed": None if self.writer.failed is None else repr(self.writer.failed)},
            "outbox": {"acked_offset": self.outbox.acked_offset, "cursor": self.outbox.cursor,
                       "blocked": None if blocked is None else {"journal_offset": blocked.journal_offset, "reason": blocked.reason}},
            "snapshot": self.snapshot.status(),
            "judge": vars(self.judge.stats).copy(),
            "live_input": None if self.live_input is None else {
                **self.live_input.status(), "detector": vars(self.perception.feed.stats).copy()},
            "file_input": None if self.file_input is None else {
                **self.file_input.status(), "detector": vars(self.perception.feed.stats).copy(),
                "jsonl": None if self.jsonl is None else {"world_state": self.jsonl.world_state_lines,
                                                          "risk_event": self.jsonl.risk_event_lines,
                                                          "failed": self.jsonl.failed}},
            "ec2_connected": self._ec2_ok(),
            "laptop_connected": self.control is not None and self.control.connected,
            "marker_error": self.marker_error,
            "avatar_share": None if avatar_writer is None else {
                "dropped": avatar_writer.dropped, "errors": avatar_writer.errors},
            "reid": None if self.reid_collector is None else self.reid_collector.status(),
            "reid_submission": (self.perception.feed.reid_submission_status()
                                if isinstance(getattr(self.perception, "feed", None), DetectorFeed) else None),
            # 추적 계측 누계(S15P31A101-413, 판정에 쓰지 않음): 늦은 카메라 시간 비율·보간 탈락·카메라 쌍 거부·World Track 생명주기
            "input_timing": None if self.live_input is None else self.live_input.bridge.timing.summary(),
            "stage_timing": self.perception.timing.summary(),
            "detector_timing": (self.perception.feed.timing.summary()
                                if isinstance(self.perception.feed, DetectorFeed) else None),
            "skipped_camera_pairs": sorted("/".join(pair) for pair in self.perception.associator.skipped_camera_pairs),
            "tracking_diagnostics": (None if getattr(self, "perception", None) is None
                                     else self.perception.diagnostics.summary()),
            # 카메라 쌍별 지금 재투영 문턱·표본 수·출처(S15P31A101-476, 판정에 쓰지 않음)
            "pair_thresholds": (None if getattr(self, "perception", None) is None
                                else self.perception.associator.pair_threshold_snapshot()),
        }

    def _sample_load(self) -> None:
        """분석 워커 접수 문의 지연 표본(simai_ai.gpu.admission): 보정 T_lag. 모르면(시계 차이 모름·프레임 없음) None.

        LIVE 입력이면 추론 대기 상한을 넘겨 버린 프레임(I0Ingest SKIPPED_BY_POLICY QUEUE_WAIT_EXCEEDED, 카메라 합)의 직전 표본
        뒤 증가분도 싣는다(명세 3.2 queue_wait_ms). 그 누계를 읽지 못하면 지연도 모름으로 센다. SIM 은 대기열이 없어 싣지 않는다."""
        try:
            lag = self.health.evaluate().report.lag_ms
        except Exception:  # noqa: BLE001 - 표본을 못 만들면 모름으로 센다(분석 워커는 받지 않는다)
            log.exception("지연 표본 실패")
            lag = None
        qwe: int | None = None
        live = self.live_input
        if live is not None:
            try:
                total = sum(int(live.ingest.log.counts(cam)["skipped_by_reason"].get(QUEUE_WAIT_EXCEEDED, 0))  # type: ignore[union-attr]
                            for cam in live.camera_ids)
            except Exception:  # noqa: BLE001
                log.exception("추론 대기 상한 초과 누계를 읽지 못했다(지연 표본을 모름으로 센다)")
                lag = None
            else:
                qwe = max(0, total - self._qwe_total)
                self._qwe_total = total
        self.load.add(lag, queue_wait_exceeded=qwe)

    def _gpu_section(self, phase: str) -> dict[str, Any]:
        load = self.load.take() if phase == RUNNING else None
        section = marker_section(phase=phase, reserve=self.gpu.status(), load=load, written_at_ms=self._wall_ms(),
                                 pid=os.getpid())
        section["processing"] = {"judge": self.judge_processing.take(), "detect": self.detect_processing.take(),
                                 "detect_required": isinstance(getattr(getattr(self, "perception", None), "feed", None),
                                                               DetectorFeed)}
        return section

    def _write_phase_marker(self, phase: str) -> None:
        """기동 중·기동 실패 표시 파일(status 없이). 쓰지 못하면 로그만 남긴다."""
        doc = {"started": False, "engine": self.settings.engine_path, "previous_unclean": self.previous_unclean,
               "gpu_priority": self._gpu_section(phase)}
        self._put_marker(doc)

    def _put_marker(self, doc: dict[str, Any]) -> None:
        """표시 파일 원자적 쓰기. 실패는 판정을 멈추지 않고 marker_error·로그(바뀔 때만)·status 로 알린다."""
        try:
            _atomic_write(self.settings.live_marker, json.dumps(doc, ensure_ascii=False, default=str).encode("utf-8"))
        except OSError as e:
            error = f"{type(e).__name__}: {e}"
            if error != self.marker_error:
                log.error("표시 파일을 쓰지 못했다: %s (%s). 분석 워커는 새 작업을 받지 않는다(모름)", self.settings.live_marker, error)
            self.marker_error = error
            return
        if self.marker_error is not None:
            log.info("해소: 표시 파일 쓰기 (%s)", self.settings.live_marker)
        self.marker_error = None

    def _clear_clean_stop(self) -> None:
        """시작: 이전 정상 종료 기록을 표시 파일보다 먼저 지운다(분석 워커가 기동 중을 '정상 종료로 끝남'으로 보지 않게, S15P31A101-403).
        지우지 못하면 로그만 남긴다. 표시 파일이 있는 동안은 워커가 표시 파일을 먼저 본다."""
        try:
            clean_stop_path(self.settings.live_marker).unlink(missing_ok=True)
        except OSError:
            log.exception("이전 정상 종료 기록을 지우지 못했다(표시 파일이 없는 틈에 분석 워커가 LIVE 아님으로 볼 수 있다)")

    def _write_clean_stop(self) -> None:
        """정상 종료(모든 종료 단계 성공·표시 파일 지움) 뒤에만 남긴다. 분석 워커는 이것을 'LIVE 아님'으로 본다(S15P31A101-403).
        표시 파일을 먼저 지우므로 그 사이에 꺼지면 기록이 없다(모름 → LIVE, 안전한 쪽)."""
        path = clean_stop_path(self.settings.live_marker)
        try:
            _atomic_write(path, json.dumps(clean_stop_record(stopped_at_ms=self._wall_ms(), pid=os.getpid())).encode("utf-8"))
            log.info("정상 종료 기록을 남겼다: %s", path)
        except OSError:
            log.exception("정상 종료 기록을 쓰지 못했다(분석 워커는 Tier 0 상태를 모름으로 본다)")

    def _start_failed_marker(self) -> None:
        """기동 실패: 이번에 만든 표시 파일은 지운다. 이전 실행이 정상 종료하지 않았으면 그 사실을 남긴다(다음 시작이 안다)."""
        if self.previous_unclean:
            self._write_phase_marker("start_failed")
            return
        try:
            self.settings.live_marker.unlink(missing_ok=True)
        except OSError:
            log.exception("표시 파일을 지우지 못했다")

    def _write_marker(self, shutdown_failures: list[str] | None = None) -> None:
        doc: dict[str, Any] = {"started": self.started, "engine": self.settings.engine_path}
        if shutdown_failures is not None:
            doc["shutdown_failed"] = shutdown_failures
        try:
            doc["gpu_priority"] = self._gpu_section(RUNNING if shutdown_failures is None else "shutdown_failed")
        except Exception as e:  # noqa: BLE001 - 지연 신호를 못 만들어도 표시 파일은 남긴다(분석 워커는 칸이 없으면 받지 않는다)
            doc["gpu_priority_error"] = repr(e)
        try:
            doc["status"] = self.status()
        except Exception as e:  # noqa: BLE001 - 일부 객체가 없거나 멈춰도 표시 파일은 남긴다
            doc["status_error"] = repr(e)
        self._put_marker(doc)

    # --- 종료 ------------------------------------------------------------------------

    def stop(self, timeout_s: float | None = None) -> bool:
        """종료: 판정 → 정리 주기 → I0 → I1 → 저널 비우기 → 마지막 체크포인트 → 저널 닫기 → 표시 파일 지우기 → 정상 종료 기록.

        모든 단계가 공통 deadline(timeout_s, 기본 drain_s) 하나를 나눠 쓴다(리뷰 F3). 디스크가 멈춰 저널 쓰기·체크포인트가
        시간 안에 끝나지 않으면 마지막 체크포인트와 저널 닫기를 동기로 다시 기다리지 않는다(둘 다 같은 쓰기 lock 을 기다리게
        된다). 그때는 표시 파일에 실패를 남기고 False 다(다음 시작이 저널·체크포인트로 복구한다).
        """
        timeout = self.settings.drain_s if timeout_s is None else timeout_s
        failures = self._shutdown(time.monotonic() + timeout, final_checkpoint=True)
        ok = not failures
        if ok:
            try:
                self.settings.live_marker.unlink(missing_ok=True)
            except OSError:
                log.exception("표시 파일을 지우지 못했다")
            else:
                self._write_clean_stop()
        else:
            log.error("정상 종료하지 못했다: %s", failures)
            # 표시 파일도 같은 볼륨이다. 쓰기가 멈추면 짧게만 기다린다(실패는 로그에 이미 남겼다)
            self._bounded(time.monotonic() + 1.0, "marker", lambda: self._write_marker(shutdown_failures=failures), [])
        self._event("stopped")
        return ok

    def _bounded(self, deadline: float, name: str, fn: Callable[[], Any], failures: list[str]) -> bool:
        """fn 을 별도 스레드에서 돌리고 deadline 까지만 기다린다. 끝나지 않거나 예외면 failures 에 이름을 남기고 False."""
        error: list[BaseException] = []

        def run() -> None:
            try:
                fn()
            except BaseException as exc:  # noqa: BLE001 - 호출자에게 실패로 알린다
                error.append(exc)

        t = threading.Thread(target=run, name=f"tier0-stop-{name}", daemon=True)
        t.start()
        t.join(max(0.0, deadline - time.monotonic()))
        if t.is_alive():
            failures.append(f"{name}:timeout")
            return False
        if error:
            failures.append(f"{name}:{type(error[0]).__name__}")
            log.error("종료 단계 %s 실패: %r", name, error[0])
            return False
        return True

    def _shutdown(self, deadline: float, *, final_checkpoint: bool) -> list[str]:
        """stop·_abort_start 공통. 만들지 않은 객체는 건너뛴다. 실패한 단계 이름 목록을 돌려준다."""
        failures: list[str] = []
        left = lambda: max(0.0, deadline - time.monotonic())  # noqa: E731
        self._stop.set()
        for t in self._threads:
            t.join(left())
        failures += [f"thread:{t.name}" for t in self._threads if t.is_alive()]
        self._event("judge_stopped")
        if self.event_frames is not None:
            if not self.event_frames.stop(left()):
                failures.append("thread:tier0-event-frames")
        if self.drift_watch is not None:
            self._bounded(deadline, "calibration_baselines",
                          lambda: save_baselines(self.settings.baselines_file, self.drift_watch.baselines()), failures)
        control, link, live = self.control, self.link, self.live_input
        if live is not None:
            stuck: list[str] = []
            # 수신 스레드는 POLL_S(0.5 s)마다 멈춤을 본다. HELLO 대기 중인 연결은 hello 시간 제한까지 걸릴 수 있다
            if self._bounded(deadline, "i0_video", lambda: stuck.extend(live.stop(min(left(), 7.0))), failures):
                failures += [f"thread:{n}" for n in stuck]
        if control is not None and self._bounded(deadline, "i0", lambda: control.close(left()), failures):
            failures += [f"thread:{n}" for n in control.alive_threads()]
        if link is not None and self._bounded(deadline, "i1", lambda: link.close(left()), failures):
            failures += [f"thread:{n}" for n in link.alive_threads()]
        self._event("links_closed")
        writer = getattr(self, "writer", None)
        drained = True
        if writer is not None:
            self._bounded(deadline, "journal_writer", lambda: writer.stop(left()), failures)
            backlog = writer.backlog()
            drained = backlog == 0
            if not drained:
                failures.append(f"journal_backlog:{backlog}")
            self._event(f"journal_flushed:{backlog}")
        checkpointer = getattr(self, "checkpointer", None)
        if checkpointer is not None:
            checkpointer_stopped = self._bounded(deadline, "checkpointer", lambda: checkpointer.stop(left()), failures)
            # 저널이 다 기록되지 않았거나 체크포인트 스레드가 멈췄으면 마지막 체크포인트를 다시 기다리지 않는다
            if final_checkpoint and drained and checkpointer_stopped:
                if self._bounded(deadline, "final_checkpoint", checkpointer.run_once, failures):
                    self._event("final_checkpoint")
        feed = getattr(getattr(self, "perception", None), "feed", None)
        avatar_writer = getattr(getattr(self, "perception", None), "avatar_writer", None)
        if avatar_writer is not None:
            avatar_writer.close()
        if feed is not None:
            feed.close()
        if self.jsonl is not None:
            self.jsonl.close()
        journal = getattr(self, "journal", None)
        if journal is not None:
            if drained and not any(f.startswith(("final_checkpoint", "checkpointer")) for f in failures):
                self._bounded(deadline, "journal_close", journal.close, failures)
            else:
                # 쓰기 lock 을 쥔 기록·정리가 끝나지 않았다. 닫기를 기다리지 않는다(프로세스가 끝나면 OS 가 정리한다)
                threading.Thread(target=_quiet(journal.close), name="tier0-stop-journal-close-late", daemon=True).start()
                failures.append("journal_close:skipped")
        return failures
