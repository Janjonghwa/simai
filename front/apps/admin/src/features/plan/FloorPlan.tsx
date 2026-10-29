import { HoverTip, useDwell, type TipTone } from '@/features/plan/HoverTip';
import {
  markFor,
  markTone,
  PLAN_MARGIN,
  ROOM,
  sigmaRadius,
  toPlan,
  trackIds,
  trackLook,
  type PlanMark,
  type PlanMarks,
} from '@/features/plan/plan';
import type { Zone, ZoneBox } from '@/features/zones/types';
import { ZONE_TYPE_LABEL } from '@/features/zones/zoneForm';
import type { WorldTrack } from '@simai/contracts';
import { cn } from '@simai/ui/lib/utils';
import { memo, type KeyboardEvent, type PointerEvent } from 'react';

// 위에서 내려다본 2D 평면(명세 13.6 비상 화면, 4.1 BEV 2D 개요). 단위는 m 이고 색은 토큰만 쓴다.
// 데이터는 props 로만 받는다. 실시간 관제는 LivePlan 이 채운다.

/** 예측 위치에서 속도선을 그리는 시간(초). 디자인 데모와 같다. */
const LOOKAHEAD_S = 1.8;

const RING_TONE = {
  critical: 'fill-sev-critical/15 stroke-sev-critical',
  caution: 'fill-sev-caution/15 stroke-sev-caution',
} as const;
/** 구역 기본 색: 입체 판정은 점선, 바닥 판정은 실선(관제 데모와 같다). 위험이 걸리면 등급 색으로 바뀐다. */
const ZONE_LOOK = {
  VOLUME_3D: { className: 'fill-zone-volume/10 stroke-zone-volume', dash: '0.18 0.1' },
  GROUND_FOOTPRINT: { className: 'fill-zone-floor/10 stroke-zone-floor', dash: undefined },
} as const;
const NEUTRAL_LOOK = { className: 'fill-foreground/5 stroke-muted-foreground', dash: '0.18 0.1' };

const GLYPH = { CRITICAL: '▲▲', HIGH: '▲', CAUTION: '!', INFO: 'i' } as const;

/** 2D 에 그리는 구역. 서버 구역이면 판정 유형·유형·등급으로 색과 말풍선을 정한다. */
export type PlanZone = ZoneBox & Partial<Pick<Zone, 'judgeType' | 'zoneType' | 'severity'>>;

function zoneTip(zone: PlanZone): string {
  const parts = [`${zone.zoneId} ${zone.name}`];
  if (zone.zoneType) parts.push(ZONE_TYPE_LABEL[zone.zoneType] ?? zone.zoneType);
  if (zone.severity) parts.push(`${GLYPH[zone.severity]} ${zone.severity}`);
  return parts.join(' · ');
}

type Bind = (key: string) => {
  onPointerEnter: (e: PointerEvent) => void;
  onPointerMove: (e: PointerEvent) => void;
  onPointerLeave: () => void;
};

const RoomBase = memo(function RoomBase() {
  const lines = [];
  for (let i = 1; i < ROOM.width; i++)
    lines.push(<line key={`v${i}`} x1={i} y1={0} x2={i} y2={ROOM.depth} />);
  for (let i = 1; i < ROOM.depth; i++)
    lines.push(<line key={`h${i}`} x1={0} y1={i} x2={ROOM.width} y2={i} />);
  return (
    <g>
      <rect width={ROOM.width} height={ROOM.depth} rx={0.08} className="fill-surface-2" />
      <g className="stroke-border" strokeWidth={0.025}>
        {lines}
      </g>
      <rect
        width={ROOM.width}
        height={ROOM.depth}
        rx={0.08}
        fill="none"
        className="stroke-muted-foreground"
        strokeWidth={0.06}
      />
    </g>
  );
});

