import {
  selectActiveEvents,
  type SpatialDataStore,
} from '@/features/viewer3d/data/spatialData.store';
import { LEGACY_ROSTER, type Roster } from '@/features/viewer3d/dev-harness/mockUi';
import { MODE_EVENT_SOURCE } from '@/features/viewer3d/risk/riskPresentation';
import type { ViewerMode } from '@/features/viewer3d/types';
import { zoneHeightText } from '@/features/viewer3d/zones/zonePresentation';
import { useMemo } from 'react';
import { useStore } from 'zustand';

/**
 * 업무 패널 목록. 입력이 있으면 Provider 의 World ID·활성 이벤트를 쓰고, 입력 없음이면 예전 고정 목록이다.
 * 이벤트는 3D 강조와 같은 출처(뷰어 모드)·같은 Store 상태에서 고르고, 등급·전이·순번을 계약 그대로 보인다.
 * 사람 목록이 바뀔 때만 다시 계산한다(10 Hz 좌표 갱신에는 반응하지 않는다).
 */
export function useRoster(
  store: SpatialDataStore,
  legacy: boolean,
  mode: ViewerMode = 'LIVE',
): Roster {
  const idsKey = useStore(store, (s) => (s.world?.tracks ?? []).map((t) => t.worldId).join('\n'));
  const events = useStore(store, (s) => s.events);
  const zones = useStore(store, (s) => s.zones);
  return useMemo(() => {
    if (legacy) return LEGACY_ROSTER;
    const people = idsKey ? idsKey.split('\n').map((id) => ({ id, name: `작업자 ${id}` })) : [];
    const active = selectActiveEvents(
      { events } as Parameters<typeof selectActiveEvents>[0],
      MODE_EVENT_SOURCE[mode],
    ).filter((event) => event.delivery !== 'AUDIT_ONLY');
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
        // 목록 행은 event_id 로 구분하고, 누르면 기존 선택 브리지(person·zone)로 대상만 고른다.
        const person = event.worldIds[0];
        const target = person
          ? ({ type: 'person', id: person } as const)
          : event.zoneId
            ? ({ type: 'zone', id: event.zoneId } as const)
            : null;
        if (!target) return [];
        return [
          {
            id: event.eventId,
            severity: event.severity,
            title: `${event.ruleId} · ${event.zoneId ?? '구역 없음'} · ${event.lifecycle} #${event.eventSequence ?? '—'}`,
            target,
          },
        ];
      }),
    };
  }, [idsKey, events, zones, legacy, mode]);
}
