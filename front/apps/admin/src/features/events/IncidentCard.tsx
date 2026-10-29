import { useCanAck } from '@/features/events/ack';
import {
  entryStatus,
  eventSubject,
  firstRaisedAt,
  formatTime,
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

/** 경보 확인 버튼. 확인 대기 경보이고 권한(반장·관리자)이 있을 때만 보인다. */
export function AckButton({ entry, label = '확인' }: { entry: EventEntry; label?: string }) {
  const canAck = useCanAck();
  if (!canAck || entryStatus(entry).kind !== 'alarm') return null;
  return <AckControl event={entry.event} label={label} />;
}

/** 보내는 중에도 포커스를 잃지 않게 막기만 한다. 실패하면 문구를 보인다. */
function AckControl({ event, label }: { event: EventItem; label: string }) {
  const { ack, pending, error } = useAckEvent(event.event_id);
  const text = pending ? '확인 중…' : label;
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

/** 규칙 한 줄: 등급·규칙 이름·구역. 심각도와 같이 보여야 하는 확인 필요·지연 알림만 붙인다(명세 7.3). 누르면 사건 상세. */
function RuleRow({ entry, onOpen }: { entry: EventEntry; onOpen: (eventId: string) => void }) {
  const { event } = entry;
  const resolved = entryStatus(entry).kind === 'resolved';

  return (
    <li>
      <button
        type="button"
        onClick={() => onOpen(event.event_id)}
        className={cn(
          '-mx-1 flex w-[calc(100%+0.5rem)] flex-wrap items-center gap-2 rounded-md px-1 py-0.5 text-left text-sm hover:bg-surface-3',
          resolved && 'opacity-50',
        )}
      >
        <SeverityBadge severity={event.severity} />
        <span>
          {ruleName(event.rule_id)}
          {event.zone_id && <> · {event.zone_id}</>}
        </span>
        {needsCheck(event) && <Badge variant="outline">확인 필요</Badge>}
        {event.delivery === 'DELAYED_ACTIVE' && (
          <Badge variant="outline" className="border-delay text-delay">
            지연 알림
          </Badge>
        )}
      </button>
    </li>
  );
}

interface IncidentCardProps {
  incident: Incident;
  selected: boolean;
  onSelect: (incident: Incident) => void;
  onOpen: (eventId: string) => void;
}

/**
 * 한 사람(또는 구역)에 걸린 규칙 묶음 카드(명세 7.4). 머리줄은 대상과 첫 발생 시각(누르면 선택·3D 초점),
 * 규칙 줄은 등급·이름·구역만 보인다. 지표·처리 상태·확인 버튼은 사건 상세 창에 있다(관제 데모와 같다).
 */
export function IncidentCard({ incident, selected, onSelect, onOpen }: IncidentCardProps) {
  const high = SEVERITY_RANK[incident.severity] >= SEVERITY_RANK.HIGH;
  const tone = !incident.active
    ? undefined
    : high
      ? 'ring-1 ring-sev-critical ring-inset'
      : incident.severity === 'CAUTION'
        ? 'ring-1 ring-sev-caution ring-inset'
        : undefined;

  return (
    <li>
      <article
        aria-label={`${targetLabel(incident.target)} 이벤트`}
        className={cn(
          'relative flex flex-col gap-1 overflow-hidden rounded-[10px] bg-surface-2 px-3.5 py-3',
          'hover:ring-1 hover:ring-point-soft hover:ring-inset',
          tone,
          incident.alarming && 'pl-[18px]',
          !incident.active && 'opacity-55',
          selected && 'outline-2 outline-offset-1 outline-primary',
        )}
      >
        {incident.alarming && (
          <span
            aria-hidden
            className="absolute inset-y-0 left-0 w-1 bg-sev-critical motion-safe:animate-alarm-blink"
          />
        )}
        <button
          type="button"
          aria-pressed={selected}
          onClick={() => onSelect(incident)}
          className="-mx-1 flex items-baseline justify-between gap-2 rounded-md px-1 text-left hover:bg-surface-3"
        >
          <strong className="font-mono text-[15px]">{targetLabel(incident.target)}</strong>
          <span className="font-mono text-xs text-muted-foreground">
            {formatTime(firstRaisedAt(incident))}
          </span>
        </button>
        <ul className="flex flex-col gap-0.5">
          {incident.entries.map((entry) => (
            <RuleRow key={entry.event.event_id} entry={entry} onOpen={onOpen} />
          ))}
        </ul>
      </article>
    </li>
  );
}
