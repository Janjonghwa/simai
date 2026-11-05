import type { WatchBinding } from '@/features/watch/api';
import type { ViewerHealthPayload } from '@simai/contracts';

export type HealthWatches = NonNullable<ViewerHealthPayload['watches']>;

/** 워치 한 대의 화면 줄. 연결은 health(1초마다)가 정본이고, 배정은 REST 로 받는다. */
export interface WatchRow {
  deviceId: string;
  connected: boolean;
  batteryPct: number | null;
  worldId: string | null;
  method: string | null;
}

/**
 * 등록된 워치 목록(health)과 배정(REST)을 한 줄씩 합친다. health 에 없는 배정 워치(health 를 아직 못 받음 등)는
 * 배정 응답의 채널 상태로 보인다. 기기 ID 순.
 */
export function watchRows(
  watches: HealthWatches | undefined,
  bindings: readonly WatchBinding[],
): WatchRow[] {
  const byDevice = new Map(bindings.map((b) => [b.device_id, b] as const));
  const rows = new Map<string, WatchRow>();
  for (const item of watches?.items ?? []) {
    const b = byDevice.get(item.device_id);
    rows.set(item.device_id, {
      deviceId: item.device_id,
      connected: item.link === 'CONNECTED',
      batteryPct: item.battery_pct,
      worldId: b?.world_id ?? null,
      method: b?.method ?? null,
    });
  }
  for (const b of bindings) {
    if (!rows.has(b.device_id)) {
      rows.set(b.device_id, {
        deviceId: b.device_id,
        connected: b.channel_state === 'BOUND',
        batteryPct: null,
        worldId: b.world_id,
        method: b.method,
      });
    }
  }
  return [...rows.values()].sort((a, b) => a.deviceId.localeCompare(b.deviceId));
}

/**
 * 아바타 이름표 뒤에 붙일 글(World ID → 글). 배정된 사람만 싣는다: 연결이면 '워치 기기ID', 끊김이면 '워치 끊김 기기ID'.
 * 배정 없는 사람(UNBOUND)은 싣지 않는다. 착용 여부는 알 수 없으므로 '미착용'으로 단정하지 않는다(FR-117).
 */
export function watchBadges(rows: readonly WatchRow[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const row of rows) {
    if (row.worldId)
      out[row.worldId] = row.connected ? `워치 ${row.deviceId}` : `워치 끊김 ${row.deviceId}`;
  }
  return out;
}

/**
 * 이름표에 쓰는 연결만 뽑은 문자열('기기=CONNECTED|기기=LOST', 기기 순). health 는 1초마다 새 객체라 이 원시값을 구독해야
 * 연결이 실제로 바뀔 때만 다시 그린다(배터리·시각 변화로 3D 호스트를 다시 그리지 않게).
 */
export function linkKeyOf(watches: HealthWatches | undefined): string {
  return (watches?.items ?? [])
    .map((i) => `${i.device_id}=${i.link}`)
    .sort()
    .join('|');
}

/** linkKeyOf 를 되돌린 최소 health(배터리·시각 없음). 빈 문자열이면 health 없음 */
export function watchesOfLinkKey(key: string): HealthWatches | undefined {
  if (!key) return undefined;
  const items = key.split('|').map((part) => {
    const at = part.lastIndexOf('=');
    const link: 'CONNECTED' | 'LOST' = part.slice(at + 1) === 'CONNECTED' ? 'CONNECTED' : 'LOST';
    return {
      device_id: part.slice(0, at),
      link,
      last_heartbeat_at: 0,
      battery_pct: null,
      transport: null,
    };
  });
  return {
    registered: items.length,
    connected: items.filter((i) => i.link === 'CONNECTED').length,
    items,
  } as HealthWatches;
}

/** 채널 상태(부록 C.6): 배정·연결 BOUND, 배정·끊김 LOST, 배정 없음 UNBOUND */
export function channelOf(row: WatchRow | undefined): 'BOUND' | 'LOST' | 'UNBOUND' {
  if (!row?.worldId) return 'UNBOUND';
  return row.connected ? 'BOUND' : 'LOST';
}
