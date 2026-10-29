import { ROOM } from '@/features/plan/plan';

// 설치 · 보정 화면의 계산. 좌표는 World(m, +X 오른쪽, +Y 앞)이고, 화면 좌표로는 toPlan 으로 바꾼다.

export type Point = readonly [number, number];

/** 걸은 경로에서 이 거리(m) 안에 가운데가 든 1m 칸을 사람 관측이 덮은 칸으로 본다. */
export const COVER_RADIUS_M = 1.3;

/** 경로를 이 간격(m) 이하로 나눠 본다. */
const SAMPLE_STEP_M = 0.2;

/** 경로의 앞쪽 f(0~1)만큼을 꺾은선으로 돌려준다. */
export function partialPath(path: readonly Point[], f: number): Point[] {
  if (path.length === 0) return [];
  const lens = path.slice(1).map((q, i) => Math.hypot(q[0] - path[i]![0], q[1] - path[i]![1]));
  let left = lens.reduce((a, b) => a + b, 0) * Math.min(1, Math.max(0, f));
  const out: Point[] = [path[0]!];
  for (let i = 1; i < path.length && left > 0; i++) {
    const [ax, ay] = path[i - 1]!;
    const [bx, by] = path[i]!;
    const len = lens[i - 1]!;
    const t = len > 0 ? Math.min(1, left / len) : 1;
    out.push([ax + (bx - ax) * t, ay + (by - ay) * t]);
    left -= len;
  }
  return out;
}

/** 걸은 경로들이 덮은 1m 칸(칸 왼쪽 아래 모서리 [i, j]). 교실 밖 칸은 없다. */
export function coveredCells(lines: readonly (readonly Point[])[]): Point[] {
  const samples: Point[] = [];
  for (const pts of lines) {
    pts.forEach((b, i) => {
      const a = pts[i - 1] ?? b;
      const n = Math.max(1, Math.ceil(Math.hypot(b[0] - a[0], b[1] - a[1]) / SAMPLE_STEP_M));
      for (let k = 0; k <= n; k++)
        samples.push([a[0] + ((b[0] - a[0]) * k) / n, a[1] + ((b[1] - a[1]) * k) / n]);
    });
  }
  const cells: Point[] = [];
  for (let i = 0; i < ROOM.width; i++)
    for (let j = 0; j < ROOM.depth; j++)
      if (samples.some(([x, y]) => Math.hypot(x - i - 0.5, y - j - 0.5) <= COVER_RADIUS_M))
        cells.push([i, j]);
  return cells;
}

/** 교실 칸 가운데 덮은 칸의 비율(%). */
export const coverPercent = (cellCount: number): number =>
  Math.round((cellCount / (ROOM.width * ROOM.depth)) * 100);

/** 초를 「분:초」로 보인다(예: 1:05). */
export const formatClock = (secs: number): string =>
  `${Math.floor(secs / 60)}:${String(Math.floor(secs % 60)).padStart(2, '0')}`;
