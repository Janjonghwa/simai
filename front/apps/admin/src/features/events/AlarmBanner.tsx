import { useCanAck } from '@/features/events/ack';
import { eventSubject, isAlarming, ruleName, SEVERITY_RANK } from '@/features/events/events';
import { useEventsStore } from '@/features/events/events.store';
import { AckButton } from '@/features/events/IncidentCard';
import { SeverityBadge } from '@/features/events/SeverityBadge';
import { useAlarmSoundReady } from '@/features/events/useAlarmSound';
import { useLiveEvents } from '@/features/events/useLiveEvents';
import { alarmSound } from '@/lib/alarmSound';
import { Button } from '@simai/ui/components/button';
import { cn } from '@simai/ui/lib/utils';
import { useNavigate } from '@tanstack/react-router';
import { Volume2 } from 'lucide-react';
import { useMemo } from 'react';

/**
 * 확인 대기 경보 배너(명세 FR-8). 경보음이 어느 화면에서나 울리므로 배너도 모든 화면 상단 바 아래에 뜬다.
 * 가장 높은 등급·최근 경보를 보이고, 테두리가 깜빡인다. 확인하면 사라진다.
 * withPanel: 이 화면 오른쪽에 이벤트 패널이 있다(실시간 관제). 배너가 패널 자리를 비키고, 「상세」를 그 패널에서 연다.
 */
export function AlarmBanner({ withPanel }: { withPanel: boolean }) {
  const { entries } = useLiveEvents();
  const alarms = useMemo(
    () =>
      entries
        .filter(isAlarming)
        .sort(
          (a, b) =>
            SEVERITY_RANK[b.event.severity] - SEVERITY_RANK[a.event.severity] ||
            (b.event.updated_at ?? 0) - (a.event.updated_at ?? 0),
        ),
    [entries],
  );
  const soundReady = useAlarmSoundReady();
  const canAck = useCanAck();
  const openEvent = useEventsStore((s) => s.openEvent);
  const navigate = useNavigate();
  const top = alarms[0];
  if (!top) return null;
  const { event } = top;
  const subject = eventSubject(event);

  const showDetail = () => {
    openEvent(event.event_id);
    if (!withPanel) void navigate({ to: '/' });
  };

  return (
    <div
      className={cn(
        'pointer-events-none absolute inset-x-5 top-[72px] z-[6] flex justify-center',
        withPanel && 'right-[370px]',
      )}
    >
      <div
        role="group"
        aria-label="확인 대기 경보"
        className="pointer-events-auto relative flex max-w-[680px] items-center gap-3 rounded-[12px] bg-card/94 px-4 py-2.5 shadow-float backdrop-blur-[8px]"
      >
        <span
          aria-hidden
          className="absolute inset-0 rounded-[12px] ring-2 ring-sev-critical ring-inset motion-safe:animate-alarm-blink"
        />
        <SeverityBadge severity={event.severity} />
        {/* 가장 위 경보가 바뀔 때만 읽는다. 버튼·건수는 읽지 않는다. */}
        <span role="alert" className="min-w-0 truncate font-medium">
          {ruleName(event.rule_id)} · {subject}
          {event.zone_id && event.world_ids[0] ? ` · ${event.zone_id}` : ''}
        </span>
        {alarms.length > 1 && (
          <span className="text-sm whitespace-nowrap text-muted-foreground">
            외 {alarms.length - 1}건
          </span>
        )}
        {/* 새로고침 뒤 아직 아무것도 누르지 않았거나 소리가 멈췄으면 브라우저가 막고 있다. 열람 계정은 소리를 내지 않는다. */}
        {canAck && !soundReady && (
          <Button variant="secondary" size="sm" onClick={() => void alarmSound.unlock()}>
            <Volume2 aria-hidden />
            소리 켜기
          </Button>
        )}
        <Button variant="secondary" size="sm" onClick={showDetail}>
          상세
        </Button>
        <AckButton entry={top} label="경보 확인(ACK)" />
      </div>
    </div>
  );
}
