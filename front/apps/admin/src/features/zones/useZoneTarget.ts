import type { SpatialViewportValue } from '@/features/layout/SpatialViewportHost';
import type { Zone } from '@/features/zones/types';
import { NEW_KEY } from '@/features/zones/zoneEditModel';
import { useEffect, useRef, useState } from 'react';

export interface ZoneTarget {
  /** 고른 구역. 새 구역 양식이면 null. */
  zone: Zone | null;
  /** 고른 대상의 초안 키(구역 ID 나 NEW_KEY). */
  key: string;
  /** 3D 손잡이가 작업면을 고치는 중인지(대상을 바꾸면 구역으로 돌아간다). */
  surfMode: boolean;
  setSurfaceMode: (on: boolean) => void;
  openZone: (zoneId: string) => void;
  openNew: () => void;
  /** 드롭다운에서 고른 값. */
  pick: (value: string) => void;
}

const findZone = (zones: readonly Zone[], id: string): Zone | null =>
  zones.find((z) => z.zoneId === id) ?? null;

/**
 * 고른 대상: 구역 ID 나 새 구역(NEW_KEY). 3D 에서 구역을 누르면 호스트의 선택이 바뀌어 따라간다. 빈 곳을 눌러 선택이
 * 풀려도 대상은 그대로다(드롭다운과 3D 구역 선택만 대상을 바꾼다). ?zone=z-xx 로 들어오면 구역 데이터가 도착한 뒤
 * 그 구역을 고르고 초점을 맞춘다.
 */
export function useZoneTarget(
  viewport: SpatialViewportValue,
  {
    initialZoneId,
    onSearchConsumed,
  }: { initialZoneId: string | null; onSearchConsumed?: () => void },
): ZoneTarget {
  const { zones, selectedZoneId, selectTarget, clearSelection } = viewport;
  const [target, setTarget] = useState<string>(selectedZoneId ?? NEW_KEY);
  const [seenSelected, setSeenSelected] = useState(selectedZoneId);
  if (seenSelected !== selectedZoneId) {
    setSeenSelected(selectedZoneId);
    if (selectedZoneId) setTarget(selectedZoneId);
  }
  const zone = findZone(zones, target);
  const key = zone ? zone.zoneId : NEW_KEY;
  const [surfModeKey, setSurfModeKey] = useState<string | null>(null);

  const openZone = (zoneId: string) => {
    setTarget(zoneId);
    selectTarget({ type: 'zone', id: zoneId }, { focus: true });
  };
  const openNew = () => {
    setTarget(NEW_KEY);
    clearSelection();
  };
  const pick = (value: string) => {
    if (value === NEW_KEY || !findZone(zones, value)) openNew();
    else openZone(value);
  };

  const consumed = useRef<string | null>(null);
  useEffect(() => {
    if (!initialZoneId || consumed.current === initialZoneId) return;
    if (zones.length > 0 && !findZone(zones, initialZoneId)) return;
    consumed.current = initialZoneId;
    selectTarget({ type: 'zone', id: initialZoneId }, { focus: selectedZoneId !== initialZoneId });
    onSearchConsumed?.();
  }, [initialZoneId, zones, selectedZoneId, selectTarget, onSearchConsumed]);

  return {
    zone,
    key,
    surfMode: surfModeKey === key,
    setSurfaceMode: (on) => setSurfModeKey(on ? key : null),
    openZone,
    openNew,
    pick,
  };
}
