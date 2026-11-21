import { eventRuleName, formatTime } from '@/features/events/format';
import { topEntry, type Incident } from '@/features/events/incidents';
import { targetShortLabel } from '@/features/events/incidentView';
import { SeverityBadge } from '@/features/events/SeverityBadge';
import { usePersonDisplayMap } from '@/features/people/usePersonDisplay';
import { useZoneName } from '@/features/zones/useZoneName';
import { Button } from '@simai/ui/components/button';
import type { Ref } from 'react';

export function EventDetailHeader({
  incident,
  closeRef,
  onClose,
}: {
  incident: Incident;
  closeRef: Ref<HTMLButtonElement>;
  onClose: () => void;
}) {
  const zoneName = useZoneName();
  const personNames = usePersonDisplayMap();
  const entry = topEntry(incident);
  const { event } = entry;
  const zone = event.zone_id ? zoneName(event.zone_id) : null;
  return (
    <header className="flex flex-col gap-2">
      <div className="flex items-center gap-2">
        <SeverityBadge severity={event.severity} />
        <h2 className="text-xl font-bold">{eventRuleName(event)}</h2>
        <Button ref={closeRef} variant="secondary" size="sm" className="ml-auto" onClick={onClose}>
          닫기 ✕
        </Button>
      </div>
      <p className="text-sm text-muted-foreground">
        {[
          targetShortLabel(incident.target, zoneName, personNames),
          zone,
          formatTime(event.raised_at),
          // 사이드 카드·상세에는 확인 개념을 두지 않는다(확인은 위쪽 배너를 내리는 용도, S15P31A101-838 후속). 상태는 진행 중·해소만
          event.lifecycle === 'RESOLVED'
            ? `해소${event.resolved_at ? ` ${formatTime(event.resolved_at)}` : ''}`
            : '진행 중',
        ]
          .filter(Boolean)
          .join(' · ')}
      </p>
    </header>
  );
}
