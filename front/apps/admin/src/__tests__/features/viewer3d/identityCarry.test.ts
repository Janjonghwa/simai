import { aliasOwners, identityUncertainSince } from '@/features/viewer3d/data/identityCarry';
import type { TrackView, WorldFrame } from '@/features/viewer3d/data/spatialFrame';
import { describe, expect, it } from 'vitest';

type Track = Pick<TrackView, 'worldId' | 'aliases' | 'identityConfidence' | 'identityRevision'>;
const frame = (t: number, tracks: Track[], since?: Map<string, number>) =>
  ({
    tLatestGlobal: t,
    tracks: tracks as unknown as TrackView[],
    ...(since ? { identityUncertainSince: since } : {}),
  }) as unknown as WorldFrame;
const track = (worldId: string, over: Partial<Track> = {}): Track => ({
  worldId,
  aliases: [],
  identityConfidence: 'HIGH',
  identityRevision: 0,
  ...over,
});

describe('aliasOwners', () => {
  it('흡수된 ID 를 지금 그 사람의 World ID 로 잇고 자기 ID 는 무시한다', () => {
    const owners = aliasOwners([track('w-1', { aliases: ['w-2', 'w-1'] }), track('w-3')]);
    expect([...owners]).toEqual([['w-2', 'w-1']]);
  });
});

describe('identityUncertainSince', () => {
  it('같은 차수로 이어진 불확실은 시작 시각을 잇고, 차수가 바뀌거나 풀리면 새로 센다', () => {
    const first = identityUncertainSince(
      null,
      frame(1000, [track('w-1', { identityConfidence: 'IDENTITY_UNCERTAIN' })]),
    );
    expect(first.get('w-1')).toBe(1000);
    const prev = frame(
      1000,
      [track('w-1', { identityConfidence: 'IDENTITY_UNCERTAIN' })],
      new Map(first),
    );
    const same = identityUncertainSince(
      prev,
      frame(1400, [track('w-1', { identityConfidence: 'IDENTITY_UNCERTAIN' })]),
    );
    expect(same.get('w-1')).toBe(1000);
    const bumped = identityUncertainSince(
      prev,
      frame(1500, [
        track('w-1', { identityConfidence: 'IDENTITY_UNCERTAIN', identityRevision: 1 }),
      ]),
    );
    expect(bumped.get('w-1')).toBe(1500);
    expect(identityUncertainSince(prev, frame(1600, [track('w-1')])).size).toBe(0);
  });
});
