import { ROOM, toPlan } from '@/features/plan/plan';
import {
  TBM_DOOR,
  TBM_OPENING,
  TBM_ZONES,
  type LayoutItem,
  type Rect,
  type TbmTask,
} from '@/features/tbm/tbmDemo';
import { cn } from '@simai/ui/lib/utils';

// 작업 하나의 가상 레이아웃(2D 평면, m). 같은 시간대의 다른 작업은 점선으로, 배치가 겹친 자리는 '▲ 간섭'으로 그린다.

/** World 직사각형 → 평면 직사각형(+y 아래). 변환은 toPlan 으로만 한다. */
function planRect(r: Rect): Rect {
  const [x, y] = toPlan(r.x, r.y + r.h);
  return { x, y, w: r.w, h: r.h };
}

function PlanItem({ item, mine, tag }: { item: LayoutItem; mine: boolean; tag?: string }) {
  const height = item.kind !== 'path' && item.hgt ? ` · ${item.hgt}m` : '';
  const name = `${tag ? `${tag} · ` : ''}${item.label}${height}`;
  const stroke = mine ? 'stroke-point-soft' : 'stroke-muted-foreground';
  if (item.kind === 'path') {
    const pts = item.pts.map(([x, y]) => toPlan(x, y));
    const points = pts.map((p) => p.join(',')).join(' ');
    const [lx, ly] = pts[0]!;
    return (
      <g>
        <polyline
          points={points}
          fill="none"
          className={mine ? 'stroke-point-soft/15' : 'stroke-muted-foreground/15'}
          strokeWidth={item.w}
          strokeLinejoin="round"
        />
        <polyline
          points={points}
          fill="none"
          className={stroke}
          strokeWidth={0.05}
          strokeDasharray="0.15 0.1"
        />
        <text
          x={lx + item.w / 2 + 0.12}
          y={ly - 0.3}
          fontSize={0.24}
          className={mine ? 'fill-point-soft' : 'fill-muted-foreground'}
        >
          {name}
        </text>
      </g>
    );
  }
  const r = planRect(item);
  return (
    <g>
      <rect
        x={r.x}
        y={r.y}
        width={r.w}
        height={r.h}
        className={cn(mine ? 'fill-point-soft/15' : 'fill-muted-foreground/5', stroke)}
        strokeWidth={0.05}
        strokeDasharray={mine ? undefined : '0.15 0.1'}
      />
      <text
        x={r.x + 0.1}
        y={r.y + r.h / 2}
        fontSize={0.24}
        className={mine ? 'fill-foreground' : 'fill-muted-foreground'}
      >
        {name}
      </text>
    </g>
  );
}

export function TbmPlan({
  task,
  others,
  spots,
}: {
  task: TbmTask;
  /** 같은 시간대의 다른 작업. */
  others: readonly TbmTask[];
  /** 배치가 겹친 자리(World). */
  spots: readonly Rect[];
}) {
  const grid = [];
  for (let i = 1; i < ROOM.width; i++)
    grid.push(<line key={`v${i}`} x1={i} y1={0} x2={i} y2={ROOM.depth} />);
  for (let i = 1; i < ROOM.depth; i++)
    grid.push(<line key={`h${i}`} x1={0} y1={i} x2={ROOM.width} y2={i} />);
  const door = planRect(TBM_DOOR);
  const opening = planRect(TBM_OPENING);
  return (
    <svg
      viewBox={`-0.5 -0.5 ${ROOM.width + 1} ${ROOM.depth + 1}`}
      role="img"
      aria-label={`${task.id} 가상 레이아웃 평면 도식`}
      className="block aspect-square h-auto max-h-[440px] w-full"
    >
      <rect width={ROOM.width} height={ROOM.depth} className="fill-background" />
      <g className="stroke-stripe-a" strokeWidth={0.025}>
        {grid}
      </g>
      <rect
        width={ROOM.width}
        height={ROOM.depth}
        fill="none"
        className="stroke-muted-foreground"
        strokeWidth={0.07}
      />
      <rect
        x={door.x}
        y={door.y}
        width={door.w}
        height={door.h}
        className="fill-background stroke-muted-foreground"
        strokeWidth={0.03}
      />
      {TBM_ZONES.map((zone) => {
        const r = planRect(zone);
        return (
          <g key={zone.id}>
            <rect
              x={r.x}
              y={r.y}
              width={r.w}
              height={r.h}
              fill="none"
              className="stroke-faint/60"
              strokeWidth={0.04}
            />
            <text x={r.x + 0.08} y={r.y + r.h - 0.1} fontSize={0.2} className="fill-faint">
              {zone.id}
            </text>
          </g>
        );
      })}
      <rect
        x={opening.x}
        y={opening.y}
        width={opening.w}
        height={opening.h}
        className="fill-sev-critical/15 stroke-sev-critical"
        strokeWidth={0.04}
      />
      <text
        x={opening.x + 0.05}
        y={opening.y + opening.h + 0.28}
        fontSize={0.21}
        className="fill-sev-critical"
      >
        개구부
      </text>
      {others.map((other) =>
        other.items.map((item, n) => (
          <PlanItem key={`${other.id}-${n}`} item={item} mine={false} tag={other.id} />
        )),
      )}
      {task.items.map((item, n) => (
        <PlanItem key={`mine-${n}`} item={item} mine />
      ))}
      {spots.map((spot, n) => {
        const r = planRect(spot);
        return (
          <g key={`spot-${n}`}>
            <rect
              x={r.x}
              y={r.y}
              width={r.w}
              height={r.h}
              className="fill-sev-critical/25 stroke-sev-critical"
              strokeWidth={0.06}
            />
            <text
              x={r.x + r.w + 0.1}
              y={r.y + r.h / 2 + 0.08}
              fontSize={0.24}
              fontWeight={700}
              className="fill-sev-critical"
            >
              ▲ 간섭
            </text>
          </g>
        );
      })}
    </svg>
  );
}
