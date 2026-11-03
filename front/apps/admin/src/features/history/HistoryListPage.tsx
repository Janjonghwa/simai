import type { WorkUnit } from '@/features/history/types';
import { useWorks } from '@/features/history/useWorkReplay';
import { formatDate, groupByDate, timeRange, worksLoadingLine } from '@/features/history/works';
import { PageTitle } from '@/features/layout/PageTitle';
import { cn } from '@simai/ui/lib/utils';
import { useNavigate } from '@tanstack/react-router';
import { ChevronRight } from 'lucide-react';
import { useMemo } from 'react';

// 작업 한 줄의 칸: 시간대 · 이름 · 장소 · 계획 인원 · 이벤트 수 · 화살표.
const WORK_ROW =
  'grid w-full cursor-pointer grid-cols-[120px_minmax(0,1.4fr)_minmax(0,1fr)_96px_minmax(0,1fr)_20px] items-center gap-4 rounded-[12px] bg-card px-4 py-3 text-left ring-1 ring-border ring-inset hover:bg-surface-2';

function EventCounts({ counts }: { counts: WorkUnit['event_counts'] }) {
  if (!counts) return <span className="text-muted-foreground">—</span>;
  const critical = counts.CRITICAL ?? 0;
  const high = counts.HIGH ?? 0;
  if (critical === 0 && high === 0)
    return <span className="text-muted-foreground">위험·경고 없음</span>;
  return (
    <span className="flex gap-3 font-mono text-sm">
      <span
        className={cn(critical > 0 ? 'font-semibold text-sev-critical' : 'text-muted-foreground')}
      >
        위험 {critical}
      </span>
      <span className={cn(high > 0 ? 'text-sev-critical' : 'text-muted-foreground')}>
        경고 {high}
      </span>
    </span>
  );
}

function WorkItem({ work }: { work: WorkUnit }) {
  const navigate = useNavigate();
  return (
    <li>
      <button
        type="button"
        onClick={() => void navigate({ to: '/history/$workId', params: { workId: work.work_id } })}
        className={WORK_ROW}
      >
        <span className="font-mono">{timeRange(work.from_ms, work.to_ms)}</span>
        <span className="truncate font-semibold">{work.name}</span>
        <span className="truncate text-muted-foreground">{work.place ?? '—'}</span>
        <span>계획 {work.planned_people}명</span>
        <EventCounts counts={work.event_counts} />
        <ChevronRight aria-hidden className="size-4 text-muted-foreground" />
      </button>
    </li>
  );
}

function WorkGroups({ works }: { works: readonly WorkUnit[] }) {
  const groups = useMemo(() => groupByDate(works), [works]);
  if (groups.length === 0) return <p className="text-muted-foreground">기록된 작업이 없습니다.</p>;
  return (
    <>
      {groups.map((group) => (
        <section
          key={group.date}
          aria-label={formatDate(group.date)}
          className="flex flex-col gap-2"
        >
          <h2 className="text-[15px] font-bold">{formatDate(group.date)}</h2>
          <ul className="grid gap-2">
            {group.works.map((work) => (
              <WorkItem key={work.work_id} work={work} />
            ))}
          </ul>
        </section>
      ))}
    </>
  );
}

/** 히스토리 목록: 시작한 최근 TBM 작업을 날짜별로 보인다. 누르면 그 작업 구간을 다시 본다. */
export function HistoryListPage() {
  const { works, failed } = useWorks();

  return (
    <div className="flex flex-none flex-col gap-4">
      <PageTitle>히스토리</PageTitle>
      {works === undefined ? (
        <p className="text-muted-foreground">{worksLoadingLine(failed)}</p>
      ) : (
        <WorkGroups works={works} />
      )}
    </div>
  );
}
