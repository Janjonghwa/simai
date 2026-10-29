import { useCurrentRole } from '@/features/auth/useCurrentRole';
import { hasClips } from '@/features/clips/clips';
import { EventClips } from '@/features/clips/EventClips';
import {
  CONFIDENCE_REASON,
  entryStatus,
  firstRaisedAt,
  formatTime,
  incidentTimeline,
  metricLabels,
  needsCheck,
  ruleName,
  type Incident,
} from '@/features/events/events';
import { AckButton, statusText, targetLabel } from '@/features/events/IncidentCard';
import { SeverityBadge } from '@/features/events/SeverityBadge';
import { Badge } from '@simai/ui/components/badge';
import { Button } from '@simai/ui/components/button';
import { cn } from '@simai/ui/lib/utils';
import { useEffect, useRef, type ReactNode } from 'react';

function Panel({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section aria-label={title} className="flex flex-col gap-2 rounded-[12px] bg-card px-4 py-3">
      <h3 className="text-[15px] leading-[22px] font-bold text-muted-foreground">{title}</h3>
      {children}
    </section>
  );
}

const None = () => <p className="text-sm text-muted-foreground">—</p>;

/** 머리줄 상태: 진행 중이면 발생·갱신, 모두 해소면 마지막 해소 시각. */
function stateLabel(incident: Incident): string {
  if (incident.active) {
    return incident.entries.some((e) => e.event.lifecycle === 'UPDATED') ? '갱신' : '발생';
  }
  const times = incident.entries.map((e) => {
    const status = entryStatus(e);
    return status.kind === 'resolved' ? (status.at ?? 0) : 0;
  });
  return `해소 ${formatTime(Math.max(...times))}`;
}

interface EventDetailProps {
  incident: Incident;
  /** 눌러서 연 규칙. 근거 목록에서 표시한다. */
  openEventId: string;
  onClose: () => void;
}

/**
 * 사건 상세 창(관제 데모와 같다). 한 대상에 걸린 규칙 전부를 장면 위에 띄워 보인다. 바깥을 누르거나 Esc 로 닫는다.
 * 상세 API(GET /api/events/{id}) 형식이 정해지기 전이라 받은 이벤트 값으로 그린다. 조치 요청·조치 단계는 서버 API(M2)가 없어 두지 않는다.
 */
export function EventDetail({ incident, openEventId, onClose }: EventDetailProps) {
  const role = useCurrentRole();
  const close = useRef<HTMLButtonElement>(null);
  const { entries } = incident;
  const events = entries.map((e) => e.event);
  const clipEntries = role === 'ADMIN' ? entries.filter((e) => hasClips(e.event)) : [];
  const timeline = incidentTimeline(incident);
  const ai = entries.filter((e) => e.event.rationale || e.event.proposed_severity);

  // 열 때(다른 사건으로 바꿀 때)만 닫기 버튼에 포커스를 둔다. 패널이 다시 그려질 때마다 옮기면 ACK 등에 둔 포커스를 빼앗는다.
  useEffect(() => {
    close.current?.focus();
  }, [incident.key]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  return (
    <div
      data-testid="incident-layer"
      onClick={onClose}
      className="absolute inset-x-0 top-[calc(60px+var(--notices-h,0px))] bottom-[86px] z-[6] flex justify-end bg-foreground/14 p-4 dark:bg-background/45"
    >
      <section
        role="dialog"
        aria-label="사건 상세"
        onClick={(e) => e.stopPropagation()}
        className="flex min-w-0 flex-[0_1_1000px] flex-col gap-3 overflow-auto rounded-[16px] bg-background/97 px-4 py-3.5 shadow-float ring-1 ring-border ring-inset"
      >
        <header className="flex flex-wrap items-center gap-x-3.5 gap-y-2.5">
          <h2 className="text-[22px] leading-[30px] font-bold">
            <span className="font-mono">{targetLabel(incident.target)}</span> 사건
          </h2>
          <SeverityBadge severity={incident.severity} />
          <Badge variant="secondary">{stateLabel(incident)}</Badge>
          <span className="font-mono text-sm text-muted-foreground">
            발생 {formatTime(firstRaisedAt(incident))}
          </span>
          {events.some(needsCheck) && <Badge variant="outline">확인 필요</Badge>}
          {events.some((e) => e.verification === 'UNVERIFIABLE') && (
            <Badge variant="outline">해소 확인 불가</Badge>
          )}
          {events.some((e) => e.delivery === 'DELAYED_ACTIVE') && (
            <Badge variant="outline" className="border-delay text-delay">
              지연 알림
            </Badge>
          )}
          <Button ref={close} variant="secondary" size="sm" className="ml-auto" onClick={onClose}>
            닫기 ✕
          </Button>
        </header>
        <div className="grid min-h-0 flex-1 grid-cols-2 gap-3.5">
          <div className="flex min-h-0 flex-col gap-3 overflow-auto">
            <Panel title="규칙과 근거">
              <ul className="flex flex-col">
                {entries.map((entry) => {
                  const { event } = entry;
                  const reasons = event.confidence_reasons ?? [];
                  const metrics = metricLabels(event.metrics);
                  return (
                    <li
                      key={event.event_id}
                      aria-current={event.event_id === openEventId || undefined}
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
                      {reasons.length > 0 && (
                        <span className="text-sm">
                          확인 필요 · {reasons.map((r) => CONFIDENCE_REASON[r] ?? r).join(', ')}
                        </span>
                      )}
                    </li>
                  );
                })}
              </ul>
            </Panel>
            <Panel title="AI 설명">
              {ai.length === 0 ? (
                <None />
              ) : (
                ai.map(({ event }) => (
                  <div key={event.event_id} className="flex flex-col gap-1 text-sm">
                    {ai.length > 1 && (
                      <span className="text-muted-foreground">{ruleName(event.rule_id)}</span>
                    )}
                    {event.rationale && <p>{event.rationale}</p>}
                    {event.proposed_severity && (
                      <span className="flex items-center gap-2">
                        상향 의견 <SeverityBadge severity={event.proposed_severity} />
                      </span>
                    )}
                  </div>
                ))
              )}
            </Panel>
            <Panel title="이벤트 클립">
              {clipEntries.length === 0 ? (
                <None />
              ) : (
                clipEntries.map(({ event }) => (
                  <EventClips key={event.event_id} event={event} title={ruleName(event.rule_id)} />
                ))
              )}
            </Panel>
          </div>
          <div className="flex min-h-0 flex-col gap-3 overflow-auto">
            <Panel title="처리">
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
                          className={cn(
                            status.kind === 'alarm'
                              ? 'font-bold text-sev-critical'
                              : 'text-muted-foreground',
                          )}
                        >
                          {statusText(status)}
                        </span>
                      </span>
                      <AckButton entry={entry} label="경보 확인(ACK)" />
                    </li>
                  );
                })}
              </ul>
            </Panel>
            <Panel title="타임라인">
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
            </Panel>
          </div>
        </div>
      </section>
    </div>
  );
}
