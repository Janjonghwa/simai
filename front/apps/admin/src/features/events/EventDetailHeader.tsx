import { acknowledgedAt, isUnacknowledgedResolved } from '@/features/events/entries';
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
  const ackedAt =
    event.lifecycle === 'RESOLVED'
      ? Math.max(0, ...(event.acks ?? []).map((ack) => ack.acked_at)) || null
      : acknowledgedAt(entry);
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
          isUnacknowledgedResolved(event)
            ? '해소 · 미확인'
            : event.severity === 'HIGH' || event.severity === 'CAUTION'
              ? '안내'
              : ackedAt !== null
                ? `확인됨 ${formatTime(ackedAt).slice(0, 5)}`
                : '확인 대기',
        ]
          .filter(Boolean)
          .join(' · ')}
      </p>
    </header>
  );
}
