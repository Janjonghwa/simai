import {
  boundsDepth,
  boundsWidth,
  PLAN_MARGIN,
  trackLook,
  type PlanBounds,
  type PlanZone,
} from '@/features/plan/plan';
import type { PlanEmphasis } from '@/features/plan/planDraw';
import type { WorldTrack } from '@simai/contracts';

// 미니맵이 비추는 World 범위(-800). 방 전체를 작은 칸에 담으면 구역·사람이 점처럼 보여서, 위험 구역과 사람이 있는 곳에 맞춘다.
// 사람이 조금 움직일 때마다 범위가 흔들리지 않게 한다: 사람이 가장자리를 넘으면 여유를 더해 바로 넓히고,
// 좁힐 때는 좁은 범위로 충분한 상태가 FIT_SHRINK_AFTER_MS 동안 이어질 때만 좁힌다. 화면 표시용이고 판정에 쓰지 않는다.

/** 구역·사람 둘레 여백(m). */
export const FIT_PAD_M = 1;
/** 범위의 최소 한 변(m). 사람 한 명만 있어도 이보다 크게 당기지 않는다. */
export const FIT_MIN_EXTENT_M = 6;
/** 구역·사람이 지금 범위 가장자리에서 이만큼(m) 안쪽 선을 넘으면 넓힌다. */
export const FIT_EDGE_M = 0.5;
/** 넓힐 때 여백에 더하는 여유(m). 걷는 사람 때문에 프레임마다 넓히지 않게 한다. */
export const FIT_GROW_SLACK_M = 1.5;
/** 맞출 범위의 한 변이 지금 범위의 이 비율보다 작은 채로 FIT_SHRINK_AFTER_MS 가 지나면 좁힌다. */
export const FIT_SHRINK_RATIO = 0.75;
export const FIT_SHRINK_AFTER_MS = 4000;

/** 미니맵 사람 표식 배율의 기준 범위(m). 이 한 변의 범위에서 배율 1(점 반지름 0.25 m)이다. */
export const MARKER_REF_EXTENT_M = 8;
const MARKER_SCALE = { min: 0.8, max: 5 } as const;
/** 미니맵 구역 선은 사람 표식보다 조금 더 굵게 한다(작은 칸에서도 선이 보이게). */
const ZONE_STROKE_GAIN = 1.6;
/** 미니맵 구역 채움 불투명도. 큰 평면(0.1·0.24)보다 짙다. */
const MINIMAP_ZONE_FILL = { normal: 0.22, selected: 0.36 } as const;

/** 미니맵이 비추는 범위와 좁히기 대기 상태. */
export interface MinimapView {
  view: PlanBounds;
  /** 구역·사람이 없어 방 전체를 비춘다. 다음에 무엇이 생기면 기다리지 않고 맞춘다. */
  whole: boolean;
  /** 좁힐 수 있게 된 world 시각(ms). 좁힐 수 없으면 null. */
  shrinkSince: number | null;
}

export interface MinimapInput {
  zones: readonly PlanZone[];
  tracks: readonly WorldTrack[];
  /** 방 범위(평면도 또는 기본 10×10 m). */
  bounds: PlanBounds;
}

const EPS = 1e-6;

const pad = (b: PlanBounds, m: number): PlanBounds => ({
  minX: b.minX - m,
  minY: b.minY - m,
  maxX: b.maxX + m,
  maxY: b.maxY + m,
});

const union = (a: PlanBounds | null, b: PlanBounds): PlanBounds =>
  a
    ? {
        minX: Math.min(a.minX, b.minX),
        minY: Math.min(a.minY, b.minY),
        maxX: Math.max(a.maxX, b.maxX),
        maxY: Math.max(a.maxY, b.maxY),
      }
    : b;

const contains = (outer: PlanBounds, inner: PlanBounds) =>
  inner.minX >= outer.minX - EPS &&
  inner.minY >= outer.minY - EPS &&
  inner.maxX <= outer.maxX + EPS &&
  inner.maxY <= outer.maxY + EPS;

const sameBounds = (a: PlanBounds, b: PlanBounds) => contains(a, b) && contains(b, a);

const side = (b: PlanBounds) => Math.max(boundsWidth(b), boundsDepth(b));

/** 구역 상자들을 감싸는 상자(다각형 구역도 둘레 상자 min·max 를 쓴다). 구역이 없으면 null. */
function zonesBox(zones: readonly PlanZone[]): PlanBounds | null {
  let box: PlanBounds | null = null;
  for (const zone of zones) {
    box = union(box, {
      minX: zone.min[0],
      minY: zone.min[1],
      maxX: zone.max[0],
      maxY: zone.max[1],
    });
  }
  return box;
}

/** 미니맵이 넘지 않는 범위: 큰 평면이 비추는 범위(방 + 여백)와 구역 전체(+ 여백). */
function limitOf({ zones, bounds }: MinimapInput): PlanBounds {
  const zone = zonesBox(zones);
  const room = pad(bounds, PLAN_MARGIN);
  return zone ? union(room, pad(zone, FIT_PAD_M)) : room;
}

