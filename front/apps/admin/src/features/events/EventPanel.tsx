import { EventDetail } from '@/features/events/EventDetail';
import {
  groupIncidents,
  incidentOf,
  type EventEntry,
  type Incident,
} from '@/features/events/events';
import { useEventsStore } from '@/features/events/events.store';
import { IncidentCard } from '@/features/events/IncidentCard';
import {
  fitPanel,
  movePanel,
  PANEL_GAP,
  PANEL_TOP,
  resizePanel,
  type Bounds,
  type PanelBox,
  type PanelEdge,
} from '@/features/events/panelBox';
import { cn } from '@simai/ui/lib/utils';
import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from 'react';

/** 해소된 묶음은 최근 것만 목록 아래에 남긴다. */
const RECENT_RESOLVED = 4;

interface Drag {
  x: number;
  y: number;
  start: PanelBox;
  bounds: Bounds;
}

type Measured = { box: PanelBox; bounds: Bounds } | null;

/** 패널의 지금 자리와 부모(장면) 크기. */
function measure(el: HTMLElement | null): Measured {
  const parent = el?.offsetParent;
  if (!el || !(parent instanceof HTMLElement)) return null;
  return {
    box: { left: el.offsetLeft, top: el.offsetTop, width: el.offsetWidth, height: el.offsetHeight },
    bounds: { width: parent.clientWidth, height: parent.clientHeight },
  };
}

/** 누른 채 끄는 동안 시작 자리에서 움직인 만큼 onMove 를 부른다. 안의 버튼(접기)은 끌기로 잡지 않는다. */
function useDrag(
  startOf: () => Measured,
  onMove: (start: PanelBox, dx: number, dy: number, bounds: Bounds) => void,
) {
  const drag = useRef<Drag | null>(null);
  const [active, setActive] = useState(false);
  const end = () => {
    drag.current = null;
    setActive(false);
  };
  return {
    active,
    handlers: {
      onPointerDown: (e: ReactPointerEvent<HTMLElement>) => {
        if (e.button !== 0 || (e.target as HTMLElement).closest('button')) return;
        const now = startOf();
        if (!now) return;
        e.preventDefault();
        e.currentTarget.setPointerCapture?.(e.pointerId);
        drag.current = { x: e.clientX, y: e.clientY, start: now.box, bounds: now.bounds };
        setActive(true);
      },
      onPointerMove: (e: ReactPointerEvent<HTMLElement>) => {
        const d = drag.current;
        if (d) onMove(d.start, e.clientX - d.x, e.clientY - d.y, d.bounds);
      },
      onPointerUp: end,
      onPointerCancel: end,
    },
  };
}

