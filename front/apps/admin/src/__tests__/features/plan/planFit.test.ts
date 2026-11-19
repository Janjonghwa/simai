import { boundsDepth, boundsWidth, type PlanBounds, type PlanZone } from '@/features/plan/plan';
import {
  FIT_MIN_EXTENT_M,
  FIT_PAD_M,
  FIT_SHRINK_AFTER_MS,
  fitView,
  minimapEmphasis,
  nextMinimapView,
  type MinimapInput,
} from '@/features/plan/planFit';
import type { WorldTrack } from '@simai/contracts';
import { describe, expect, it } from 'vitest';

// 미니맵 범위(-800): 방 전체 대신 위험 구역과 사람이 있는 곳에 맞추고, 사람이 조금 움직여도 흔들리지 않는다.

/** 큰 방(평면도 30×30 m). 방 전체를 작은 칸에 담으면 점처럼 보인다. */
const ROOM: PlanBounds = { minX: 0, minY: 0, maxX: 30, maxY: 30 };

const zone = (id: string, min: [number, number], max: [number, number]): PlanZone => ({
  zoneId: id,
  name: id,
  min: [min[0], min[1], 0],
  max: [max[0], max[1], 2],
});

const track = (
  id: string,
  x: number,
  y: number,
  observation_state: WorldTrack['observation_state'] = 'OBSERVED',
) =>
  ({
    world_id: id,
    position_xy: [x, y],
    position_xy_sigma: [0.1, 0.1],
    observation_state,
    track_lifecycle: 'CONFIRMED',
  }) as unknown as WorldTrack;

const input = (change: Partial<MinimapInput> = {}): MinimapInput => ({
  zones: [],
  tracks: [],
  bounds: ROOM,
  ...change,
});

/** inner 가 outer 안에 margin 이상 들어가 있다. */
const inside = (outer: PlanBounds, inner: PlanBounds, margin = 0) =>
  inner.minX - outer.minX >= margin - 1e-9 &&
  inner.minY - outer.minY >= margin - 1e-9 &&
  outer.maxX - inner.maxX >= margin - 1e-9 &&
  outer.maxY - inner.maxY >= margin - 1e-9;

const point = (x: number, y: number): PlanBounds => ({ minX: x, minY: y, maxX: x, maxY: y });

describe('미니맵 범위 맞추기', () => {
  it('구역과 사람을 모두 여백을 두고 담고, 방 전체보다 작다(정사각형)', () => {
    const zones = [zone('z-1', [10, 10], [14, 12])];
    const view = fitView(input({ zones, tracks: [track('w-1', 17, 13)] }));
    expect(inside(view, { minX: 10, minY: 10, maxX: 14, maxY: 12 }, FIT_PAD_M)).toBe(true);
    expect(inside(view, point(17, 13), FIT_PAD_M)).toBe(true);
    expect(boundsWidth(view)).toBeCloseTo(boundsDepth(view));
    expect(boundsWidth(view)).toBeLessThan(15);
  });

  it('사람 한 명만 있어도 최소 한 변보다 크게 당기지 않는다', () => {
    const view = fitView(input({ tracks: [track('w-1', 15, 15)] }));
    expect(boundsWidth(view)).toBeCloseTo(FIT_MIN_EXTENT_M);
    expect(boundsDepth(view)).toBeCloseTo(FIT_MIN_EXTENT_M);
    expect(inside(view, point(15, 15), FIT_MIN_EXTENT_M / 2 - 1e-6)).toBe(true);
  });

  it('구역·사람이 없으면 방 전체(+ 여백)를 비춘다', () => {
    const view = fitView(input());
    expect(view.minX).toBeLessThan(0);
    expect(view.maxX).toBeGreaterThan(30);
  });

  it('방 가장자리의 사람은 범위를 방(+ 여백) 밖으로 밀지 않고 안으로 민다', () => {
    const view = fitView(input({ tracks: [track('w-1', 0.2, 0.2)] }));
    expect(view.minX).toBeGreaterThanOrEqual(-1);
    expect(view.minY).toBeGreaterThanOrEqual(-1);
    expect(inside(view, point(0.2, 0.2))).toBe(true);
  });

  it('확인 불가 사람과 방에서 멀리 떨어진 엉뚱한 위치는 범위를 넓히지 않는다', () => {
    const zones = [zone('z-1', [10, 10], [12, 12])];
    const base = fitView(input({ zones }));
    expect(
      fitView(input({ zones, tracks: [track('w-9', 25, 25, 'UNKNOWN'), track('w-8', 400, 400)] })),
    ).toEqual(base);
  });
});

