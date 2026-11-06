import { boxOfDraftItem } from '@/features/setup/setup';
import type { ZoneDraftItem } from '@/features/setup/types';
import { geometryErrors, wrapYaw } from '@/features/surfaces/surfaces';
import type { SurfaceGeometry } from '@/features/surfaces/types';
import type { ViewerEditBox } from '@/features/viewer3d';
import type { Zone, ZoneCommitRequest } from '@/features/zones/types';
import { createRequestOf } from '@/features/zones/zoneCommit';
import {
  newZoneForm,
  r3,
  SURFACE_ZONE_MARGIN_M,
  surfaceZoneForm,
  surfaceZoneShape,
  type SurfaceDraft,
} from '@/features/zones/zoneEditModel';
import type { ZoneForm, ZoneShape } from '@/features/zones/zoneForm';

// 세팅 AI 구역 초안 → 구역 저장 요청(순수 계산). 고소 작업면 초안은 작업면과 그 둘레 구역(관제에서 작업면을 추가할 때와 같은
// surfaceZoneShape)이고, 그 밖(개구부·바닥 가장자리, 상판 높이를 믿을 수 없는 작업면)은 AI 다각형을 바깥으로
// SURFACE_ZONE_MARGIN_M 민 구역이다. 늘 AI 원본 초안에서 계산하므로 같은 초안을 몇 번 계산해도 한 번만 넓혀진다.

type Point = readonly [number, number];

/** 작업면으로 믿는 상판 높이(m). 밖이면(깨진 Z 범위 등) 작업면을 만들지 않고 일반 구역으로 둔다. */
const SURFACE_TOP_MIN_M = 0.2;
const SURFACE_TOP_MAX_M = 3;
/** 모서리 연장선 교점이 꼭짓점에서 여유의 이 배수보다 멀면 그 거리에서 모서리를 자른다(뾰족한 모서리). */
const MITER_LIMIT = 2;
/** 구역 좌표가 초안 계산과 같다고 볼 차이(m). 서버가 돌려준 좌표와 견준다. */
const SAME_POINT_EPS_M = 1e-4;

/** 초안 하나로 만들 구역: 양식·모양과, 고소 작업면이면 함께 만들 작업면. */
interface DraftZonePlan {
  form: ZoneForm;
  shape: ZoneShape;
  surface: SurfaceDraft | null;
}

/**
 * 고소 작업면 초안의 작업면 형상. 작업면 초안이고 obb 가 있으며 상판 높이(top_height_m, 없으면 obb 높이)가 믿을 만한
 * 범위이고 형상 검사를 통과할 때만 만든다. 아니면 null(지어내지 않는다).
 */
export function draftSurfaceGeometry(item: ZoneDraftItem): SurfaceGeometry | null {
  const { obb } = item;
  if (item.candidate_kind !== 'ELEVATED_WORK_SURFACE' || obb === null) return null;
  const top = item.top_height_m ?? obb.size[2];
  if (!(top >= SURFACE_TOP_MIN_M && top <= SURFACE_TOP_MAX_M)) return null;
  const geometry: SurfaceGeometry = {
    centerXy: [obb.center[0], obb.center[1]],
    sizeXy: [obb.size[0], obb.size[1]],
    yawRad: wrapYaw(obb.yaw_rad),
    topHeightM: top,
  };
  return geometryErrors(geometry) === null ? geometry : null;
}

/** o→a 와 o→b 의 외적. 양수면 o→a→b 가 왼쪽(반시계)으로 돈다. */
const cross = (o: Point, a: Point, b: Point) =>
  (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);

/** 볼록 껍질(반시계, 한 직선 위의 점은 뺀다). 점이 한 직선 위에만 있으면 3개보다 적다. */
export function convexHull(points: readonly Point[]): Point[] {
  const sorted = [...points].sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const chain = (pts: readonly Point[]) => {
    const out: Point[] = [];
    for (const p of pts) {
      while (out.length >= 2 && cross(out[out.length - 2]!, out[out.length - 1]!, p) <= 0) {
        out.pop();
      }
      out.push(p);
    }
    out.pop();
    return out;
  };
  return [...chain(sorted), ...chain([...sorted].reverse())];
}

/** 단위 방향과 바깥쪽 법선(반시계 다각형이면 오른쪽). */
function edgeFrame(from: Point, to: Point) {
  const length = Math.hypot(to[0] - from[0], to[1] - from[1]);
  const dir: Point = [(to[0] - from[0]) / length, (to[1] - from[1]) / length];
  const normal: Point = [dir[1], -dir[0]];
  return { dir, normal };
}

/**
 * 볼록 다각형(반시계)의 각 변을 바깥으로 margin 만큼 민다. 모서리는 이웃 변 연장선의 교점으로 잇고, 그 교점이 꼭짓점에서
 * margin×MITER_LIMIT 보다 멀면(뾰족한 모서리) 그 거리에서 이등분선에 수직으로 잘라 두 점으로 둔다. 어느 쪽이든 원래 다각형의
 * 모든 점에서 margin 이상 떨어진다. 좌표는 0.001 m 로 맞춘다.
 */
