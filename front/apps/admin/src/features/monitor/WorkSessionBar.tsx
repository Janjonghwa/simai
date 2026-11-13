import { ec2Now } from '@/features/events/format';
import { useSharedNow } from '@/features/events/useSharedNow';
import { formatClock } from '@/features/history/works';
import { useMonitorWorks } from '@/features/monitor/useMonitorWorks';
import { useTodayWorks } from '@/features/monitor/useTodayWorks';
import { workElapsedLabel, workStartedAt, workZoneLabel } from '@/features/monitor/workSession';
import type { TbmWorkUnit } from '@/features/tbm/types';
import { workNameOf } from '@/features/work/workDay';
import { useRealtimeStore } from '@/store/realtime.store';
import { Button } from '@simai/ui/components/button';
import { useNavigate } from '@tanstack/react-router';

/** 작업 경과. 매 초 바뀌므로 이 칸만 다시 그린다(EC2 시계 기준, Since 와 같다). */
function Elapsed({ startedAt }: { startedAt: number }) {
  const now = useSharedNow();
  const offset = useRealtimeStore((s) => s.health?.clockOffsetMs);
  return <span className="tabular-nums">{workElapsedLabel(ec2Now(now, offset) - startedAt)}</span>;
}

function ActiveSession({ work }: { work: TbmWorkUnit }) {
  const navigate = useNavigate();
  const startedAt = workStartedAt(work);
  return (
    <section
      aria-label="작업 세션"
      className="pointer-events-auto flex max-w-[680px] items-center gap-3 rounded-[12px] bg-card/94 px-4 py-2 shadow-float ring-1 ring-border backdrop-blur-[8px] ring-inset"
    >
      <span aria-hidden className="size-2 shrink-0 rounded-full bg-ok" />
      <b className="min-w-0 truncate">{workNameOf(work.name)}</b>
      {startedAt !== null && (
        <span className="text-sm whitespace-nowrap text-muted-foreground">
          시작 {formatClock(startedAt)} · 경과 <Elapsed startedAt={startedAt} />
        </span>
      )}
      <span className="min-w-0 truncate text-sm text-muted-foreground">
        작업 구역 {workZoneLabel(work)}
      </span>
      <Button
        size="sm"
        variant="secondary"
        className="shrink-0"
        onClick={() => void navigate({ to: '/work', search: { workUnitId: work.work_id } })}
      >
        작업 관리로
      </Button>
    </section>
  );
}

/** 작업 시작 = 관제 시작(S15P31A101-679): 세팅을 마쳤지만 작업을 시작하지 않은 동안 Tier 0 는 경보를 내지 않는다. 짧게 알린다. */
export const IDLE_NOTICE = '작업 시작 전 · 경보 꺼짐';

function IdleNotice() {
  return (
    <section
      aria-label="관제 상태"
      className="pointer-events-auto flex items-center gap-2 rounded-full bg-card/94 px-3 py-1 text-sm text-muted-foreground shadow-float ring-1 ring-border backdrop-blur-[8px] ring-inset"
    >
      <span aria-hidden className="size-2 shrink-0 rounded-full bg-muted-foreground" />
      {IDLE_NOTICE}
    </section>
  );
}

/**
 * 실시간 관제 상단의 세션 바: 지금 진행 중(IN_PROGRESS) 작업의 이름·시작 시각·경과·작업 구역과 「작업 관리로」.
 * 진행 중인 작업이 없고 관제 화면이 열려 있으면(세팅을 마친 오늘 작업, 대기 화면 아님) 같은 자리(경보 배너 아래)에
 * 「작업 시작 전 · 경보 꺼짐」을 보인다(작업 시작 전에는 위치·3D·CCTV 는 보이지만 경보는 없다, -679).
 * 작업 목록을 아직 모르면 그리지 않는다(없음은 대기 화면이 보인다, MonitorStandby).
 */
export function WorkSessionBar() {
  const works = useMonitorWorks();
  const today = useTodayWorks({ passive: true });
  if (works.state !== 'ready') return null;
  if (works.active) return <ActiveSession work={works.active} />;
  return today.state === 'ready' && today.standby === null ? <IdleNotice /> : null;
}
