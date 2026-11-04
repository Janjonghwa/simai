import { sameGeometry, sameName } from '@/features/surfaces/surfaces';
import type { SurfaceGeometry, WorkSurface } from '@/features/surfaces/types';
import type { ViewerBoxEdit, ViewerBoxGhost, ViewerEditBox } from '@/features/viewer3d';
import type { Severity, Zone, ZoneBox } from '@/features/zones/types';
import type { ZoneForm } from '@/features/zones/zoneForm';
import type { RoomLimit } from '@/lib/roomLimit';

// 구역 편집 화면의 초안 모델(순수 계산). 이름·심각도 고침, 구역 상자, 작업면 하나를 구역(또는 새 구역) 하나의 초안으로 묶는다.

/** 새 구역 양식의 초안 키. */
export const NEW_KEY = '__new__';
/** 화면에서 고르지 않는 칸의 기본값. 새 구역은 이 값으로 만들고, 기존 구역은 받은 값을 그대로 둔다. */
export const NEW_ZONE_KIND = 'OPENING';
export const NEW_EVAL_TYPE = 'VOLUME_3D';
export const NEW_ZONE_SEVERITY: Severity = 'HIGH';
/** 이름을 정하지 않은 새 구역의 기본 이름 앞부분(`구역 1`, `구역 2`…). */
const NEW_ZONE_NAME_PREFIX = '구역';

export const SEVERITY_LABEL: Record<Severity, string> = {
  CRITICAL: '위험',
  HIGH: '경고',
  CAUTION: '주의',
  INFO: '안내',
};

const NEW_ZONE_SIZE_M = 2;
const NEW_ZONE_HEIGHT_M = 2;
/** 작업면을 붙인 새 구역이 작업면 둘레에 남기는 여유(m). */
const SURFACE_ZONE_MARGIN_M = 0.3;
/** 새 작업면의 크기·상판 높이(m, 관제 데모와 같다). */
const NEW_SURFACE_SIZE_M = 1;
const NEW_SURFACE_TOP_M = 0.4;
/** 좌표(m)·각도(rad)가 이 값보다 덜 다르면 같다고 본다. */
const SAME_EPS = 1e-6;
/** 좌표를 맞추는 배율(0.001 m 단위). */
const ROUND_SCALE = 1000;

/** 작업면을 새로 만들거나 바꿀 때 중심이 구역 밖이면 그 구역의 작업면이 아니라 저장하지 않는다. */
export const SURFACE_OUTSIDE_TEXT = '작업면 중심이 구역 안에 있어야 합니다.';

const r3 = (v: number) => Math.round(v * ROUND_SCALE) / ROUND_SCALE;
const clamp = (v: number, lo: number, hi: number) => Math.min(Math.max(v, lo), hi);

type LinkedSurface = Pick<WorkSurface, 'objectId' | 'name' | 'geometry'>;

/** 작업면 초안. id 가 null 이면 아직 등록하지 않은 새 작업면이다. */
export interface SurfaceDraft {
  id: string | null;
  name: string | null;
  geometry: SurfaceGeometry;
}

/** 고친 칸. 구역 종류·판정 유형은 화면에서 고르지 않으므로 이름과 심각도만 둔다. */
export interface ZonePatch {
  name?: string;
  severity?: Severity;
}

/**
 * 구역(또는 새 구역) 하나의 저장 전 초안. box 는 고친 구역 상자(안 고쳤으면 null), surface 는 undefined 가 건드리지 않음,
 * null 이 저장된 작업면을 지움, 객체가 만들었거나 고친 작업면이다.
 */
export interface TargetDraft {
  patch: ZonePatch;
  box: ViewerEditBox | null;
  surface?: SurfaceDraft | null;
}

export function boxOfZone(zone: ZoneBox): ViewerEditBox {
  return {
    min: [zone.min[0], zone.min[1], zone.min[2]],
    max: [zone.max[0], zone.max[1], zone.max[2]],
  };
}

