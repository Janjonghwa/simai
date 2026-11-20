import { formatTime, ruleName } from '@/features/events/events';
import type { Severity } from '@/features/events/types';
import type { EventRow } from '@/features/history/replayIndex';
import type { Span } from '@/features/history/types';
import { formatClock } from '@/features/history/works';
import type { ReplayStatus } from '@/features/viewer3d';
import { Button } from '@simai/ui/components/button';
import { cn } from '@simai/ui/lib/utils';
import { Pause, Play } from 'lucide-react';
import {
  memo,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
  type MouseEvent,
} from 'react';

/** 배속(재생 엔진 REPLAY_SPEEDS 와 같다). 값을 쓰려고 three.js 를 끄는 입구를 정적으로 부르지 않는다. */
export const SPEEDS = [0.5, 1, 2, 4] as const;

// 이벤트 표시는 위험 등급색이다(빨강·주황·노랑은 위험 등급에만, CONVENTION). 데이터 없음은 줄무늬다.
const MARK: Record<Severity, string> = {
  CRITICAL: 'text-sev-critical',
  HIGH: 'text-sev-caution',
  CAUTION: 'text-sev-caution',
  INFO: 'text-muted-foreground',
};

const PANEL =
  'flex flex-none items-center gap-4 rounded-[12px] bg-card px-4 py-2.5 ring-1 ring-border ring-inset';
const CLOCK_TEXT = 'font-mono text-sm text-muted-foreground';
/** 데이터 없음 줄무늬. 막대(h-2)보다 높다. */
const GAP_MARK =
  'pointer-events-none absolute top-1/2 h-5 min-w-1 -translate-y-1/2 rounded-sm bg-stripes ring-1 ring-border ring-inset';
/** 이벤트 표시 버튼. 맨 위에 깔린 투명 슬라이더보다 위(z-20)에 있어야 눌린다. */
const EVENT_BUTTON =
  'group/mark absolute top-0 z-20 flex h-7 -translate-x-1/2 cursor-pointer items-start justify-center pt-0.5 outline-none focus-visible:ring-2 focus-visible:ring-ring';
const THUMB =
  'pointer-events-none absolute top-1/2 z-10 size-4 -translate-x-1/2 -translate-y-1/2 rounded-full bg-primary ring-2 ring-card';

/** t 가 구간 [from, to] 의 어디쯤인지(0–100%). 구간 밖은 끝에 붙인다. */
function percentOf(t: number, from: number, to: number): string {
  const span = Math.max(1, to - from);
  return `${((Math.min(Math.max(t, from), to) - from) / span) * 100}%`;
}

export interface TimelineProps {
  from: number;
  to: number;
  /** 슬라이더 한 칸(ms). ←·→ 키로 이만큼 옮긴다. */
  stepMs: number;
  status: ReplayStatus;
  events: readonly EventRow[];
  gaps: readonly Span[];
  /** 지금 시각이 "데이터 없음"인지(서버 gaps 안이고 보이는 기록이 없음). 기록 없는 구간 밖에서 비어 있으면 사람이 없던 것이다. */
  noData: boolean;
  disabled?: boolean;
  onSeek: (t: number) => void;
  /** 이벤트 표시를 눌렀다. */
  onJump: (event: EventRow) => void;
  onPlay: () => void;
  onPause: () => void;
  onSpeed: (speed: number) => void;
}

/** 데이터 없음 줄무늬 칸들(지나간 구간에서도 보인다). 구간·기록이 바뀔 때만 다시 그린다. */
const GapMarks = memo(function GapMarks({
  gaps,
  from,
  to,
}: {
  gaps: readonly Span[];
  from: number;
  to: number;
}) {
  return (
    <>
      {gaps.map((g) => (
        <span
          key={g.from}
          role="img"
          aria-label={`데이터 없음 ${formatClock(g.from)}–${formatClock(g.to)}`}
          className={GAP_MARK}
          style={{
            left: percentOf(g.from, from, to),
            width: `calc(${percentOf(g.to, from, to)} - ${percentOf(g.from, from, to)})`,
          }}
        />
      ))}
    </>
  );
});

