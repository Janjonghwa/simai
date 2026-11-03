import { zoneOf } from '@/__tests__/features/zones/zoneFixture';
import type { SurfaceGeometry } from '@/features/surfaces/types';
import { linkSurfaces, pointInPolygon } from '@/features/zones/zoneSurfaceLink';
import { describe, expect, it } from 'vitest';

const surfaceOf = (objectId: string, zoneId: string | null) => ({ objectId, zoneId });

const geometryAt = (cx: number, cy: number): SurfaceGeometry => ({
  centerXy: [cx, cy],
  sizeXy: [1, 1],
  yawRad: 0,
  topHeightM: 0.4,
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

describe('linkSurfaces(서버가 작업면에 저장한 zone_id 로 잇는다)', () => {
  const a = zoneOf({ zoneId: 'z-a', name: 'A', min: [0, 0, 0], max: [4, 4, 2] });
  const b = zoneOf({ zoneId: 'z-b', name: 'B', min: [6, 6, 0], max: [8, 8, 2] });

  it('작업면의 zoneId 가 가리키는 구역에 잇는다', () => {
    const ws1 = surfaceOf('ws-1', 'z-a');
    const ws2 = surfaceOf('ws-2', 'z-b');
    const links = linkSurfaces([a, b], [ws1, ws2]);
    expect([...links.keys()]).toEqual(['z-a', 'z-b']);
    expect(links.get('z-a')).toBe(ws1);
    expect(links.get('z-b')).toBe(ws2);
  });

  it('지금 구역에 없는(보관된) 구역 ID 에 걸린 작업면은 거른다', () => {
    const links = linkSurfaces([a], [surfaceOf('ws-1', 'z-a'), surfaceOf('ws-2', 'z-archived')]);
    expect([...links.keys()]).toEqual(['z-a']);
    expect(links.get('z-a')?.objectId).toBe('ws-1');
    expect(linkSurfaces([a], [surfaceOf('ws-2', 'z-archived')]).size).toBe(0);
  });

  it('zoneId 가 null 인 작업면은 어느 구역에도 잇지 않는다', () => {
    expect(linkSurfaces([a, b], [surfaceOf('ws-1', null)]).size).toBe(0);
  });

  it('한 구역에 작업면이 둘 이상 걸리면 작업면 ID 순으로 첫 번째만 잇는다(입력 순서와 무관)', () => {
    const ws1 = surfaceOf('ws-1', 'z-a');
    const ws2 = surfaceOf('ws-2', 'z-a');
    for (const surfaces of [
      [ws2, ws1],
      [ws1, ws2],
    ]) {
      const links = linkSurfaces([a], surfaces);
      expect(links.size).toBe(1);
      expect(links.get('z-a')).toBe(ws1);
    }
  });

  it('구역이 겹치거나 작업면 위치가 구역 밖이어도 기하가 아니라 zoneId 만 본다', () => {
    const inner = zoneOf({ zoneId: 'z-inner', name: '안', min: [1, 1, 0], max: [3, 3, 2] });
    // 중심은 안쪽 구역 안(a·inner 모두 포함)이지만 zoneId 는 바깥 구역이다
    const inside = { ...surfaceOf('ws-1', 'z-a'), geometry: geometryAt(2, 2) };
    expect([...linkSurfaces([a, inner], [inside]).keys()]).toEqual(['z-a']);
    // 중심이 어느 구역 안에도 없지만 zoneId 가 있으면 잇는다
    const outside = { ...surfaceOf('ws-2', 'z-b'), geometry: geometryAt(20, 20) };
    expect([...linkSurfaces([a, b], [outside]).keys()]).toEqual(['z-b']);
    // 중심이 구역 안에 있어도 zoneId 가 null 이면 잇지 않는다
    const unlinked = { ...surfaceOf('ws-3', null), geometry: geometryAt(2, 2) };
    expect(linkSurfaces([a, inner], [unlinked]).size).toBe(0);
  });

  it('구역이나 작업면이 없으면 빈 연결이다', () => {
    expect(linkSurfaces([], [surfaceOf('ws-1', 'z-a')]).size).toBe(0);
    expect(linkSurfaces([a], []).size).toBe(0);
  });
});
