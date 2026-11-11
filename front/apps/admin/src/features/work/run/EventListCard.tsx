import { formatTime } from '@/features/events/events';
import { SeverityBadge } from '@/features/events/SeverityBadge';
import type { EventItem } from '@/features/events/types';
import { useLiveEvents } from '@/features/events/useLiveEvents';
import { workEventsOf } from '@/features/work/run/run';
import { useZoneName } from '@/features/zones/useZoneName';
import { zoneNameOrDefault, type ZoneNameOf } from '@/features/zones/zoneName';
import { Card, CardContent, CardHeader, CardTitle } from '@simai/ui/components/card';
import { useMemo } from 'react';

// 작업 종료 단계 오른쪽 가운데 카드: 작업을 시작한 뒤 난 사건(위험·경고)을 최근 것부터. 구역을 모르는 사건은 '현장'.

/** 구역을 알 수 없는 사건에 쓰는 장소 이름. */
const NO_ZONE_NAME = '현장';

function EventRow({ event, zoneName }: { event: EventItem; zoneName: ZoneNameOf }) {
  return (
    <li className="grid grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-3 py-2.5 first:pt-0 last:pb-0">
      <span className="font-mono tabular-nums">{formatTime(event.raised_at)}</span>
      <span className="truncate font-semibold">
        {event.zone_id ? zoneNameOrDefault(event.zone_id, zoneName) : NO_ZONE_NAME}
      </span>
      <SeverityBadge severity={event.severity} />
    </li>
  );
}

/** 사건. startedAt 은 작업을 시작한 시각(EC2 ms)이고, 없으면 사건이 없는 것으로 본다. */
export function EventListCard({ startedAt }: { startedAt: number | null }) {
  const { entries } = useLiveEvents();
  const zoneName = useZoneName();
  const events = useMemo(() => workEventsOf(entries, startedAt), [entries, startedAt]);
  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle className="flex items-center justify-between gap-2">
          <h2>사건</h2>
          <span className="text-sm font-normal text-muted-foreground tabular-nums">
            {events.length}건
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent>
        {events.length === 0 ? (
          <p className="text-muted-foreground">사건 없음</p>
        ) : (
          <ul
            aria-label="사건 목록"
            className="flex max-h-[260px] flex-col divide-y divide-border overflow-auto"
          >
            {events.map((event) => (
              <EventRow key={event.event_id} event={event} zoneName={zoneName} />
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
