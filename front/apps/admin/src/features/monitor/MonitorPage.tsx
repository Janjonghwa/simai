import { useZoneEditActive } from '@/features/monitor/useZoneEditActive';
import { ZonesPage } from '@/features/zones/ZonesPage';
import { stopZoneEdit } from '@/store/zoneEditMode.store';
import { useEffect } from 'react';

/**
 * 실시간 관제. 관리자 편집 모드가 켜져 있으면 장면 위에 구역 편집을 얹는다(켜고 끄는 버튼은 도구 모음에 있다).
 * 이 화면을 떠나면 편집 모드만 끄고 고치던 초안은 남긴다.
 */
export function MonitorPage() {
  const editing = useZoneEditActive();
  useEffect(() => stopZoneEdit, []);
  return (
    <>
      <h1 className="sr-only">실시간 관제</h1>
      {editing && <ZonesPage />}
    </>
  );
}
