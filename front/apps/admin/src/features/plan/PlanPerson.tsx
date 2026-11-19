import {
  personKey,
  planDirection,
  severityTone,
  sigmaRadius,
  toPlan,
  trackLook,
  type PlanBounds,
  type PlanMark,
  type TrackLook,
} from '@/features/plan/plan';
import {
  ALARM_BLINK,
  DEFAULT_EMPHASIS,
  RING_TONE,
  type Bind,
  type PlanEmphasis,
} from '@/features/plan/planDraw';
import { LOOK_LABEL } from '@/features/plan/planTip';
import { SEVERITY_LABEL } from '@/lib/ruleNames';
import type { WorldTrack } from '@simai/contracts';
import { cn } from '@simai/ui/lib/utils';
import type { KeyboardEvent } from 'react';

// 평면의 사람 한 명: 관측 모양·진행 방향 + 불확실 원·등급 고리·선택·포커스·속도선.
// 아래 반지름·굵기는 배율 1 의 값(m)이고, 미니맵은 emphasis.marker 배율로 키운다(-800). 불확실 원 반지름만 실제 크기다.

/** 예측 위치에서 속도선을 그리는 시간(초). 디자인 데모와 같다. */
const LOOKAHEAD_S = 1.8;

/** 반지름(m, 배율 1). */
const RADIUS = {
  /** 누르는 자리(투명 원). */
  hit: 0.45,
  ring: 0.46,
  selected: 0.6,
  focus: 0.72,
  observed: 0.25,
  observedCore: 0.09,
  predicted: 0.24,
  unknown: 0.26,
} as const;
/** 선 굵기(m, 배율 1). 확인 대기 고리는 점멸이 멈추는 설정에서도 보이게 굵다. */
const STROKE = {
  sigma: 0.03,
  ring: 0.06,
  ringAlarm: 0.1,
  mark: 0.05,
  ahead: 0.04,
} as const;
/** 점선 간격(m, 배율 1). */
const DASH = {
  sigma: [0.1, 0.08],
  focus: [0.12, 0.08],
  ahead: [0.08, 0.08],
  mark: [0.08, 0.06],
} as const;
const UNKNOWN_GLYPH_SIZE = 0.32;
const UNKNOWN_GLYPH_DROP = 0.11;
/**
 * 방향 표식(지도 앱의 내 위치 표식처럼 점 둘레에서 진행 방향으로 튀어나온 삼각형, m·rad, 배율 1).
 * 밑변 두 꼭짓점은 점 안쪽에 두어 점과 이어져 보이게 하고, 끝은 점 반지름의 2배까지 나온다.
 */
const POINTER = { tip: 0.5, base: 0.22, halfAngle: (52 * Math.PI) / 180 } as const;

const dashOf = (dash: readonly number[], scale: number) => dash.map((d) => d * scale).join(' ');

interface Point {
  x: number;
  y: number;
}

/**
 * 방향 표식 삼각형의 꼭짓점(평면 좌표, 끝 꼭짓점이 처음). 방향은 World yaw 를 planDirection(toPlan 과 같은 변환)으로 바꿔
 * 위치가 움직이는 화면 방향과 늘 같다.
 */
function pointerPoints(x: number, y: number, yaw: number, scale = 1): string {
  const at = (angle: number, radius: number) => {
    const [dx, dy] = planDirection(yaw + angle);
    return `${x + dx * radius * scale},${y + dy * radius * scale}`;
  };
  return [
    at(0, POINTER.tip),
    at(POINTER.halfAngle, POINTER.base),
    at(-POINTER.halfAngle, POINTER.base),
  ].join(' ');
}

/** 관측 상태별 모양: 관측은 채운 점, 예측은 점선 원, 확인 불가는 ? 가 든 점선 원. 방향을 알면 방향 표식을 붙인다. */
function PersonDot({
  look,
  yaw,
  scale,
  x,
  y,
}: { look: TrackLook; yaw: number | undefined; scale: number } & Point) {
  // 확인 불가 사람은 지금 방향을 모르므로 방향 표식을 그리지 않는다.
  const pointer = yaw !== undefined && look !== 'unknown' && (
    <polygon
      data-heading
      points={pointerPoints(x, y, yaw, scale)}
      strokeLinejoin="round"
      strokeWidth={STROKE.mark * scale}
      className={
        look === 'observed'
          ? 'fill-foreground stroke-foreground'
          : 'fill-foreground/55 stroke-foreground/55'
      }
    />
  );
  if (look === 'observed') {
    return (
      <>
        {pointer}
        <circle cx={x} cy={y} r={RADIUS.observed * scale} className="fill-foreground" />
        <circle cx={x} cy={y} r={RADIUS.observedCore * scale} className="fill-surface-2" />
      </>
    );
  }
  if (look === 'predicted') {
    return (
      <>
        {pointer}
        <circle
          cx={x}
          cy={y}
          r={RADIUS.predicted * scale}
          strokeWidth={STROKE.mark * scale}
          strokeDasharray={dashOf(DASH.mark, scale)}
          className="fill-foreground/25 stroke-foreground"
        />
      </>
    );
  }
  return (
    <>
      <circle
        cx={x}
        cy={y}
        r={RADIUS.unknown * scale}
        strokeWidth={STROKE.mark * scale}
        strokeDasharray={dashOf(DASH.mark, scale)}
        className="fill-surface-2 stroke-muted-foreground"
      />
      <text
        x={x}
        y={y + UNKNOWN_GLYPH_DROP * scale}
        fontSize={UNKNOWN_GLYPH_SIZE * scale}
        fontWeight={700}
        textAnchor="middle"
        className="fill-muted-foreground"
      >
        ?
      </text>
    </>
  );
}

