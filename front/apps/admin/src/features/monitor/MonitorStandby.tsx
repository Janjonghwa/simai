import { useMonitorWorks } from '@/features/monitor/useMonitorWorks';
import { Button } from '@simai/ui/components/button';
import { useNavigate } from '@tanstack/react-router';

function Standby() {
  const navigate = useNavigate();
  return (
    <section
      aria-label="관제 대기"
      // 경보는 이 화면 위에 그대로 보인다: 경보 배너는 장면 밖(AppLayout 알림)이고, 이벤트 패널은 이 화면보다 뒤에 그려져 위에 뜬다.
      className="pointer-events-auto absolute inset-0 z-[5] flex items-center justify-center bg-background/95 pr-[370px]"
    >
      <div className="flex max-w-[420px] flex-col items-center gap-3 text-center">
        <h2 className="text-xl font-bold">진행 중인 작업이 없습니다</h2>
        <p className="text-muted-foreground">작업을 시작하면 관제가 시작됩니다.</p>
        <Button onClick={() => void navigate({ to: '/work' })}>작업 관리로</Button>
      </div>
    </section>
  );
}

/**
 * 진행 중인 작업이 없을 때 관제 화면 대신 보이는 대기 화면(S15P31A101-457).
 * 작업 목록을 받는 중이거나 받지 못했으면 "없음"을 알 수 없으므로 그리지 않는다.
 */
export function MonitorStandby() {
  const works = useMonitorWorks();
  if (works.state !== 'ready' || works.active) return null;
  return <Standby />;
}
