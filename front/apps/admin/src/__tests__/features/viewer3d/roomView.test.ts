import {
  DEFAULT_VIEW_ELEVATION_DEG,
  DEFAULT_VIEW_FOV_DEG,
  roomViewOf,
  viewBoundsFrom,
} from '@/features/viewer3d/scene/roomView';
import { threeToWorld, worldPoint, worldToThree } from '@/features/viewer3d/worldCoordinates';
import { DEFAULT_ROOM_LIMIT, type RoomBounds } from '@/lib/roomLimit';
import { PerspectiveCamera, Vector3 } from 'three';
import { describe, expect, it } from 'vitest';

const toWorld = (three: readonly [number, number, number]) =>
  threeToWorld([three[0], three[1], three[2]] as Parameters<typeof threeToWorld>[0]);

/** 실제 Three PerspectiveCamera 로 경계 상자 8개 꼭짓점을 화면 NDC(-1~1)에 투영한다. */
function projectCorners(bounds: RoomBounds, fovDeg: number, aspect: number) {
  const view = roomViewOf(bounds, { fovDeg, aspect });
  const camera = new PerspectiveCamera(fovDeg, aspect, 0.1, 500);
  camera.position.set(view.home[0], view.home[1], view.home[2]);
  camera.lookAt(view.target[0], view.target[1], view.target[2]);
  camera.updateMatrixWorld();
  camera.updateProjectionMatrix();

  const ndcs: Array<{ x: number; y: number; z: number }> = [];
  for (const x of [bounds.min[0], bounds.max[0]]) {
    for (const y of [bounds.min[1], bounds.max[1]]) {
      for (const z of [bounds.min[2], bounds.max[2]]) {
        const p = new Vector3(...worldToThree(worldPoint(x, y, z))).project(camera);
        ndcs.push({ x: p.x, y: p.y, z: p.z });
      }
    }
  }
  return { view, ndcs };
}

