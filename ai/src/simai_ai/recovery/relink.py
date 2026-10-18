"""재시작 뒤 이전 이벤트 연결 (명세 3.2 재시작 복구, 부록 C·D-G3-09, S15P31A101-54).

재시작 전 활성 이벤트는 recover 가 UNVERIFIABLE 로 복원한다. 그 사람(재시작 전 World ID)은 재시작 뒤 새 World ID 로만
다시 보인다(World Track Manager 는 위치만으로 잇지 않는다). 재관측된 사람에게 같은 위험이 다시 걸려 새 이벤트가 생기면
이전 이벤트와의 연결(`related_event_id`)을 남긴다(3.2). 이 모듈은 새 이벤트를 열 때 그 연결을 정하는 함수
(`risk.lifecycle.RelatedEventFn`)를 만든다. 연결은 운영자가 볼 정보일 뿐이다. 복원 이벤트를 새 ID 로 옮기거나(키 바꾸기)
해소하지 않으므로, 재시작 뒤 다른 사람이 같은 자리에 나타나도 이전 경보가 그 사람의 관측으로 해소되지 않는다(G3).

새 이벤트(규칙 X, 대상 N)를 복원 이벤트(규칙 X, 대상 R)와 잇는 조건. 연결은 재시작 뒤 첫 프레임부터
`WorldTrackConfig.restore_max_age_ms`(30 s, `RestoreStatus.link_until`)까지만 새로 정한다. 그 안에서 정한 연결은 발생
지속 조건을 채우는 동안 연결 시간이 끝나도 LifecycleTracker 가 기억했다가 발생(RAISED)할 때 싣는다(리뷰 M1).

1. 복원 Track 후보: R 이 N 의 `restored_candidates`(N 의 첫 관측이 R 의 마지막 위치에서 갈 수 있는 거리 안)에 있다.
   후보가 여럿이면 가까운 R 의 이벤트를 고른다(두 복원 Track 이 한 사람을 다툼. 어느 쪽인지는 모르므로 연결만 한다).
2. 위치를 모를 때: N 이 처음 관측될 때 R 의 복원 Track 이 없었으면(체크포인트에 없음, 30 s 넘은 체크포인트, 이미
   후보에서 빠짐) 같은 규칙·같은 구역(zone_id)일 때 잇는다. 1 보다 뒤로 둔다.
3. R 의 복원 Track 이 있었는데 N 의 첫 관측이 갈 수 없는 거리였으면(`restored_excluded`) N 은 R 이 아니다. 잇지 않는다.

구역·시스템 대상 이벤트(R7 형태, (규칙, 구역) 키)는 재시작 뒤에도 같은 키라 복원 이벤트가 그대로 갱신된다(새 이벤트가
아니다). 그래서 사람 대상만 다룬다.
"""

from collections.abc import Iterable

from simai_ai.risk.common import Origin
from simai_ai.risk.lifecycle import EventKey, EventState, Lifecycle, RelatedEventFn
from simai_ai.risk.rule import Finding
from simai_ai.tracking.world_track import RestoreStatus, WorldTrackFrame


class RestoredEventLinker:
    """복원한 활성 이벤트(RecoveryResult.restored)로 새 이벤트의 related_event_id 를 정한다.

    Tier 0 조립부 사용법(S15P31A101-171):

        result = recover(journal=..., store=..., tracker=tracker)
        manager.restore(result.tracks, result.tracks_t)      # 첫 update 전
        linker = RestoredEventLinker(result.restored)
        # 판정 주기마다
        frame = manager.update(association, rig, sync_invalid)
        for rev in frame.revisions:
            tracker.correct_identity(rev.to_correction())
        step = tracker.apply(evaluation, world, config_version=..., evidence_ref=...,
                             related=linker.for_frame(frame, manager.restore_status))
    """

    def __init__(self, restored: Iterable[EventState]) -> None:
        events = []
        for state in restored:
            if state.origin is not Origin.LIVE:
                raise ValueError(f"LIVE 가 아닌 이벤트는 재시작 복원 대상이 아니다: {state.event_id} ({state.origin})")
            if state.lifecycle is Lifecycle.RESOLVED:
                raise ValueError(f"해소된 이벤트는 복원 이벤트가 아니다: {state.event_id}")
            if state.world_ids:
                events.append(state)
        self._events = tuple(sorted(events, key=lambda e: e.event_id))

    @property
    def events(self) -> tuple[EventState, ...]:
        """연결 대상이 되는 사람 대상 복원 이벤트 (event_id 순서)."""
        return self._events

    def for_frame(self, frame: WorldTrackFrame, status: RestoreStatus) -> RelatedEventFn | None:
        """이 World Track 결과로 판정할 때 apply(related=...) 에 넘길 함수. 연결 시간 밖이거나 대상이 없으면 None."""
        if not self._events or not status.link_open(frame.t):
            return None
        snapshots = {s.world_id: s for s in frame.tracks}
        events = self._events

        def related(key: EventKey, finding: Finding) -> str | None:
            if not key.world_ids:
                return None
            best: tuple[int, int, str] | None = None
            for subject in key.world_ids:
                snap = snapshots.get(subject)
                if snap is None:
                    continue
                candidates = snap.restored_candidates
                excluded = set(snap.restored_excluded)
                for event in events:
                    if event.rule_id != key.rule_id:
                        continue
                    for previous in event.world_ids:
                        if previous in candidates:
                            score = (0, candidates.index(previous), event.event_id)
                        elif previous in excluded:
                            continue  # 복원 Track 이 있었는데 갈 수 없는 거리였다: 그 사람이 아니다
                        elif event.zone_id is not None and event.zone_id == finding.zone_id:
                            score = (1, 0, event.event_id)
                        else:
                            continue
                        if best is None or score < best:
                            best = score
            return None if best is None else best[2]

        return related
