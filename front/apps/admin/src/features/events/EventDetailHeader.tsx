import { isActive, needsConfirmation } from '@/features/events/entries';
import { ConfirmBadge, DelayedBadge } from '@/features/events/EventBadges';
import { formatTime } from '@/features/events/format';
import { firstRaisedAt, type Incident } from '@/features/events/incidents';
import { resolvedLabel, targetLabel } from '@/features/events/incidentView';
import { useMismatches } from '@/features/events/mismatch';
import { SeverityBadge } from '@/features/events/SeverityBadge';
import { Badge } from '@simai/ui/components/badge';
import { Button } from '@simai/ui/components/button';
import type { Ref } from 'react';

/** 머리줄 상태: 진행 중이면 발생·갱신, 모두 해소면 마지막 해소 시각. */
function stateLabel(incident: Incident): string {
  if (!incident.active) return resolvedLabel(incident);
  return incident.entries.some((e) => e.event.lifecycle === 'UPDATED') ? '갱신' : '발생';
}

/** 미확인 경보 표시: 작은 외곽선만 깜빡인다(1 Hz, 움직임 줄이기 설정이면 멈춘다). */
function UnackedMark() {
  return (
    <span className="relative rounded-[4px] px-1.5 py-px text-xs text-sev-critical">
      <span
        aria-hidden
        className="absolute inset-0 rounded-[4px] border border-sev-critical motion-safe:animate-alarm-blink"
      />
      미확인
    </span>
  );
}

/** 사건 상세 머리줄: 대상 · 등급 · 상태 · 꼬리표들 · 닫기. */
export function EventDetailHeader({
  incident,
  closeRef,
  onClose,
}: {
  incident: Incident;
  closeRef: Ref<HTMLButtonElement>;
  onClose: () => void;
}) {
  const events = incident.entries.map((e) => e.event);
  // 조치는 완료로 보고됐는데 관측상 아직 활성인 규칙(관리자 전용 토픽). 해소된 규칙은 보이지 않는다.
  const mismatches = useMismatches();
  const mismatched = events.some((e) => isActive(e) && mismatches[e.event_id] !== undefined);
  const confirm = events.find((e) => isActive(e) && needsConfirmation(e));

  return (
    <header className="flex flex-wrap items-center gap-x-3.5 gap-y-2.5">
      <h2 className="text-[22px] leading-[30px] font-bold">
        <span className="font-mono">{targetLabel(incident.target)}</span> 사건
      </h2>
      <SeverityBadge severity={incident.severity} />
      <Badge variant="secondary">{stateLabel(incident)}</Badge>
      {incident.alarming && <UnackedMark />}
      <span className="font-mono text-sm text-muted-foreground">
        발생 {formatTime(firstRaisedAt(incident))}
      </span>
      {mismatched && <Badge variant="outline">불일치 · 조치 완료, 관측상 위험 지속</Badge>}
      {confirm && <ConfirmBadge event={confirm} />}
      {events.some((e) => e.verification === 'UNVERIFIABLE') && (
        <Badge variant="outline">해소 확인 불가</Badge>
      )}
      {events.some((e) => e.delivery === 'DELAYED_ACTIVE') && <DelayedBadge />}
      <Button ref={closeRef} variant="secondary" size="sm" className="ml-auto" onClick={onClose}>
        닫기 ✕
      </Button>
    </header>
  );
}
