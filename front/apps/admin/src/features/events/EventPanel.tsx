import { EventDetail } from '@/features/events/EventDetail';
import {
  groupIncidents,
  severityCounts,
  type EventEntry,
  type Incident,
} from '@/features/events/events';
import { IncidentCard } from '@/features/events/IncidentCard';
import { cn } from '@simai/ui/lib/utils';
import { useEffect, useMemo } from 'react';

/** 해소된 묶음은 최근 것만 목록 아래에 남긴다. */
const RECENT_RESOLVED = 4;

function CountCell({
  glyph,
  label,
  count,
  tone,
}: {
  glyph?: string;
  label: string;
  count: number;
  tone: string;
}) {
  return (
    <div className="flex flex-col items-center gap-0.5 rounded-md bg-surface-2 py-1.5">
      <span className={cn('font-mono text-lg font-bold', count > 0 ? tone : 'text-faint')}>
        {count}
      </span>
      <span className="text-[11px] whitespace-nowrap text-muted-foreground">
        {glyph && <span aria-hidden>{glyph} </span>}
        {label}
      </span>
    </div>
  );
}

interface EventPanelProps {
  entries: EventEntry[];
  /** 목록(REST)을 받아 합쳤는지. false 면 STOMP 로 받은 것만 있다. */
  loaded: boolean;
  /** 목록을 아직 못 받았고 마지막 시도가 실패했다. */
  failed: boolean;
  selectedKey: string | null;
  onSelect: (incident: Incident) => void;
  openEventId: string | null;
  onOpen: (eventId: string | null) => void;
}

/** 실시간 관제 오른쪽 이벤트 패널: 등급별 수, 사람별 묶음 목록, 상세(명세 13.4, FR-4). */
export function EventPanel({
  entries,
  loaded,
  failed,
  selectedKey,
  onSelect,
  openEventId,
  onOpen,
}: EventPanelProps) {
  const counts = useMemo(() => severityCounts(entries), [entries]);
  const incidents = useMemo(() => {
    const all = groupIncidents(entries);
    return [
      ...all.filter((i) => i.active),
      ...all.filter((i) => !i.active).slice(0, RECENT_RESOLVED),
    ];
  }, [entries]);
  const opened = openEventId
    ? entries.find((entry) => entry.event.event_id === openEventId)
    : undefined;
  const openedMissing = openEventId !== null && !opened && loaded;

  // 연 이벤트가 목록에서 빠지면(오래된 해소 정리, 다른 계정) 상세를 닫는다. 나중에 같은 이벤트가 다시 와도 저절로 열리지 않게 한다.
  useEffect(() => {
    if (openedMissing) onOpen(null);
  }, [openedMissing, onOpen]);

  return (
    <aside
      aria-label="이벤트"
      className="absolute top-[72px] right-3.5 bottom-3.5 z-[5] flex w-[340px] flex-col gap-3 rounded-[16px] bg-card/94 p-3 shadow-float ring-1 ring-border backdrop-blur-[8px] ring-inset"
    >
      <div className="grid grid-cols-4 gap-1.5">
        <CountCell glyph="▲▲" label="CRITICAL" count={counts.critical} tone="text-sev-critical" />
        <CountCell glyph="▲" label="HIGH" count={counts.high} tone="text-sev-critical" />
        <CountCell glyph="!" label="CAUTION" count={counts.caution} tone="text-sev-caution" />
        <CountCell label="확인 필요" count={counts.needsCheck} tone="text-foreground" />
      </div>
      {failed && (
        <p role="alert" className="px-2 text-sm text-muted-foreground">
          이벤트 목록을 받지 못했습니다
        </p>
      )}
      <div className="min-h-0 flex-1 overflow-y-auto">
        {opened ? (
          <EventDetail entry={opened} onBack={() => onOpen(null)} />
        ) : incidents.length > 0 ? (
          <ul className="flex flex-col gap-2 p-0.5">
            {incidents.map((incident) => (
              <IncidentCard
                key={incident.key}
                incident={incident}
                selected={incident.key === selectedKey}
                onSelect={onSelect}
                onOpen={onOpen}
              />
            ))}
          </ul>
        ) : loaded ? (
          <p className="p-2 text-sm text-muted-foreground">활성 이벤트 없음</p>
        ) : (
          !failed && <p className="p-2 text-sm text-muted-foreground">불러오는 중</p>
        )}
      </div>
    </aside>
  );
}
