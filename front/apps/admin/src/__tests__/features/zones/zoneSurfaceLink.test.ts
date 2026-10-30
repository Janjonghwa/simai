import { zoneOf } from '@/__tests__/features/zones/zoneFixture';
import type { SurfaceGeometry } from '@/features/surfaces/types';
import { linkSurfaces, pointInPolygon } from '@/features/zones/zoneSurfaceLink';
import { describe, expect, it } from 'vitest';

const surfaceAt = (objectId: string, cx: number, cy: number, size: [number, number] = [1, 1]) => ({
  objectId,
  geometry: {
    centerXy: [cx, cy],
    sizeXy: size,
    yawRad: 0,
    topHeightM: 0.4,
  } satisfies SurfaceGeometry,
});

const SQUARE = [
  [0, 0],
  [4, 0],
  [4, 4],
  [0, 4],
] as const;

describe('pointInPolygon', () => {
  it('안·밖·경계 위(경계는 안)를 가른다', () => {
    expect(pointInPolygon([2, 2], SQUARE)).toBe(true);
    expect(pointInPolygon([5, 2], SQUARE)).toBe(false);
    expect(pointInPolygon([-0.01, 2], SQUARE)).toBe(false);
    expect(pointInPolygon([4, 2], SQUARE)).toBe(true);
    expect(pointInPolygon([0, 0], SQUARE)).toBe(true);
  });

  it('오목 다각형(L 모양)의 파인 곳은 밖이다', () => {
    const l = [
      [0, 0],
      [4, 0],
      [4, 2],
      [2, 2],
      [2, 4],
      [0, 4],
    ] as const;
    expect(pointInPolygon([1, 3], l)).toBe(true);
    expect(pointInPolygon([3, 1], l)).toBe(true);
    expect(pointInPolygon([3, 3], l)).toBe(false);
  });
});

describe('linkSurfaces(작업면 중심이 구역 바닥 다각형 안이면 그 구역의 것)', () => {
  const a = zoneOf({ zoneId: 'z-a', name: 'A', min: [0, 0, 0], max: [4, 4, 2] });
  const b = zoneOf({ zoneId: 'z-b', name: 'B', min: [6, 6, 0], max: [8, 8, 2] });

  it('중심이 안에 있으면 잇고, 밖이면 어느 구역에도 잇지 않는다', () => {
    const links = linkSurfaces([a, b], [surfaceAt('ws-1', 2, 2), surfaceAt('ws-2', 5, 5)]);
    expect([...links.keys()]).toEqual(['z-a']);
    expect(links.get('z-a')?.objectId).toBe('ws-1');
  });

  it('작업면 크기가 구역 밖으로 나가도 중심만 본다', () => {
    expect(linkSurfaces([a], [surfaceAt('ws-1', 3.9, 2, [3, 3])]).get('z-a')?.objectId).toBe(
      'ws-1',
    );
  });

  it('겹친 두 구역에 들면 바닥 넓이가 작은 구역이 가진다', () => {
    const inner = zoneOf({ zoneId: 'z-inner', name: '안', min: [1, 1, 0], max: [3, 3, 2] });
    const links = linkSurfaces([a, inner], [surfaceAt('ws-1', 2, 2)]);
    expect([...links.keys()]).toEqual(['z-inner']);
  });

  it('넓이가 같으면 구역 ID 순으로 가진다', () => {
    const c = zoneOf({ zoneId: 'z-c', name: 'C', min: [0, 0, 0], max: [4, 4, 2] });
    const links = linkSurfaces([c, a], [surfaceAt('ws-1', 2, 2)]);
    expect([...links.keys()]).toEqual(['z-a']);
  });

  it('한 구역에 작업면이 둘 이상 들면 작업면 ID 순으로 첫 번째만 잇는다', () => {
    const links = linkSurfaces([a], [surfaceAt('ws-2', 1, 1), surfaceAt('ws-1', 3, 3)]);
    expect(links.size).toBe(1);
    expect(links.get('z-a')?.objectId).toBe('ws-1');
  });

  it('구역이나 작업면이 없으면 빈 연결이다', () => {
    expect(linkSurfaces([], [surfaceAt('ws-1', 2, 2)]).size).toBe(0);
    expect(linkSurfaces([a], []).size).toBe(0);
  });

  it('바닥이 직사각형이 아닌 구역도 다각형으로 판정한다', () => {
    const l = zoneOf(
      { zoneId: 'z-l', name: 'L', min: [0, 0, 0], max: [4, 4, 2] },
      {
        polygon: [
          [0, 0],
          [4, 0],
          [4, 2],
          [2, 2],
          [2, 4],
          [0, 4],
        ],
      },
    );
    expect(linkSurfaces([l], [surfaceAt('ws-1', 3, 3)]).size).toBe(0);
    expect(linkSurfaces([l], [surfaceAt('ws-1', 1, 3)]).size).toBe(1);
  });
});
