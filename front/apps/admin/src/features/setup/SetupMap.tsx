import { PLAN_MARGIN, ROOM, toPlan } from '@/features/plan/plan';
import type { Point } from '@/features/setup/setup';
import {
  CAMERAS,
  CHECKS,
  DESKS,
  DOOR,
  EXTRA_WALK,
  OPENING,
  PODIUM,
  PROPS,
  type PlanRect,
  type SetupView,
} from '@/features/setup/setupDemo';
import { cn } from '@simai/ui/lib/utils';
import { memo } from 'react';

// 위에서 내려다본 교실에 단계마다 정물, 걸은 경로와 덮은 칸, 검증 지점, 보정 뒤 평면도를 그린다.
// 단위는 m 이고 색은 토큰만 쓴다.

/** 글자가 선·칸 위에서도 읽히게 바닥색 테두리를 두른다. */
const HALO = { paintOrder: 'stroke', strokeWidth: 0.09, strokeLinejoin: 'round' } as const;
const HALO_CLASS = 'stroke-surface-2';

const points = (path: readonly Point[]) => path.map((p) => toPlan(...p).join(',')).join(' ');

/** World 사각형(왼쪽 아래 모서리·크기) → 평면 SVG 사각형. */
function planRect(r: PlanRect) {
  const [x, y] = toPlan(r.x, r.y + r.depth);
  return { x, y, width: r.width, height: r.depth };
}

const Room = memo(function Room({ grid }: { grid: boolean }) {
  const lines = [];
  if (grid) {
    for (let i = 1; i < ROOM.width; i++)
      lines.push(<line key={`v${i}`} x1={i} y1={0} x2={i} y2={ROOM.depth} />);
    for (let i = 1; i < ROOM.depth; i++)
      lines.push(<line key={`h${i}`} x1={0} y1={i} x2={ROOM.width} y2={i} />);
  }
  return (
    <g>
      <rect width={ROOM.width} height={ROOM.depth} rx={0.08} className="fill-surface-2" />
      {grid && (
        <g className="stroke-border" strokeWidth={0.025}>
          {lines}
        </g>
      )}
    </g>
  );
});

const Walls = memo(function Walls() {
  const [doorX, doorY] = toPlan(DOOR.x, ROOM.depth);
  return (
    <g>
      <rect
        width={ROOM.width}
        height={ROOM.depth}
        rx={0.08}
        fill="none"
        strokeWidth={0.07}
        className="stroke-muted-foreground"
      />
      <rect
        x={doorX}
        y={doorY - 0.14}
        width={DOOR.width}
        height={0.28}
        strokeWidth={0.03}
        className="fill-card stroke-muted-foreground"
      />
    </g>
  );
});

/** 보정 뒤 BEV 평면도의 책상·단상·개구부. */
const Furniture = memo(function Furniture() {
  const podium = planRect(PODIUM);
  return (
    <g>
      {DESKS.map((desk, n) => (
        <rect
          key={n}
          {...planRect(desk)}
          rx={0.05}
          strokeWidth={0.03}
          className="fill-surface-3 stroke-border"
        />
      ))}
      <rect {...podium} rx={0.05} className="fill-muted-foreground/25" />
      <text
        x={podium.x + podium.width / 2}
        y={podium.y + podium.height / 2 + 0.1}
        fontSize={0.24}
        textAnchor="middle"
        className="fill-muted-foreground"
      >
        단상
      </text>
      <rect
        {...planRect(OPENING)}
        strokeWidth={0.03}
        className="fill-faint/40 stroke-border dark:fill-background"
      />
    </g>
  );
});

function Props({ view }: { view: SetupView }) {
  const dim = view.stage > 0;
  return (
    <g>
      {PROPS.map((prop, n) => {
        const [x, y] = toPlan(...prop.at);
        const seen = view.propsSeen[n];
        return (
          <g key={prop.name} opacity={dim ? 0.45 : 1}>
            <polygon
              points={`${x},${y - 0.32} ${x + 0.26},${y + 0.2} ${x - 0.26},${y + 0.2}`}
              strokeWidth={0.05}
              strokeDasharray={seen ? undefined : '0.1 0.07'}
              className={seen ? 'fill-sev-caution stroke-sev-caution' : 'fill-none stroke-faint'}
            />
            {!dim && (
              <text
                x={x + 0.38}
                y={y + 0.12}
                fontSize={0.26}
                fontWeight={600}
                {...HALO}
                className={cn(HALO_CLASS, seen ? 'fill-sev-caution' : 'fill-muted-foreground')}
              >
                {seen ? prop.name : `${prop.name} · 미감지`}
              </text>
            )}
          </g>
        );
      })}
    </g>
  );
}

