import type { WorkSurface } from '@/features/surfaces/types';
import type { LayoutWriteBody } from '@/features/tbm/api';
import type { LayoutDetail, LayoutObbItem } from '@/features/tbm/types';
import type {
  PlannedLayoutInput,
  ViewerBoxEdit,
  ViewerBoxGhost,
  ViewerEditBox,
} from '@/features/viewer3d';
import type { Zone } from '@/features/zones/types';

// 가상 레이아웃 3D 편집 모델(순수 계산).
// 좌표 규약(AGENTS.md 규칙 3): World 는 미터, 오른손, +Z 위, 바닥 Z = 0, 각도 라디안 [-π, π].
// 모르면 비운다(AGENTS.md 규칙 2): 구역에서 가져올 때 높이(z_min_m·z_max_m)가 없으면 기본값(0.1 m·바닥 0)으로 채우지 않고
// null 로 비워 두며, 저장 전에 검증으로 막는다.

export type LayoutItemKind = LayoutObbItem['kind'];

export const LAYOUT_ITEM_KIND_LABELS: Record<LayoutItemKind, string> = {
  OPENING: '개구부',
  SCAFFOLD: '비계',
  EQUIPMENT: '장비',
};

const ROUND_SCALE = 1000;
const r3 = (v: number): number => Math.round(v * ROUND_SCALE) / ROUND_SCALE;

const MAX_YAW_RAD = 3.1415926;

/** 라디안 각도를 [-π, π] 범위로 정규화한다. */
export function normalizeYawRad(rad: number): number {
  if (!Number.isFinite(rad)) return 0;
  const wrapped = ((((rad + Math.PI) % (2 * Math.PI)) + 2 * Math.PI) % (2 * Math.PI)) - Math.PI;
  const rounded = r3(wrapped);
  if (rounded > MAX_YAW_RAD) return 3.141;
  if (rounded < -MAX_YAW_RAD) return -3.141;
  return Object.is(rounded, -0) ? 0 : rounded;
}

/** 도(°) → 라디안(rad, [-π, π]). */
export function degToYawRad(deg: number): number {
  if (!Number.isFinite(deg)) return 0;
  return normalizeYawRad((deg * Math.PI) / 180);
}

/** 라디안(rad) → 도(°, 소수 첫째 자리). */
export function yawRadToDeg(rad: number): number {
  if (!Number.isFinite(rad)) return 0;
  return Math.round(((rad * 180) / Math.PI) * 10) / 10;
}

/**
 * 레이아웃 편집 초안 항목 하나.
 * bottomZ 또는 height 가 null 이면(예: 높이 없는 바닥 구역에서 가져온 개구부) 사용자가 직접 입력하기 전까지 저장을 막는다.
 */
export interface LayoutDraftItem {
  item_id: string;
  kind: LayoutItemKind;
  label: string;
  centerXy: [number, number];
  sizeXy: [number, number];
  /** 바닥(하단) 높이 Z(m). 미입력 상태면 null. */
  bottomZ: number | null;
  /** 물체 높이 Z 크기(m, > 0). 미입력 상태면 null. */
  height: number | null;
  yaw_rad: number;
}

const DEFAULT_SPEC: Record<
  LayoutItemKind,
  { prefix: string; labelPrefix: string; sizeXy: [number, number]; height: number }
> = {
  OPENING: { prefix: 'open', labelPrefix: '개구부', sizeXy: [1.2, 1.2], height: 0.2 },
  SCAFFOLD: { prefix: 'scaf', labelPrefix: '비계', sizeXy: [1.2, 2.0], height: 2.0 },
  EQUIPMENT: { prefix: 'equip', labelPrefix: '장비', sizeXy: [2.0, 2.0], height: 1.2 },
};

