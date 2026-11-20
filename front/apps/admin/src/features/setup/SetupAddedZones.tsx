import { SeverityBadge } from '@/features/events/SeverityBadge';
import { planItemViewId, type SetupZonePlanView } from '@/features/setup/setupZonePlan';
import { useSetupZoneReview } from '@/features/setup/useSetupZoneReview';
import type { Zone } from '@/features/zones/types';
import { X } from 'lucide-react';

/** AI 초안이 아닌 세션 구역과 이전 맵에서 이어 온 구역을 보여 준다. */
export function SetupAddedZones({
  sessionId,
  plan,
  zones,
  onHover,
}: {
  sessionId: string;
  plan: SetupZonePlanView | undefined;
  zones: readonly Zone[];
  onHover: (zone: Zone | null) => void;
}) {
  const review = useSetupZoneReview(sessionId);
  const items = plan?.items ?? [];
  const rows = items.flatMap((item, index) => {
    if (item.source === 'AI' && !item.zone_id) return [];
    const zone = zones.find((z) => z.zoneId === planItemViewId(items, index));
    return zone ? [{ zone, previous: item.zone_id !== null }] : [];
  });
  if (!rows.length) return null;
  return (
    <section className="border-t border-dashed border-input/45 pt-3" aria-label="사용자 추가 구역">
      <h3 className="mb-2 text-sm font-semibold">사용자 추가 구역</h3>
      <ul className="flex flex-col gap-2">
        {rows.map(({ zone, previous }) => (
          <li
            key={zone.zoneId}
            onMouseEnter={() => onHover(zone)}
            onMouseLeave={() => onHover(null)}
            className="flex items-center gap-2 rounded-lg border border-border bg-surface-2 px-3 py-2 text-sm"
          >
            <div className="flex min-w-0 flex-1 flex-wrap items-center gap-1.5">
              <strong>{zone.name}</strong>
              <SeverityBadge severity={zone.severity} />
              {previous && <span className="text-xs text-muted-foreground">이전 맵</span>}
            </div>
            <button
              type="button"
              title="구역 빼기"
              aria-label={`${zone.name} 구역 빼기`}
              disabled={review.busy}
              onClick={() => {
                onHover(null);
                review.remove(zone.zoneId);
              }}
              className="shrink-0 rounded p-1 text-muted-foreground hover:bg-surface-3"
            >
              <X className="size-4" aria-hidden />
            </button>
          </li>
        ))}
      </ul>
      {review.error && (
        <p role="alert" className="mt-2 text-xs text-delay">
          {review.error}
        </p>
      )}
    </section>
  );
}
