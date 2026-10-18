"""재시작 복원 Track 후보와 이전 이벤트 연결 배선 (명세 3.2 재시작 복구, 부록 C·D-G3-09, S15P31A101-178).

-54 가 만든 두 조각을 Tier 0 서비스에 잇는다(docs/design/tier0-recovery.md "복원 Track", tier0-world-track.md).

- `WorldTrackManager.restore(result.tracks, result.tracks_t)`: recover 직후, 첫 World Track update 전에 한 번 부른다.
  복원 Track 은 재관측과 맞출 **후보**일 뿐 위치 정본이 아니다. world_state·export_tracks·연결·필터에 들어가지 않는다
  (복원 위치가 OBSERVED 로 새지 않는다). 받을지는 재시작 뒤 첫 프레임 시각으로 정한다(30 s 규칙).
- `RestoredEventLinker(result.restored)`: 판정마다 `for_frame(frame, manager.restore_status)` 가 돌려준 함수를
  `LifecycleTracker.apply(..., related=)` 에 넘긴다. 새 이벤트의 related_event_id 만 정한다(정보). 복원 이벤트를 새 ID 로
  옮기거나 해소하지 않으므로 재시작 뒤 다른 사람이 같은 자리에 나타나도 이전 경보가 해소되지 않는다(G3).

실패를 판정·현장 경보로 번지게 하지 않는다. restore 가 체크포인트 Track 을 거부하면(같은 세션 ID·중복 ID) 그 Track 은 쓰지
않고(모르면 비운다) 빈 후보로 다시 부른다. 그러면 연결 시간은 그대로 열리고 연결은 위치를 모를 때의 규칙(같은 규칙·같은 구역)만
쓴다. `for_frame` 이 예외를 내면 그 판정은 연결 없이 반영한다(연결 함수 자체의 예외는 LifecycleTracker 가 연결 없음으로 낮춘다).

복원 Track 처리 결과(ACCEPTED·DISCARDED)는 정해지는 판정 tick 에 한 번 로그에 남긴다. DISCARDED 면 이유(오래된 체크포인트·
시각 역행·시각 모름)를 WARNING 으로 남긴다. `status()` 는 표시 파일(state/tier0.live)에 싣는다.

이 객체의 related 는 판정 lock 안(JudgmentLoop)에서만 부른다. status 는 다른 스레드(정리 주기)가 lock 없이 읽으므로 불변 사본을
바꿔 끼우기만 한다.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from types import MappingProxyType
from typing import Any

from simai_ai.recovery import RecoveryResult
from simai_ai.recovery.relink import RestoredEventLinker
from simai_ai.risk.lifecycle import RelatedEventFn
from simai_ai.tracking.world_track import RestoreState, RestoreStatus, TrackCheckpoint, WorldTrackFrame, WorldTrackManager

log = logging.getLogger("services.tier0.restore_link")


class RestoreLink:
    """복원 Track 후보(manager.restore)와 이전 이벤트 연결(RestoredEventLinker)을 묶는다. 판정 루프가 related 를 부른다."""

    def __init__(self, manager: WorldTrackManager, result: RecoveryResult) -> None:
        self._manager = manager
        self.rejected_tracks: str | None = None
        _restore(manager, result.tracks, result.tracks_t, self)
        self._linker = RestoredEventLinker(result.restored)
        self.related_errors = 0
        self._reported = RestoreState.NONE
        self._status: MappingProxyType[str, Any] = MappingProxyType({})
        self._note(manager.restore_status)

    @property
    def linkable_events(self) -> tuple[str, ...]:
        """연결 대상이 되는 사람 대상 복원 이벤트 ID."""
        return tuple(e.event_id for e in self._linker.events)

    def related(self, frame: WorldTrackFrame) -> RelatedEventFn | None:
        """이 World Track 결과로 판정할 때 apply(related=...) 에 넘길 함수. 연결 시간 밖·대상 없음·오류면 None."""
        status = self._manager.restore_status
        self._note(status)
        try:
            return self._linker.for_frame(frame, status)
        except Exception:  # noqa: BLE001 - 연결은 정보다. 실패가 판정·현장 경보를 막지 않는다
            self.related_errors += 1
            log.exception("이전 이벤트 연결 함수를 만들지 못했다(이번 판정은 연결 없이 반영한다): t=%d", frame.t)
            return None

    def status(self) -> MappingProxyType[str, Any]:
        """표시 파일용 불변 사본(lock 없이 읽는다)."""
        return self._status

    def _note(self, status: RestoreStatus) -> None:
        if status.state is not self._reported:
            self._reported = status.state
            if status.state is RestoreState.DISCARDED:
                log.warning("복원 Track 을 버렸다: %s (체크포인트 Track 시각 %s, 재시작 뒤 첫 프레임 %s). 이전 이벤트 연결은 "
                            "같은 규칙·같은 구역 규칙만 쓴다", status.reason, status.tracks_t, status.decided_t)
            elif status.state is RestoreState.ACCEPTED:
                log.info("복원 Track 후보 %d 개를 받았다(체크포인트 Track 시각 %s, 첫 프레임 %s, 연결은 %s 까지)",
                         len(status.restored), status.tracks_t, status.decided_t, status.link_until)
        self._status = MappingProxyType({
            "state": str(status.state),
            "reason": status.reason,
            "tracks_t": status.tracks_t,
            "decided_t": status.decided_t,
            "restored": list(status.restored),
            "alive": list(self._manager.restored_alive) if status.state is RestoreState.ACCEPTED else [],
            "link_until": status.link_until,
            "linkable_events": list(self.linkable_events),
            "rejected_tracks": self.rejected_tracks,
            "related_errors": self.related_errors,
        })


def _restore(manager: WorldTrackManager, tracks: Iterable[TrackCheckpoint], tracks_t: int | None, link: RestoreLink) -> None:
    try:
        manager.restore(tracks, tracks_t)
    except (TypeError, ValueError) as e:
        # 체크포인트 Track 을 믿을 수 없다: 쓰지 않는다(모르면 비운다). 연결 시간은 열어 두고 위치 없는 규칙만 쓴다
        link.rejected_tracks = f"{type(e).__name__}: {e}"
        log.error("체크포인트 Track 을 복원 후보로 받지 못했다(%s). 후보 없이 시작한다", link.rejected_tracks)
        manager.restore((), None)
