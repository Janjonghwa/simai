import { RECENT_RESOLVED_INCIDENTS } from '@/features/events/constants';
import type { EventEntry } from '@/features/events/entries';
import { EventDetail } from '@/features/events/EventDetail';
import { IncidentCard } from '@/features/events/IncidentCard';
import { groupIncidents, incidentOf, type Incident } from '@/features/events/incidents';
import { useMismatches } from '@/features/events/mismatch';
import { movePanel, PANEL_GAP, PANEL_TOP, type PanelBox } from '@/features/events/panelBox';
import { PanelGrip } from '@/features/events/PanelGrip';
import { measure, type Measured } from '@/features/events/panelMeasure';
import { useAutoFold } from '@/features/events/useAutoFold';
import { useFloatingBox } from '@/features/events/useFloatingBox';
import { usePanelDrag } from '@/features/events/usePanelDrag';
import { useZones } from '@/features/zones/useZones';
import { Button } from '@simai/ui/components/button';
import { cn } from '@simai/ui/lib/utils';
import { useCallback, useEffect, useMemo, useRef, type CSSProperties } from 'react';

/** 기본 자리: 오른쪽 위(PANEL_TOP·PANEL_GAP 과 같다), 폭 340px. 상단 알림이 비키는 간격(panelBox)도 이 폭을 전제한다. */
const PANEL_CLASS =
  'absolute top-[72px] right-3.5 bottom-3.5 z-[5] flex w-[340px] flex-col gap-2 rounded-[16px] bg-card/82 p-2.5 shadow-float ring-1 ring-border backdrop-blur-[8px] ring-inset';

const NOTE_CLASS = 'mx-2 my-4 text-sm text-faint';

/** 진행 중 묶음 전부와 최근 해소된 묶음 몇 건(진행 중이 앞선다). */
function visibleIncidents(entries: readonly EventEntry[]): Incident[] {
  const all = groupIncidents(entries);
  return [
    ...all.filter((i) => i.active),
    ...all.filter((i) => !i.active).slice(0, RECENT_RESOLVED_INCIDENTS),
  ];
}

/** 옮긴 자리의 인라인 스타일. 접으면 높이는 머리줄만큼이다. */
const boxStyle = (box: PanelBox, folded: boolean): CSSProperties => ({
  left: box.left,
  top: box.top,
  width: box.width,
  height: folded ? undefined : box.height,
  right: 'auto',
  bottom: 'auto',
});

function FoldButton({ folded, onToggle }: { folded: boolean; onToggle: () => void }) {
  return (
    <button
      type="button"
      aria-expanded={!folded}
      aria-label={folded ? '이벤트 펼치기' : '이벤트 접기'}
      onClick={onToggle}
      className="inline-flex size-8 items-center justify-center rounded-md text-muted-foreground hover:bg-surface-2 hover:text-foreground"
    >
      <span
        aria-hidden
        className={cn('inline-block transition-transform duration-200', folded && 'rotate-180')}
      >
        ▾
      </span>
    </button>
  );
}

interface EventListProps {
  incidents: Incident[];
  loaded: boolean;
  failed: boolean;
  selectedKey: string | null;
  zoneName: (zoneId: string) => string | undefined;
  onSelect: (incident: Incident) => void;
  onOpen: (eventId: string) => void;
}

/** 묶음 카드 목록. 비었으면 상태(없음·받는 중)를 한 줄로 보인다. 받지 못했으면 패널 위의 실패 문구가 대신한다. */
function EventList({ incidents, loaded, failed, selectedKey, ...card }: EventListProps) {
  const mismatches = useMismatches();
  if (incidents.length > 0) {
    return (
      <ul className="flex flex-col gap-2 p-0.5">
        {incidents.map((incident) => (
          <IncidentCard
            key={incident.key}
            incident={incident}
            selected={incident.key === selectedKey}
            mismatches={mismatches}
            {...card}
          />
        ))}
      </ul>
    );
  }
  if (loaded) return <p className={NOTE_CLASS}>활성 이벤트 없음</p>;
  return failed ? null : <p className={NOTE_CLASS}>불러오는 중…</p>;
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
  /** 「모두 지우기」. 화면에서만 지우고 서버는 건드리지 않는다. */
  onClearAll: () => void;
}

