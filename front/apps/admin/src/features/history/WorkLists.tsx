import { DashPanel } from '@/features/dashboard/DashPanel';
import { formatTime, ruleName } from '@/features/events/events';
import { SeverityBadge } from '@/features/events/SeverityBadge';
import { isRowActiveAt, pastEventRows, type PastEventRow } from '@/features/history/pastEventRows';
import type { EventRow, WorkerRow } from '@/features/history/replayIndex';
import { formatClock } from '@/features/history/works';
import {
  resolveHistoricalWorkerName,
  workerShortLabel,
  type HistoricalPersonNameSpan,
} from '@/features/people/personDisplay';
import { useZoneName } from '@/features/zones/useZoneName';
import { zoneNameOrDefault, type ZoneNameOf } from '@/features/zones/zoneName';
import { cn } from '@simai/ui/lib/utils';
import { ListOrdered, Users } from 'lucide-react';
import { useEffect, useMemo, useRef } from 'react';

const LIST = 'flex min-h-0 flex-col gap-1 overflow-auto px-2.5 py-2';
const ROW =
  'flex w-full cursor-pointer items-center rounded-lg px-2.5 py-1.5 text-left hover:bg-surface-2';
const EMPTY_ROW = 'px-1.5 py-1 text-muted-foreground';

function WorkerItem({
  worker,
  visible,
  selected,
  personNames,
  onPick,
}: {
  worker: WorkerRow;
  visible: boolean;
  selected: boolean;
  personNames?: readonly HistoricalPersonNameSpan[];
  onPick: (worldId: string) => void;
}) {
  const resolvedName = resolveHistoricalWorkerName(
    worker.worldId,
    worker.firstSeen,
    worker.lastSeen,
    personNames,
  );
  const label = resolvedName ?? workerShortLabel(worker.worldId);
  return (
    <li>
      <button
        type="button"
        aria-pressed={selected}
        onClick={() => onPick(worker.worldId)}
        className={cn(ROW, 'gap-3', selected && 'bg-surface-2 ring-1 ring-primary ring-inset')}
      >
        <span className={cn('font-semibold', !resolvedName && 'font-mono')} title={worker.worldId}>
          {label}
        </span>
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

/** 구간에 잡힌 작업자(그 시각 이름이 있으면 이름, 없으면 작업자 #NNNN, hover 로 World ID). 누르면 고른다. */
export function WorkerList({
  workers,
  planned,
  visible,
  selectedId,
  personNames,
  onPick,
}: {
  workers: readonly WorkerRow[];
  planned: number;
  visible: ReadonlySet<string>;
  selectedId: string | null;
  personNames?: readonly HistoricalPersonNameSpan[];
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
            personNames={personNames}
            onPick={onPick}
          />
        ))}
      </ul>
    </DashPanel>
  );
}

function EventItem({
  row,
  playhead,
  zoneName,
  personNames,
  onPick,
  selected,
}: {
  selected: boolean;
  row: PastEventRow;
  playhead: number | null;
  zoneName: ZoneNameOf;
  personNames?: readonly HistoricalPersonNameSpan[];
  onPick: (event: EventRow) => void;
}) {
  const ref = useRef<HTMLLIElement>(null);
  useEffect(() => {
    if (selected) ref.current?.scrollIntoView?.({ block: 'nearest' });
  }, [selected]);
  const { first, people } = row;
  const active = playhead !== null && isRowActiveAt(row, playhead);
  const later = playhead !== null && first.raisedAt > playhead;
  // 그 시각 표시 이름이 있으면 이름으로, 없으면 「작업자 #n」으로 보이고, 「외 n명」 묶음과 hover 의 World ID 는 유지한다(S15P31A101-751, -835)
  const workers = people.length
    ? `${workerShortLabel(people[0]!, first.raisedAt, personNames)}${people.length > 1 ? ` 외 ${people.length - 1}명` : ''}`
    : '';
  // 대상은 작업자만 보인다(구역 이름을 붙이면 줄이 길어져 경보 이름이 잘린다, S15P31A101-853).
  // 작업자가 없는 경보만 구역이 대상이라 구역 이름(모르면 `구역`)을 보인다.
  const zone = workers ? null : first.zoneId && zoneNameOrDefault(first.zoneId, zoneName);
  return (
    <li ref={ref}>
      <button
        type="button"
        aria-current={selected || active ? 'true' : undefined}
        onClick={() => onPick(first)}
        className={cn(
          ROW,
          'gap-2.5',
          active && 'bg-surface-2 font-semibold',
          selected && 'bg-primary/15 font-semibold ring-2 ring-primary ring-inset',
          later && 'text-muted-foreground',
        )}
      >
        <span className="font-mono text-sm">{formatTime(first.raisedAt)}</span>
        <SeverityBadge severity={row.severity} />
        <span className="min-w-0 flex-1 truncate">{ruleName(first.ruleId)}</span>
        <span className="text-sm text-muted-foreground">
          <span title={people.length ? people.join(' · ') : undefined}>{workers}</span>
          {zone}
        </span>
      </button>
    </li>
  );
}

/**
 * 구간 이벤트(발생순). 같은 위험이 이어진 것(같은 초·트랙 번호만 바뀐 경보 포함)은 한 줄 「×n」으로 묶는다(관제 카드와 같은 묶음, 머리 수는
 * 줄 수 = 사건 수). 지금 시각에 활성인 것을 강조하고, 누르면 그 시각으로 옮긴다. 확인(ACK)은 없다.
 */
export function EventLog({
  events,
  playhead,
  onPick,
  selectedEventId = null,
  personNames,
}: {
  selectedEventId?: string | null;
  events: readonly EventRow[];
  playhead: number | null;
  personNames?: readonly HistoricalPersonNameSpan[];
  onPick: (event: EventRow) => void;
}) {
  const zoneName = useZoneName();
  const rows = useMemo(() => pastEventRows(events), [events]);
  return (
    <DashPanel icon={ListOrdered} title={`이벤트 로그 · ${rows.length}건`}>
      <ol aria-label="이벤트 로그" className={LIST}>
        {rows.length === 0 && <li className={EMPTY_ROW}>위험·주의 없음</li>}
        {rows.map((row) => (
          <EventItem
            key={row.key}
            row={row}
            selected={row.members.some((event) => event.eventId === selectedEventId)}
            playhead={playhead}
            zoneName={zoneName}
            personNames={personNames}
            onPick={onPick}
          />
        ))}
      </ol>
    </DashPanel>
  );
}