/** 서버 LayoutObbItem → 편집 초안 항목. */
export function obbToDraftItem(item: LayoutObbItem): LayoutDraftItem {
  const [cx, cy, cz] = item.center;
  const [sx, sy, sz] = item.size;
  const bottomZ = r3(cz - sz / 2);
  return {
    item_id: item.item_id,
    kind: item.kind,
    label: item.label ?? item.item_id,
    centerXy: [r3(cx), r3(cy)],
    sizeXy: [r3(sx), r3(sy)],
    bottomZ,
    height: r3(sz),
    yaw_rad: normalizeYawRad(item.yaw_rad ?? 0),
  };
}

/** 서버 LayoutDetail → 편집 초안 항목 배열. */
export function layoutDetailToDraftItems(
  layout: LayoutDetail | null | undefined,
): LayoutDraftItem[] {
  if (!layout) return [];
  return layout.items.map(obbToDraftItem);
}

/** 초안 항목의 Z 값(바닥 높이·높이)이 유효하게 채워져 있는지 검사한다. */
export function isDraftItemZComplete(
  item: LayoutDraftItem,
): item is LayoutDraftItem & { bottomZ: number; height: number } {
  return (
    item.bottomZ !== null &&
    Number.isFinite(item.bottomZ) &&
    item.height !== null &&
    Number.isFinite(item.height) &&
    item.height > 0
  );
}

/** 편집 초안 항목 → 서버 LayoutObbItem (Z가 미입력이면 null 반환). */
export function draftItemToObb(item: LayoutDraftItem): LayoutObbItem | null {
  if (!isDraftItemZComplete(item)) return null;
  const [cx, cy] = item.centerXy;
  const [sx, sy] = item.sizeXy;
  if (!Number.isFinite(cx) || !Number.isFinite(cy)) return null;
  if (!Number.isFinite(sx) || !Number.isFinite(sy) || sx <= 0 || sy <= 0) return null;
  const sz = r3(item.height);
  const cz = r3(item.bottomZ + sz / 2);
  const trimmedLabel = item.label.trim();
  return {
    item_id: item.item_id.trim(),
    kind: item.kind,
    center: [r3(cx), r3(cy), cz],
    size: [r3(sx), r3(sy), sz],
    yaw_rad: normalizeYawRad(item.yaw_rad),
    label: trimmedLabel.length > 0 ? trimmedLabel : null,
  };
}

/** 중복되지 않는 새 item_id 를 만든다. */
export function nextLayoutItemId(
  kind: LayoutItemKind,
  existingItems: readonly LayoutDraftItem[],
): string {
  const used = new Set(existingItems.map((i) => i.item_id));
  const prefix = DEFAULT_SPEC[kind].prefix;
  let seq = 1;
  while (used.has(`${prefix}-${String(seq).padStart(2, '0')}`)) {
    seq += 1;
  }
  return `${prefix}-${String(seq).padStart(2, '0')}`;
}

/**
 * 새 가상 물체를 만든다.
 * surface 를 넘기면 그 등록 작업면의 윗면(topHeightM)과 중심(centerXy) 위에 놓는다.
 * 없으면 바닥(Z=0)과 방 가운데(기본 [5, 5])에 놓는다.
 */
export function createDraftItem(
  kind: LayoutItemKind,
  existingItems: readonly LayoutDraftItem[],
  options?: {
    surface?: Pick<WorkSurface, 'name' | 'objectId' | 'geometry'> | null;
    roomCenterXy?: readonly [number, number];
  },
): LayoutDraftItem {
  const spec = DEFAULT_SPEC[kind];
  const item_id = nextLayoutItemId(kind, existingItems);
  const countOfKind = existingItems.filter((i) => i.kind === kind).length + 1;
  const surface = options?.surface ?? null;
  const baseCenter: [number, number] = surface
    ? [r3(surface.geometry.centerXy[0]), r3(surface.geometry.centerXy[1])]
    : options?.roomCenterXy
      ? [r3(options.roomCenterXy[0]), r3(options.roomCenterXy[1])]
      : [5, 5];
  const bottomZ = surface ? r3(surface.geometry.topHeightM) : 0;

  return {
    item_id,
    kind,
    label: `${spec.labelPrefix} ${countOfKind}`,
    centerXy: baseCenter,
    sizeXy: [...spec.sizeXy],
    bottomZ,
    height: spec.height,
    yaw_rad: surface ? normalizeYawRad(surface.geometry.yawRad) : 0,
  };
}

