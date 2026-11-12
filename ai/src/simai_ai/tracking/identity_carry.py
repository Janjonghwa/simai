"""World ID 교정을 받은 소비자가 상태를 이어 갈지 정하는 판정 (S15P31A101-661, docs/design/S15P31A101-661-merge-carryover.md).

World Track Manager(`world_track`)는 병합 때 남는 Track 의 identity_revision 을 올리고 사라지는 ID 를 aliases 뒤에 붙인다.
분할 때는 원래 ID W 와 새 ID N 의 identity_revision 을 같은 값으로 함께 올리고 N 에 `split_from = W` 를 붙인다. aliases 는
바꾸지 않는다. 그 밖에 ID 맞바꿈 되돌리기(swap revert)도 aliases 없이 identity_revision 만 올린다.

**순수 병합**: 소비자(PPE·아바타)가 앞서 본 같은 World ID 의 (identity_revision, aliases, split_from) 과 이번 값을 비교해
1. identity_revision 이 올랐고
2. 앞서 본 aliases 가 그대로 앞에 남아 있으며(빼지 않음) 이번에 새 alias 가 하나 이상 붙었고
3. split_from 이 그대로이고(새로 생기거나 바뀌지 않음)
4. 같은 world_state 에 `split_from` 이 이 ID 이고 identity_revision 이 이번 값과 같은 Track(같은 시각 분할로 갈라져 나간 N)이
   없을 때다.
이때만 새로 붙은 alias(흡수된 ID)를 돌려준다. 하나라도 어긋나면 None 이고 소비자는 지금처럼 처음부터 다시 쌓는다.

identity_confidence 는 보지 않는다. 병합한 시각부터 남는 Track 은 일관성 확인이 끝날 때까지(merge_confirm_frames + 유지 1 s)
늘 IDENTITY_UNCERTAIN(LATE_REASSOCIATION)이라 그것으로 거르면 순수 병합이 생기지 않는다. 불확실 구간은 각 소비자의 기존
규칙(PPE: 불확실 칸 건너뛰기·3초 초과 초기화, 아바타: 누적 일시정지)이 그대로 맡는다.

이 모듈은 World ID·위치를 만들거나 바꾸지 않는다(Tier 0 정본, AGENTS 규칙 1). 표준 라이브러리만 쓴다.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any


def split_marks(tracks: Iterable[Mapping[str, Any]]) -> frozenset[tuple[str, int]]:
    """world_state 트랙들에서 (분할 원래 ID, 분할 차수) 표시를 모은다. 분할은 W·N 의 차수를 같은 값으로 올린다."""
    marks = set()
    for tr in tracks:
        source = tr.get("split_from")
        revision = tr.get("identity_revision")
        if isinstance(source, str) and source and isinstance(revision, int) and not isinstance(revision, bool):
            marks.add((source, revision))
    return frozenset(marks)


def pure_merge_absorbed(
    world_id: str,
    prev_revision: int,
    prev_aliases: Sequence[str],
    prev_split_from: str | None,
    revision: int,
    aliases: Sequence[str],
    split_from: str | None,
    marks: frozenset[tuple[str, int]] | set[tuple[str, int]] = frozenset(),
) -> tuple[str, ...] | None:
    """순수 병합이면 이번에 흡수된 ID(새로 붙은 alias, 순서대로), 아니면 None."""
    if revision <= prev_revision:
        return None
    aliases = tuple(aliases)
    prev = tuple(prev_aliases)
    if aliases[: len(prev)] != prev:
        return None   # 별칭이 빠지거나 순서가 바뀌었다: 알 수 없는 교정
    absorbed = tuple(a for a in dict.fromkeys(aliases[len(prev):]) if a and a != world_id and a not in prev)
    if not absorbed:
        return None   # 별칭 변화 없이 차수만 올랐다(분할 원래 ID, 맞바꿈 되돌리기 등)
    if split_from != prev_split_from:
        return None
    if (world_id, revision) in marks:
        return None   # 같은 차수로 이 ID 에서 갈라져 나간 Track 이 있다(병합과 분할이 겹침)
    return absorbed
