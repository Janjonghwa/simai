import {
  zoneItemSchema,
  type ViewerZones,
  type Zone,
  type ZoneBox,
  type ZoneItem,
  type ZonesResponse,
} from '@/features/zones/types';

/** 높이(z_min_m·z_max_m)가 없는 바닥 구역을 3D·2D 에 그릴 때의 두께(m). 형상 편집은 막는다. */
const FOOTPRINT_SLAB_M = 0.05;

type Polygon = readonly (readonly [number, number])[];

/**
 * 바닥 다각형이 축에 맞춘 직사각형(네 꼭짓점이 x 두 값·y 두 값의 조합)인지. 끝점이 첫 점과 같은 닫힌 고리(5점)도
 * 받는다(GeoJSON·PostGIS 방식).
 */
function isAxisRect(polygon: Polygon): boolean {
  const [first, last] = [polygon[0], polygon[polygon.length - 1]];
  const closed =
    polygon.length === 5 &&
    first !== undefined &&
    last !== undefined &&
    first[0] === last[0] &&
    first[1] === last[1];
  if (closed) return isAxisRect(polygon.slice(0, 4));
  if (polygon.length !== 4) return false;
  const xs = new Set(polygon.map(([x]) => x));
  const ys = new Set(polygon.map(([, y]) => y));
  const corners = new Set(polygon.map(([x, y]) => `${x},${y}`));
  return xs.size === 2 && ys.size === 2 && corners.size === 4;
}

function toZone(item: ZoneItem): Zone | null {
  const xs = item.polygon_xy.map(([x]) => x);
  const ys = item.polygon_xy.map(([, y]) => y);
  // 높이는 둘 다 있을 때만 쓴다(서버도 VOLUME_3D 는 둘 다 받는다).
  const zRange =
    item.z_min_m !== null && item.z_max_m !== null ? ([item.z_min_m, item.z_max_m] as const) : null;
  const min = [Math.min(...xs), Math.min(...ys), zRange?.[0] ?? 0] as const;
  const max = [Math.max(...xs), Math.max(...ys), zRange?.[1] ?? FOOTPRINT_SLAB_M] as const;
  if (!max.every((value, axis) => value > min[axis]!)) return null;
  return {
    zoneId: item.zone_id,
    name: item.name,
    min,
    max,
    version: item.zone_version,
    judgeType: item.eval_type,
    zoneType: item.zone_kind,
    severity: item.severity,
    polygon: item.polygon_xy,
    zBounds: [item.z_min_m, item.z_max_m],
    zRange,
    hasGuardrail: item.has_guardrail ?? null,
    requirements: item.requirements ?? null,
    validFrom: item.valid_from,
    shapeEditable: zRange !== null && isAxisRect(item.polygon_xy),
  };
}

/**
 * 응답에서 그릴 수 있는 구역만 고른다. 형식이 틀렸거나 범위가 뒤집혔거나 0 인 구역은 넓히거나 줄여 맞추지 않고 뺀다.
 * 같은 zone_id 가 둘 이상이면 어느 쪽이 맞는지 알 수 없어 그 ID 의 구역을 모두 뺀다(3D 뷰어와 같은 규칙).
 */
export function readZones(response: ZonesResponse): Zone[] {
  // 형식이 틀린 항목도 ID 는 센다. 같은 ID 의 틀린 항목이 있으면 맞는 항목도 어느 쪽이 맞는지 알 수 없다.
  const count = new Map<string, number>();
  for (const item of response) {
    const id = typeof item === 'object' && item !== null ? Reflect.get(item, 'zone_id') : null;
    if (typeof id === 'string') count.set(id, (count.get(id) ?? 0) + 1);
  }
  return response.flatMap((item) => {
    const parsed = zoneItemSchema.safeParse(item);
    if (!parsed.success || count.get(parsed.data.zone_id) !== 1) return [];
    const zone = toZone(parsed.data);
    return zone ? [zone] : [];
  });
}

/** 상자의 바닥을 직사각형 다각형(반시계, World x·y)으로 바꾼다. 저장할 때 보낸다. */
export function boxToPolygon(box: Pick<ZoneBox, 'min' | 'max'>): [number, number][] {
  const [x0, y0] = box.min;
  const [x1, y1] = box.max;
  return [
    [x0, y0],
    [x1, y0],
    [x1, y1],
    [x0, y1],
  ];
}

/** 3D 뷰어 Source 에 넘길 상자 형식. 뷰어는 서버 형식이 아니라 이 형식을 검사한다. */
export function toViewerZones(zones: readonly ZoneBox[]): ViewerZones {
  return {
    contract: 'DEMO_UNCONFIRMED',
    zones: zones.map((zone) => ({
      zone_id: zone.zoneId,
      name: zone.name,
      aabb: { min_xyz: zone.min, max_xyz: zone.max },
    })),
  };
}
