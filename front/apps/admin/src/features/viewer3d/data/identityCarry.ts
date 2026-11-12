import type { TrackView, WorldFrame } from '@/features/viewer3d/data/spatialFrame';

// World ID 교정을 화면 상태가 따라가는 곳(S15P31A101-661, docs/design/S15P31A101-661-merge-carryover.md).
// 위치·접지·ID 는 늘 Tier 0(world_state)이고 여기서 만들거나 바꾸지 않는다. 받은 aliases 로 "그 ID 가 지금 누구인가"만 찾는다.

/**
 * 선택·초점 대상이 장면에서 사라진 뒤, 그 ID 를 aliases 에 가진 생존 트랙이 나타나기를 기다리는 시간(ms).
 * 병합은 같은 world_state 한 장에서 흡수 ID 를 빼고 생존 트랙 aliases 에 넣으므로 보통 같은 프레임이다. 10 Hz 에서
 * 프레임 하나가 늦거나 순서가 바뀌어도(직후 프레임) 받도록 몇 프레임(500 ms)을 둔다. 넘으면 지금처럼 선택을 비운다.
 */
export const MERGE_CARRY_GRACE_MS = 500;

/** 병합으로 흡수된 ID → 지금 그 사람의 World ID. 자기 ID 를 별칭으로 든 값은 무시한다. */
export function aliasOwners(
  tracks: readonly Pick<TrackView, 'worldId' | 'aliases'>[],
): ReadonlyMap<string, string> {
  const owners = new Map<string, string>();
  for (const track of tracks) {
    for (const alias of track.aliases) {
      if (alias !== track.worldId) owners.set(alias, track.worldId);
    }
  }
  return owners;
}

/**
 * 같은 identity_revision 으로 IDENTITY_UNCERTAIN 이 이어지기 시작한 World 시각(t_latest_global, ms). 키 world_id.
 * 앞 프레임에서도 같은 차수로 불확실이었으면 그 시작을 잇고, 아니면 이번 프레임 시각이다. 불확실이 아니면 없다.
 */
export function identityUncertainSince(
  prev: WorldFrame | null,
  next: Pick<WorldFrame, 'tLatestGlobal' | 'tracks'>,
): ReadonlyMap<string, number> {
  const since = new Map<string, number>();
  const before = new Map(prev?.tracks.map((t) => [t.worldId, t]));
  for (const track of next.tracks) {
    if (track.identityConfidence !== 'IDENTITY_UNCERTAIN') continue;
    const old = before.get(track.worldId);
    const started = prev?.identityUncertainSince?.get(track.worldId);
    const continued =
      old !== undefined &&
      started !== undefined &&
      old.identityConfidence === 'IDENTITY_UNCERTAIN' &&
      old.identityRevision === track.identityRevision;
    since.set(track.worldId, continued ? started : next.tLatestGlobal);
  }
  return since;
}