function Walk({ view }: { view: SetupView }) {
  const { mainPath, extraPath } = view;
  const walker = (extraPath.length > 1 ? extraPath : mainPath).at(-1);
  const [wx, wy] = walker ? toPlan(...walker) : [0, 0];
  return (
    <g>
      {view.gap && (
        <polyline
          points={points(EXTRA_WALK)}
          fill="none"
          strokeWidth={0.07}
          strokeDasharray="0.2 0.14"
          className="stroke-point-soft"
        />
      )}
      <polyline
        points={points(mainPath)}
        fill="none"
        strokeWidth={0.08}
        strokeLinejoin="round"
        className="stroke-primary"
      />
      {extraPath.length > 1 && (
        <polyline
          points={points(extraPath)}
          fill="none"
          strokeWidth={0.08}
          strokeLinejoin="round"
          className="stroke-primary"
        />
      )}
      {walker && (
        <>
          <circle cx={wx} cy={wy} r={0.24} className="fill-foreground" />
          <circle cx={wx} cy={wy} r={0.09} className="fill-surface-2" />
        </>
      )}
    </g>
  );
}

function Checks({ checked }: { checked: number }) {
  return (
    <g>
      {CHECKS.map((check, n) => {
        const [x, y] = toPlan(...check.at);
        const done = n < checked;
        const cur = n === checked;
        return (
          <g key={n}>
            {cur && (
              <circle
                cx={x}
                cy={y}
                r={0.5}
                strokeWidth={0.05}
                className="fill-primary/15 stroke-primary motion-safe:animate-pulse"
              />
            )}
            <circle
              cx={x}
              cy={y}
              r={0.3}
              strokeWidth={0.05}
              strokeDasharray={done || cur ? undefined : '0.1 0.07'}
              className={done ? 'fill-ok' : cur ? 'fill-primary' : 'fill-surface-2 stroke-faint'}
            />
            <text
              x={x}
              y={y + 0.1}
              fontSize={0.28}
              fontWeight={700}
              textAnchor="middle"
              className={cn(
                'font-mono',
                done || cur ? 'fill-primary-foreground' : 'fill-muted-foreground',
              )}
            >
              {n + 1}
            </text>
            {done && (
              <text
                x={x + 0.42}
                y={y + 0.1}
                fontSize={0.26}
                fontWeight={600}
                {...HALO}
                className={cn(HALO_CLASS, 'fill-ok font-mono')}
              >
                {check.errorCm}cm
              </text>
            )}
          </g>
        );
      })}
    </g>
  );
}

function Cameras({ view }: { view: SetupView }) {
  return (
    <g>
      {CAMERAS.map((cam, k) => {
        const [px, py] = toPlan(...cam.corner);
        // 모서리 바깥쪽 방향.
        const sx = px <= 0 ? -1 : 1;
        const sy = py <= 0 ? -1 : 1;
        const warn = view.cameras[k]?.tone === 'warn';
        return (
          <g key={cam.id}>
            <polygon
              points={`${px + 0.4 * sx},${py + 0.4 * sy} ${px - 0.2 * sx},${py + 0.4 * sy} ${px + 0.4 * sx},${py - 0.2 * sy}`}
              className={warn ? 'fill-sev-caution' : 'fill-faint'}
            />
            <text
              x={sx < 0 ? px - 0.78 : px - 0.18}
              y={sy < 0 ? py - 0.52 : py + 0.78}
              fontSize={0.22}
              className={cn('font-mono', warn ? 'fill-sev-caution' : 'fill-muted-foreground')}
            >
              {warn ? `${cam.id} ${cam.tilt}°` : cam.id}
            </text>
          </g>
        );
      })}
    </g>
  );
}

export function SetupMap({ view }: { view: SetupView }) {
  const size = (n: number) => n + PLAN_MARGIN * 2;
  const { stage } = view;
  return (
    <svg
      viewBox={`${-PLAN_MARGIN} ${-PLAN_MARGIN} ${size(ROOM.width)} ${size(ROOM.depth)}`}
      preserveAspectRatio="xMidYMid meet"
      role="img"
      aria-label={`교실 ${ROOM.width}×${ROOM.depth}m 위에서 본 보정 화면`}
      className="block max-h-[400px] w-full"
    >
      <Room grid={stage < 3} />
      {(stage === 1 || stage === 2) && (
        <g>
          {view.cells.map(([i, j]) => {
            const [x, y] = toPlan(i, j + 1);
            return (
              <rect
                key={`${i}-${j}`}
                x={x + 0.03}
                y={y + 0.03}
                width={0.94}
                height={0.94}
                className="fill-primary/15"
              />
            );
          })}
        </g>
      )}
      {stage === 3 && <Furniture />}
      <Walls />
      {stage < 3 && <Props view={view} />}
      {stage === 1 && <Walk view={view} />}
      {stage === 2 && <Checks checked={view.checked} />}
      <Cameras view={view} />
    </svg>
  );
}
