import { AckButton } from '@/features/events/AckButton';
import { CameraButton } from '@/features/events/CameraButton';
import { SEVERITY_RANK } from '@/features/events/constants';
import { isAlarming, type EventEntry } from '@/features/events/entries';
import { useEventsStore } from '@/features/events/events.store';
import { entryEtaLine, eventSubject, ruleName } from '@/features/events/format';
import { incidentTarget } from '@/features/events/incidents';
import { SeverityBadge } from '@/features/events/SeverityBadge';
import { Since } from '@/features/events/Since';
import type { EventItem } from '@/features/events/types';
import { useLiveEvents } from '@/features/events/useLiveEvents';
import { Button } from '@simai/ui/components/button';
import { useNavigate } from '@tanstack/react-router';
import { useMemo } from 'react';

/** 배너에 먼저 뜨는 순서: 높은 등급, 같으면 최근 갱신. */
const byAlarmPriority = (a: EventEntry, b: EventEntry) =>
  SEVERITY_RANK[b.event.severity] - SEVERITY_RANK[a.event.severity] ||
  (b.event.updated_at ?? 0) - (a.event.updated_at ?? 0);

/**
 * 규칙 · 대상 · 구역. 가장 위 경보가 바뀔 때만 읽는다(role=alert). 경과·버튼·건수는 읽지 않는다.
 * 칸 사이 공백은 화면 낭독기가 낱말을 붙여 읽지 않게 둔다.
 */
function AlarmHeadline({ event }: { event: EventItem }) {
  return (
    <span role="alert" className="min-w-0 truncate font-medium">
      <span>{ruleName(event.rule_id)}</span> ·{' '}
      <span className="font-mono">{eventSubject(event)}</span>
      {event.zone_id && event.world_ids[0] && (
        <>
          {' '}
          · <span>{event.zone_id}</span>
        </>
      )}
    </span>
  );
}

/** 진입 예측 근거와 발생 뒤 경과. 둘 다 없으면 줄이 없다. */
function AlarmSubline({ event }: { event: EventItem }) {
  const eta = entryEtaLine(event);
  const { raised_at: raisedAt } = event;
  if (!eta && raisedAt === undefined) return null;
  return (
    <span className="truncate text-sm">
      {eta}
      {eta && raisedAt !== undefined && ' · '}
      {raisedAt !== undefined && <Since raisedAt={raisedAt} className="text-muted-foreground" />}
    </span>
  );
}

/**
 * 확인 대기 경보 배너(명세 FR-8). 경보음이 어느 화면에서나 울리므로 배너도 모든 화면 상단 바 아래에 뜬다.
 * 가장 높은 등급·최근 경보 하나를 이벤트 카드와 같은 규칙으로 보인다: 등급 · 규칙 · 대상 · 구역, 발생 뒤 경과,
 * 버튼(상세 · 카메라 · 확인). 다른 확인 대기 경보가 더 있으면 +N. 테두리가 깜빡이고, 확인하면 사라진다.
 * 자리는 레이아웃(AppLayout)이 장애 띠 아래로 잡는다.
 * withPanel: 이 화면 오른쪽에 이벤트 패널이 있다(실시간 관제). 「상세」를 그 패널에서 열고 카메라 버튼도 둔다.
 * 소리를 켜고 끄는 버튼은 상단 바에 있다(SoundButton).
 */
export function AlarmBanner({ withPanel }: { withPanel: boolean }) {
  const { entries } = useLiveEvents();
  const alarms = useMemo(() => entries.filter(isAlarming).sort(byAlarmPriority), [entries]);
  const openEvent = useEventsStore((s) => s.openEvent);
  const navigate = useNavigate();
  const top = alarms[0];
  if (!top) return null;
  const { event } = top;
  const more = alarms.length - 1;

  const showDetail = () => {
    openEvent(event.event_id);
    if (!withPanel) void navigate({ to: '/' });
  };

  return (
    <div
      role="group"
      aria-label="확인 대기 경보"
      className="pointer-events-auto relative flex max-w-[680px] items-center gap-3 rounded-[12px] bg-card/94 px-4 py-2.5 shadow-float backdrop-blur-[8px]"
    >
      {/* 깜빡이는 테두리는 배너 전체를 덮으므로 클릭을 통과시킨다(막으면 아래 버튼이 눌리지 않는다). */}
      <span
        aria-hidden
        className="pointer-events-none absolute inset-0 rounded-[12px] ring-2 ring-sev-critical ring-inset motion-safe:animate-alarm-blink"
      />
      <SeverityBadge severity={event.severity} />
      <span className="flex min-w-0 flex-col">
        <span className="flex min-w-0 items-center gap-2">
          <AlarmHeadline event={event} />
          {more > 0 && (
            <span className="text-sm whitespace-nowrap text-muted-foreground">
              <span aria-hidden>+{more}</span>
              <span className="sr-only">외 {more}건</span>
            </span>
          )}
        </span>
        <AlarmSubline event={event} />
      </span>
      <span className="ml-auto flex items-center gap-2">
        <Button variant="secondary" size="sm" onClick={showDetail}>
          상세
        </Button>
        {withPanel && <CameraButton target={incidentTarget(event)} variant="banner" />}
        <AckButton entry={top} label="확인" />
      </span>
    </div>
  );
}
