import { MonitorStandby } from '@/features/monitor/MonitorStandby';
import { useZoneEditActive } from '@/features/monitor/useZoneEditActive';
import { ZonesPage } from '@/features/zones/ZonesPage';
import { stopZoneEdit } from '@/store/zoneEditMode.store';
import { useEffect } from 'react';

/**
 * 실시간 관제. 관리자 편집 모드가 켜져 있으면 장면 위에 구역 편집을 얹는다(켜고 끄는 버튼은 도구 모음에 있다).
 * 이 화면을 떠나면 편집 모드만 끄고 고치던 초안은 남긴다. 편집 중이 아니면 진행 중 작업이 없을 때 대기 화면을 띄운다
 * (구역 편집 — 세팅·구역 발행은 작업 시작 전에 한다 — 은 막지 않는다, S15P31A101-457).
 */
export function MonitorPage() {
  const editing = useZoneEditActive();
  useEffect(() => stopZoneEdit, []);
  return (
    <>
      <h1 className="sr-only">실시간 관제</h1>
      {editing && <ZonesPage />}
      {!editing && <MonitorStandby />}
    </>
  );
}
