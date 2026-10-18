"""Config Sync: 구성 묶음의 원자적 전환과 동기화 상태 (명세 3.2, 6.2 Config Sync, FR-17·FR-83, 부록 D-G3-06).

**원자적 전환.** 검증을 통과한 묶음은 바로 쓰지 않고 '대기'에 둔다. 판정 스레드는 판정 한 번(tick)을 시작할 때
`begin_tick()` 을 부르고, 이 경계에서만 대기 묶음이 활성 묶음으로 바뀐다. `begin_tick()` 이 돌려준 ActiveConfig 는
읽기 전용이라 tick 도중에 묶음이 바뀌어도 그 tick 의 구역·규칙·보정 버전·작업면·config_version 은 한 묶음에서 온다
(반쪽 적용 없음). 전환 시각(applied_at)을 실어 `config_ack`(APPLIED)를 보낼 목록에 넣는다.

**거부.** 검증에 실패하면 `config_ack`(REJECTED, 거부 사유)를 바로 넣고 활성·대기 묶음은 그대로 둔다.

**덮어쓰지 않는다.** 검증한 묶음(versions)과 전환 기록(applied)은 추가만 한다. 같은 config_version 에 다른 묶음(해시)이
오거나, 활성·대기 묶음보다 오래된(issued_at 이 같거나 이른) 묶음이 오면 REFERENCE_INVALID 로 거부한다.

**상태(6.2).** 어느 상태든 판정은 계속한다(활성 묶음이 있으면 그것으로).

| 상태 | 조건 |
|---|---|
| UNVERIFIED | EC2 와 연결이 끊겼거나, (재)연결 뒤 EC2 의 묶음 알림을 아직 받지 못해 최신 여부를 확인할 수 없다 |
| PENDING | EC2 가 알린 더 새 묶음을 검증하는 중이거나 검증을 마치고 다음 판정 경계를 기다린다(전환 중) |
| CURRENT | EC2 가 마지막으로 알린 묶음이 활성 묶음과 같다(config_version·bundle_hash 일치 확인) |
| STALE | EC2 가 마지막으로 알린 묶음이 활성 묶음과 다르다(불일치 확인. 예: 그 묶음을 거부했다) |

재접속 뒤 더 새 묶음 알림을 받으면 UNVERIFIED → STALE(불일치 확인) → PENDING(전환) → CURRENT 로 간다(3.2 "재접속 후
더 새 묶음이 있으면 STALE로 바꾸고 전환한다"). 연결 중 새 묶음은 CURRENT → PENDING → CURRENT 다. 끊긴 동안에 검증을 마친
묶음도 다음 경계에서 전환하고(이미 검증했다), ACK 는 목록에 남아 재연결 뒤 보낸다. 상태 변화는 transitions 에 추가만 한다.

**전제(서버).** Tier 0 는 최신 묶음을 묻는 메시지가 계약에 없다. EC2 가 I1 (재)연결 때 최신 묶음 알림(config_bundle)을 다시
보낸다고 본다. 그 알림이 오기 전까지는 UNVERIFIED 다.

**이벤트·상태의 config_version.** 판정 tick 은 ActiveConfig 하나로 `evaluate()` 하고 `LifecycleTracker.apply(...,
config_version=active.config_version)` 로 이벤트에 그 tick 의 버전을 싣는다(lifecycle 수정 없이 연결된다). health 에는
`health_fields()`(config_version, config_sync)를 싣는다. world_state 계약에는 config_version 필드가 없다(MR 남은 일).

시계는 주입받는다(Tier 0 코드 규칙). 메시지 msg_id 는 UUIDv7(risk.EventIdFactory)이다.
"""

from __future__ import annotations

import bisect
import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from functools import cached_property
from typing import Any, Protocol

from simai_ai.config.bundle import BundleNotice, ConfigBundle
from simai_ai.config.verify import BundleRejected, BundleVerifier, RejectReason, notice_header
from simai_ai.risk import (
    Clock,
    Evaluation,
    EventIdFactory,
    MonitoringState,
    Origin,
    RiskEngine,
    WorkSurfaceSet,
    WorldState,
    Zone,
    work_surfaces_from_operational_geometry,
)

CONTRACTS_VERSION = "2.0.0"
"""보내는 config_ack 봉투의 schema_version (contracts/README.md 의 현재 계약 버전)"""
ACK_MSG_TYPE = "config_ack"

