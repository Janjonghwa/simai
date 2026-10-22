import { useCanAck } from '@/features/events/ack';
import {
  entryStatus,
  eventSubject,
  formatTime,
  metricLabels,
  needsCheck,
  ruleName,
  SEVERITY_RANK,
  type EntryStatus,
  type EventEntry,
  type Incident,
} from '@/features/events/events';
import { SeverityBadge } from '@/features/events/SeverityBadge';
import type { EventItem } from '@/features/events/types';
import { useAckEvent } from '@/features/events/useAckEvent';
import { Badge } from '@simai/ui/components/badge';
import { Button } from '@simai/ui/components/button';
import { cn } from '@simai/ui/lib/utils';

export const targetLabel = (target: Incident['target']) =>
  !target ? '대상 없음' : target.type === 'person' ? target.id : `구역 ${target.id}`;

export function statusText(status: EntryStatus) {
  switch (status.kind) {
    case 'alarm':
      return '확인 대기';
    case 'acked':
      return `확인 ${formatTime(status.at)}`;
    case 'notice':
      return '안내';
    case 'resolved':
      return `${status.closed ? '운영자 종료' : '해소'} ${formatTime(status.at)}`;
  }
}

/** 확인 필요·해소 확인 불가·지연 알림 표시(명세 FR-4). 지연만 주황(CONVENTION). */
export function EventTags({ entry }: { entry: EventEntry }) {
  const { event } = entry;
  return (
    <>
      {needsCheck(event) && <Badge variant="outline">확인 필요</Badge>}
      {event.verification === 'UNVERIFIABLE' && <Badge variant="outline">해소 확인 불가</Badge>}
      {event.delivery === 'DELAYED_ACTIVE' && (
        <Badge variant="outline" className="border-delay text-delay">
          지연 알림
        </Badge>
      )}
    </>
  );
}

/** 경보 확인 버튼. 확인 대기 경보이고 권한(반장·관리자)이 있을 때만 보인다. */
export function AckButton({ entry, label = '확인' }: { entry: EventEntry; label?: string }) {
  const canAck = useCanAck();
  if (!canAck || entryStatus(entry).kind !== 'alarm') return null;
  return <AckControl event={entry.event} label={label} />;
}

/** 보내는 중에도 포커스를 잃지 않게 막기만 한다. 실패하면 문구를 보인다. */
function AckControl({ event, label }: { event: EventItem; label: string }) {
  const { ack, pending, error } = useAckEvent(event.event_id);
  const text = pending ? '확인 중' : label;
  return (
    <span className="flex flex-col items-end gap-1">
      <Button
        size="sm"
        disabled={pending}
        focusableWhenDisabled
        // 목록에 「확인」이 여러 개라 어느 경보인지 함께 읽힌다.
        aria-label={`${ruleName(event.rule_id)} ${eventSubject(event)} ${text}`}
        onClick={() => ack(event.event_sequence)}
      >
        {text}
      </Button>
      {error && (
        <span role="alert" className="text-xs text-muted-foreground">
          {error}
        </span>
      )}
    </span>
  );
}

function RuleRow({ entry, onOpen }: { entry: EventEntry; onOpen: (eventId: string) => void }) {
  const { event } = entry;
  const status = entryStatus(entry);

  return (
    <li className={cn('flex items-start gap-2', status.kind === 'resolved' && 'opacity-55')}>
      <button
        type="button"
        onClick={() => onOpen(event.event_id)}
        className="flex min-w-0 flex-1 flex-col items-start gap-1 rounded-md p-1 text-left hover:bg-surface-3"
      >
        <span className="flex flex-wrap items-center gap-1.5">
          <SeverityBadge severity={event.severity} />
          <span className="font-medium">{ruleName(event.rule_id)}</span>
          {event.zone_id && <span className="text-muted-foreground">{event.zone_id}</span>}
        </span>
        <span className="flex flex-wrap gap-x-2 font-mono text-xs text-muted-foreground">
          {metricLabels(event.metrics).map((label) => (
            <span key={label}>{label}</span>
          ))}
        </span>
        <span className="flex flex-wrap items-center gap-1.5 text-xs">
          <span className={cn(status.kind === 'alarm' ? 'font-bold' : 'text-muted-foreground')}>
            {statusText(status)}
          </span>
          <EventTags entry={entry} />
        </span>
      </button>
      <AckButton entry={entry} />
    </li>
  );
}

interface IncidentCardProps {
  incident: Incident;
  selected: boolean;
  onSelect: (incident: Incident) => void;
  onOpen: (eventId: string) => void;
}

/** 한 사람(또는 구역)에 걸린 규칙 묶음 카드(명세 7.4). */
export function IncidentCard({ incident, selected, onSelect, onOpen }: IncidentCardProps) {
  const high = SEVERITY_RANK[incident.severity] >= SEVERITY_RANK.HIGH;
  const tone = !incident.active
    ? 'border-border'
    : high
      ? 'border-sev-critical'
      : incident.severity === 'CAUTION'
        ? 'border-sev-caution'
        : 'border-border';

  return (
    <li>
      <article
        aria-label={`${targetLabel(incident.target)} 이벤트`}
        className={cn(
          'relative flex flex-col gap-2 overflow-hidden rounded-[12px] border bg-surface-2 p-3 pl-4',
          tone,
          !incident.active && 'opacity-70',
          selected && 'ring-2 ring-primary',
        )}
      >
        <span
          aria-hidden
          className={cn(
            'absolute inset-y-0 left-0 w-1',
            incident.alarming
              ? 'bg-sev-critical motion-safe:animate-alarm-blink'
              : 'bg-transparent',
          )}
        />
        <button
          type="button"
          aria-pressed={selected}
          onClick={() => onSelect(incident)}
          className="flex items-baseline justify-between gap-2 text-left"
        >
          <strong className="font-mono">{targetLabel(incident.target)}</strong>
          <span className="text-xs text-muted-foreground">
            규칙 {incident.entries.length}건 · {formatTime(incident.latestAt)}
          </span>
        </button>
        <ul className="flex flex-col gap-1">
          {incident.entries.map((entry) => (
            <RuleRow key={entry.event.event_id} entry={entry} onOpen={onOpen} />
          ))}
        </ul>
      </article>
    </li>
  );
}
