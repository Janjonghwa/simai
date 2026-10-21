import { Viewer3D } from '@/features/viewer3d';
import { useRealtimeStore } from '@/store/realtime.store';

// 3D 장면이 화면을 채운다. 상단 바와 메뉴는 그 위에 뜬다.
// 데이터는 앱의 STOMP 연결이 세션마다 만든 Source 로 받는다(lib/stomp.ts). 로그아웃하면 Source 가 바뀌어 장면 데이터가 비워진다.
export function MonitorPage() {
  const source = useRealtimeStore((s) => s.source);
  return (
    <section aria-label="실시간 관제" className="relative flex-1 bg-background">
      <h1 className="sr-only">실시간 관제</h1>
      <Viewer3D source={source} className="absolute inset-0" />
    </section>
  );
}