export function bufferConvexPolygon(hull: readonly Point[], margin: number): [number, number][] {
  const limit = margin * MITER_LIMIT;
  return hull.flatMap((p, i) => {
    const prev = edgeFrame(hull[(i - 1 + hull.length) % hull.length]!, p);
    const next = edgeFrame(p, hull[(i + 1) % hull.length]!);
    const sum: Point = [prev.normal[0] + next.normal[0], prev.normal[1] + next.normal[1]];
    const sumLength = Math.hypot(sum[0], sum[1]);
    const bisector: Point = [sum[0] / sumLength, sum[1] / sumLength];
    // 법선과 이등분선 사이 각의 코사인. 교점은 꼭짓점에서 margin / cos 만큼 떨어진다.
    const cos = prev.normal[0] * bisector[0] + prev.normal[1] * bisector[1];
    const at = (dx: number, dy: number): [number, number] => [r3(p[0] + dx), r3(p[1] + dy)];
    const reach = margin / cos;
    if (reach <= limit) return [at(bisector[0] * reach, bisector[1] * reach)];
    // 민 변 위에서 이등분선 방향 거리가 limit 인 점: margin·cos + s·(방향·이등분선) = limit.
    const sin = Math.sqrt(1 - cos * cos);
    const slide = (limit - margin * cos) / sin;
    return [
      at(
        prev.normal[0] * margin + prev.dir[0] * slide,
        prev.normal[1] * margin + prev.dir[1] * slide,
      ),
      at(
        next.normal[0] * margin - next.dir[0] * slide,
        next.normal[1] * margin - next.dir[1] * slide,
      ),
    ];
  });
}

/** 편집 상자의 바닥 네 꼭짓점(반시계). */
const boxCorners = ({ min, max }: ViewerEditBox): Point[] => [
  [min[0], min[1]],
  [max[0], min[1]],
  [max[0], max[1]],
  [min[0], max[1]],
];

/**
 * 작업면이 없는 초안의 구역 모양: AI 다각형의 볼록 껍질을 SURFACE_ZONE_MARGIN_M 바깥으로 민 다각형. 점이 한 직선 위에만
 * 있으면 초안 상자(boxOfDraftItem)의 바닥을 쓴다. 높이는 초안 상자 그대로다(바닥 아래는 자르고, 너무 낮으면 2 m).
 */
function draftZoneShape(item: ZoneDraftItem): ZoneShape {
  const box = boxOfDraftItem(item);
  const hull = convexHull(item.polygon_xy);
  const outline = hull.length >= 3 ? hull : boxCorners(box);
  return {
    polygon_xy: bufferConvexPolygon(outline, SURFACE_ZONE_MARGIN_M),
    z_min_m: r3(box.min[2]),
    z_max_m: r3(box.max[2]),
  };
}

/** 초안 하나로 만들 구역. 작업면을 믿을 수 있으면 작업면 구역, 아니면 AI 다각형을 민 일반 구역이다. */
function planOf(item: ZoneDraftItem): DraftZonePlan {
  const name = item.recommended_name;
  const geometry = draftSurfaceGeometry(item);
  if (geometry) {
    return {
      form: surfaceZoneForm(newZoneForm({ name }, name)),
      shape: surfaceZoneShape(geometry),
      surface: { id: null, name: null, geometry },
    };
  }
  return {
    form: newZoneForm({ name, severity: item.recommended_severity }, name),
    shape: draftZoneShape(item),
    surface: null,
  };
}

/** 초안을 수락할 때 보낼 구역 만들기 요청(관제의 구역 추가와 같은 POST /api/zones/commit). */
export const draftCreateRequest = (item: ZoneDraftItem): ZoneCommitRequest =>
  createRequestOf(planOf(item));

/** 두 다각형이 같은 꼭짓점들인지(시작점·순서와 상관없이, 오차 SAME_POINT_EPS_M 안). */
function samePolygon(a: readonly Point[], b: readonly Point[]): boolean {
  const near = (p: Point, q: Point) =>
    Math.abs(p[0] - q[0]) <= SAME_POINT_EPS_M && Math.abs(p[1] - q[1]) <= SAME_POINT_EPS_M;
  return a.length === b.length && a.every((p) => b.some((q) => near(p, q)));
}

/**
 * 이미 구역으로 만든 초안 ID: 초안에서 계산한 구역 다각형이 지금 구역 중 하나와 같다. 새로 고쳐 수락 기록을 잊어도 같은
 * 초안으로 구역을 또 만들지 않게 한다.
 */
export function acceptedByZones(
  drafts: readonly ZoneDraftItem[],
  zones: readonly Pick<Zone, 'polygon'>[],
): string[] {
  return drafts
    .filter((item) => {
      const polygon = planOf(item).shape.polygon_xy;
      return zones.some((zone) => samePolygon(zone.polygon, polygon));
    })
    .map((item) => item.candidate_id);
}
