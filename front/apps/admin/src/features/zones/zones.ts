import { zoneItemSchema, type ZoneBox, type ZonesResponse } from '@/features/zones/types';

/**
 * 응답에서 그릴 수 있는 구역만 고른다. 형식이 틀렸거나 범위가 뒤집혔거나 0 인 구역은 넓히거나 줄여 맞추지 않고 뺀다.
 * 같은 zone_id 가 둘 이상이면 어느 쪽이 맞는지 알 수 없어 그 ID 의 구역을 모두 뺀다(3D 뷰어와 같은 규칙).
 */
export function readZones(response: ZonesResponse): ZoneBox[] {
  // 형식이 틀린 항목도 ID 는 센다. 같은 ID 의 틀린 항목이 있으면 맞는 항목도 어느 쪽이 맞는지 알 수 없다.
  const count = new Map<string, number>();
  for (const item of response.zones) {
    const id = typeof item === 'object' && item !== null ? Reflect.get(item, 'zone_id') : null;
    if (typeof id === 'string') count.set(id, (count.get(id) ?? 0) + 1);
  }
  const parsed = response.zones.flatMap((item) => {
    const zone = zoneItemSchema.safeParse(item);
    return zone.success ? [zone.data] : [];
  });
  return parsed
    .filter((zone) => count.get(zone.zone_id) === 1)
    .filter(({ aabb }) => aabb.max_xyz.every((max, axis) => max > aabb.min_xyz[axis]!))
    .map((zone) => ({
      zoneId: zone.zone_id,
      name: zone.name,
      min: zone.aabb.min_xyz,
      max: zone.aabb.max_xyz,
    }));
}
