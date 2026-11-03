import { DashPanel } from '@/features/dashboard/DashPanel';
import { formatTime, ruleName } from '@/features/events/events';
import { SeverityBadge } from '@/features/events/SeverityBadge';
import { isActiveAt, type EventRow, type WorkerRow } from '@/features/history/replayIndex';
import { formatClock } from '@/features/history/works';
import { cn } from '@simai/ui/lib/utils';
import { ListOrdered, Users } from 'lucide-react';

const LIST = 'flex min-h-0 flex-col gap-1 overflow-auto px-2.5 py-2';
const ROW =
  'flex w-full cursor-pointer items-center rounded-lg px-2.5 py-1.5 text-left hover:bg-surface-2';
const EMPTY_ROW = 'px-1.5 py-1 text-muted-foreground';

function WorkerItem({
  worker,
  visible,
  selected,
  onPick,
}: {
  worker: WorkerRow;
  visible: boolean;
  selected: boolean;
  onPick: (worldId: string) => void;
}) {
  return (
    <li>
      <button
        type="button"
        aria-pressed={selected}
        onClick={() => onPick(worker.worldId)}
        className={cn(ROW, 'gap-3', selected && 'bg-surface-2 ring-1 ring-primary ring-inset')}
      >
        <span className="font-mono font-semibold">{worker.worldId}</span>
        <span className="font-mono text-sm text-muted-foreground">
          {formatClock(worker.firstSeen)}–{formatClock(worker.lastSeen)}
        </span>
        <span className="ml-auto text-sm text-muted-foreground">이벤트 {worker.events}</span>
        <span
          className={cn('w-8 text-right text-sm', visible ? 'text-ok' : 'text-muted-foreground')}
        >
          {visible ? '보임' : '—'}
        </span>
      </button>
    </li>
  );
}

/** 구간에 잡힌 작업자(익명 World ID). 누르면 고른다. */
export function WorkerList({
  workers,
  planned,
  visible,
  selectedId,
  onPick,
}: {
  workers: readonly WorkerRow[];
  planned: number;
  visible: ReadonlySet<string>;
  selectedId: string | null;
  onPick: (worldId: string) => void;
}) {
  return (
    <DashPanel icon={Users} title="작업자" count={`잡힘 ${workers.length} · 계획 ${planned}`}>
      <ul aria-label="작업자 목록" className={LIST}>
        {workers.length === 0 && <li className={EMPTY_ROW}>잡힌 작업자가 없습니다.</li>}
        {workers.map((w) => (
          <WorkerItem
            key={w.worldId}
            worker={w}
            visible={visible.has(w.worldId)}
            selected={selectedId === w.worldId}
            onPick={onPick}
          />
        ))}
      </ul>
    </DashPanel>
  );
}

function EventItem({
  event,
  playhead,
  onPick,
}: {
  event: EventRow;
  playhead: number | null;
  onPick: (event: EventRow) => void;
}) {
  const active = playhead !== null && isActiveAt(event, playhead);
  const later = playhead !== null && event.raisedAt > playhead;
  return (
    <li>
      <button
        type="button"
        aria-current={active ? 'true' : undefined}
        onClick={() => onPick(event)}
        className={cn(
          ROW,
          'gap-2.5',
          active && 'bg-surface-2 font-semibold',
          later && 'text-muted-foreground',
        )}
      >
        <span className="font-mono text-sm">{formatTime(event.raisedAt)}</span>
        <SeverityBadge severity={event.severity} />
        <span className="min-w-0 flex-1 truncate">{ruleName(event.ruleId)}</span>
        <span className="font-mono text-sm text-muted-foreground">
          {[...event.worldIds, event.zoneId].filter(Boolean).join(' · ')}
        </span>
      </button>
    </li>
  );
}

/** 구간 이벤트(발생순). 지금 시각에 활성인 것을 강조하고, 누르면 그 시각으로 옮긴다. 확인(ACK)은 없다. */
export function EventLog({
  events,
  playhead,
  onPick,
}: {
  events: readonly EventRow[];
  playhead: number | null;
  onPick: (event: EventRow) => void;
}) {
  return (
    <DashPanel icon={ListOrdered} title="이벤트 로그" count={events.length}>
      <ol aria-label="이벤트 로그" className={LIST}>
        {events.length === 0 && <li className={EMPTY_ROW}>위험·경고 없음</li>}
        {events.map((e) => (
          <EventItem key={e.eventId} event={e} playhead={playhead} onPick={onPick} />
        ))}
      </ol>
    </DashPanel>
  );
}