describe('미니맵 범위 흔들림 막기', () => {
  const zones = [zone('z-1', [10, 10], [14, 14])];

  it('사람이 범위 안에서 조금 움직이면 같은 범위(같은 객체)를 둔다', () => {
    const first = nextMinimapView(null, input({ zones, tracks: [track('w-1', 12, 12)] }), 0);
    const next = nextMinimapView(first, input({ zones, tracks: [track('w-1', 12.4, 11.7)] }), 100);
    expect(next).toBe(first);
  });

  it('사람이 가장자리를 넘으면 바로 넓혀 그 사람을 담는다', () => {
    const first = nextMinimapView(null, input({ zones, tracks: [track('w-1', 12, 12)] }), 0);
    const out = nextMinimapView(first, input({ zones, tracks: [track('w-1', 20, 12)] }), 100);
    expect(out.view).not.toEqual(first.view);
    expect(inside(out.view, point(20, 12), FIT_PAD_M)).toBe(true);
    expect(inside(out.view, first.view)).toBe(true);
    // 넓힌 뒤 조금 더 걸어도 다시 넓히지 않는다(여유).
    const walk = nextMinimapView(out, input({ zones, tracks: [track('w-1', 20.5, 12)] }), 200);
    expect(walk.view).toBe(out.view);
  });

  it('사람이 돌아와도 바로 좁히지 않고 좁은 범위로 충분한 상태가 이어져야 좁힌다', () => {
    const wide = nextMinimapView(null, input({ zones, tracks: [track('w-1', 24, 24)] }), 0);
    const back = input({ zones, tracks: [track('w-1', 12, 12)] });
    const waiting = nextMinimapView(wide, back, 1_000);
    expect(waiting.view).toBe(wide.view);
    const still = nextMinimapView(waiting, back, 1_000 + FIT_SHRINK_AFTER_MS - 1);
    expect(still.view).toBe(wide.view);
    const shrunk = nextMinimapView(still, back, 1_000 + FIT_SHRINK_AFTER_MS);
    expect(boundsWidth(shrunk.view)).toBeLessThan(boundsWidth(wide.view));
    expect(shrunk.view).toEqual(fitView(back));
  });

  it('구역·사람이 없다가 생기면 기다리지 않고 바로 맞춘다', () => {
    const empty = nextMinimapView(null, input(), 0);
    expect(empty.whole).toBe(true);
    expect(nextMinimapView(empty, input(), 100)).toBe(empty);
    const fitted = nextMinimapView(empty, input({ zones }), 100);
    expect(fitted.whole).toBe(false);
    expect(fitted.view).toEqual(fitView(input({ zones })));
  });
});

describe('미니맵 표시 크기', () => {
  it('넓은 범위일수록 사람 표식을 키우고(상한 있음), 구역 채움을 큰 평면보다 짙게 한다', () => {
    const small = minimapEmphasis({ minX: 0, minY: 0, maxX: 6, maxY: 6 });
    const large = minimapEmphasis({ minX: 0, minY: 0, maxX: 24, maxY: 24 });
    expect(large.marker).toBeGreaterThan(small.marker);
    expect(large.zoneStroke).toBeGreaterThan(large.marker);
    expect(minimapEmphasis({ minX: 0, minY: 0, maxX: 500, maxY: 500 }).marker).toBe(5);
    expect(small.zoneFill.normal).toBeGreaterThan(0.1);
  });
});
