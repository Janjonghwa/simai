import { AckButton } from '@/features/events/AckButton';
import { ALARM_RING } from '@/features/events/alarmTone';
import { SEVERITY_RANK } from '@/features/events/constants';
import { isAlarming, type EventEntry } from '@/features/events/entries';
import { eventRuleName, eventShortSubject } from '@/features/events/format';
import { incidentTarget } from '@/features/events/incidents';
import { SeverityBadge } from '@/features/events/SeverityBadge';
import { Since } from '@/features/events/Since';
import type { EventItem } from '@/features/events/types';
import { useVisibleLiveEvents } from '@/features/events/useVisibleLiveEvents';
import { formatPersonFull, type PersonDisplayMap } from '@/features/people/personDisplay';
import { usePersonDisplayMap } from '@/features/people/usePersonDisplay';
import { useZoneName } from '@/features/zones/useZoneName';
import type { ZoneNameOf } from '@/features/zones/zoneName';
import { cn } from '@simai/ui/lib/utils';
import { useEffect, useMemo, useState } from 'react';

/** 배너에 먼저 뜨는 순서: 높은 등급, 같으면 최근 갱신. */
const byAlarmPriority = (a: EventEntry, b: EventEntry) =>
  SEVERITY_RANK[b.event.severity] - SEVERITY_RANK[a.event.severity] ||
  (b.event.updated_at ?? 0) - (a.event.updated_at ?? 0);

/**
 * 사건(굵게) / 사람(또는 구역) 두 줄. 사람 경보에 구역 이름을 붙이지 않는다(-798, 카드 제목과 같게).
 * 가장 위 경보가 바뀔 때만 읽는다(role=alert). 경과·버튼은 읽지 않는다.
 */
function AlarmHeadline({
  event,
  zoneName,
  personNames,
}: {
  event: EventItem;
  zoneName: ZoneNameOf;
  personNames: PersonDisplayMap;
}) {
  const target = incidentTarget(event);
  return (
    <span role="alert" className="flex min-w-0 flex-col">
      <span className="truncate text-[17px] font-bold">{eventRuleName(event)}</span>
      <span
        className="truncate text-sm text-muted-foreground"
        title={target?.type === 'person' ? formatPersonFull(target.id, personNames) : undefined}
      >
        {eventShortSubject(event, zoneName, personNames)}
      </span>
    </span>
  );
}

/**
 * 확인 대기 경보 배너(명세 FR-8). 경보음이 어느 화면에서나 울리므로 배너도 모든 화면 상단 바 아래에 뜬다.
 * 가장 높은 등급·최근 경보 하나를 큰 배지 · 사건/사람 · 경과(1초 갱신) · 큰 확인 단추 순서로 보인다.
 * 확인하거나 이벤트 패널에서 목록에서 숨기면 사라진다. 자리는 AppLayout 이 잡는다.
 * 소리를 켜고 끄는 버튼은 상단 바에 있다(SoundButton).
 */
export function AlarmBanner({
  holdOnExit = false,
}: {
  withPanel?: boolean;
  /** 닫힘 전환(150ms) 동안 내용을 잠깐 붙들어 둘지 여부. */
  holdOnExit?: boolean;
}) {
  const { entries } = useVisibleLiveEvents();
  const alarms = useMemo(() => entries.filter(isAlarming).sort(byAlarmPriority), [entries]);
  const zoneName = useZoneName();
  const personNames = usePersonDisplayMap();
  const current = alarms[0];
  const [held, setHeld] = useState<EventEntry | undefined>(current);
  if (current && current !== held) setHeld(current);

  useEffect(() => {
    if (current || !held) return;
    const timer = window.setTimeout(() => setHeld(undefined), holdOnExit ? 150 : 0);
    return () => window.clearTimeout(timer);
  }, [current, held, holdOnExit]);

  const top = current ?? (holdOnExit ? held : undefined);
  if (!top) return null;
  const { event } = top;
  const startedAt = top.stageStartedAt ?? event.raised_at ?? null;

  return (
    <div
      role="group"
      aria-label="확인 대기 경보"
      className="pointer-events-auto relative flex min-h-[62px] max-w-[680px] min-w-[440px] items-center gap-3 rounded-[12px] bg-card/94 px-4 py-2.5 shadow-float backdrop-blur-[8px]"
    >
      {/* 깜빡이는 테두리는 배너 전체를 덮으므로 클릭을 통과시킨다(막으면 아래 버튼이 눌리지 않는다). */}
      <span
        aria-hidden
        className={cn(
          'pointer-events-none absolute inset-0 rounded-[12px] ring-2 ring-inset motion-safe:animate-alarm-blink',
          ALARM_RING[event.severity],
        )}
      />
      <SeverityBadge severity={event.severity} size="large" />
      <span className="flex min-w-0 flex-col">
        <span className="flex min-w-0 items-center gap-2">
          <AlarmHeadline event={event} zoneName={zoneName} personNames={personNames} />
        </span>
      </span>
      <span className="ml-auto flex shrink-0 items-center gap-2">
        {startedAt !== null && (
          <Since raisedAt={startedAt} className="text-sm text-muted-foreground tabular-nums" />
        )}
        <AckButton
          entry={top}
          label="확인"
          size="default"
          zoneName={zoneName}
          personNames={personNames}
        />
      </span>
    </div>
  );
}
