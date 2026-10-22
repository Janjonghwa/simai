import {
  CONFIDENCE_REASON,
  entryStatus,
  formatTime,
  metricLabels,
  ruleName,
  type EventEntry,
} from '@/features/events/events';
import { AckButton, EventTags, statusText } from '@/features/events/IncidentCard';
import { SeverityBadge } from '@/features/events/SeverityBadge';
import { Button } from '@simai/ui/components/button';
import { ChevronLeft } from 'lucide-react';
import type { ReactNode } from 'react';

const CHANNEL: Record<string, string> = {
  CONTROL_ROOM: '관제',
  FOREMAN_PWA: '반장 휴대폰',
  SITE_BUTTON: '현장 버튼',
};

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[88px_1fr] gap-2 text-sm">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="min-w-0">{children}</dd>
    </div>
  );
}

/** 규칙 하나의 상세. 상세 API(GET /api/events/{id}) 형식이 정해지기 전이라 받은 이벤트 값으로 그린다. */
export function EventDetail({ entry, onBack }: { entry: EventEntry; onBack: () => void }) {
  const { event } = entry;
  const reasons = event.confidence_reasons ?? [];

  return (
    <section aria-label="이벤트 상세" className="flex min-h-0 flex-col gap-3">
      <div className="flex items-center gap-2">
        <Button variant="ghost" size="sm" onClick={onBack}>
          <ChevronLeft aria-hidden />
          목록
        </Button>
      </div>
      <header className="flex flex-wrap items-center gap-2">
        <SeverityBadge severity={event.severity} />
        <h2 className="text-base font-bold">{ruleName(event.rule_id)}</h2>
        <span className="font-mono text-xs text-muted-foreground">{event.rule_id}</span>
      </header>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="flex flex-wrap items-center gap-1.5 text-sm">
          {statusText(entryStatus(entry))}
          <EventTags entry={entry} />
        </span>
        <AckButton entry={entry} label="경보 확인(ACK)" />
      </div>
      <dl className="flex flex-col gap-2">
        <Row label="대상">{event.world_ids.length ? event.world_ids.join(', ') : '—'}</Row>
        <Row label="구역">{event.zone_id ?? '—'}</Row>
        <Row label="지표">
          <span className="flex flex-wrap gap-x-2 font-mono">
            {metricLabels(event.metrics).map((label) => (
              <span key={label}>{label}</span>
            ))}
          </span>
        </Row>
        {reasons.length > 0 && (
          <Row label="확인 필요">{reasons.map((r) => CONFIDENCE_REASON[r] ?? r).join(', ')}</Row>
        )}
        <Row label="발생">{formatTime(event.raised_at)}</Row>
        <Row label="갱신">{formatTime(event.updated_at)}</Row>
        {event.lifecycle === 'RESOLVED' && <Row label="해소">{formatTime(event.resolved_at)}</Row>}
        {event.resolution && <Row label="종료 사유">{event.resolution.reason}</Row>}
        <Row label="확인 기록">
          {(event.acks ?? []).length === 0 ? (
            '—'
          ) : (
            <ul className="flex flex-col gap-0.5">
              {(event.acks ?? []).map((a) => (
                <li key={a.ack_id} className="font-mono text-xs">
                  {formatTime(a.acked_at)} · {CHANNEL[a.channel] ?? a.channel} · {a.actor_id}
                </li>
              ))}
            </ul>
          )}
        </Row>
        {event.rationale && <Row label="AI 근거">{event.rationale}</Row>}
        {event.proposed_severity && (
          <Row label="상향 의견">
            <SeverityBadge severity={event.proposed_severity} />
          </Row>
        )}
      </dl>
    </section>
  );
}