describe('기본 시점 계산(roomViewOf)', () => {
  it('World 남쪽(−Y)에서 수평 위 35° 로 Scene 중심(xy)을 내려다본다', () => {
    const lobby: RoomBounds = { min: [0, 0, 0], max: [7.9, 8.4, 2.8] };
    const view = roomViewOf(lobby, { fovDeg: DEFAULT_VIEW_FOV_DEG, aspect: 1440 / 900 });
    const targetW = toWorld(view.target);
    const homeW = toWorld(view.home);

    // 바라보는 점: 중심(xy), 높이 min(0.8m, 높이×0.3) = 0.8m
    expect(targetW[0]).toBeCloseTo(3.95, 6);
    expect(targetW[1]).toBeCloseTo(4.2, 6);
    expect(targetW[2]).toBeCloseTo(0.8, 6);

    // 남쪽(−Y) 정면: World X 는 중심과 같고, Y 는 남쪽(target 보다 작음), Z 는 위(target 보다 큼)
    const dx = homeW[0] - targetW[0];
    const dy = homeW[1] - targetW[1];
    const dz = homeW[2] - targetW[2];
    expect(dx).toBeCloseTo(0, 6);
    expect(dy).toBeLessThan(0);
    expect(dz).toBeGreaterThan(0);

    // Three 좌표에서는 World −Y 가 +Z 쪽이다.
    expect(view.home[0]).toBeCloseTo(view.target[0], 6);
    expect(view.home[1]).toBeGreaterThan(view.target[1]);
    expect(view.home[2]).toBeGreaterThan(view.target[2]);

    // 올려다보는 각도 = 수평 위 35°
    const elevationDeg = (Math.atan2(dz, -dy) * 180) / Math.PI;
    expect(elevationDeg).toBeCloseTo(DEFAULT_VIEW_ELEVATION_DEG, 6);
  });

  it('바라보는 점 높이는 min(0.8m, 높이×0.3)을 따른다', () => {
    // 높이 1.5m → 0.45m (< 0.8m)
    const low = toWorld(roomViewOf({ min: [0, 0, 0], max: [6, 8, 1.5] }).target);
    expect(low[2]).toBeCloseTo(0.45, 6);

    // 높이 3.0m → min(0.8, 0.9) = 0.8m
    const normal = toWorld(roomViewOf({ min: [0, 0, 0], max: [6, 8, 3.0] }).target);
    expect(normal[2]).toBeCloseTo(0.8, 6);

    // 바닥이 살짝 음수(-0.02m)인 Scene 도 경계 바닥 기준으로 계산한다
    const demo = toWorld(roomViewOf({ min: [-0.1, 0, -0.02], max: [10, 10.1, 2.2] }).target);
    expect(demo[0]).toBeCloseTo(4.95, 6);
    expect(demo[1]).toBeCloseTo(5.05, 6);
    expect(demo[2]).toBeCloseTo(-0.02 + 2.22 * 0.3, 6);
  });

  it.each<[string, RoomBounds]>([
    ['로비(7.9×8.4×2.8)', { min: [0, 0, 0], max: [7.9, 8.4, 2.8] }],
    ['목업 Scene(10.1×10.1×2.22)', { min: [-0.1, 0, -0.02], max: [10, 10.1, 2.2] }],
    ['가로로 긴 방(18×6×3)', { min: [-4, 1, 0], max: [14, 7, 3] }],
    ['세로로 깊은 방(4×14×3)', { min: [0, 0, 0], max: [4, 14, 3] }],
    ['높은 방(6×6×5)', { min: [0, 0, 0], max: [6, 6, 5] }],
  ])('%s 이 1440×900·1288×674·4:3·세로 화면 모두에서 잘리지 않고 알맞게 찬다', (_, bounds) => {
    for (const aspect of [1440 / 900, 1288 / 674, 800 / 600, 600 / 800]) {
      const { ndcs } = projectCorners(bounds, DEFAULT_VIEW_FOV_DEG, aspect);
      let maxExtent = 0;
      for (const p of ndcs) {
        expect(Math.abs(p.x)).toBeLessThan(1);
        expect(Math.abs(p.y)).toBeLessThan(1);
        expect(p.z).toBeGreaterThan(-1);
        expect(p.z).toBeLessThan(1);
        maxExtent = Math.max(maxExtent, Math.abs(p.x), Math.abs(p.y));
      }
      // 여유 배수(1.15) 때문에 가장 바깥 꼭짓점이 화면 반경의 약 1/1.15(≈0.87)에 닿는다.
      expect(maxExtent).toBeGreaterThan(0.8);
      expect(maxExtent).toBeLessThan(0.95);
    }
  });

  it('세로 시야각이 좁거나 화면 비율이 좁아지면 방이 잘리지 않게 거리가 멀어진다', () => {
    const wideRoom: RoomBounds = { min: [0, 0, 0], max: [14, 6, 2.8] };
    const dist = (v: ReturnType<typeof roomViewOf>) =>
      new Vector3(...v.home).distanceTo(new Vector3(...v.target));

    const narrowFov = roomViewOf(wideRoom, { fovDeg: 35, aspect: 1440 / 900 });
    const wideFov = roomViewOf(wideRoom, { fovDeg: 60, aspect: 1440 / 900 });
    expect(dist(narrowFov)).toBeGreaterThan(dist(wideFov));

    const narrowAspect = roomViewOf(wideRoom, { fovDeg: 50, aspect: 4 / 3 });
    const wideAspect = roomViewOf(wideRoom, { fovDeg: 50, aspect: 1288 / 674 });
    expect(dist(narrowAspect)).toBeGreaterThan(dist(wideAspect));
  });

  it('시야각·화면 비율이 잘못 들어와도 기본값으로 안전하게 계산한다', () => {
    const bounds: RoomBounds = { min: [0, 0, 0], max: [8, 8, 3] };
    const fallback = roomViewOf(bounds);
    expect(roomViewOf(bounds, { fovDeg: 0, aspect: -1 })).toEqual(fallback);
    expect(roomViewOf(bounds, { fovDeg: Number.NaN, aspect: Number.POSITIVE_INFINITY })).toEqual(
      fallback,
    );
  });
});

describe('시점용 경계 상자(viewBoundsFrom)', () => {
  it('쓸 수 있는 스캔은 실측 z 높이를 그대로 유지한다', () => {
    const scan: RoomBounds = { min: [-0.1, 0, -0.02], max: [10, 10.1, 2.2] };
    expect(viewBoundsFrom(scan)).toBe(scan);
  });

  it('스캔이 없거나 xy 가 쓸 수 없으면 기본 교실 범위로 돌아간다', () => {
    expect(viewBoundsFrom(null)).toBe(DEFAULT_ROOM_LIMIT);
    expect(viewBoundsFrom({ min: [0, 0, 0], max: [0.2, 0.2, 3] })).toBe(DEFAULT_ROOM_LIMIT);
    expect(viewBoundsFrom({ min: [0, 0, 0], max: [Number.NaN, 5, 3] })).toBe(DEFAULT_ROOM_LIMIT);
  });

  it('z 가 이상치(50m 초과)면 xy 는 유지하고 높이만 기본 범위(0~5m)로 되돌린다', () => {
    expect(viewBoundsFrom({ min: [1, 2, -300], max: [9, 10, 300] })).toEqual({
      min: [1, 2, 0],
      max: [9, 10, 5],
    });
  });
});