/**
 * 2D 다각형 꼭짓점 배열에서 최소 면적 외접 방향 상자(OBB: centerXy, sizeXy, yaw_rad)를 구한다.
 * 축 정렬 직사각형이면 yaw_rad = 0 이 되도록 후보 각도 첫 번째에 0 을 둔다.
 */
export function polygonToObb2D(polygon: readonly (readonly [number, number])[]): {
  centerXy: [number, number];
  sizeXy: [number, number];
  yaw_rad: number;
} {
  if (polygon.length === 0) {
    return { centerXy: [5, 5], sizeXy: [1, 1], yaw_rad: 0 };
  }

  // 닫힌 고리(마지막 점 === 첫 점)면 마지막 점을 뺀다.
  const pts =
    polygon.length > 3 &&
    polygon[0]![0] === polygon[polygon.length - 1]![0] &&
    polygon[0]![1] === polygon[polygon.length - 1]![1]
      ? polygon.slice(0, -1)
      : polygon;

  const candidateAngles: number[] = [0];
  for (let i = 0; i < pts.length; i++) {
    const a = pts[i]!;
    const b = pts[(i + 1) % pts.length]!;
    const dx = b[0] - a[0];
    const dy = b[1] - a[1];
    if (Math.hypot(dx, dy) > 1e-6) {
      candidateAngles.push(Math.atan2(dy, dx));
    }
  }

  let bestArea = Number.POSITIVE_INFINITY;
  let bestAngle = 0;
  let bestMinU = 0;
  let bestMaxU = 1;
  let bestMinV = 0;
  let bestMaxV = 1;

  for (const rawAngle of candidateAngles) {
    // [-π/2, π/2] 범위로 정규화해 불필요한 90°/180° 뒤집힘을 줄인다.
    let angle = normalizeYawRad(rawAngle);
    if (angle > Math.PI / 2) angle -= Math.PI;
    if (angle <= -Math.PI / 2) angle += Math.PI;
    if (Math.abs(angle) < 1e-6) angle = 0;

    const c = Math.cos(angle);
    const s = Math.sin(angle);
    let minU = Number.POSITIVE_INFINITY;
    let maxU = Number.NEGATIVE_INFINITY;
    let minV = Number.POSITIVE_INFINITY;
    let maxV = Number.NEGATIVE_INFINITY;

    for (const [x, y] of pts) {
      const u = x * c + y * s;
      const v = -x * s + y * c;
      if (u < minU) minU = u;
      if (u > maxU) maxU = u;
      if (v < minV) minV = v;
      if (v > maxV) maxV = v;
    }

    const w = Math.max(maxU - minU, 0.05);
    const d = Math.max(maxV - minV, 0.05);
    const area = w * d;
    if (area < bestArea - 1e-7) {
      bestArea = area;
      bestAngle = angle;
      bestMinU = minU;
      bestMaxU = maxU;
      bestMinV = minV;
      bestMaxV = maxV;
    }
  }

  const midU = (bestMinU + bestMaxU) / 2;
  const midV = (bestMinV + bestMaxV) / 2;
  const c = Math.cos(bestAngle);
  const s = Math.sin(bestAngle);
  const cx = midU * c - midV * s;
  const cy = midU * s + midV * c;
  const sx = Math.max(r3(bestMaxU - bestMinU), 0.1);
  const sy = Math.max(r3(bestMaxV - bestMinV), 0.1);

  return {
    centerXy: [r3(cx), r3(cy)],
    sizeXy: [sx, sy],
    yaw_rad: normalizeYawRad(bestAngle),
  };
}

