import { EventPanel } from '@/features/events/EventPanel';
import type { Incident } from '@/features/events/events';
import { useEventsStore } from '@/features/events/events.store';
import { useLiveEvents } from '@/features/events/useLiveEvents';
import { Viewer3D, type Viewer3DHandle, type ViewerTarget } from '@/features/viewer3d';
import { useRealtimeStore } from '@/store/realtime.store';
import { useRef, useState } from 'react';

// 3D 장면이 화면을 채우고, 상단 바·메뉴·이벤트 패널이 그 위에 뜬다. 경보 배너는 모든 화면에 뜨도록 레이아웃(AppLayout)이 그린다.
// 데이터는 앱의 STOMP 연결이 세션마다 만든 Source 로 받는다(lib/stomp.ts). 로그아웃하면 Source 가 바뀌어 장면 데이터가 비워진다.
// 경보 대상 강조는 뷰어가 같은 Source 의 risk_event 로 한다(S15P31A101-185). 여기서는 목록과 3D 선택만 잇는다.
export function MonitorPage() {
  const source = useRealtimeStore((s) => s.source);
  const viewer = useRef<Viewer3DHandle>(null);
  const { entries, loaded, failed } = useLiveEvents();
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const openEventId = useEventsStore((s) => s.openEventId);
  const openEvent = useEventsStore((s) => s.openEvent);

  // 카드를 고르면 3D 에서 그 사람(또는 구역)을 고르고 비춘다. 장면에 없으면 뷰어가 실패만 돌려준다.
  const selectIncident = (incident: Incident) => {
    setSelectedKey(incident.key);
    if (!incident.target) return;
    viewer.current?.select(incident.target);
    viewer.current?.focus(incident.target);
  };

  // 3D 에서 사람·구역을 고르면 그 묶음을 목록에서 표시한다.
  const onViewerSelect = (target: ViewerTarget | null) =>
    setSelectedKey(
      target && (target.type === 'person' || target.type === 'zone')
        ? `${target.type}:${target.id}`
        : null,
    );

  return (
    <main aria-label="실시간 관제" className="relative flex-1 bg-background">
      <h1 className="sr-only">실시간 관제</h1>
      <Viewer3D
        ref={viewer}
        source={source}
        onSelectionChange={onViewerSelect}
        className="absolute inset-0"
      />
      <EventPanel
        entries={entries}
        loaded={loaded}
        failed={failed}
        selectedKey={selectedKey}
        onSelect={selectIncident}
        openEventId={openEventId}
        onOpen={openEvent}
      />
    </main>
  );
}