/** 두 상자의 좌표가 모두 같은지(오차 SAME_EPS 안). */
function sameEditBox(a: ViewerEditBox, b: ViewerEditBox): boolean {
  return (
    a.min.every((v, i) => Math.abs(v - b.min[i]!) <= SAME_EPS) &&
    a.max.every((v, i) => Math.abs(v - b.max[i]!) <= SAME_EPS)
  );
}

/**
 * 새 구역의 처음 상자: 기존 구역들을 감싼 상자의 가운데 바닥에 2×2×2 m, 구역이 없으면 방 가운데. 교실 안에 둔다.
 * 교실 범위(limit)는 3D 손잡이의 범위와 같다: 스캔이 보이면 스캔 범위, 아니면 목업 교실(10×10 m).
 */
export function defaultNewBox(zones: readonly ZoneBox[], limit: RoomLimit): ViewerEditBox {
  const mid = (axis: 0 | 1) =>
    zones.length === 0
      ? (limit.min[axis] + limit.max[axis]) / 2
      : (Math.min(...zones.map((z) => z.min[axis])) + Math.max(...zones.map((z) => z.max[axis]))) /
        2;
  const half = NEW_ZONE_SIZE_M / 2;
  const cx = clamp(r3(mid(0)), limit.min[0] + half, limit.max[0] - half);
  const cy = clamp(r3(mid(1)), limit.min[1] + half, limit.max[1] - half);
  return { min: [cx - half, cy - half, 0], max: [cx + half, cy + half, NEW_ZONE_HEIGHT_M] };
}

/**
 * 작업면을 붙인 새 구역의 처음 상자: 회전한 작업면 발자국을 감싼 축 정렬 상자에 여유(SURFACE_ZONE_MARGIN_M)를 더하고, 바닥에서
 * 새 구역 높이와 상판 중 큰 값까지. 구역 상자는 직사각형이라 회전한 발자국은 감싸는 상자로 잡는다. 3D 손잡이의 범위 밖에서
 * 시작하지 않게 defaultNewBox 처럼 교실 범위(limit) 안으로 자른다.
 */
export function surfaceZoneBox(g: SurfaceGeometry, limit: RoomLimit): ViewerEditBox {
  const [cx, cy] = g.centerXy;
  const [width, depth] = g.sizeXy;
  const cos = Math.abs(Math.cos(g.yawRad));
  const sin = Math.abs(Math.sin(g.yawRad));
  const halfX = (width * cos + depth * sin) / 2 + SURFACE_ZONE_MARGIN_M;
  const halfY = (width * sin + depth * cos) / 2 + SURFACE_ZONE_MARGIN_M;
  return {
    min: [r3(Math.max(cx - halfX, limit.min[0])), r3(Math.max(cy - halfY, limit.min[1])), 0],
    max: [
      r3(Math.min(cx + halfX, limit.max[0])),
      r3(Math.min(cy + halfY, limit.max[1])),
      r3(Math.min(Math.max(NEW_ZONE_HEIGHT_M, g.topHeightM), limit.max[2])),
    ],
  };
}

/** 작업면 형상 → 3D 편집 상자(바닥 0 ~ 상판). 회전은 상자에 나타나지 않는다. */
export function surfaceBox(g: SurfaceGeometry): ViewerEditBox {
  const [cx, cy] = g.centerXy;
  const [w, d] = g.sizeXy;
  return {
    min: [r3(cx - w / 2), r3(cy - d / 2), 0],
    max: [r3(cx + w / 2), r3(cy + d / 2), r3(g.topHeightM)],
  };
}

/** 편집 상자 → 작업면 형상. 회전은 base 의 값을 그대로 둔다. */
export function surfaceOfBox(box: ViewerEditBox, base: SurfaceGeometry): SurfaceGeometry {
  return {
    centerXy: [r3((box.min[0] + box.max[0]) / 2), r3((box.min[1] + box.max[1]) / 2)],
    sizeXy: [r3(box.max[0] - box.min[0]), r3(box.max[1] - box.min[1])],
    yawRad: base.yawRad,
    topHeightM: r3(box.max[2]),
  };
}

