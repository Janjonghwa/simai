import type { WorkSurface } from '@/features/surfaces/types';
import type { Zone } from '@/features/zones/types';

// 구역↔작업면 연결은 서버가 작업면에 저장한 zone_id 가 기준이다(GET /api/static-objects). 화면은 그 값을 읽기만 하고 기하로 다시
// 계산하지 않는다. 작업면을 바꿀 때 중심이 구역 안인지 확인하는 pointInPolygon 만 남아 있다.

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

type LinkableSurface = Pick<WorkSurface, 'objectId' | 'zoneId'>;

/**
 * 구역마다 이은 작업면(하나). 서버가 저장한 작업면의 zone_id 를 읽는다. 지금 구역에 걸린 것만 잇는다(보관한 구역에 걸린 것은
 * 뺀다). 한 구역에 작업면이 둘 이상이면 작업면 ID 순 첫 번째만 잇는다(서버 V22 가 기존 작업면을 채운 규칙과 같다).
 */
export function linkSurfaces<S extends LinkableSurface>(
  zones: readonly Pick<Zone, 'zoneId'>[],
  surfaces: readonly S[],
): Map<string, S> {
  const current = new Set(zones.map((zone) => zone.zoneId));
  const links = new Map<string, S>();
  const byId = [...surfaces].sort((a, b) => a.objectId.localeCompare(b.objectId));
  for (const surface of byId) {
    const { zoneId } = surface;
    if (zoneId !== null && current.has(zoneId) && !links.has(zoneId)) links.set(zoneId, surface);
  }
  return links;
}
