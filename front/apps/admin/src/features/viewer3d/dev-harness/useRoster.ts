import {
  selectActiveEvents,
  type SpatialDataStore,
} from '@/features/viewer3d/data/spatialData.store';
import { LEGACY_ROSTER, type Roster } from '@/features/viewer3d/dev-harness/mockUi';
import { zoneHeightText } from '@/features/viewer3d/zones/zonePresentation';
import { useMemo } from 'react';
import { useStore } from 'zustand';

/**
 * 업무 패널 목록. 입력이 있으면 Provider 의 World ID·LIVE 활성 이벤트를 쓰고, 입력 없음이면 예전 고정 목록이다.
 * 사람 목록이 바뀔 때만 다시 계산한다(10 Hz 좌표 갱신에는 반응하지 않는다).
 */
export function useRoster(store: SpatialDataStore, legacy: boolean): Roster {
  const idsKey = useStore(store, (s) => (s.world?.tracks ?? []).map((t) => t.worldId).join('\n'));
  const events = useStore(store, (s) => s.events);
  const zones = useStore(store, (s) => s.zones);
  return useMemo(() => {
    if (legacy) return LEGACY_ROSTER;
    const people = idsKey ? idsKey.split('\n').map((id) => ({ id, name: `작업자 ${id}` })) : [];
    const active = selectActiveEvents(
      { events } as Parameters<typeof selectActiveEvents>[0],
      'LIVE',
    );
    return {
      people,
      zones: zones.zones.map((zone) => ({
        id: zone.zoneId,
        name: zone.name,
        height: zoneHeightText(zone),
        legacy: false,
      })),
      zoneNote: [`구역 입력 ${zones.status}`, zones.reason, 'DEMO 형상(계약 미확정)']
        .filter(Boolean)
        .join(' · '),
      rejectedZones: zones.invalid.map(({ zoneId, reason }) => ({ id: zoneId, reason })),
      events: active.flatMap((event) => {
        const target = event.worldIds[0];
        if (!target) return [];
        return [
          {
            id: event.eventId,
            severity:
              event.severity === 'CRITICAL' || event.severity === 'HIGH' ? 'HIGH' : 'CAUTION',
            title: `${event.ruleId} · ${event.zoneId ?? '구역 없음'} · ${event.lifecycle}`,
            target: { type: 'person', id: target },
          } as const,
        ];
      }),
    };
  }, [idsKey, events, zones, legacy]);
}