log = logging.getLogger("simai_ai.config.sync")


class ConfigSyncState(StrEnum):
    """계약 common/defs config_sync (명세 6.2)."""

    CURRENT = "CURRENT"
    PENDING = "PENDING"
    UNVERIFIED = "UNVERIFIED"
    STALE = "STALE"


class BundleStore(Protocol):
    """검증한 묶음을 Network Volume 에 보관하는 곳(archive.BundleArchive). 재시작 뒤 오프라인 복구용."""

    def save(self, bundle: ConfigBundle) -> Any: ...


@dataclass(frozen=True)
class ActiveConfig:
    """판정 한 번(tick)에 쓰는 묶음. 읽기 전용이며 tick 동안 바뀌지 않는다."""

    bundle: ConfigBundle
    engine: RiskEngine
    applied_at: int

    @property
    def config_version(self) -> str:
        return self.bundle.config_version

    @property
    def calibration_version(self) -> str:
        return self.bundle.calibration_version

    @property
    def zones(self) -> tuple[Zone, ...]:
        return self.bundle.zones

    @cached_property
    def work_surfaces(self) -> WorkSurfaceSet:
        """이 묶음 Operational Geometry 의 등록 작업면(명세 7.5, R2·R3·R5 입력). ContactEstimator 와 같은 항목을 읽고 형식이
        틀린 항목은 똑같이 뺀다(rejected). 묶음은 읽기 전용이라 한 번만 읽는다."""
        return work_surfaces_from_operational_geometry(self.bundle.operational_geometry, self.bundle.geometry_version)

    def evaluate(self, world: WorldState, origin: Origin, *, monitoring: MonitoringState | None = None) -> Evaluation:
        """이 묶음의 규칙·구역·등록 작업면으로 판정한다."""
        return self.engine.evaluate(
            world, self.bundle.zones, origin, monitoring=monitoring, work_surfaces=self.work_surfaces.surfaces
        )


@dataclass(frozen=True)
class AppliedRecord:
    """전환 기록(추가만). 리플레이·What-if 는 그 시각에 적용돼 있던 버전을 쓴다(3.2)."""

    config_version: str
    bundle_hash: str
    applied_at: int


@dataclass(frozen=True)
class SyncTransition:
    at: int
    before: ConfigSyncState
    after: ConfigSyncState
    cause: str


@dataclass(frozen=True)
class ReceiveResult:
    """알림 하나를 받은 결과. accepted 는 대기·활성 묶음이 됐거나 이미 그 묶음이다. ack 는 바로 보낼 ACK(없을 수 있다)."""

    config_version: str | None
    accepted: bool
    ack: Mapping[str, Any] | None = None
    reason: RejectReason | None = None
    detail: str | None = None


@dataclass(frozen=True)
class _Remote:
    config_version: str
    bundle_hash: str
    issued_at: int