/** 크기 손잡이(왼쪽 가장자리·아래 가장자리·왼쪽 아래 모서리). 두 번 누르면 기본 자리·크기로 돌아간다. */
function Grip({
  edge,
  startOf,
  onResize,
  onReset,
}: {
  edge: PanelEdge;
  startOf: () => Measured;
  onResize: (box: PanelBox) => void;
  onReset: () => void;
}) {
  const { active, handlers } = useDrag(startOf, (start, dx, dy, bounds) =>
    onResize(resizePanel(start, edge, dx, dy, bounds)),
  );
  return (
    <div
      aria-hidden
      data-grip={edge}
      {...handlers}
      onDoubleClick={onReset}
      className={cn(
        'absolute z-[2] touch-none rounded-[6px] hover:bg-primary/35',
        active && 'bg-primary/35',
        edge === 'x' && 'top-3.5 bottom-3.5 -left-1.5 w-2.5 cursor-ew-resize',
        edge === 'y' && 'right-3.5 -bottom-1.5 left-3.5 h-2.5 cursor-ns-resize',
        edge === 'xy' &&
          '-bottom-1.5 -left-1.5 size-[18px] cursor-nesw-resize after:absolute after:inset-[5px] after:rounded-bl-[4px] after:border-b-2 after:border-l-2 after:border-faint',
      )}
    />
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

/**
 * 실시간 관제 오른쪽 이벤트 패널: 사람별 묶음 목록(명세 13.4, FR-4)과 사건 상세 창.
 * 머리줄을 끌어 옮기고(두 번 누르면 제자리), 가장자리 손잡이로 크기를 바꾸고, ▾ 로 접는다(관제 데모와 같다).
 */
export function EventPanel({
  entries,
  loaded,
  failed,
  selectedKey,
  onSelect,
  openEventId,
  onOpen,
}: EventPanelProps) {
  const incidents = useMemo(() => {
    const all = groupIncidents(entries);
    return [
      ...all.filter((i) => i.active),
      ...all.filter((i) => !i.active).slice(0, RECENT_RESOLVED),
    ];
  }, [entries]);
  const activeCount = incidents.filter((i) => i.active).length;
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
  // null 이면 오른쪽 기본 자리다. 끌거나 크기를 바꾸면 그 자리를 갖는다.
  const [box, setBox] = useState<PanelBox | null>(null);
  const [folded, setFolded] = useState(false);
  // 끌기 시작 자리. 접힌 패널은 머리줄 높이로 재지므로 펼친 높이(옮긴 높이, 없으면 기본 자리 높이)로 바꾼다.
  const startOf = (): Measured => {
    const now = measure(panel.current);
    if (!now || !folded) return now;
    const height = box?.height ?? now.bounds.height - PANEL_TOP - PANEL_GAP;
    return { ...now, box: { ...now.box, height } };
  };
  const move = useDrag(startOf, (start, dx, dy, bounds) =>
    setBox(movePanel(start, dx, dy, bounds)),
  );
  const reset = () => setBox(null);

  // 옮긴 패널의 가로 자리를 알린다(상단 알림이 비킨다). 기본 자리로 돌아가거나 패널이 사라지면 지운다.
  const setPanelSpan = useEventsStore((s) => s.setPanelSpan);
  useLayoutEffect(() => {
    const now = box && measure(panel.current);
    setPanelSpan(
      box && now
        ? { left: box.left, width: box.width, right: now.bounds.width - box.left - box.width }
        : null,
    );
  }, [box, setPanelSpan]);
  useEffect(() => () => setPanelSpan(null), [setPanelSpan]);

  // 창 크기가 바뀌면 옮긴 패널이 화면 밖으로 나가지 않게 다시 맞춘다.
  const moved = box !== null;
  useEffect(() => {
    if (!moved) return undefined;
    const refit = () => {
      const now = measure(panel.current);
      if (now) setBox((prev) => (prev ? fitPanel(prev, now.bounds) : prev));
    };
    window.addEventListener('resize', refit);
    return () => window.removeEventListener('resize', refit);
  }, [moved]);

  return (
    <>
      <aside
        ref={panel}
        aria-label="이벤트"
        style={
          box
            ? {
                left: box.left,
                top: box.top,
                width: box.width,
                height: folded ? undefined : box.height,
                right: 'auto',
                bottom: 'auto',
              }
            : undefined
        }
        className={cn(
          'absolute top-[72px] right-3.5 bottom-3.5 z-[5] flex w-[340px] flex-col gap-2 rounded-[16px] bg-card/82 p-2.5 shadow-float ring-1 ring-border backdrop-blur-[8px] ring-inset',
          folded && 'bottom-auto',
        )}
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
          <button
            type="button"
            aria-expanded={!folded}
            aria-label={folded ? '이벤트 펼치기' : '이벤트 접기'}
            onClick={() => setFolded((f) => !f)}
            className="ml-auto inline-flex size-8 items-center justify-center rounded-md text-muted-foreground hover:bg-surface-2 hover:text-foreground"
          >
            <span
              aria-hidden
              className={cn(
                'inline-block transition-transform duration-200',
                folded && 'rotate-180',
              )}
            >
              ▾
            </span>
          </button>
        </div>
        {!folded && (
          <>
            {failed && (
              <p role="alert" className="px-2 text-sm text-muted-foreground">
                이벤트 목록을 받지 못했습니다
              </p>
            )}
            <div className="min-h-0 flex-1 overflow-y-auto">
              {incidents.length > 0 ? (
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
                <p className="mx-2 my-4 text-sm text-faint">활성 이벤트 없음</p>
              ) : (
                !failed && <p className="mx-2 my-4 text-sm text-faint">불러오는 중…</p>
              )}
            </div>
            <Grip edge="y" startOf={startOf} onResize={setBox} onReset={reset} />
            <Grip edge="xy" startOf={startOf} onResize={setBox} onReset={reset} />
          </>
        )}
        <Grip edge="x" startOf={startOf} onResize={setBox} onReset={reset} />
      </aside>
      {opened && openEventId && (
        <EventDetail incident={opened} openEventId={openEventId} onClose={() => onOpen(null)} />
      )}
    </>
  );
}