/**
 * 등록된 위험 구역(/api/zones) 하나를 골라 그 다각형의 외접 방향 상자로 OPENING 하나를 만든다.
 * 높이·바닥 높이는 구역 값(zBounds / zRange)이 있으면 그것을 쓰고,
 * 없으면 기본 0.1 m·바닥 0 대신 비워 두고(null) 사용자에게 입력을 요구한다(규칙 2 모르면 비운다).
 */
export function importOpeningFromZone(
  zone: Pick<Zone, 'zoneId' | 'name' | 'polygon' | 'zBounds' | 'zRange'>,
  existingItems: readonly LayoutDraftItem[],
): LayoutDraftItem {
  const obb2d = polygonToObb2D(zone.polygon);
  const used = new Set(existingItems.map((i) => i.item_id));
  const baseId = `open-${zone.zoneId}`;
  let item_id = baseId;
  let seq = 2;
  while (used.has(item_id)) {
    item_id = `${baseId}-${seq}`;
    seq += 1;
  }

  const [zMin, zMax] = zone.zBounds;
  const hasBottom = zMin !== null && Number.isFinite(zMin);
  const hasHeight =
    zMin !== null && zMax !== null && Number.isFinite(zMin) && Number.isFinite(zMax) && zMax > zMin;

  return {
    item_id,
    kind: 'OPENING',
    label: zone.name ? `${zone.name} (${zone.zoneId})` : zone.zoneId,
    centerXy: obb2d.centerXy,
    sizeXy: obb2d.sizeXy,
    bottomZ: hasBottom ? r3(zMin) : null,
    height: hasHeight ? r3(zMax - zMin) : null,
    yaw_rad: obb2d.yaw_rad,
  };
}

/**
 * 초안 항목을 3D 상자 손잡이(ViewerEditBox)로 바꾼다.
 * 바닥 높이·높이가 아직 비어 있는 항목은 3D 상자 손잡이를 띄울 수 없으므로 null 을 돌려준다.
 */
export function draftItemToEditBox(item: LayoutDraftItem): ViewerEditBox | null {
  if (!isDraftItemZComplete(item)) return null;
  const [cx, cy] = item.centerXy;
  const [sx, sy] = item.sizeXy;
  return {
    min: [r3(cx - sx / 2), r3(cy - sy / 2), r3(item.bottomZ)],
    max: [r3(cx + sx / 2), r3(cy + sy / 2), r3(item.bottomZ + item.height)],
  };
}

/**
 * 3D 상자 손잡이(onBoxEditChange)가 올린 새 상자를 초안 항목에 반영한다.
 * 회전(yaw_rad)·종류·이름은 유지하고 중심·크기·바닥 높이·높이를 갱신한다.
 */
export function applyEditBoxToDraftItem(
  item: LayoutDraftItem,
  box: ViewerEditBox,
): LayoutDraftItem {
  const cx = r3((box.min[0] + box.max[0]) / 2);
  const cy = r3((box.min[1] + box.max[1]) / 2);
  const sx = Math.max(r3(box.max[0] - box.min[0]), 0.1);
  const sy = Math.max(r3(box.max[1] - box.min[1]), 0.1);
  const bottomZ = r3(box.min[2]);
  const height = Math.max(r3(box.max[2] - box.min[2]), 0.1);
  return {
    ...item,
    centerXy: [cx, cy],
    sizeXy: [sx, sy],
    bottomZ,
    height,
  };
}

/** 선택된 초안 항목에 대한 ViewerBoxEdit props 생성. */
export function buildLayoutBoxEdit(selectedItem: LayoutDraftItem | null): ViewerBoxEdit | null {
  if (!selectedItem) return null;
  const box = draftItemToEditBox(selectedItem);
  if (!box) return null;
  return {
    id: selectedItem.item_id,
    kind: 'zone',
    box,
  };
}

/** 선택되지 않은 나머지 초안 항목들의 윤곽(ViewerBoxGhost) 목록. */
export function buildLayoutBoxGhosts(
  items: readonly LayoutDraftItem[],
  selectedItemId: string | null,
): ViewerBoxGhost[] {
  return items.flatMap((item) => {
    if (item.item_id === selectedItemId) return [];
    const box = draftItemToEditBox(item);
    return box ? [{ id: item.item_id, box }] : [];
  });
}