/**
 * 실시간 관제 오른쪽 이벤트 패널: 사람별 묶음 목록(명세 13.4, FR-4)과 사건 상세 창.
 * 머리줄을 끌어 옮기고(두 번 누르면 제자리), 가장자리 손잡이로 크기를 바꾸고, ▾ 로 접는다(관제 데모와 같다).
 * 보이는 묶음이 있으면 머리줄에 「모두 지우기」가 있다.
 */
export function EventPanel({
  entries,
  loaded,
  failed,
  selectedKey,
  onSelect,
  openEventId,
  onOpen,
  onClearAll,
}: EventPanelProps) {
  const incidents = useMemo(() => visibleIncidents(entries), [entries]);
  const activeCount = incidents.filter((i) => i.active).length;
  const { zones } = useZones();
  const zoneName = useCallback((id: string) => zones.find((z) => z.zoneId === id)?.name, [zones]);
  const opened = useMemo(
    () => (openEventId ? incidentOf(entries, openEventId) : undefined),
    [entries, openEventId],
  );
  const openedMissing = openEventId !== null && !opened && loaded;

  // 연 이벤트가 목록에서 빠지면(오래된 해소 정리, 다른 계정) 상세를 닫는다. 나중에 같은 이벤트가 다시 와도 저절로 열리지 않게 한다.
  useEffect(() => {
    if (openedMissing) onOpen(null);
  }, [openedMissing, onOpen]);

  const panel = useRef<HTMLElement>(null);
  const { box, setBox, reset } = useFloatingBox(panel);
  // 목록을 받았는데 진행·최근 해소가 모두 0이면 접는다. 목록을 못 받았거나 받는 중이면 접지 않는다(실패 문구를 가리지 않는다).
  const { folded, setFolded } = useAutoFold(loaded && incidents.length === 0);
  // 끌기 시작 자리. 접힌 패널은 머리줄 높이로 재지므로 펼친 높이(옮긴 높이, 없으면 기본 자리 높이)로 바꾼다.
  const startOf = (): Measured => {
    const now = measure(panel.current);
    if (!now || !folded) return now;
    const height = box?.height ?? now.bounds.height - PANEL_TOP - PANEL_GAP;
    return { ...now, box: { ...now.box, height } };
  };
  const move = usePanelDrag(startOf, (start, dx, dy, bounds) =>
    setBox(movePanel(start, dx, dy, bounds)),
  );

  return (
    <>
      <aside
        ref={panel}
        aria-label="이벤트"
        style={box ? boxStyle(box, folded) : undefined}
        className={cn(PANEL_CLASS, folded && 'bottom-auto')}
      >
        <div
          data-testid="event-panel-head"
          {...move.handlers}
          onDoubleClick={(e) => {
            if (!(e.target as HTMLElement).closest('button')) reset();
          }}
          className={cn(
            'flex min-h-9 touch-none items-center gap-2 rounded-md pr-0.5 pl-2 select-none',
            move.active ? 'cursor-grabbing' : 'cursor-grab',
          )}
        >
          <b className="text-[15px]">
            이벤트<span className="ml-1 font-mono">{activeCount}</span>
          </b>
          <div className="ml-auto flex items-center gap-1">
            {incidents.length > 0 && (
              <Button variant="secondary" size="sm" onClick={onClearAll}>
                모두 지우기
              </Button>
            )}
            <FoldButton folded={folded} onToggle={() => setFolded(!folded)} />
          </div>
        </div>
        {!folded && (
          <>
            {failed && (
              <p role="alert" className="px-2 text-sm text-muted-foreground">
                이벤트 목록을 받지 못했습니다
              </p>
            )}
            <div className="min-h-0 flex-1 overflow-y-auto">
              <EventList
                incidents={incidents}
                loaded={loaded}
                failed={failed}
                selectedKey={selectedKey}
                zoneName={zoneName}
                onSelect={onSelect}
                onOpen={onOpen}
              />
            </div>
            <PanelGrip edge="y" startOf={startOf} onResize={setBox} onReset={reset} />
            <PanelGrip edge="xy" startOf={startOf} onResize={setBox} onReset={reset} />
          </>
        )}
        <PanelGrip edge="x" startOf={startOf} onResize={setBox} onReset={reset} />
      </aside>
      {opened && openEventId && (
        <EventDetail incident={opened} openEventId={openEventId} onClose={() => onOpen(null)} />
      )}
    </>
  );
}