/** 구역 상자 가운데에 1×1 m, 상판 0.4 m 로 놓는 새 작업면 초안. */
export function newSurfaceDraft(zoneBox: ViewerEditBox): SurfaceDraft {
  return {
    id: null,
    name: null,
    geometry: {
      centerXy: [
        r3((zoneBox.min[0] + zoneBox.max[0]) / 2),
        r3((zoneBox.min[1] + zoneBox.max[1]) / 2),
      ],
      sizeXy: [NEW_SURFACE_SIZE_M, NEW_SURFACE_SIZE_M],
      yawRad: 0,
      topHeightM: NEW_SURFACE_TOP_M,
    },
  };
}

export const surfaceDraftOf = (surface: LinkedSurface): SurfaceDraft => ({
  id: surface.objectId,
  name: surface.name,
  geometry: {
    ...surface.geometry,
    centerXy: [...surface.geometry.centerXy],
    sizeXy: [...surface.geometry.sizeXy],
  },
});

/**
 * 크기를 그대로 두고 바닥(X·Y)만 옮긴 상자면 그 이동량(m), 아니면 null. 구역을 옮기면 이은 작업면도 같이 옮겨 중심이
 * 구역 안에 남게 한다(크기를 바꾸면 작업면은 그대로 두고, 중심이 구역 밖이 되면 저장을 막는다).
 */
export function floorShift(from: ViewerEditBox, to: ViewerEditBox): [number, number] | null {
  const sizeKept = [0, 1, 2].every(
    (axis) =>
      Math.abs(to.max[axis]! - to.min[axis]! - (from.max[axis]! - from.min[axis]!)) < SAME_EPS,
  );
  const heightKept = Math.abs(to.min[2] - from.min[2]) < SAME_EPS;
  if (!sizeKept || !heightKept) return null;
  const shift: [number, number] = [r3(to.min[0] - from.min[0]), r3(to.min[1] - from.min[1])];
  return shift[0] === 0 && shift[1] === 0 ? null : shift;
}

/** 작업면을 바닥에서 shift 만큼 옮긴 초안. */
const shiftedSurface = (surface: SurfaceDraft, shift: readonly [number, number]): SurfaceDraft => ({
  ...surface,
  geometry: {
    ...surface.geometry,
    centerXy: [
      r3(surface.geometry.centerXy[0] + shift[0]),
      r3(surface.geometry.centerXy[1] + shift[1]),
    ],
  },
});

/** 회전이 없는 작업면만 상자 손잡이로 고친다(회전한 작업면은 높이 칸만 고친다). */
export const surfaceBoxEditable = (g: SurfaceGeometry) => Math.abs(g.yawRad) < SAME_EPS;

/** 이은 저장 작업면과 다른지. 같은 작업면이면 값이 같을 때 바뀐 것이 아니다. */
export function surfaceChanged(
  draft: SurfaceDraft | null | undefined,
  linked: LinkedSurface | null,
): boolean {
  if (draft === undefined) return false;
  if (draft === null) return linked !== null;
  if (linked === null || linked.objectId !== draft.id) return true;
  return !sameName(draft.name ?? '', linked.name) || !sameGeometry(draft.geometry, linked.geometry);
}

/** 이 구역의 지금 보이는 작업면 초안: 건드렸으면 그 값, 아니면 이은 작업면 그대로(없으면 null). */
export function currentSurface(
  draft: TargetDraft | undefined,
  linked: LinkedSurface | null,
): SurfaceDraft | null {
  if (draft?.surface !== undefined) return draft.surface;
  return linked ? surfaceDraftOf(linked) : null;
}

/** 구역의 저장된 값에 고친 칸을 덮은 양식. */
export function formWithPatch(zone: Zone, patch: ZonePatch): ZoneForm {
  return {
    name: patch.name ?? zone.name,
    judgeType: zone.judgeType,
    zoneType: zone.zoneType,
    severity: patch.severity ?? zone.severity,
  };
}

/**
 * 이름을 정하지 않은 새 구역의 기본 이름: 겹치지 않는 가장 작은 번호를 붙인다(`구역 1`, `구역 2`…).
 * 이미 같은 이름이 있으면 번호를 건너뛰어, 드롭다운에서 구역을 이름만으로 가릴 수 있게 한다.
 */