const ZoneLayer = memo(function ZoneLayer({
  zones,
  marks,
  selectedKey,
  onSelect,
  bind,
}: {
  zones: readonly PlanZone[];
  marks: PlanMarks['zones'];
  selectedKey: string | null;
  onSelect?: (key: string) => void;
  bind: Bind;
}) {
  return (
    <g>
      {zones.map((zone) => {
        const [x, y] = toPlan(zone.min[0], zone.max[1]);
        const width = zone.max[0] - zone.min[0];
        const height = zone.max[1] - zone.min[1];
        const mark = marks.get(zone.zoneId);
        const tone = mark && markTone(mark);
        const key = `zone:${zone.zoneId}`;
        const selected = selectedKey === key;
        const look = (zone.judgeType && ZONE_LOOK[zone.judgeType]) ?? NEUTRAL_LOOK;
        return (
          <rect
            key={zone.zoneId}
            data-zone={zone.zoneId}
            x={x}
            y={y}
            width={width}
            height={height}
            // 확인 대기는 점멸에 더해 선을 굵게 한다. 움직임 줄이기 설정이면 점멸이 멈추기 때문이다.
            strokeWidth={mark?.alarming || selected ? 0.09 : 0.05}
            strokeDasharray={tone || selected ? undefined : look.dash}
            fillOpacity={selected ? 0.24 : undefined}
            onClick={onSelect && (() => onSelect(key))}
            {...bind(key)}
            className={cn(
              tone ? RING_TONE[tone] : look.className,
              selected && 'stroke-point-soft',
              mark?.alarming && 'motion-safe:animate-alarm-blink',
              onSelect && 'cursor-pointer',
            )}
          />
        );
      })}
    </g>
  );
});

const LOOK_LABEL = { observed: '', predicted: ' · 예측', unknown: ' · 확인 불가' } as const;

function personTip(track: WorldTrack, mark: PlanMark | undefined): string {
  const look = trackLook(track);
  const glyph = mark ? `${GLYPH[mark.severity]} ` : look === 'unknown' ? '? ' : '';
  return `${glyph}${track.world_id}${LOOK_LABEL[look]}`;
}

function Person({
  track,
  mark,
  selected,
  onSelect,
  bind,
}: {
  track: WorldTrack;
  mark: PlanMark | undefined;
  selected: boolean;
  onSelect?: (key: string) => void;
  bind: Bind;
}) {
  const [x, y] = toPlan(...track.position_xy);
  const look = trackLook(track);
  const sigma = sigmaRadius(track);
  const tone = mark && markTone(mark);
  const velocity = track.velocity;
  const ahead =
    look === 'predicted' && velocity
      ? toPlan(
          track.position_xy[0] + velocity[0] * LOOKAHEAD_S,
          track.position_xy[1] + velocity[1] * LOOKAHEAD_S,
        )
      : null;
  const key = `person:${track.world_id}`;
  const select = onSelect && (() => onSelect(key));
  const onKeyDown = (e: KeyboardEvent) => {
    if (select && (e.key === 'Enter' || e.key === ' ')) {
      e.preventDefault();
      select();
    }
  };

  return (
    <g
      {...(select && {
        role: 'button',
        tabIndex: 0,
        'aria-pressed': selected,
        'aria-label': `${track.world_id}${LOOK_LABEL[look]}${mark ? ` · ${mark.severity}` : ''}`,
        onClick: select,
        onKeyDown,
      })}
      {...bind(key)}
      className={cn('group/person', select && 'cursor-pointer outline-hidden')}
    >
      {/* 불확실 원·고리·속도선은 보이기만 한다. 누르는 자리는 아래 투명 원이라 겹친 다른 사람을 고르지 않는다. */}
      <g className="pointer-events-none">
        {sigma !== null && (
          <circle
            cx={x}
            cy={y}
            r={sigma}
            strokeWidth={0.03}
            strokeDasharray="0.1 0.08"
            className="fill-foreground/5 stroke-foreground/25"
          />
        )}
        {tone && (
          <circle
            cx={x}
            cy={y}
            r={0.46}
            // 확인 대기는 점멸에 더해 선을 굵게 한다. 움직임 줄이기 설정이면 점멸이 멈추기 때문이다.
            strokeWidth={mark.alarming ? 0.1 : 0.06}
            className={cn(RING_TONE[tone], mark.alarming && 'motion-safe:animate-alarm-blink')}
          />
        )}
        {selected && (
          <circle cx={x} cy={y} r={0.6} fill="none" strokeWidth={0.05} className="stroke-primary" />
        )}
        {/* 키보드 포커스 표시. outline 은 SVG 그룹에 그려지지 않아 고리로 보인다. */}
        <circle
          cx={x}
          cy={y}
          r={0.72}
          fill="none"
          strokeWidth={0.05}
          strokeDasharray="0.12 0.08"
          className="stroke-transparent group-focus-visible/person:stroke-primary"
        />
        {ahead && (
          <line
            x1={x}
            y1={y}
            x2={ahead[0]}
            y2={ahead[1]}
            strokeWidth={0.04}
            strokeDasharray="0.08 0.08"
            className="stroke-foreground/70"
          />
        )}
      </g>
      <circle cx={x} cy={y} r={0.45} fill="transparent" />
      {look === 'observed' && (
        <>
          <circle cx={x} cy={y} r={0.25} className="fill-foreground" />
          <circle cx={x} cy={y} r={0.09} className="fill-surface-2" />
        </>
      )}
      {look === 'predicted' && (
        <circle
          cx={x}
          cy={y}
          r={0.24}
          strokeWidth={0.05}
          strokeDasharray="0.08 0.06"
          className="fill-foreground/25 stroke-foreground"
        />
      )}
      {look === 'unknown' && (
        <>
          <circle
            cx={x}
            cy={y}
            r={0.26}
            strokeWidth={0.05}
            strokeDasharray="0.08 0.06"
            className="fill-surface-2 stroke-muted-foreground"
          />
          <text
            x={x}
            y={y + 0.11}
            fontSize={0.32}
            fontWeight={700}
            textAnchor="middle"
            className="fill-muted-foreground"
          >
            ?
          </text>
        </>
      )}
    </g>
  );
}

