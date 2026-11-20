"""R5(위험구역 접근 예측)의 새 발생을 자세가 바뀌는 동안만 막는 판정용 기억(S15P31A101-833). 낙상 진행·최근 낙상, 머리 높이 낮음, 머리 높이 급변을 본다."""

from collections import deque
from dataclasses import dataclass, field

from simai_ai.risk.world import ObservationState, Validity, WorldState

R4_RECENT_MS = 10_000
HEAD_WINDOW_MS = 1_000
HEAD_CHANGE_M = 0.30
LOW_HEAD_M = 1.20
LOW_HEAD_RATIO = 0.75


@dataclass
class _Person:
    revision: int
    last_seen: int
    samples: deque = field(default_factory=deque)
    standing: float | None = None


class PostureGate:
    def __init__(self):
        self.people = {}
        self.recent_r4 = {}
        self.calibration = None
        self.last_time = None

    def observe(self, world: WorldState) -> None:
        now = world.t_latest_global
        if self.calibration != world.calibration_version or (self.last_time is not None and now < self.last_time):
            self.people.clear()
            self.recent_r4.clear()
        self.calibration, self.last_time = world.calibration_version, now
        for track in world.tracks:
            person = self.people.get(track.world_id)
            if person is None or person.revision != track.identity_revision or track.t_last_valid < person.last_seen:
                person = self.people[track.world_id] = _Person(track.identity_revision, track.t_last_valid)
                self.recent_r4.pop(track.world_id, None)
            if track.observation_state != ObservationState.OBSERVED or track.head_z is None:
                continue
            if track.head_z_validity not in (None, Validity.VALID):
                continue
            if person.samples and track.t_last_valid <= person.samples[-1][0]:
                continue
            person.last_seen = track.t_last_valid
            person.samples.append((track.t_last_valid, track.head_z))
            while person.samples and person.samples[0][0] < track.t_last_valid - HEAD_WINDOW_MS:
                person.samples.popleft()
            if track.ground_anchor_xyz is not None and track.z_validity == Validity.VALID:
                height = track.head_z - track.ground_anchor_xyz[2]
                if height >= 1.35:
                    person.standing = max(person.standing or 0, height)
        self.people = {k: v for k, v in self.people.items() if now - v.last_seen <= 30_000}
        self.recent_r4 = {k: v for k, v in self.recent_r4.items() if now - v <= R4_RECENT_MS}

    def blocked(self, world_id, track, now, active_r4):
        if world_id in active_r4 or now - self.recent_r4.get(world_id, now - R4_RECENT_MS - 1) <= R4_RECENT_MS:
            return True
        person = self.people.get(world_id)
        if person is None or track is None or track.observation_state != ObservationState.OBSERVED:
            return False
        if track.head_z is None or track.t_last_valid != person.last_seen or now - track.t_last_valid > 500:
            return False
        if track.ground_anchor_xyz is not None and track.z_validity == Validity.VALID:
            height = track.head_z - track.ground_anchor_xyz[2]
            threshold = LOW_HEAD_M if person.standing is None else person.standing * LOW_HEAD_RATIO
            if height < threshold:
                return True
        return len(person.samples) >= 2 and max(v for _, v in person.samples) - min(v for _, v in person.samples) >= HEAD_CHANGE_M