export function nextZoneName(zones: readonly Pick<ZoneBox, 'name'>[]): string {
  const taken = new Set(zones.map((zone) => zone.name.trim()));
  let number = 1;
  while (taken.has(`${NEW_ZONE_NAME_PREFIX} ${number}`)) number += 1;
  return `${NEW_ZONE_NAME_PREFIX} ${number}`;
}

/** 새 구역의 양식. 이름을 고치지 않았으면 defaultName 이고, 화면에서 고르지 않는 칸은 기본값이다. */
export function newZoneForm(patch: ZonePatch, defaultName: string): ZoneForm {
  return {
    name: patch.name ?? defaultName,
    judgeType: NEW_EVAL_TYPE,
    zoneType: NEW_ZONE_KIND,
    severity: patch.severity ?? NEW_ZONE_SEVERITY,
  };
}

/** 저장된 값과 같은 칸은 고친 것이 아니다. */
export function changedPatch(zone: Zone, next: ZonePatch): ZonePatch {
  const patch: ZonePatch = {};
  if (next.name !== undefined && next.name !== zone.name) patch.name = next.name;
  if (next.severity !== undefined && next.severity !== zone.severity)
    patch.severity = next.severity;
  return patch;
}

/** 원본과 다르게 고친 구역 상자. 안 고쳤으면 null. */
export function editedBox(zone: Zone, draft: TargetDraft | undefined): ViewerEditBox | null {
  return draft?.box && !sameEditBox(draft.box, boxOfZone(zone)) ? draft.box : null;
}

/** 구역(속성·상자)에서 저장 전에 고친 것이 있는지(작업면은 따로 본다). */
export function zoneChanged(zone: Zone, draft: TargetDraft | undefined): boolean {
  if (!draft) return false;
  return Object.keys(changedPatch(zone, draft.patch)).length > 0 || editedBox(zone, draft) !== null;
}

/** 구역이나 작업면에서 저장 전에 고친 것이 있는지. */
export function targetChanged(
  zone: Zone,
  draft: TargetDraft | undefined,
  linked: LinkedSurface | null,
): boolean {
  return zoneChanged(zone, draft) || surfaceChanged(draft?.surface, linked);
}

// ---- 초안 갱신: 지금 초안(prev)에서 한 곳만 바꾼 새 초안 ------------------------------------------

/** 초안이 비었는지(고친 칸·상자·작업면이 없다). 빈 초안은 저장소에서 지운다. */
export const isEmptyDraft = (draft: TargetDraft): boolean =>
  Object.keys(draft.patch).length === 0 && draft.box === null && draft.surface === undefined;

/** 작업면 칸을 그대로 이어받는 값(건드리지 않았으면 칸을 두지 않는다). */
const keepSurface = (prev: TargetDraft | undefined) =>
  prev?.surface === undefined ? {} : { surface: prev.surface };

/** 이름·심각도를 고친 초안. 기존 구역이면 저장된 값과 같은 칸은 뺀다. */
export function withPatch(
  prev: TargetDraft | undefined,
  zone: Zone | null,
  next: ZonePatch,
): TargetDraft {
  const merged = { ...prev?.patch, ...next };
  return {
    patch: zone ? changedPatch(zone, merged) : merged,
    box: prev?.box ?? null,
    ...keepSurface(prev),
  };
}

/** 작업면을 바꾼 초안(undefined 는 건드리지 않음, null 은 지움). */
export function withSurface(
  prev: TargetDraft | undefined,
  next: SurfaceDraft | null | undefined,
): TargetDraft {
  return {
    patch: prev?.patch ?? {},
    box: prev?.box ?? null,
    ...(next === undefined ? {} : { surface: next }),
  };
}

/** 3D 에서 작업면 상자를 끈 초안. */
export function withSurfaceBox(
  prev: TargetDraft | undefined,
  surface: SurfaceDraft,
  box: ViewerEditBox,
): TargetDraft {
  return withSurface(prev, { ...surface, geometry: surfaceOfBox(box, surface.geometry) });
}

/**
 * 3D 에서 구역 상자를 끈 초안. 원본(zone)과 같아지면 고친 상자를 지운다. 크기 그대로 옮겼으면 이은 작업면도 같이 옮긴다
 * (그대로 두면 중심이 구역 밖에 남는다). before 는 끌기 전 상자다.
 */
