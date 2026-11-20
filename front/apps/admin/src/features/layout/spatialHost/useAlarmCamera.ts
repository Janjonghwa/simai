import { SEVERITY_RANK } from '@/features/events/constants';
import {
  eventTime,
  isAlarmSeverity,
  isCurrent,
  mergeEvent,
  opensNewStage,
  type EventEntry,
} from '@/features/events/entries';
import { bestCamera, incidentTarget } from '@/features/events/incidents';
import { useLiveEvents } from '@/features/events/useLiveEvents';
import { useRealtimeStore } from '@/store/realtime.store';
import { useCallback, useEffect, useRef } from 'react';

/** 이전 목록과 비교하고 관측한 이벤트는 창이 닫혀 있어도 기억한다. */
export function newAlarmStages(
  known: Map<string, EventEntry>,
  entries: readonly EventEntry[],
): EventEntry[] {
  const opened: EventEntry[] = [];
  for (const entry of entries) {
    const prev = known.get(entry.event.event_id);
    const merged = mergeEvent(prev, entry.event);
    known.set(entry.event.event_id, merged);
    if (
      (!prev || opensNewStage(prev.event, merged.event)) &&
      isCurrent(merged.event) &&
      isAlarmSeverity(merged.event.severity) &&
      merged.event.delivery !== 'AUDIT_ONLY'
    ) {
      opened.push(merged);
    }
  }
  return opened.sort(
    (a, b) =>
      SEVERITY_RANK[b.event.severity] - SEVERITY_RANK[a.event.severity] ||
      (b.stageStartedAt ?? eventTime(b.event) ?? 0) - (a.stageStartedAt ?? eventTime(a.event) ?? 0),
  );
}

export function useAlarmCamera({
  enabled,
  now,
  onCamera,
}: {
  enabled: boolean;
  now: () => number;
  onCamera: (cameraId: string) => void;
}) {
  const { entries, loaded } = useLiveEvents();
  const known = useRef(new Map<string, EventEntry>());
  const initialized = useRef(false);
  const lastManual = useRef<number | null>(null);
  const markManual = useCallback(() => {
    lastManual.current = now();
  }, [now]);
  useEffect(() => {
    if (!loaded) return;
    const opened = newAlarmStages(known.current, entries);
    if (!initialized.current) {
      initialized.current = true;
      return;
    }
    if (!enabled || (lastManual.current !== null && now() - lastManual.current < 15_000)) return;
    const first = opened[0];
    if (!first) return;
    const camera = bestCamera(
      useRealtimeStore.getState().world?.payload.tracks,
      incidentTarget(first.event),
    );
    if (camera) onCamera(camera);
  }, [entries, loaded, enabled, now, onCamera]);
  return { markManual };
}
