import type { WorkSurface } from '@/features/surfaces/types';
import type { Zone } from '@/features/zones/types';

// 서버 구역에는 작업면을 잇는 칸이 없다. 작업면은 OBB 중심이 구역 바닥 다각형(XY) 안에 있으면 그 구역의 것으로 본다.
// 서버 목록(GET /api/zones · GET /api/static-objects)만으로 화면이 계산한다.

type Polygon = readonly (readonly [number, number])[];

/** 점이 선분 위에 있다고 보는 오차. */
const BOUNDARY_EPS = 1e-9;

/** 점이 다각형 안(경계 포함)에 있는지. 짝홀 규칙에 경계 위 점을 따로 센다. */
export function pointInPolygon(point: readonly [number, number], polygon: Polygon): boolean {
  const [px, py] = point;
  let inside = false;
  for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i, i += 1) {
    const [xi, yi] = polygon[i]!;
    const [xj, yj] = polygon[j]!;
    // 경계 위(선분과 점의 거리가 0)면 안으로 본다.
    const cross = (px - xi) * (yj - yi) - (py - yi) * (xj - xi);
    if (
      Math.abs(cross) <= BOUNDARY_EPS &&
      px >= Math.min(xi, xj) - BOUNDARY_EPS &&
      px <= Math.max(xi, xj) + BOUNDARY_EPS &&
      py >= Math.min(yi, yj) - BOUNDARY_EPS &&
      py <= Math.max(yi, yj) + BOUNDARY_EPS
    )
      return true;
    if (yi > py !== yj > py && px < ((xj - xi) * (py - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

/** 다각형 넓이(신발끈 공식). */
function polygonAreaOf(polygon: Polygon): number {
  let sum = 0;
  polygon.forEach(([x0, y0], i) => {
    const [x1, y1] = polygon[(i + 1) % polygon.length]!;
    sum += x0 * y1 - x1 * y0;
  });
  return Math.abs(sum) / 2;
}

type LinkableSurface = Pick<WorkSurface, 'objectId' | 'geometry'>;

/**
 * 구역마다 이은 작업면(하나). 작업면은 중심이 들어 있는 구역의 것이다. 여러 구역이 겹쳐 둘 이상에 들면 바닥 넓이가 가장
 * 작은 구역(같으면 구역 ID 순)이 가진다. 한 구역에 작업면이 둘 이상 들면 작업면 ID 순으로 첫 번째만 잇는다(구역에는 하나).
 */
export function linkSurfaces<S extends LinkableSurface>(
  zones: readonly Pick<Zone, 'zoneId' | 'polygon'>[],
  surfaces: readonly S[],
): Map<string, S> {
  // 넓이는 구역마다 한 번만 센다(비교 함수 안에서 세면 비교마다 다시 센다).
  const order = zones
    .map((zone) => ({ zone, area: polygonAreaOf(zone.polygon) }))
    .sort((a, b) => a.area - b.area || a.zone.zoneId.localeCompare(b.zone.zoneId))
    .map(({ zone }) => zone);
  const links = new Map<string, S>();
  const byId = [...surfaces].sort((a, b) => a.objectId.localeCompare(b.objectId));
  for (const surface of byId) {
    const owner = order.find((zone) => pointInPolygon(surface.geometry.centerXy, zone.polygon));
    if (owner && !links.has(owner.zoneId)) links.set(owner.zoneId, surface);
  }
  return links;
}
