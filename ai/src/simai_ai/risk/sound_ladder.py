"""소리만 억제하는 단계·ACK 정책(S15P31A101-497). 위험 등급은 바꾸지 않는다."""

RANK = {"INFO": 0, "CAUTION": 1, "HIGH": 2, "CRITICAL": 3}
ACK_QUIET_MS = 30_000
# 같은 사람·구역·규칙이 소리 낸 뒤 같거나 낮은 단계로 다시 걸려도 이 시간 동안은 화면만(사용자 결정 2026-11-05).
SOUND_COOLDOWN_MS = 60_000


class SoundLadder:
    def __init__(self) -> None:
        self.active: dict[str, tuple[tuple[str, ...], int]] = {}
        self.acks: dict[tuple[str, str | None], tuple[int, int]] = {}
        self.sounded: dict[tuple[str, str | None, str], tuple[int, int]] = {}

    def acknowledge(self, worlds: tuple[str, ...], zone: str | None, severity: str, at: int) -> None:
        for world in worlds:
            key = (world, zone)
            previous = self.acks.get(key)
            if previous is None or at >= previous[0]:
                self.acks[key] = (at, RANK[severity])

    def decide(self, event_id: str, worlds: tuple[str, ...], zone: str | None,
               severity: str, rule: str, at: int, live_ids: set[str]) -> str:
        self.active = {eid: value for eid, value in self.active.items() if eid in live_ids}
        self.acks = {key: value for key, value in self.acks.items() if at - value[0] < ACK_QUIET_MS}
        self.sounded = {key: value for key, value in self.sounded.items() if at - value[0] < SOUND_COOLDOWN_MS}
        rank = RANK[severity]
        # 위험(CRITICAL)은 시간 억제(ACK 뒤·소리 뒤)를 받지 않는다.
        quiet_allowed = severity != "CRITICAL"
        blocked = any(
            rank <= level and set(worlds).intersection(ids)
            for ids, level in self.active.values()
        )
        blocked |= quiet_allowed and any(
            (ack := self.acks.get((world, zone))) is not None
            and 0 <= at - ack[0] < ACK_QUIET_MS and rank <= ack[1]
            for world in worlds
        )
        blocked |= quiet_allowed and any(
            (prior := self.sounded.get((world, zone, rule))) is not None
            and 0 <= at - prior[0] < SOUND_COOLDOWN_MS and rank <= prior[1]
            for world in worlds
        )
        if blocked:
            return "SILENT"
        self.active[event_id] = (worlds, rank)
        for world in worlds:
            self.sounded[(world, zone, rule)] = (at, rank)
        return "ONCE" if rule == "R3" else "REPEAT"