/** 트랙 폭에 맞춰 이벤트 핀의 중심 X(px)와 버튼 폭을 겹치지 않게 계산한다. */
function layoutPins(
  events: readonly EventRow[],
  from: number,
  to: number,
  width: number,
): { btnWidth: number; byId: Map<string, number>; items: { event: EventRow; x: number }[] } {
  const sorted = [...events].sort(
    (a, b) => a.raisedAt - b.raisedAt || a.eventId.localeCompare(b.eventId),
  );
  if (width <= 0 || sorted.length === 0) {
    return { btnWidth: 24, byId: new Map(), items: [] };
  }
  const btnWidth = Math.max(8, Math.min(20, Math.floor(width / sorted.length)));
  const half = btnWidth / 2;
  const span = Math.max(1, to - from);
  const pos = sorted.map((e) =>
    Math.max(half, Math.min(width - half, ((e.raisedAt - from) / span) * width)),
  );
  for (let i = 1; i < pos.length; i++) {
    if (pos[i]! < pos[i - 1]! + btnWidth) pos[i] = pos[i - 1]! + btnWidth;
  }
  if (pos[pos.length - 1]! > width - half) {
    pos[pos.length - 1] = width - half;
    for (let i = pos.length - 2; i >= 0; i--) {
      if (pos[i]! > pos[i + 1]! - btnWidth) pos[i] = pos[i + 1]! - btnWidth;
    }
  }
  const byId = new Map<string, number>();
  const items = sorted.map((event, i) => {
    const x = pos[i]!;
    byId.set(event.eventId, x);
    return { event, x };
  });
  return { btnWidth, byId, items };
}

/** 이벤트 표시들. 막대 위쪽 줄에 두고, 누르면 그 시각으로 옮긴다. 구간·기록이 바뀔 때만 다시 그린다. */
const EventMarks = memo(function EventMarks({
  events,
  from,
  to,
  disabled,
  onJump,
  width,
  pinLayout,
}: {
  events: readonly EventRow[];
  from: number;
  to: number;
  disabled?: boolean;
  onJump: (event: EventRow) => void;
  width: number;
  pinLayout: { btnWidth: number; byId: Map<string, number> };
}) {
  return (
    <>
      {events.map((e) => {
        const x = pinLayout.byId.get(e.eventId);
        return (
          <button
            key={e.eventId}
            type="button"
            aria-label={`이벤트 ${formatTime(e.raisedAt)} ${ruleName(e.ruleId)}`}
            disabled={disabled}
            onClick={(event) => {
              event.stopPropagation();
              onJump(e);
            }}
            className={EVENT_BUTTON}
            style={
              width > 0 && x !== undefined
                ? { left: `${x}px`, width: `${pinLayout.btnWidth}px` }
                : { left: percentOf(e.raisedAt, from, to), width: '24px' }
            }
          >
            <span
              aria-hidden
              className={cn(
                'pointer-events-none text-sm leading-none font-bold transition-transform group-hover/mark:scale-125 group-focus-visible/mark:scale-125',
                MARK[e.severity],
              )}
            >
              {e.severity === 'CRITICAL' ? '▲' : e.severity === 'INFO' ? '•' : '!'}
            </span>
          </button>
        );
      })}
    </>
  );
});

function SpeedButtons({
  speed,
  disabled,
  onSpeed,
}: {
  speed: number;
  disabled?: boolean;
  onSpeed: (speed: number) => void;
}) {
  return (
    <div role="group" aria-label="배속" className="flex flex-none gap-0.5">
      {SPEEDS.map((option) => (
        <Button
          key={option}
          size="sm"
          variant={speed === option ? 'secondary' : 'ghost'}
          aria-pressed={speed === option}
          disabled={disabled}
          onClick={() => onSpeed(option)}
          className="px-2 font-mono"
        >
          {option}×
        </Button>
      ))}
    </div>
  );
}