export function withZoneBox(
  prev: TargetDraft | undefined,
  zone: Zone | null,
  surface: SurfaceDraft | null,
  before: ViewerEditBox,
  box: ViewerEditBox,
): TargetDraft {
  const shift = surface ? floorShift(before, box) : null;
  return {
    patch: prev?.patch ?? {},
    box: zone && sameEditBox(box, boxOfZone(zone)) ? null : box,
    ...(surface && shift ? { surface: shiftedSurface(surface, shift) } : keepSurface(prev)),
  };
}

/**
 * 이은 작업면(linked)과 값이 같아진 작업면 초안을 뺀다. 높이를 바꿨다가 되돌린 것처럼 실제로 바뀐 것이 없는 초안이
 * 남으면, 초안이 있다는 이유로 편집을 끌 때 버릴 것이 있는지 묻게 된다(바뀐 것만 초안이다).
 */
export function withoutUnchangedSurface(
  draft: TargetDraft,
  linked: LinkedSurface | null,
): TargetDraft {
  if (draft.surface === undefined || surfaceChanged(draft.surface, linked)) return draft;
  return { patch: draft.patch, box: draft.box };
}

// ---- 3D 로 내려 줄 값 ---------------------------------------------------------------------------

/**
 * 3D 손잡이가 고칠 상자. 작업면을 고르는 중이면 작업면 상자(회전한 작업면·형상 오류가 있으면 없음), 아니면 구역 상자다.
 * 형상을 못 고치는 구역이면 null 이다.
 */
export function boxEditOf(input: {
  surfMode: boolean;
  key: string;
  zone: Zone | null;
  zoneBox: ViewerEditBox;
  surface: SurfaceDraft | null;
  surfaceValid: boolean;
}): ViewerBoxEdit | null {
  const { surfMode, key, zone, zoneBox, surface, surfaceValid } = input;
  if (surfMode) {
    return surface && surfaceBoxEditable(surface.geometry) && surfaceValid
      ? { id: `surface:${key}`, kind: 'surface', box: surfaceBox(surface.geometry) }
      : null;
  }
  return !zone || zone.shapeEditable ? { id: key, kind: 'zone', box: zoneBox } : null;
}

/**
 * 윤곽만 보이는 보관 초안(다른 구역의 고친 상자). 작업면을 조절하는 동안은 이 구역의 고친 상자도 윤곽으로 남긴다.
 * 없어진 구역의 초안은 건너뛴다.
 */
export function ghostsOf(
  drafts: Readonly<Record<string, TargetDraft>>,
  key: string,
  surfMode: boolean,
  zones: readonly ZoneBox[],
): ViewerBoxGhost[] {
  return Object.entries(drafts).flatMap(([id, draft]) => {
    if (!draft.box || (id === key && !surfMode)) return [];
    if (id !== NEW_KEY && !zones.some((z) => z.zoneId === id)) return [];
    return [{ id, box: draft.box }];
  });
}

// ---- 3D 로 내려 줄 값 비교 --------------------------------------------------------------------

/** 두 상자의 좌표가 정확히 같은지. */
export const sameBox = (a: ViewerEditBox, b: ViewerEditBox) =>
  [0, 1, 2].every((axis) => a.min[axis] === b.min[axis] && a.max[axis] === b.max[axis]);

/** 3D 에 내려 줄 편집 상자가 같은지(값 비교). 같으면 저장소를 다시 쓰지 않아 3D 를 다시 그리지 않는다. */
export function sameBoxEdit(a: ViewerBoxEdit | null, b: ViewerBoxEdit | null): boolean {
  if (a === null || b === null) return a === b;
  return a.id === b.id && a.kind === b.kind && sameBox(a.box, b.box);
}

/** 보관 초안 윤곽 목록이 같은지(값 비교). */
export function sameGhosts(a: readonly ViewerBoxGhost[], b: readonly ViewerBoxGhost[]): boolean {
  return a.length === b.length && a.every((g, i) => g.id === b[i]!.id && sameBox(g.box, b[i]!.box));
}