/** 속도선 끝. 예측 위치만 속도 방향으로 LOOKAHEAD_S 초 앞을 그린다. */
function aheadPoint(
  track: WorldTrack,
  look: TrackLook,
  bounds: PlanBounds,
): readonly [number, number] | null {
  const velocity = track.velocity;
  if (look !== 'predicted' || !velocity) return null;
  return toPlan(
    track.position_xy[0] + velocity[0] * LOOKAHEAD_S,
    track.position_xy[1] + velocity[1] * LOOKAHEAD_S,
    bounds,
  );
}

/** 불확실 원·등급 고리·선택 고리·키보드 포커스 고리·속도선. 보이기만 하고 누르지 않는다. */
function PersonHalo({
  track,
  look,
  bounds,
  mark,
  selected,
  scale,
  x,
  y,
}: {
  track: WorldTrack;
  look: TrackLook;
  bounds: PlanBounds;
  mark: PlanMark | undefined;
  selected: boolean;
  scale: number;
} & Point) {
  const sigma = sigmaRadius(track);
  const tone = mark && severityTone(mark.severity);
  const ahead = aheadPoint(track, look, bounds);
  return (
    <g className="pointer-events-none">
      {sigma !== null && (
        <circle
          cx={x}
          cy={y}
          r={sigma}
          strokeWidth={STROKE.sigma * scale}
          strokeDasharray={dashOf(DASH.sigma, scale)}
          className="fill-foreground/5 stroke-foreground/25"
        />
      )}
      {tone && (
        <circle
          cx={x}
          cy={y}
          r={RADIUS.ring * scale}
          strokeWidth={(mark.alarming ? STROKE.ringAlarm : STROKE.ring) * scale}
          className={cn(RING_TONE[tone], mark.alarming && ALARM_BLINK)}
        />
      )}
      {selected && (
        <circle
          cx={x}
          cy={y}
          r={RADIUS.selected * scale}
          fill="none"
          strokeWidth={STROKE.mark * scale}
          className="stroke-primary"
        />
      )}
      {/* 키보드 포커스 표시. outline 은 SVG 그룹에 그려지지 않아 고리로 보인다. */}
      <circle
        cx={x}
        cy={y}
        r={RADIUS.focus * scale}
        fill="none"
        strokeWidth={STROKE.mark * scale}
        strokeDasharray={dashOf(DASH.focus, scale)}
        className="stroke-transparent group-focus-visible/person:stroke-primary"
      />
      {ahead && (
        <line
          x1={x}
          y1={y}
          x2={ahead[0]}
          y2={ahead[1]}
          strokeWidth={STROKE.ahead * scale}
          strokeDasharray={dashOf(DASH.ahead, scale)}
          className="stroke-foreground/70"
        />
      )}
    </g>
  );
}

/** 고를 수 있을 때만 버튼 역할과 Enter·Space 선택을 붙인다. */
function selectProps(select: (() => void) | undefined, selected: boolean, label: string) {
  if (!select) return {};
  return {
    role: 'button',
    tabIndex: 0,
    'aria-pressed': selected,
    'aria-label': label,
    onClick: select,
    onKeyDown: (e: KeyboardEvent) => {
      if (e.key !== 'Enter' && e.key !== ' ') return;
      e.preventDefault();
      select();
    },
  };
}

export function PlanPerson({
  track,
  bounds,
  mark,
  selected,
  onSelect,
  bind,
  yaw,
  emphasis = DEFAULT_EMPHASIS,
}: {
  track: WorldTrack;
  bounds: PlanBounds;
  mark: PlanMark | undefined;
  selected: boolean;
  onSelect?: (key: string) => void;
  bind: Bind;
  /** 진행 방향(World yaw, rad, planHeading). 모르면 없고 점만 그린다. */
  yaw?: number;
  /** 미니맵은 표식을 키운다(-800). */
  emphasis?: PlanEmphasis;
}) {
  const [x, y] = toPlan(track.position_xy[0], track.position_xy[1], bounds);
  const look = trackLook(track);
  const key = personKey(track.world_id);
  const select = onSelect && (() => onSelect(key));
  const scale = emphasis.marker;
  const label = `${track.world_id}${LOOK_LABEL[look]}${mark ? ` · ${SEVERITY_LABEL[mark.severity]}` : ''}`;

  return (
    <g
      {...selectProps(select, selected, label)}
      {...bind(key)}
      data-person={track.world_id}
      className={cn('group/person', select && 'cursor-pointer outline-hidden')}
    >
      {/* 겹친 표시는 보이기만 한다. 누르는 자리는 아래 투명 원이라 겹친 다른 사람을 고르지 않는다. */}
      <PersonHalo
        track={track}
        look={look}
        bounds={bounds}
        mark={mark}
        selected={selected}
        scale={scale}
        x={x}
        y={y}
      />
      <circle cx={x} cy={y} r={RADIUS.hit * scale} fill="transparent" />
      <PersonDot look={look} yaw={yaw} scale={scale} x={x} y={y} />
    </g>
  );
}
