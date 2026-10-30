import { useCurrentRole } from '@/features/auth/useCurrentRole';
import { hasClips } from '@/features/clips/clips';
import { EventClips } from '@/features/clips/EventClips';
import { AckButton } from '@/features/events/AckButton';
import { CONFIDENCE_REASON } from '@/features/events/constants';
import { entryStatus, needsConfirmation, type EventEntry } from '@/features/events/entries';
import { escalationStatusLabel, mergedEscalations } from '@/features/events/escalation';
import { EscalationControl } from '@/features/events/EscalationControl';
import { formatTime, metricLabels, ruleName, statusText } from '@/features/events/format';
import type { Incident } from '@/features/events/incidents';
import { incidentTimeline } from '@/features/events/incidentView';
import { SeverityBadge } from '@/features/events/SeverityBadge';
import type { EventEscalation } from '@/features/events/types';
import { useEscalationHistory } from '@/features/events/useEscalateEvent';
import { cn } from '@simai/ui/lib/utils';
import type { ReactNode } from 'react';

// 사건 상세 창의 칸들. 받은 이벤트 값만으로 그린다.

function DetailPanel({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section aria-label={title} className="flex flex-col gap-2 rounded-[12px] bg-card px-4 py-3">
      <h3 className="text-[15px] leading-[22px] font-bold text-muted-foreground">{title}</h3>
      {children}
    </section>
  );
}

const None = () => <p className="text-sm text-muted-foreground">—</p>;

/** 규칙 하나의 근거 줄: 등급 · 규칙 · 구역 · 발생 · 상태, 지표, 확인 필요 이유. */
function RuleRow({ entry, current }: { entry: EventEntry; current: boolean }) {
  const { event } = entry;
  const reasons = event.confidence_reasons ?? [];
  const metrics = metricLabels(event.metrics);
  return (
    <li
      aria-current={current || undefined}
      className={cn(
        'flex flex-col gap-1 py-2.5 shadow-[0_1px_0_var(--border)] last:shadow-none',
        entryStatus(entry).kind === 'resolved' && 'opacity-50',
      )}
    >
      <span className="flex flex-wrap items-center gap-x-2.5 gap-y-1.5">
        <SeverityBadge severity={event.severity} />
        <b>
          {event.rule_id} {ruleName(event.rule_id)}
        </b>
        <span className="text-[13px] text-muted-foreground">
          {event.zone_id ?? '구역 없음'} ·{' '}
          <span className="font-mono">{formatTime(event.raised_at)}</span> 발생 ·{' '}
          {statusText(entryStatus(entry))}
        </span>
      </span>
      {metrics.length > 0 && (
        <span className="flex flex-wrap gap-x-2 font-mono text-sm">
          {metrics.map((label) => (
            <span key={label}>{label}</span>
          ))}
        </span>
      )}
      {needsConfirmation(event) && (
        <span className="text-sm">
          확인 필요
          {reasons.length > 0 && ` · ${reasons.map((r) => CONFIDENCE_REASON[r] ?? r).join(', ')}`}
        </span>
      )}
    </li>
  );
}

interface EntriesProps {
  entries: EventEntry[];
}

export function RulesPanel({
  entries,
  openEventId,
}: EntriesProps & {
  /** 눌러서 연 규칙. */
  openEventId: string;
}) {
  return (
    <DetailPanel title="규칙과 근거">
      <ul className="flex flex-col">
        {entries.map((entry) => (
          <RuleRow
            key={entry.event.event_id}
            entry={entry}
            current={entry.event.event_id === openEventId}
          />
        ))}
      </ul>
    </DetailPanel>
  );
}

