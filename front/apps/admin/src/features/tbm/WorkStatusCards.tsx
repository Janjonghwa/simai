import { SeverityBadge } from '@/features/events/SeverityBadge';
import { useNow } from '@/features/layout/useNow';
import type { TbmTask } from '@/features/tbm/tbmDemo';
import { Button } from '@simai/ui/components/button';
import { Card } from '@simai/ui/components/card';

// 작업 상태별 본문 머리. 진행 중 = 시작 시각 · 경과 · 사건 수 + 실시간 관제로 가기 · 작업 종료, 종료 후 = 작업 중 사건 집계 띠
// (사건 수 · 작업 마무리 문단 · 작업 시각).
// 사건 수는 서버 work_unit.event_counts(위험 · 경고) 그대로다.

/** 경과 시간을 다시 그리는 간격(ms). */
const ELAPSED_TICK_MS = 30_000;

const timeOf = (value: TbmTask['actualStartedAt']) => (value == null ? null : new Date(value));

const hhmm = (d: Date) =>
  `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;

const elapsedText = (ms: number) => {
  const min = Math.max(0, Math.floor(ms / 60_000));
  return min >= 60 ? `${Math.floor(min / 60)}시간 ${min % 60}분` : `${min}분`;
};

function EventCounts({ task }: { task: TbmTask }) {
  const counts = task.eventCounts;
  const shown = (['CRITICAL', 'HIGH'] as const).filter((k) => (counts?.[k] ?? 0) > 0);
  if (!counts) return <span className="text-muted-foreground">확인 불가</span>;
  if (!shown.length) return <span className="text-muted-foreground">없음</span>;
  return (
    <span className="inline-flex flex-wrap items-center gap-2.5">
      {shown.map((k) => (
        <span key={k} className="inline-flex items-center gap-1">
          <SeverityBadge severity={k} />
          <b className="font-mono">{counts[k]}</b>
        </span>
      ))}
    </span>
  );
}

export function WorkRunningCard({
  task,
  pending,
  showTbm,
  onMonitor,
  onEnd,
  onToggleTbm,
}: {
  task: TbmTask;
  pending: boolean;
  /** 접어 둔 TBM 3블록을 펼쳤는지. */
  showTbm: boolean;
  onMonitor: () => void;
  onEnd: () => void;
  onToggleTbm: () => void;
}) {
  const now = useNow(ELAPSED_TICK_MS);
  const started = timeOf(task.actualStartedAt);
  return (
    <Card
      role="region"
      aria-label={`${task.id} 진행 중`}
      className="flex flex-none flex-row flex-wrap items-center gap-x-8 gap-y-3 px-6 py-5"
    >
      <div className="flex min-w-0 flex-col gap-1">
        <span className="inline-flex items-center gap-2 font-bold text-ok">
          <span aria-hidden className="size-2.5 animate-pulse rounded-full bg-ok" />
          진행 중
        </span>
        <b className="text-[22px] leading-[30px]">
          {task.id} · {task.name}
        </b>
        {started && (
          <span className="font-mono text-[13px] text-muted-foreground">
            {hhmm(started)} 시작 · {elapsedText(now - started.getTime())} 지남
          </span>
        )}
      </div>
      <div className="flex items-center gap-2.5 text-sm">
        <span className="text-muted-foreground">사건</span>
        <EventCounts task={task} />
      </div>
      <div className="ml-auto flex items-center gap-2">
        <Button onClick={onMonitor} className="h-11 px-5 font-bold">
          실시간 관제로 가기
        </Button>
        <Button
          variant="secondary"
          disabled={pending}
          onClick={onEnd}
          className="h-11 bg-surface-3 px-5"
        >
          작업 종료
        </Button>
        <Button
          variant="secondary"
          size="sm"
          aria-expanded={showTbm}
          onClick={onToggleTbm}
          className="h-[30px] bg-surface-2 px-3 text-[13px] text-muted-foreground"
        >
          {showTbm ? 'TBM 접기' : 'TBM 보기'}
        </Button>
      </div>
    </Card>
  );
}

export function WrapUpBand({
  task,
  summary,
}: {
  task: TbmTask;
  /** 작업 마무리 문단(또는 만드는 중 안내). 없으면 사건 수만 보인다. */
  summary: string | null;
}) {
  const started = timeOf(task.actualStartedAt);
  const ended = timeOf(task.actualEndedAt);
  return (
    <div
      role="status"
      aria-label="작업 중 사건"
      className="flex flex-none items-center gap-x-3.5 rounded-xl bg-card px-4 py-2.5 text-sm ring-1 ring-border ring-inset"
    >
      <b className="flex-none">작업 중 사건</b>
      <span className="flex-none">
        <EventCounts task={task} />
      </span>
      {summary && <span className="min-w-0 flex-1 text-muted-foreground">{summary}</span>}
      {started && ended && (
        <span className="ml-auto flex-none font-mono text-[13px] text-muted-foreground">
          {hhmm(started)}–{hhmm(ended)}
        </span>
      )}
    </div>
  );
}