export interface FloorPlanProps {
  tracks: readonly WorldTrack[];
  zones: readonly PlanZone[];
  marks: PlanMarks;
  /** 고른 대상(이벤트 패널과 같은 키, 예: person:w-0012). */
  selectedKey?: string | null;
  onSelect?: (key: string) => void;
  /** 위치가 지금 것이 아니다(연결 끊김·world 정지). 흐리게 그린다. */
  stale?: boolean;
  className?: string;
}

export function FloorPlan({
  tracks,
  zones,
  marks,
  selectedKey = null,
  onSelect,
  stale = false,
  className,
}: FloorPlanProps) {
  const size = (n: number) => n + PLAN_MARGIN * 2;
  const dwell = useDwell();
  const tip = dwell.tip && planTip(dwell.tip.key, tracks, zones, marks);
  return (
    <>
      <svg
        viewBox={`${-PLAN_MARGIN} ${-PLAN_MARGIN} ${size(ROOM.width)} ${size(ROOM.depth)}`}
        preserveAspectRatio="xMidYMid meet"
        role="group"
        aria-label="2D 평면"
        className={cn('transition-opacity', stale && 'opacity-50', className)}
      >
        <RoomBase />
        <ZoneLayer
          zones={zones}
          marks={marks.zones}
          selectedKey={selectedKey}
          onSelect={onSelect}
          bind={dwell.on}
        />
        <g>
          {tracks.map((track) => {
            const ids = trackIds(track);
            return (
              <Person
                key={track.world_id}
                track={track}
                mark={markFor(marks.people, ids)}
                selected={ids.some((id) => selectedKey === `person:${id}`)}
                onSelect={onSelect}
                bind={dwell.on}
              />
            );
          })}
        </g>
      </svg>
      <HoverTip tip={dwell.tip} text={tip?.text} tone={tip?.tone} />
    </>
  );
}

/** 지금 머무는 대상의 말풍선 글자. 그 사이 대상이 사라졌으면 없다. */
function planTip(
  key: string,
  tracks: readonly WorldTrack[],
  zones: readonly PlanZone[],
  marks: PlanMarks,
): { text: string; tone?: TipTone } | undefined {
  if (key.startsWith('zone:')) {
    const zone = zones.find((z) => `zone:${z.zoneId}` === key);
    return zone && { text: zoneTip(zone) };
  }
  const track = tracks.find((t) => `person:${t.world_id}` === key);
  if (!track) return undefined;
  const mark = markFor(marks.people, trackIds(track));
  return { text: personTip(track, mark), tone: mark && markTone(mark) };
}