class ConfigSync:
    """Tier 0 의 구성 묶음 동기화. I1 수신 스레드가 receive·on_connected·on_disconnected 를, 판정 스레드가 begin_tick 을 부른다.

    restored 를 주면 재시작 복구(보관소에서 다시 검증한 묶음, restored_applied_at 은 체크포인트의 적용 시각)로 시작한다.
    이 경우에도 EC2 확인 전이라 UNVERIFIED 다.
    """

    def __init__(
        self,
        verifier: BundleVerifier,
        clock: Clock,
        *,
        source_id: str,
        store: BundleStore | None = None,
        id_factory: EventIdFactory | None = None,
        restored: ConfigBundle | None = None,
        restored_applied_at: int | None = None,
    ) -> None:
        if not source_id:
            raise ValueError("ConfigSync: source_id 가 비었다")
        self._verifier = verifier
        self._clock = clock
        self._source_id = source_id
        self._store = store
        self._ids = EventIdFactory(clock) if id_factory is None else id_factory
        self._lock = threading.Lock()
        self._active: ActiveConfig | None = None
        self._staged: ActiveConfig | None = None  # applied_at 은 전환 때 정한다(대기 중에는 0)
        self._inflight: dict[str, _Remote] = {}  # 검증 중인 config_version → 그 알림(해시·issued_at)
        self._connected = False
        self._remote: _Remote | None = None
        self._versions: list[ConfigBundle] = []
        self._applied: list[AppliedRecord] = []
        self._transitions: list[SyncTransition] = []
        self._acks: list[Mapping[str, Any]] = []
        self._state = ConfigSyncState.UNVERIFIED
        if restored is not None:
            if restored_applied_at is None:
                raise ValueError("ConfigSync: restored 에는 restored_applied_at 이 있어야 한다")
            self._active = ActiveConfig(restored, RiskEngine(restored.rules, clock), restored_applied_at)
            self._versions.append(restored)
            self._applied.append(AppliedRecord(restored.config_version, restored.bundle_hash, restored_applied_at))

    # --- 조회 -----------------------------------------------------------------------

    @property
    def state(self) -> ConfigSyncState:
        return self._state

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def active(self) -> ActiveConfig | None:
        """현재 활성 묶음(판정 밖에서 보는 값). 판정은 begin_tick() 이 돌려준 값을 쓴다."""
        return self._active

    def versions(self) -> tuple[ConfigBundle, ...]:
        """검증을 통과한 묶음(받은 순서, 추가만). 검증은 통과했지만 더 새 묶음에 밀려 '대체됨'으로 거부 ACK 를 낸
        묶음도 들어 있다(판정에 쓰였는지는 applied() 로 본다)."""
        with self._lock:
            return tuple(self._versions)

    def applied(self) -> tuple[AppliedRecord, ...]:
        with self._lock:
            return tuple(self._applied)

    def transitions(self) -> tuple[SyncTransition, ...]:
        with self._lock:
            return tuple(self._transitions)

    def version_at(self, t_ms: int) -> str | None:
        """t_ms 에 적용돼 있던 config_version(전환 시각 이후 다음 전환 전까지). 그 전이면 None."""
        with self._lock:
            times = [r.applied_at for r in self._applied]
            i = bisect.bisect_right(times, t_ms)
            return self._applied[i - 1].config_version if i else None

    def health_fields(self) -> dict[str, Any]:
        """I1 health payload 의 config_version·config_sync. 활성 묶음이 없으면 config_version 을 싣지 않는다."""
        with self._lock:
            out: dict[str, Any] = {"config_sync": self._state.value}
            if self._active is not None:
                out["config_version"] = self._active.config_version
            return out

    def take_acks(self) -> list[Mapping[str, Any]]:
        """보낼 config_ack 봉투를 꺼낸다(꺼낸 것은 목록에서 빠진다. 보내지 못하면 호출자가 다시 보낸다)."""
        with self._lock:
            out, self._acks = self._acks, []
            return out

    # --- 연결 -----------------------------------------------------------------------

    def on_connected(self) -> None:
        with self._lock:
            self._connected = True
            self._remote = None  # 재연결 뒤 EC2 알림을 받기 전까지는 최신 여부를 모른다
            self._update_state("EC2 연결")

    def on_disconnected(self) -> None:
        with self._lock:
            self._connected = False
            self._remote = None
            self._update_state("EC2 연결 끊김")

    # --- 판정 경계 ------------------------------------------------------------------

    def begin_tick(self) -> ActiveConfig | None:
        """판정 한 번을 시작한다. 대기 묶음이 있으면 여기서만 전환하고, 이 tick 에 쓸 묶음을 돌려준다."""
        with self._lock:
            staged = self._staged
            if staged is not None:
                applied_at = self._clock.now_ms()
                active = ActiveConfig(staged.bundle, staged.engine, applied_at)
                self._active = active
                self._staged = None
                self._applied.append(AppliedRecord(active.config_version, active.bundle.bundle_hash, applied_at))
                self._acks.append(self._applied_ack(active))
                self._update_state(f"{active.config_version} 전환")
            return self._active

    # --- 수신 -----------------------------------------------------------------------

    def receive(self, message: Any) -> ReceiveResult:
        """I1 config_bundle 알림 하나를 처리한다. 내려받기·검증은 잠금 밖에서 한다(판정 경계를 막지 않는다)."""
        try:
            notice = self._verifier.check_message(message)
        except BundleRejected as e:
            header = notice_header(message)
            with self._lock:
                return self._reject(header.config_version, header.bundle_hash, e)

        version = notice.config_version
        with self._lock:
            self._connected = True  # 알림을 받았으면 EC2 와 이어져 있다
            early = self._precheck(notice)
            if early is not None:
                return early
            # EC2 가 더 새 묶음을 알렸다
            prior = self._state
            self._note_remote(notice)
            if prior is ConfigSyncState.UNVERIFIED:
                self._update_state(f"재접속 뒤 더 새 묶음 {version} 확인")  # STALE
            # 이 수신의 항목(객체 동일성이 토큰). 지울 때 자기 항목인지 보고 지운다(_release)
            entry = _Remote(version, notice.bundle_hash, notice.issued_at)
            self._inflight[version] = entry
            self._update_state(f"{version} 검증 시작")  # PENDING

        try:
            return self._verify_and_stage(message, notice, entry)
        finally:
            # 어떤 예외가 나도 검증 중 표시를 남기지 않는다(남으면 영구 PENDING 이고 재알림이 삼켜진다)
            with self._lock:
                if self._release(entry):
                    self._update_state(f"{version} 검증 중단")

    def _release(self, entry: _Remote) -> bool:
        """검증 중 표시가 이 수신의 항목일 때만 지운다(같은 버전을 다시 받아 새 항목이 생겼으면 그대로 둔다)."""
        if self._inflight.get(entry.config_version) is entry:
            del self._inflight[entry.config_version]
            return True
        return False

    def _verify_and_stage(self, message: Any, notice: BundleNotice, entry: _Remote) -> ReceiveResult:
        version = notice.config_version
        try:
            bundle = self._verifier.verify(message, notice)
        except BundleRejected as e:
            with self._lock:
                self._release(entry)
                result = self._reject(version, e.bundle_hash or notice.bundle_hash, e)
                self._update_state(f"{version} 거부")
                return result
        except Exception as e:  # 수신 스레드를 죽이지 않는다
            # 계약 거부 사유에 맞는 값이 없는 예상 밖 오류(FetchError 로 감싸지 않은 전송 오류, 버그 등)다. 묶음을 받아
            # 검증을 마치지 못했으므로 계약 enum 에서 가장 가까운 DOWNLOAD_FAILED 로 거부 ACK 를 낸다(EC2 가 '적용 대기'에
            # 머물지 않게). 묶음은 보관하지 않으므로 같은 알림이 다시 오면 다시 검증한다.
            log.exception("구성 묶음 %s 검증 중 예상 밖 오류", version)
            error = BundleRejected(RejectReason.DOWNLOAD_FAILED, f"검증 중 예상 밖 오류 ({type(e).__name__})")
            with self._lock:
                self._release(entry)
                result = self._reject(version, notice.bundle_hash, error)
                self._update_state(f"{version} 검증 오류")
                return result

        if self._store is not None:
            try:
                self._store.save(bundle)
            except Exception:  # 보관 실패는 판정을 막지 않는다(메모리 보관은 남는다)
                log.exception("구성 묶음 %s 를 보관하지 못했다", version)

        with self._lock:
            self._release(entry)
            self._versions.append(bundle)
            newest = self._newest_issued_at(include_inflight=False)
            if newest is not None and bundle.issued_at <= newest:
                error = BundleRejected(
                    RejectReason.REFERENCE_INVALID, "검증하는 동안 더 새 묶음이 들어왔다", bundle.bundle_hash
                )
                result = self._reject(version, bundle.bundle_hash, error)
                self._update_state(f"{version} 대체됨")
                return result
            if self._staged is not None:
                # 아직 판정에 쓰지 않은 대기 묶음은 더 새 묶음으로 바꾼다(적용하지 않았으므로 APPLIED 를 보내지 않는다)
                log.info("대기 묶음 %s 를 %s 로 바꾼다", self._staged.config_version, version)
            self._staged = ActiveConfig(bundle, RiskEngine(bundle.rules, self._clock), 0)
            self._update_state(f"{version} 검증 통과, 판정 경계 대기")
            return ReceiveResult(version, accepted=True)

    # --- 내부 -----------------------------------------------------------------------

    def _precheck(self, notice: BundleNotice) -> ReceiveResult | None:
        """활성·대기·검증 중 묶음과 비교한다. 새로 검증할 묶음이면 None."""
        version, digest = notice.config_version, notice.bundle_hash
        for current, what in ((self._active, "활성"), (self._staged, "대기")):
            if current is None or current.config_version != version:
                continue
            self._note_remote(notice)
            if current.bundle.notice.bundle_hash != digest:
                error = BundleRejected(
                    RejectReason.REFERENCE_INVALID, f"{what} 묶음과 같은 config_version 에 다른 묶음(덮어쓰기 금지)"
                )
                result = self._reject(version, digest, error)
                self._update_state(f"{version} 다른 내용 거부")
                return result
            ack = None
            if current is self._active:
                ack = self._applied_ack(current)  # 같은 묶음 재알림: 원래 전환 시각으로 다시 ACK
                self._acks.append(ack)
            self._update_state(f"{what} 묶음 {version} 재알림")
            return ReceiveResult(version, accepted=True, ack=ack)
        if version in self._inflight:
            if self._inflight[version].bundle_hash == digest:
                # 재연결 뒤 검증 중인 묶음을 다시 알린 경우에도 EC2 최신을 기록한다(안 하면 적용 뒤에도 UNVERIFIED)
                self._note_remote(notice)
                self._update_state(f"검증 중 묶음 {version} 재알림")
                return ReceiveResult(version, accepted=True)
            error = BundleRejected(RejectReason.REFERENCE_INVALID, "검증 중인 묶음과 같은 config_version 에 다른 묶음")
            return self._reject(version, digest, error)
        if any(b.config_version == version for b in self._versions):
            error = BundleRejected(RejectReason.REFERENCE_INVALID, f"이미 지난 묶음 {version}")
            return self._reject(version, digest, error)
        newest = self._newest_issued_at(include_inflight=True)
        if newest is not None and notice.issued_at <= newest:
            error = BundleRejected(RejectReason.REFERENCE_INVALID, "활성·대기·검증 중 묶음보다 오래된 묶음")
            return self._reject(version, digest, error)
        return None

    def _newest_issued_at(self, *, include_inflight: bool) -> int | None:
        times = [c.bundle.issued_at for c in (self._active, self._staged) if c is not None]
        if include_inflight:
            times += [r.issued_at for r in self._inflight.values()]
        return max(times) if times else None

    def _note_remote(self, notice: BundleNotice) -> None:
        """EC2 가 알린 최신 묶음을 기록한다. 이번 연결 안에서 issued_at 이 뒤로 가는 알림(순서가 뒤바뀐 수신)은
        기록하지 않는다(오래된 알림이 최신 확인을 덮어 STALE 로 굳지 않게)."""
        if self._remote is None or notice.issued_at >= self._remote.issued_at:
            self._remote = _Remote(notice.config_version, notice.bundle_hash, notice.issued_at)

    def _derive(self) -> ConfigSyncState:
        if not self._connected or self._remote is None:
            return ConfigSyncState.UNVERIFIED
        if self._staged is not None or self._inflight:
            return ConfigSyncState.PENDING
        active = self._active
        if active is not None and (self._remote.config_version, self._remote.bundle_hash) == (
            active.config_version,
            active.bundle.notice.bundle_hash,
        ):
            return ConfigSyncState.CURRENT
        return ConfigSyncState.STALE

    def _update_state(self, cause: str) -> None:
        after = self._derive()
        if after is not self._state:
            self._transitions.append(SyncTransition(self._clock.now_ms(), self._state, after, cause))
            self._state = after

    def _reject(self, version: str | None, digest: str | None, error: BundleRejected) -> ReceiveResult:
        log.warning("구성 묶음 %s 거부: %s %s", version, error.reason, error.detail)
        ack = None
        if version is not None and digest is not None:
            ack = self._envelope(
                {
                    "config_version": version,
                    "bundle_hash": digest,
                    "result": "REJECTED",
                    "reject_reason": error.reason.value,
                    "reject_detail": error.detail,
                    "active_config_version": None if self._active is None else self._active.config_version,
                }
            )
            self._acks.append(ack)
        # config_version·bundle_hash 를 모르면 계약에 맞는 ACK 를 만들 수 없다(로그만 남긴다)
        return ReceiveResult(version, accepted=False, ack=ack, reason=error.reason, detail=error.detail)

    def _applied_ack(self, active: ActiveConfig) -> Mapping[str, Any]:
        return self._envelope(
            {
                "config_version": active.config_version,
                "bundle_hash": active.bundle.bundle_hash,
                "result": "APPLIED",
                "applied_at": active.applied_at,
                "active_config_version": active.config_version,
            }
        )

    def _envelope(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return {
            "schema_version": CONTRACTS_VERSION,
            "msg_type": ACK_MSG_TYPE,
            "msg_id": self._ids.new(),
            "sent_at": self._clock.now_ms(),
            "source_id": self._source_id,
            "payload": dict(payload),
        }
