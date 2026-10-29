"""Tier 0 판정 루프 한 tick (명세 3.2 이벤트 소유권·저널 계약·구성 묶음, 5.2 ID 교정, 6.4, 7장, 3.10 현장 경보).

한 tick 은 판정 lock(Tier0 의 judge_lock) 안에서 아래 순서로 돈다. 순서 불변식은 docs/design/tier0-service.md 에 있다.

1. ConfigSync.begin_tick(): 구성 묶음 전환은 이 경계에서만 일어난다. 이 tick 의 구역·규칙·작업면·config_version 은 돌려받은
   ActiveConfig 하나에서 온다(반쪽 적용 없음). 묶음이 없으면 위험 판정을 하지 않는다(health NO_ACTIVE_CONFIG).
2. 인지(Perception.step): 공통 시각 t 의 World Track·접지·world_state payload. world_state 에 이 tick 의 config_version 을
   싣고(-84·-169), 봉투 msg_id 를 여기서 정한다. 같은 값이 이 tick 판정 전이의 evidence_ref 다(-79).
3. ID 교정: WorldTrackFrame.revisions 를 병합은 correct_identity, 분할은 split_identity 로 넘긴다. **반드시 같은 tick 의 apply 전**
   (-40, docs/design/tier0-world-track.md G3). 순서를 지키면 새 ID 로 들어오는 같은 규칙 판정이 기존 이벤트를 갱신한다.
4. 판정: ActiveConfig.evaluate(world, LIVE, monitoring, calibration_state) → LifecycleTracker.apply(config_version, evidence_ref, related).
   작업면은 ActiveConfig.evaluate 가 넘긴다(-93). 감시 상태(R7)는 카메라 상태와 구역별 관측 카메라(geometry.zone_coverage).
   보정 상태는 health 와 같은 활성 보정 상태(Tier0._calibration_state)를 판정 때마다 읽어 넘긴다. world_state 계약에 없는 판정
   문맥이라 I1 계약을 바꾸지 않는다. R2·R3·R4 가 PROVISIONAL_CALIB 를 붙인다(명세 4.4, D-G3-03, -218). 읽기가 예외·None·모르는
   값이면 판정을 멈추지 않고 UNCALIBRATED 로 넘겨 확인 필요를 붙인다(모르는 보정을 CALIBRATED·해당 없음으로 보지 않는다).
   related 는 재시작 뒤 새 이벤트의 related_event_id 를 정하는 함수다(RestoreLink.related(frame), -178). 3) 교정 뒤에 만들어
   apply 에 넘긴다. 복원 이벤트를 옮기거나 해소하지 않는다(연결만). 입력 정지 tick 은 마지막 실제 World 의 frame 으로 만든다.
5. 단계마다(교정·판정·확인): JournalWriter.submit_step(기다리지 않음, fsync 는 쓰기 스레드) → 현장 경보 송신(local_alert·
   alarm_stop, fsync 를 기다리지 않음, D-G3-18) → 클립 대상 전이(EventClipService.on_transitions, -57).
6. world_state 를 I1 송신 칸에 넣고(offer_world_state, msg_id 고정), 체크포인트 재료를 넘긴다(Checkpointer.update_inputs).
7. 스냅샷 접기가 broken 이면, 저널 쓰기 대기열이 빈 것을 확인하고(지금 lock 안이라 새 단계가 들어오지 않는다) 추적기 상태와
   저널 끝으로 다시 시작한다(EventStateSnapshot.reseed, -81).
8. tick 끝에 판정 상태의 불변 사본(활성 이벤트·위험 관측 시각)을 바꿔 끼운다. I1 송신 스레드의 스냅샷 콜백
   (current_state·risk_observed_at)은 판정 lock 을 잡지 않고 이 사본을 읽는다. 스냅샷은 자기 lock 안에서 콜백을 부르고
   판정 tick 은 판정 lock 안에서 스냅샷(broken·reseed)을 부르므로, 콜백이 판정 lock 을 잡으면 lock 순서가 뒤집혀 교착한다
   (리뷰 F1). lock 순서는 늘 판정 lock → 스냅샷 lock 이다.

입력 정지(새 프레임이 없어 공통 시각이 늘지 않는 tick, 리뷰 F2): 새 관측을 지어내지 않고 마지막 실제 World(원래 촬영 시각
그대로)로 판정을 계속한다(명세 6.1 '영상이 멈춰도 판정 시계는 진행'). 같은 관측은 LifecycleTracker 가 지속 조건의 새 프레임으로
세지 않고, 관측 시각이 멈춘 채 대상을 유효하게 볼 수 없으면(lag·시계 차이 모름) 지속 대기 중인 경보를 UNVERIFIABLE 로
발생시키고 활성 이벤트는 해소 확인을 하지 않는다. 그 tick 에는 world_state 를 다시 보내지 않는다(새 관측이 없다).
evidence_ref 는 그 World 의 world_state msg_id 다.

운영자 확인(EC2 event_ack, 노트북 site_ack)도 같은 lock 안에서 처리한다. ack_id 로 중복을 거른 뒤(EventJournal.has_ack_id,
대기 중 기록 포함) acknowledge → 저널 → alarm_stop 순이다. 운영자 종료(EC2 event_close, S15P31A101-200)도 같다: close_id 로
중복을 거른 뒤(EventJournal.has_close_id) close → 저널 → (현장 경보를 낸 이벤트면) 해소 local_alert 이고, event_close_result
payload 를 돌려준다(build_i1_link 가 신뢰 대기열로 보낸다). 종료 전이는 outbox 가 risk_event_transition 2.0.0 resolution 으로
보낸다(docs/design/tier0-recovery.md "운영자 종료"). 운영자 상향(EC2 event_escalate, S15P31A101-272)도 같은 순서다: escalation_id 로
중복을 거른 뒤(EventJournal.has_escalation_id) escalate → 저널 → 상향 재경보 local_alert → 클립(CRITICAL 보존 표시)이고
event_escalate_result payload 를 돌려준다(docs/design/tier0-recovery.md "운영자 상향").

판정 출처(-215): 판정은 추적기의 출처(LifecycleTracker.origin)로 한다. LIVE 가 아닌 출처(file 입력의 REPLAY)는 7.3 표가
채널을 주지 않아 현장 경보 신호가 없고, Tier 0 저널(LIVE 전용 계약)에 넣지 않는다. 확정 전이는 record_transitions(JSONL
출력 등)로만 넘긴다.

위험 관측 시각(risk_observed_at, -81): 판정마다 발생 판정(holding 아님)이 있고 검증 상태가 VERIFIED 인 활성 이벤트의
(event_sequence, observation_time)을 기억한다. 스냅샷이 전이 없이 이어진 위험 관측까지 last_valid_observation_at 에 올린다.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from services.tier0.perception import Perceived, Perception
from simai_ai.config import ActiveConfig, ConfigSync
from simai_ai.contact import SurfaceSet
from simai_ai.eventlog import EventJournal, JournalError, JournalWriter
from simai_ai.geometry.zone_coverage import zone_cameras
from simai_ai.risk import (
    AlarmStop,
    AlertClear,
    CameraMonitor,
    CameraStatus,
    ContractAck,
    Evaluation,
    EventKey,
    EventState,
    LifecycleStep,
    LifecycleTracker,
    LocalAlert,
    MonitoringState,
    Origin,
    WorldState,
    alarm_stop_contract_payload,
    local_alert_payload,
)
from simai_ai.risk.common import CALIBRATION_STATES
from simai_ai.risk.lifecycle import CloseReject, CloseRequest, EscalateReject, EscalationRequest, RelatedEventFn
from simai_ai.tracking.world_track import RevisionKind, WorldTrackFrame

log = logging.getLogger("services.tier0.judge")

SendI0 = Callable[[str, Mapping[str, Any]], bool]


@dataclass
class JudgeStats:
    ticks: int = 0
    perceived: int = 0
    evaluated: int = 0
    frozen_evaluations: int = 0  # 입력 정지 tick 에 마지막 World 로 다시 한 판정
    no_config_ticks: int = 0
    corrections: int = 0
    transitions: int = 0
    local_alerts: int = 0
    alarm_stops: int = 0
    unsent_local_alerts: int = 0  # 계약 등급이 아니라 보내지 않은 신호(다음 목록이 맞춘다)
    journal_errors: int = 0
    acks: int = 0
    duplicate_acks: int = 0
    unknown_acks: int = 0
    closes: int = 0  # 받은 운영자 종료(event_close)
    duplicate_closes: int = 0
    rejected_closes: int = 0
    escalations: int = 0  # 받은 운영자 상향(event_escalate)
    duplicate_escalations: int = 0
    rejected_escalations: int = 0
    reseeds: int = 0
    errors: int = 0
    calibration_state_unknown: int = 0  # 활성 보정 상태를 읽지 못해 UNCALIBRATED 로 판정한 tick


@dataclass(frozen=True)
class TickResult:
    perceived: Perceived | None
    config_version: str | None
    evidence_ref: str | None
    steps: tuple[LifecycleStep, ...]


class JudgmentLoop:
    """판정 tick 과 확인 처리. lock 은 Tier 0 판정 lock(RLock)이다. 이 객체의 메서드는 모두 그 lock 안에서 돈다."""

    def __init__(
        self,
        *,
        lock: Any,
        now_ms: Callable[[], int],
        config_sync: ConfigSync,
        perception: Perception,
        tracker: LifecycleTracker,
        journal: EventJournal,
        writer: JournalWriter,
        calibration_state: Callable[[], str],
        send_i0: SendI0 | None = None,
        offer_world_state: Callable[..., Any] | None = None,
        checkpoint_inputs: Callable[..., Any] | None = None,
        clips: Any | None = None,
        record_transitions: Callable[[tuple[Any, ...]], Any] | None = None,
        snapshot: Any | None = None,
        observe_progress: Callable[[int | None], Any] | None = None,
        restore_link: Any | None = None,
        new_msg_id: Callable[[], str] = lambda: str(uuid.uuid4()),
        begin_tick: Callable[[], Any] | None = None,
    ) -> None:
        self.lock = lock
        self._now_ms = now_ms
        self.config_sync = config_sync
        self.perception = perception
        self.tracker = tracker
        self.journal = journal
        self.writer = writer
        self._calibration_state = calibration_state  # 활성 보정 상태(명세 4.4). 기본값을 두지 않는다(모르는 보정을 정상으로 보지 않음)
        self._send_i0 = send_i0
        self._offer_world_state = offer_world_state
        self._checkpoint_inputs = checkpoint_inputs
        self._clips = clips
        self._record_transitions = record_transitions  # 확정 전이 사본(file 입력의 risk_event JSONL, -215)
        self._snapshot = snapshot
        self._observe_progress = observe_progress
        self._restore_link = restore_link  # 재시작 뒤 이전 이벤트 연결(services.tier0.restore_link.RestoreLink, -178)
        self._new_msg_id = new_msg_id
        self._begin_tick = begin_tick
        self._last_calibration_state = "UNCALIBRATED"
        self._coverage_key: tuple[str, str] | None = None
        self._coverage: Mapping[str, tuple[str, ...]] = {}
        self._risk_seen: dict[str, tuple[int, int]] = {}
        self.active_config: ActiveConfig | None = None
        self.stats = JudgeStats()
        self._last: tuple[dict[str, Any], str] | None = None  # 마지막 실제 World(payload, world_state msg_id)
        self._last_frame: WorldTrackFrame | None = None  # 그 World 의 World Track 결과(입력 정지 tick 의 연결 판단)
        # 판정 상태의 불변 사본(스냅샷 콜백용, lock 없이 읽는다). 바꿔 끼우기만 한다(참조 대입은 원자적)
        self._view_states: Mapping[str, EventState] = {}
        self._view_risk: Mapping[str, tuple[int, int]] = {}
        self._publish_views()

    # --- 조회(다른 스레드는 lock 을 잡고 부른다) ----------------------------------------------

    def active_states(self) -> tuple[EventState, ...]:
        with self.lock:
            return self.tracker.active()

    def current_state(self, event_id: str) -> EventState | None:
        """스냅샷 콜백: 판정 lock 을 잡지 않고 마지막 tick·확인 뒤의 불변 사본을 읽는다(lock 순서, 모듈 설명 8)."""
        return self._view_states.get(event_id)

    def risk_observed_at(self, event_id: str) -> tuple[int, int] | None:
        """스냅샷 콜백: 판정 lock 을 잡지 않는다(위와 같음)."""
        return self._view_risk.get(event_id)

    def _publish_views(self) -> None:
        self._view_states = MappingProxyType({s.event_id: s for s in self.tracker.active()})
        self._view_risk = MappingProxyType(dict(self._risk_seen))

    def config_version(self) -> str | None:
        with self.lock:
            active = self.config_sync.active
            return None if active is None else active.config_version

    # --- tick ------------------------------------------------------------------------

    def tick(self) -> TickResult:
        with self.lock:
            self.stats.ticks += 1
            if self._begin_tick is not None:
                self._begin_tick()  # 보정도 인지 전에 같은 경계에서만 전환한다(-344)
            active = self.config_sync.begin_tick()  # 1) 구성 묶음 전환 경계
            self.active_config = active
            surfaces = SurfaceSet.from_active_config(active)
            perceived = self.perception.step(self._now_ms(), surfaces)  # 2) 인지
            if self._observe_progress is not None:
                self._observe_progress(self.perception.t_latest_global())
            if perceived is None:
                result = self._judge_frozen(active)
                self._maybe_reseed()
                self._publish_views()
                return result
            self.stats.perceived += 1
            result = self._judge(perceived, active)
            self._publish_views()
            return result

    def _judge_frozen(self, active: ActiveConfig | None) -> TickResult:
        """입력 정지 tick: 마지막 실제 World 와 원래 촬영 시각으로 판정만 다시 한다(모듈 설명). 없으면 판정하지 않는다."""
        version = None if active is None else active.config_version
        if active is None or self._last is None:
            return TickResult(None, version, None, ())
        payload, msg_id = self._last
        world = WorldState.from_payload(payload)
        state = (self._active_calibration_state()
                 if payload["calibration_version"] == self.perception.rig.calibration_version
                 else self._last_calibration_state)
        evaluation = active.evaluate(world, self.tracker.origin, monitoring=self._monitoring(active),
                                     calibration_state=state)
        step = self.tracker.apply(evaluation, world, config_version=active.config_version, evidence_ref=msg_id,
                                  related=self._related(self._last_frame))
        self.stats.frozen_evaluations += 1
        self._record(step)
        self._note_risk(evaluation)
        return TickResult(None, version, msg_id, (step,))

    def _active_calibration_state(self) -> str:
        """판정에 넘길 활성 보정 상태. 읽기 실패(예외·None·모르는 값)는 UNCALIBRATED 다: LIVE 에서 '해당 없음(None)'은 PLANNED 전용이라
        받지 않고, 판정을 빠뜨리지도 않는다(명세 4.4 '보정 상태를 알 수 없으면 CALIBRATED 로 보지 않는다')."""
        try:
            state = self._calibration_state()
        except Exception as e:  # noqa: BLE001 - 판정 tick 을 잃지 않는다
            state = e
        if isinstance(state, Exception) or state is None or str(state) not in CALIBRATION_STATES:
            self.stats.calibration_state_unknown += 1
            if self.stats.calibration_state_unknown == 1:  # 매 tick 로그를 남기지 않는다(횟수는 stats)
                log.warning("활성 보정 상태를 모른다(%r): UNCALIBRATED 로 판정한다(확인 필요)", state)
            return "UNCALIBRATED"
        return str(state)

    def _judge(self, perceived: Perceived, active: ActiveConfig | None) -> TickResult:
        self._last_calibration_state = self._active_calibration_state()
        msg_id = self._new_msg_id()
        payload = dict(perceived.payload)
        if active is not None:
            payload["config_version"] = active.config_version
        steps: list[LifecycleStep] = []
        # 3) ID 교정: 같은 tick 의 apply 전에 (순서 불변식)
        for revision in perceived.frame.revisions:
            if revision.kind is RevisionKind.MERGE:
                step = self.tracker.correct_identity(revision.to_correction())
                log.info("ID 교정 %s → %s (rev %d)", revision.previous_world_id, revision.world_id,
                         revision.identity_revision)
            elif revision.kind is RevisionKind.SPLIT:  # 이벤트는 원래 ID 에 두고 분할 상대만 기억한다(-201)
                step = self.tracker.split_identity(revision.to_split())
                log.info("ID 분할 %s → %s (rev %d)", revision.previous_world_id, revision.world_id,
                         revision.identity_revision)
            else:
                continue  # 수동 교정은 아직 내지 않는다(운영자 명령 계약 승인 뒤)
            self.stats.corrections += 1
            self._record(step)
            steps.append(step)
        related = self._related(perceived.frame)  # 3) 교정 뒤, 같은 lock 안. 복원 Track 판단(로그)은 묶음이 없어도 한다
        evaluation: Evaluation | None = None
        if active is None:
            self.stats.no_config_ticks += 1
        else:
            world = WorldState.from_payload(payload)
            evaluation = active.evaluate(world, self.tracker.origin, monitoring=self._monitoring(active),
                                         calibration_state=self._last_calibration_state)  # 4) 판정(작업면·보정 상태 포함)
            step = self.tracker.apply(evaluation, world, config_version=active.config_version, evidence_ref=msg_id,
                                      related=related)
            self.stats.evaluated += 1
            self._record(step)
            steps.append(step)
            self._note_risk(evaluation)
        self._last = (payload, msg_id)
        self._last_frame = perceived.frame
        if self._offer_world_state is not None:  # 6) world_state (msg_id = 이 tick 의 evidence_ref)
            self._offer_world_state(payload, msg_id=msg_id)
        if self._checkpoint_inputs is not None:
            self._checkpoint_inputs(
                config_version=None if active is None else active.config_version,
                last_capture_time=perceived.t_latest_global,
                tracks_t=perceived.frame.t,
                tracks=self.perception.manager.export_tracks(),
            )
        self._maybe_reseed()  # 7)
        return TickResult(perceived, None if active is None else active.config_version, msg_id, tuple(steps))

    # --- 확인 ------------------------------------------------------------------------

    def acknowledge(self, ack: ContractAck) -> LifecycleStep | None:
        """event_ack·site_ack 하나. 이미 기록(또는 기록 대기)한 ack_id 면 다시 기록하지 않는다."""
        with self.lock:
            if self.journal.has_ack_id(ack.ack_id):
                self.stats.duplicate_acks += 1
                return None
            step = self.tracker.acknowledge(**ack.acknowledge_args())
            if step is None:
                self.stats.unknown_acks += 1  # 이미 해소됐거나 모르는 이벤트: 기록하지 않는다(노트북은 다음 목록으로 맞춘다)
                return None
            self.stats.acks += 1
            self._record(step, ack_id=ack.ack_id)
            self._publish_views()
            return step

    def close(self, request: CloseRequest) -> dict[str, Any]:
        """event_close 하나 → event_close_result payload. 같은 close_id 를 이미 기록(또는 기록 대기)했으면 DUPLICATE 이고
        다시 기록하지 않는다. 거부는 기록하지 않으므로 같은 close_id 를 다시 받으면 그때 상태로 다시 판단한다."""
        with self.lock:
            result: dict[str, Any] = {"close_id": request.close_id, "event_id": request.event_id}
            if self.journal.has_close_id(request.close_id):
                self.stats.duplicate_closes += 1
                return result | {"result": "DUPLICATE", "decided_at": self._now_ms()}
            try:
                # 저널 제출이 실패하면(쓰기 대기열 포화·쓰기 정지) 추적기는 이벤트를 그대로 둔다. 재시도할 수 있게 거부한다
                outcome = self.tracker.close(request, commit=self.writer.submit_step)
            except JournalError:
                self.stats.journal_errors += 1
                log.error("운영자 종료를 저널에 넣지 못해 받지 않았다(재시도 가능): %s", request.event_id, exc_info=True)
                return result | {"result": "REJECTED", "reject_reason": "JOURNAL_UNAVAILABLE", "decided_at": self._now_ms()}
            if isinstance(outcome, CloseReject):
                self.stats.rejected_closes += 1
                log.info("운영자 종료를 받지 않았다: %s %s", request.event_id, outcome)
                return result | {"result": "REJECTED", "reject_reason": str(outcome), "decided_at": self._now_ms()}
            self.stats.closes += 1
            self._record(outcome, submitted=True)
            self._publish_views()
            sequence = outcome.transitions[0].event_sequence
            return result | {"result": "ACCEPTED", "event_sequence": sequence, "decided_at": self._now_ms()}

    def escalate(self, request: EscalationRequest) -> dict[str, Any]:
        """event_escalate 하나 → event_escalate_result payload(close 와 같은 순서). 같은 escalation_id 를 이미 기록(또는 기록 대기)했으면
        DUPLICATE 이고 다시 기록하지 않는다. 거부는 기록하지 않으므로 같은 escalation_id 를 다시 받으면 그때 상태로 다시 판단한다."""
        with self.lock:
            result: dict[str, Any] = {"escalation_id": request.escalation_id, "event_id": request.event_id}
            if self.journal.has_escalation_id(request.escalation_id):
                self.stats.duplicate_escalations += 1
                return result | {"result": "DUPLICATE", "decided_at": self._now_ms()}
            try:
                # 저널 제출이 실패하면 추적기는 이벤트를 그대로 둔다(등급·바닥값 그대로). 재시도할 수 있게 거부한다
                outcome = self.tracker.escalate(request, commit=self.writer.submit_step)
            except JournalError:
                self.stats.journal_errors += 1
                log.error("운영자 상향을 저널에 넣지 못해 받지 않았다(재시도 가능): %s", request.event_id, exc_info=True)
                return result | {"result": "REJECTED", "reject_reason": "JOURNAL_UNAVAILABLE", "decided_at": self._now_ms()}
            if isinstance(outcome, EscalateReject):
                self.stats.rejected_escalations += 1
                log.info("운영자 상향을 받지 않았다: %s %s", request.event_id, outcome)
                return result | {"result": "REJECTED", "reject_reason": str(outcome), "decided_at": self._now_ms()}
            self.stats.escalations += 1
            self._record(outcome, submitted=True)  # 상향 재경보(local_alert)·클립(CRITICAL 보존 표시)은 규칙 상향과 같은 경로
            self._publish_views()
            sequence = outcome.transitions[0].event_sequence
            return result | {"result": "ACCEPTED", "event_sequence": sequence, "decided_at": self._now_ms()}

    # --- 내부 ------------------------------------------------------------------------

    def _record(self, step: LifecycleStep, *, ack_id: str | None = None, submitted: bool = False) -> None:
        """submitted: 이미 저널에 제출한 단계(운영자 종료·상향은 추적기가 상태를 바꾸기 전에 제출한다)."""
        if not step.transitions and not step.local_signals:
            return
        self.stats.transitions += len(step.transitions)
        if step.transitions and not submitted and self.tracker.origin is Origin.LIVE:  # 저널은 LIVE 전이만(계약)
            try:
                self.writer.submit_step(step, ack_id=ack_id)  # 5) 저널: 기다리지 않는다
            except JournalError:
                self.stats.journal_errors += 1
                log.critical("저널에 단계를 넣지 못했다(쓰기 정지·실패). 현장 경보는 계속 보낸다", exc_info=True)
        self._send_signals(step, ack_id)  # 현장 경보: fsync 를 기다리지 않는다
        if self._record_transitions is not None and step.transitions:
            try:
                self._record_transitions(step.transitions)
            except Exception:  # noqa: BLE001 - 출력 파일 실패가 판정을 막지 않는다
                log.exception("확정 전이 기록(JSONL) 실패")
        if self._clips is not None and step.transitions:
            try:
                self._clips.on_transitions(step.transitions)
            except Exception:  # noqa: BLE001 - 클립 실패가 판정을 막지 않는다
                log.exception("클립 대상 전이 처리 실패")

    def _send_signals(self, step: LifecycleStep, ack_id: str | None) -> None:
        if self._send_i0 is None:
            return
        states = {t.state.event_id: t.state for t in step.transitions}
        for signal in step.local_signals:
            if isinstance(signal, (LocalAlert, AlertClear)):
                state = states.get(signal.event_id)
                if state is None:
                    state = self.tracker.get(signal.event_id)
                try:
                    if state is None:
                        raise ValueError("신호의 이벤트 상태를 모른다")
                    payload = local_alert_payload(state)
                except ValueError as e:
                    self.stats.unsent_local_alerts += 1
                    log.warning("local_alert 를 보내지 않았다(%s). 다음 active_site_alerts 목록이 맞춘다", e)
                    continue
                self._send_i0("local_alert", payload)
                self.stats.local_alerts += 1
            elif isinstance(signal, AlarmStop):
                if ack_id is None:
                    log.error("alarm_stop 에 ack_id 가 없다(보내지 않음): %s", signal.event_id)
                    continue
                self._send_i0("alarm_stop", alarm_stop_contract_payload(signal, ack_id=ack_id))
                self.stats.alarm_stops += 1

    def _related(self, frame: WorldTrackFrame | None) -> RelatedEventFn | None:
        if self._restore_link is None or frame is None:
            return None
        return self._restore_link.related(frame)

    def _monitoring(self, active: ActiveConfig) -> MonitoringState:
        key = (active.config_version, self.perception.rig.calibration_version)
        if key != self._coverage_key:
            self._coverage = zone_cameras(self.perception.rig, active.zones)
            self._coverage_key = key
        # 수신 중이어도 늦어서 융합에서 뺀 카메라는 그 구역을 관측하지 못한다(R7 정상 카메라·감시 복구 근거 아님, -219)
        excluded = set(self.perception.lagging)
        cameras = tuple(
            CameraMonitor(
                c.camera_id,
                CameraStatus.OK if self.perception.feed.camera_connected(c.camera_id) else CameraStatus.NO_SIGNAL,
                c.last_capture,
                fusion_excluded=c.camera_id in excluded,
            )
            for c in self.perception.camera_clocks()
        )
        return MonitoringState(cameras=cameras, zone_cameras=self._coverage)

    def _note_risk(self, evaluation: Evaluation) -> None:
        keys = {EventKey.of(f) for f in evaluation.findings if not f.holding}
        active = {s.event_id: s for s in self.tracker.active()}
        for event_id in [e for e in self._risk_seen if e not in active]:
            del self._risk_seen[event_id]
        for state in active.values():
            if state.key in keys and str(state.verification) == "VERIFIED":
                self._risk_seen[state.event_id] = (state.event_sequence, evaluation.observation_time)

    def _maybe_reseed(self) -> None:
        snap = self._snapshot
        if snap is None or snap.broken is None:
            return
        if self.writer.backlog() != 0:
            return  # 아직 저널에 들어가지 않은 단계가 있다: 다음 tick 에 다시
        snap.reseed(self.tracker.active(), self.journal.next_offset)
        self.stats.reseeds += 1


def steps_transitions(steps: Iterable[LifecycleStep]) -> list[Any]:
    return [t for s in steps for t in s.transitions]
