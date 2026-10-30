import {
  markTone,
  toPlan,
  zoneKey,
  type PlanBounds,
  type PlanMarks,
  type PlanZone,
} from '@/features/plan/plan';
import { ALARM_BLINK, RING_TONE, type Bind } from '@/features/plan/planDraw';
import { cn } from '@simai/ui/lib/utils';
import { memo } from 'react';

// 평면의 구역 층. 구역·진행 중 이벤트·고른 대상이 바뀔 때만 다시 그린다.

const ZONE_DASH = '0.18 0.1';
const STROKE = 0.05;
/** 확인 대기·선택은 선을 굵게 한다(점멸이 멈추는 설정에서도 보이게). */
const STROKE_STRONG = 0.09;
const SELECTED_FILL_OPACITY = 0.24;

/** 구역 기본 색: 입체 판정은 점선, 바닥 판정은 실선(관제 데모와 같다). 위험이 걸리면 등급 색으로 바뀐다. */
const ZONE_LOOK = {
  VOLUME_3D: { className: 'fill-zone-volume/10 stroke-zone-volume', dash: ZONE_DASH },
  GROUND_FOOTPRINT: { className: 'fill-zone-floor/10 stroke-zone-floor', dash: undefined },
} as const;
const NEUTRAL_LOOK = { className: 'fill-foreground/5 stroke-muted-foreground', dash: ZONE_DASH };

export const PlanZones = memo(function PlanZones({
  zones,
  bounds,
  marks,
  selectedKey,
  onSelect,
  bind,
}: {
  zones: readonly PlanZone[];
  bounds: PlanBounds;
  marks: PlanMarks['zones'];
  selectedKey: string | null;
  onSelect?: (key: string) => void;
  bind: Bind;
}) {
  return (
    <g>
      {zones.map((zone) => {
        const [x, y] = toPlan(zone.min[0], zone.max[1], bounds);
        const mark = marks.get(zone.zoneId);
        const tone = mark && markTone(mark);
        const key = zoneKey(zone.zoneId);
        const selected = selectedKey === key;
        const look = (zone.judgeType && ZONE_LOOK[zone.judgeType]) ?? NEUTRAL_LOOK;
        return (
          <rect
            key={zone.zoneId}
            data-zone={zone.zoneId}
            x={x}
            y={y}
            width={zone.max[0] - zone.min[0]}
            height={zone.max[1] - zone.min[1]}
            strokeWidth={mark?.alarming || selected ? STROKE_STRONG : STROKE}
            strokeDasharray={tone || selected ? undefined : look.dash}
            fillOpacity={selected ? SELECTED_FILL_OPACITY : undefined}
            onClick={onSelect && (() => onSelect(key))}
            {...bind(key)}
            className={cn(
              tone ? RING_TONE[tone] : look.className,
              selected && 'stroke-point-soft',
              mark?.alarming && ALARM_BLINK,
              onSelect && 'cursor-pointer',
            )}
          />
        );
      })}
    </g>
  );
});