/** 작업 구간 타임라인. 재생·일시정지·배속과 끌어서 옮기기, 이벤트·데이터 없음 표시. */
export function Timeline({
  from,
  to,
  stepMs,
  status,
  events,
  gaps,
  noData,
  disabled,
  onSeek,
  onJump,
  onPlay,
  onPause,
  onSpeed,
}: TimelineProps) {
  const track = useRef<HTMLDivElement>(null);
  const [trackWidth, setTrackWidth] = useState(0);
  useEffect(() => {
    const element = track.current;
    if (!element) return;
    const update = () => setTrackWidth(element.clientWidth);
    update();
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(update);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  const pinLayout = useMemo(
    () => layoutPins(events, from, to, trackWidth),
    [events, from, to, trackWidth],
  );
  const playhead = status.playhead ?? from;
  const playheadAt = percentOf(playhead, from, to);
  const lastStep = from + Math.floor(Math.max(1, to - from) / stepMs) * stepMs;
  // 끝에 닿으면 재생 의도가 남아 있어도 재생 중이 아니다(끝으로 옮겨도 "재생 중"에 머물지 않는다).
  const playing = status.playing && !status.ended;

  // 슬라이더는 간격 단위로만 움직여 구간 끝이 간격의 배수가 아니면 끝에 닿지 못한다. 맨 끝 칸은 끝으로 본다.
  const slide = (e: ChangeEvent<HTMLInputElement>) => {
    const t = Number(e.target.value);
    onSeek(t >= lastStep ? to : t);
  };

  // 막대 위에서 핀 근처를 클릭해도 해당 이벤트를 선택한다.
  const clickSlider = (e: MouseEvent<HTMLInputElement>) => {
    if (disabled || trackWidth <= 0 || pinLayout.items.length === 0) return;
    const rect = track.current?.getBoundingClientRect();
    if (!rect || rect.width <= 0) return;
    const x = e.clientX - rect.left;
    const threshold = Math.max(12, pinLayout.btnWidth / 2);
    let nearest: EventRow | null = null;
    let bestDist = threshold;
    for (const item of pinLayout.items) {
      const dist = Math.abs(item.x - x);
      if (dist <= bestDist) {
        bestDist = dist;
        nearest = item.event;
      }
    }
    if (nearest) onJump(nearest);
  };

  return (
    <section aria-label="타임라인" className={PANEL}>
      <Button
        size="icon"
        variant="secondary"
        disabled={disabled}
        aria-label={playing ? '일시정지' : '재생'}
        onClick={playing ? onPause : onPlay}
      >
        {playing ? <Pause /> : <Play />}
      </Button>
      <div className="flex w-[124px] flex-none flex-col leading-tight">
        <span className="font-mono text-[17px] font-semibold">{formatTime(status.playhead)}</span>
        {noData && !disabled && <span className="text-xs text-muted-foreground">데이터 없음</span>}
      </div>
      <span className={CLOCK_TEXT}>{formatClock(from)}</span>
      <div
        ref={track}
        className="relative h-9 min-w-0 flex-1 rounded-md has-focus-visible:ring-2 has-focus-visible:ring-ring"
      >
        <div className="pointer-events-none absolute inset-x-0 top-1/2 h-2 -translate-y-1/2 overflow-hidden rounded-full bg-surface-3">
          <span
            aria-hidden
            className="absolute inset-y-0 left-0 bg-primary/55"
            style={{ width: playheadAt }}
          />
        </div>
        <GapMarks gaps={gaps} from={from} to={to} />
        <EventMarks
          events={events}
          from={from}
          to={to}
          disabled={disabled}
          onJump={onJump}
          width={trackWidth}
          pinLayout={pinLayout}
        />
        <span aria-hidden className={THUMB} style={{ left: playheadAt }} />
        <input
          type="range"
          aria-label="재생 시각"
          aria-valuetext={formatTime(playhead)}
          min={from}
          max={to}
          step={stepMs}
          value={playhead}
          disabled={disabled}
          onChange={slide}
          onClick={clickSlider}
          className="absolute inset-0 size-full cursor-pointer opacity-0 disabled:cursor-default"
        />
      </div>
      <span className={CLOCK_TEXT}>{formatClock(to)}</span>
      <SpeedButtons speed={status.speed} disabled={disabled} onSpeed={onSpeed} />
    </section>
  );
}