/**
 * 맞출 내용: 구역 전체와 지금 위치가 있는 사람(관측·예측). 확인 불가 사람은 위치가 지금 것이 아니라 넣지 않는다.
 * 미니맵이 넘지 않는 범위 밖의 사람(엉뚱한 값)은 큰 평면에도 보이지 않으므로 넣지 않는다.
 */
function contentOf(input: MinimapInput, limit: PlanBounds): PlanBounds | null {
  let box = zonesBox(input.zones);
  for (const track of input.tracks) {
    if (trackLook(track) === 'unknown') continue;
    const [x, y] = track.position_xy;
    if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
    const point = { minX: x, minY: y, maxX: x, maxY: y };
    if (contains(limit, point)) box = union(box, point);
  }
  return box;
}

/** 한 축의 구간 [center ± length/2] 를 [lo, hi] 안으로 민다. 더 길면 [lo, hi] 전체. */
function fitAxis(center: number, length: number, lo: number, hi: number): [number, number] {
  if (length >= hi - lo) return [lo, hi];
  const start = Math.min(Math.max(center - length / 2, lo), hi - length);
  return [start, start + length];
}

/** 상자를 최소 한 변 이상의 정사각형으로 키우고(미니맵 칸이 정사각형이다) 넘지 않는 범위 안으로 민다. */
function squareIn(box: PlanBounds, limit: PlanBounds): PlanBounds {
  const length = Math.max(side(box), FIT_MIN_EXTENT_M);
  const [minX, maxX] = fitAxis((box.minX + box.maxX) / 2, length, limit.minX, limit.maxX);
  const [minY, maxY] = fitAxis((box.minY + box.maxY) / 2, length, limit.minY, limit.maxY);
  return { minX, minY, maxX, maxY };
}

/** 내용에 여백을 두고 맞춘 범위(앞 상태 없이). */
export function fitView(input: MinimapInput): PlanBounds {
  const limit = limitOf(input);
  const content = contentOf(input, limit);
  return content ? squareIn(pad(content, FIT_PAD_M), limit) : limit;
}

/**
 * 이번 world(시각 now, ms)에서 미니맵 범위. 순수 함수이고, 바뀌지 않으면 앞 상태 객체를 그대로 돌려준다(다시 그리지 않게).
 * - 구역·사람이 없으면 방 전체.
 * - 처음이거나 방 전체를 비추던 중이면 바로 맞춘다.
 * - 내용이 지금 범위의 안쪽 선(FIT_EDGE_M)을 넘으면 여유(FIT_GROW_SLACK_M)를 더해 바로 넓힌다.
 * - 맞출 범위가 충분히 작아진 채 FIT_SHRINK_AFTER_MS 가 지나야 좁힌다.
 */
export function nextMinimapView(
  prev: MinimapView | null,
  input: MinimapInput,
  now: number,
): MinimapView {
  const limit = limitOf(input);
  const content = contentOf(input, limit);
  if (!content) {
    return prev?.whole && sameBounds(prev.view, limit)
      ? prev
      : { view: limit, whole: true, shrinkSince: null };
  }
  const target = squareIn(pad(content, FIT_PAD_M), limit);
  // 처음, 방 전체에서 내용이 생김, 방 범위가 바뀌어 지금 범위가 그 밖으로 나감 → 바로 맞춘다.
  if (!prev || prev.whole || !contains(limit, prev.view)) {
    return { view: target, whole: false, shrinkSince: null };
  }
  if (!contains(pad(prev.view, -FIT_EDGE_M), content)) {
    const grown = squareIn(union(prev.view, pad(content, FIT_PAD_M + FIT_GROW_SLACK_M)), limit);
    if (!sameBounds(grown, prev.view)) return { view: grown, whole: false, shrinkSince: null };
    // 이미 넘지 않는 범위 끝까지 넓혔다.
    return prev.shrinkSince === null ? prev : { ...prev, shrinkSince: null };
  }
  if (side(target) < side(prev.view) * FIT_SHRINK_RATIO) {
    // 시계가 되돌아갔으면(재접속·서버 재시작) 다시 센다.
    if (prev.shrinkSince === null || now < prev.shrinkSince) return { ...prev, shrinkSince: now };
    if (now - prev.shrinkSince >= FIT_SHRINK_AFTER_MS) {
      return { view: target, whole: false, shrinkSince: null };
    }
    return prev;
  }
  return prev.shrinkSince === null ? prev : { ...prev, shrinkSince: null };
}

/**
 * 미니맵 표시 크기. 범위가 넓을수록 사람 표식·구역 선을 키워 칸 안에서 거의 같은 화면 크기로 보이게 하고,
 * 구역 채움을 짙게 한다. 사람 위치 불확실 원은 실제 크기(m)라 키우지 않는다.
 */
export function minimapEmphasis(view: PlanBounds): PlanEmphasis {
  const marker = Math.min(
    Math.max(side(view) / MARKER_REF_EXTENT_M, MARKER_SCALE.min),
    MARKER_SCALE.max,
  );
  return { marker, zoneStroke: marker * ZONE_STROKE_GAIN, zoneFill: MINIMAP_ZONE_FILL };
}