function EscalationHistoryList({ items }: { items: readonly EventEscalation[] }) {
  if (items.length === 0) return null;
  return (
    <ol aria-label="상향 이력" className="flex flex-col gap-1 pt-1 text-xs">
      {items.map((item, idx) => (
        <li
          key={item.escalation_id ?? `${item.requested_at}-${idx}`}
          className="flex flex-wrap items-center gap-2 text-muted-foreground"
        >
          <span className="font-mono text-faint">{formatTime(item.requested_at)}</span>
          <SeverityBadge severity={item.target_severity} />
          <span className="font-semibold text-foreground">{escalationStatusLabel(item)}</span>
          {item.actor_id && <span className="text-point-soft">{item.actor_id}</span>}
          {item.note && <span>{item.note}</span>}
        </li>
      ))}
    </ol>
  );
}

export function AiPanel({ entries }: EntriesProps) {
  const historyByEvent = useEscalationHistory();
  const ai = entries.filter(({ event }) => {
    const history = mergedEscalations(event, historyByEvent[event.event_id]);
    return Boolean(event.rationale || event.proposed_severity || history.length > 0);
  });
  return (
    <DetailPanel title="AI 설명">
      {ai.length === 0 ? (
        <None />
      ) : (
        ai.map(({ event }) => {
          const history = mergedEscalations(event, historyByEvent[event.event_id]);
          return (
            <div key={event.event_id} className="flex flex-col gap-1.5 text-sm">
              {ai.length > 1 && (
                <span className="text-muted-foreground">{ruleName(event.rule_id)}</span>
              )}
              {event.rationale && <p>{event.rationale}</p>}
              {event.proposed_severity && (
                <div className="flex flex-col gap-1.5">
                  <span className="flex flex-wrap items-center gap-2">
                    상향 의견 <SeverityBadge severity={event.proposed_severity} />
                  </span>
                  <EscalationControl event={event} />
                </div>
              )}
              <EscalationHistoryList items={history} />
            </div>
          );
        })
      )}
    </DetailPanel>
  );
}

/** 이벤트 클립은 관리자에게만 보인다. */
export function ClipsPanel({ entries }: EntriesProps) {
  const role = useCurrentRole();
  const clipEntries = role === 'ADMIN' ? entries.filter((e) => hasClips(e.event)) : [];
  return (
    <DetailPanel title="이벤트 클립">
      {clipEntries.length === 0 ? (
        <None />
      ) : (
        clipEntries.map(({ event }) => (
          <EventClips key={event.event_id} event={event} title={ruleName(event.rule_id)} />
        ))
      )}
    </DetailPanel>
  );
}

export function ProcessPanel({ entries }: EntriesProps) {
  return (
    <DetailPanel title="처리">
      <ul className="flex flex-col gap-2">
        {entries.map((entry) => {
          const status = entryStatus(entry);
          return (
            <li
              key={entry.event.event_id}
              className="flex flex-wrap items-center justify-between gap-2"
            >
              <span className="flex flex-wrap items-center gap-2 text-sm">
                <span>{ruleName(entry.event.rule_id)}</span>
                <span
                  className={
                    status.kind === 'alarm'
                      ? 'font-bold text-sev-critical'
                      : 'text-muted-foreground'
                  }
                >
                  {statusText(status)}
                </span>
              </span>
              <AckButton entry={entry} label="경보 확인(ACK)" />
            </li>
          );
        })}
      </ul>
    </DetailPanel>
  );
}

export function TimelinePanel({ incident }: { incident: Incident }) {
  const timeline = incidentTimeline(incident);
  return (
    <DetailPanel title="타임라인">
      {timeline.length === 0 ? (
        <None />
      ) : (
        <ol className="flex flex-col">
          {timeline.map((item, n) => (
            <li
              key={n}
              className="grid grid-cols-[72px_82px_minmax(0,1fr)] gap-2.5 py-1.5 text-[13px] [&+&]:shadow-[0_-1px_0_var(--border)]"
            >
              <span className="font-mono text-faint">{formatTime(item.at)}</span>
              <span className="text-point-soft">{item.who}</span>
              <span>{item.text}</span>
            </li>
          ))}
        </ol>
      )}
    </DetailPanel>
  );
}
