import { eventsEmptyText } from '@/features/dashboard/dashboard';
import { DashPanel } from '@/features/dashboard/DashPanel';
import { useFit } from '@/features/dashboard/useFit';
import {
  firstRaisedAt,
  formatTime,
  groupIncidents,
  isActive,
  ruleName,
  SEVERITY_RANK,
  type Incident,
} from '@/features/events/events';
import { targetLabel } from '@/features/events/IncidentCard';
import { SeverityBadge } from '@/features/events/SeverityBadge';
import { useLiveEvents } from '@/features/events/useLiveEvents';
import { cn } from '@simai/ui/lib/utils';
import { TriangleAlert } from 'lucide-react';
import { useMemo } from 'react';

/** 카드에 보이는 사건 수(넘치면 2개까지 줄인다). */
const EVENT_ROWS = 3;

type OpenEvent = (eventId: string) => void;

function useIncidents() {
  const { entries, loaded, failed } = useLiveEvents();
  const incidents = useMemo(() => groupIncidents(entries), [entries]);
  return { incidents, loaded, failed };
}

/** 사건 카드: 대상 · 시각 · 규칙 줄만 보인다(관제 데모와 같다). 누르면 실시간 관제에서 사건 상세를 연다. */
function IncidentButton({ incident, onOpen }: { incident: Incident; onOpen: OpenEvent }) {
  const high = SEVERITY_RANK[incident.severity] >= SEVERITY_RANK.HIGH;
  const tone = !incident.active
    ? 'opacity-55'
    : high
      ? 'ring-1 ring-sev-critical ring-inset'
      : incident.severity === 'CAUTION'
        ? 'ring-1 ring-sev-caution ring-inset'
        : undefined;

  return (
    <button
      type="button"
      onClick={() => onOpen(incident.entries[0]!.event.event_id)}
      className={cn(
        'relative flex w-full cursor-pointer flex-col gap-1.5 overflow-hidden rounded-[10px] bg-surface-2 px-3.5 py-3 text-left',
        tone,
        'hover:ring-1 hover:ring-point-soft hover:ring-inset',
        incident.alarming && 'pl-[18px]',
      )}
    >
      {incident.alarming && (
        <span
          aria-hidden
          className="absolute inset-y-0 left-0 w-1 bg-sev-critical motion-safe:animate-alarm-blink"
        />
      )}
      <span className="flex items-baseline justify-between gap-2">
        <strong className="font-mono text-[15px]">{targetLabel(incident.target)}</strong>
        <span className="font-mono text-xs text-muted-foreground">
          {formatTime(firstRaisedAt(incident))}
        </span>
      </span>
      {incident.entries.map(({ event }) => (
        <span
          key={event.event_id}
          className={cn(
            'flex flex-wrap items-center gap-2 text-sm',
            !isActive(event) && 'opacity-50',
          )}
        >
          <SeverityBadge severity={event.severity} />
          {ruleName(event.rule_id)}
          {event.zone_id && ` · ${event.zone_id}`}
        </span>
      ))}
    </button>
  );
}

function Empty({ loaded, failed }: { loaded: boolean; failed: boolean }) {
  return <p className="mx-2 my-4 text-sm text-faint">{eventsEmptyText(loaded, failed)}</p>;
}

export function EventsPanel({ onOpenList, onOpen }: { onOpenList: () => void; onOpen: OpenEvent }) {
  const { incidents, loaded, failed } = useIncidents();
  const shown = incidents.slice(0, EVENT_ROWS);
  const [fitRef, visible] = useFit<HTMLUListElement>(
    EVENT_ROWS,
    shown.map((incident) => incident.key).join(),
  );
  const active = incidents.filter((incident) => incident.active).length;

  return (
    <DashPanel icon={TriangleAlert} title="이벤트 목록" count={active} onOpen={onOpenList}>
      {shown.length > 0 ? (
        <ul
          ref={fitRef}
          aria-label="이벤트 목록"
          className="relative flex min-h-0 flex-1 flex-col gap-2 overflow-hidden p-3"
        >
          {shown.map((incident, n) => (
            <li key={incident.key} className={cn(n >= visible && 'invisible')}>
              <IncidentButton incident={incident} onOpen={onOpen} />
            </li>
          ))}
        </ul>
      ) : (
        <div className="p-3">
          <Empty loaded={loaded} failed={failed} />
        </div>
      )}
    </DashPanel>
  );
}

/** 큰 창의 전체 목록. */
export function EventsList({ onOpen }: { onOpen: OpenEvent }) {
  const { incidents, loaded, failed } = useIncidents();
  if (incidents.length === 0) return <Empty loaded={loaded} failed={failed} />;
  return (
    <ul aria-label="이벤트 목록" className="flex min-h-0 flex-col gap-2 overflow-auto p-0.5">
      {incidents.map((incident) => (
        <li key={incident.key}>
          <IncidentButton incident={incident} onOpen={onOpen} />
        </li>
      ))}
    </ul>
  );
}