/** Viewer3D 의 PLANNED 레이어에 넘길 PlannedLayoutInput 생성(Z가 채워진 항목만 3D 에 그린다). */
export function buildPlannedLayoutInput(
  layoutId: number | null,
  items: readonly LayoutDraftItem[],
): PlannedLayoutInput {
  return {
    layoutId: layoutId !== null ? `layout-${layoutId}` : 'layout-draft',
    objects: items.flatMap((item) => {
      const obb = draftItemToObb(item);
      if (!obb) return [];
      return [
        {
          id: obb.item_id,
          kind: obb.kind,
          label: obb.label,
          geometry: {
            center: obb.center,
            size: obb.size,
            yawRad: obb.yaw_rad,
          },
        },
      ];
    }),
  };
}

/**
 * 저장 전 레이아웃 초안 전체 검증.
 * 문제가 있으면 사용자에게 보여줄 한국어 안내 문구를 돌려주고, 유효하면 null 을 돌려준다.
 */
export function validateLayoutDraft(
  name: string,
  items: readonly LayoutDraftItem[],
): string | null {
  const trimmedName = name.trim();
  if (trimmedName.length === 0) {
    return '레이아웃 이름을 입력해 주세요.';
  }
  if (trimmedName.length > 100) {
    return '레이아웃 이름은 100자 이하로 입력해 주세요.';
  }
  if (items.length === 0) {
    return '최소 1개 이상의 가상 물체(개구부·비계·장비)를 추가해 주세요.';
  }
  if (items.length > 200) {
    return '가상 물체는 최대 200개까지 배치할 수 있습니다.';
  }

  const seenIds = new Set<string>();
  for (const item of items) {
    const id = item.item_id.trim();
    if (!id) {
      return '모든 물체에 ID가 필요합니다.';
    }
    if (seenIds.has(id)) {
      return `중복된 물체 ID가 있습니다: ${id}`;
    }
    seenIds.add(id);

    if (item.bottomZ === null || !Number.isFinite(item.bottomZ)) {
      return `${item.label || id}: 바닥 높이(Z)가 비어 있습니다. 바닥 높이를 입력해 주세요.`;
    }
    if (item.height === null || !Number.isFinite(item.height) || item.height <= 0) {
      return `${item.label || id}: 높이(Z 크기)가 비어 있거나 0 이하입니다. 0보다 큰 높이를 입력해 주세요.`;
    }
    const [sx, sy] = item.sizeXy;
    if (!Number.isFinite(sx) || !Number.isFinite(sy) || sx <= 0 || sy <= 0) {
      return `${item.label || id}: 폭과 깊이는 0보다 커야 합니다.`;
    }
    const [cx, cy] = item.centerXy;
    if (!Number.isFinite(cx) || !Number.isFinite(cy)) {
      return `${item.label || id}: 중심 좌표가 올바르지 않습니다.`;
    }
    if (!Number.isFinite(item.yaw_rad) || Math.abs(item.yaw_rad) > 3.1415927) {
      return `${item.label || id}: 회전 각도는 -180°~180° 범위여야 합니다.`;
    }
  }

  return null;
}

/** 검증을 통과한 레이아웃 초안을 서버 요청 본문(LayoutWriteBody)으로 변환한다. */
export function buildLayoutWriteBody(
  name: string,
  items: readonly LayoutDraftItem[],
  revision?: number | null,
): LayoutWriteBody | null {
  if (validateLayoutDraft(name, items) !== null) return null;
  const obbItems: LayoutObbItem[] = [];
  for (const item of items) {
    const obb = draftItemToObb(item);
    if (!obb) return null;
    obbItems.push(obb);
  }
  return {
    name: name.trim(),
    items: obbItems,
    ...(typeof revision === 'number' ? { revision } : {}),
  };
}
