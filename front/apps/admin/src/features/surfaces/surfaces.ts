import {
  staticObjectItemSchema,
  type StaticObjectBody,
  type StaticObjectItem,
  type SurfaceGeometry,
  type ViewerWorkSurfaces,
  type WorkSurface,
} from '@/features/surfaces/types';

// 작업면 순수 계산. 3D 뷰어 입구는 three.js 를 끌어오므로 값을 import 하지 않고, 같은 규칙을 여기서 계산한다
// (뷰어 surfaces/workSurfaceGeometry.ts·서버 ConfigBundleService.geometry() 와 같은 형식).

/** 계약 yaw_rad 범위(risk_plan_request work_surface). 뷰어도 이 밖의 값은 받지 않는다. */
export const YAW_LIMIT_RAD = 3.1415927;
/** 바닥 ID. 작업면 ID 로 쓸 수 없다. */
const FLOOR_ID = 'floor';
/** 서버가 상판 높이를 float4 로 돌려주므로 값 비교는 이만큼 너그럽게 한다(m·rad). */
const SAME_EPS = 1e-6;

/** 새 작업면의 기본 크기(m)와 상판 높이(m). 교실 책상 크기다. */
const NEW_SURFACE_SIZE: [number, number] = [1.2, 0.6];
const NEW_SURFACE_TOP_M = 0.74;
/** 작업면이 하나도 없을 때 새 작업면을 둘 곳(교실 10×10 m 가운데). */
const ROOM_CENTER: [number, number] = [5, 5];

/** -π~π 로 감는다. 이미 범위 안이면 그대로 둔다. */
export function wrapYaw(yaw: number): number {
  return Math.abs(yaw) <= Math.PI ? yaw : Math.atan2(Math.sin(yaw), Math.cos(yaw));
}

/** 화면은 도(°)로 보이고 받는다(명세 4.2). 값은 라디안이다. */
export const toDegrees = (rad: number) => (rad * 180) / Math.PI;
export const toRadians = (deg: number) => wrapYaw((deg * Math.PI) / 180);

function toSurface(item: StaticObjectItem): WorkSurface | null {
  if (item.kind !== 'WORK_SURFACE' || item.object_id === FLOOR_ID) return null;
  const top = item.top_height_m;
  const [cx, cy] = item.obb.center;
  const [sx, sy] = item.obb.size;
  const geometry: SurfaceGeometry = {
    centerXy: [cx, cy],
    sizeXy: [sx, sy],
    yawRad: wrapYaw(item.obb.yaw_rad),
    topHeightM: top ?? 0,
  };
  if (geometryErrors(geometry) !== null) return null;
  return {
    objectId: item.object_id,
    version: item.object_version,
    name: item.name,
    geometry,
    sigma: item.top_height_sigma_m ?? null,
    validFrom: item.valid_from,
  };
}

/**
 * 서버 목록을 작업면으로 읽는다. 배열이 아니면 없는 것으로 본다. 장애물·형식이 틀린 항목·상판이 없는 항목은 뺀다.
 * 형식이 틀린 항목도 ID 는 센다(같은 ID 가 둘이면 어느 쪽이 맞는지 알 수 없어 둘 다 뺀다).
 */
export function readWorkSurfaces(data: unknown): WorkSurface[] {
  if (!Array.isArray(data)) return [];
  const count = new Map<string, number>();
  for (const item of data) {
    const id = typeof item === 'object' && item !== null ? Reflect.get(item, 'object_id') : null;
    if (typeof id === 'string') count.set(id, (count.get(id) ?? 0) + 1);
  }
  return data.flatMap((item) => {
    const parsed = staticObjectItemSchema.safeParse(item);
    if (!parsed.success || count.get(parsed.data.object_id) !== 1) return [];
    const surface = toSurface(parsed.data);
    return surface ? [surface] : [];
  });
}

/** 3D 뷰어 Source 에 넘길 작업면 묶음. 서버 구성 묶음과 같은 항목에 이름을 더한다. */
export function toViewerWorkSurfaces(surfaces: readonly WorkSurface[]): ViewerWorkSurfaces {
  return {
    work_surfaces: surfaces.map((s) => ({
      surface_id: s.objectId,
      top_height_m: s.geometry.topHeightM,
      center_xy: [...s.geometry.centerXy],
      size_xy: [...s.geometry.sizeXy],
      yaw_rad: s.geometry.yawRad,
      ...(s.sigma === null ? {} : { top_height_sigma_m: s.sigma }),
      ...(s.name === null ? {} : { name: s.name }),
    })),
  };
}

export interface SurfaceGeometryErrors {
  topHeightM?: string;
  size?: string;
  center?: string;
  yaw?: string;
}

/** 형상 검사(서버·Tier 0·뷰어가 받는 범위). 문제가 없으면 null. */
export function geometryErrors(geometry: SurfaceGeometry): SurfaceGeometryErrors | null {
  const errors: SurfaceGeometryErrors = {};
  const finite = (v: number) => Number.isFinite(v);
  if (!finite(geometry.topHeightM) || geometry.topHeightM <= 0)
    errors.topHeightM = '상판 높이는 0 보다 커야 합니다.';
  if (!geometry.sizeXy.every((v) => finite(v) && v > 0))
    errors.size = '폭과 깊이는 0 보다 커야 합니다.';
  if (!geometry.centerXy.every(finite)) errors.center = '중심 좌표를 숫자로 입력하세요.';
  if (!finite(geometry.yawRad) || Math.abs(geometry.yawRad) > YAW_LIMIT_RAD)
    errors.yaw = '회전은 -180° ~ 180° 입니다.';
  return Object.keys(errors).length > 0 ? errors : null;
}

/** 두 형상이 같은지. 서버가 높이를 float4 로 돌려주므로 1e-6 안의 차이는 같다고 본다. */
export function sameGeometry(a: SurfaceGeometry, b: SurfaceGeometry): boolean {
  const near = (x: number, y: number) => Math.abs(x - y) <= SAME_EPS;
  return (
    near(a.centerXy[0], b.centerXy[0]) &&
    near(a.centerXy[1], b.centerXy[1]) &&
    near(a.sizeXy[0], b.sizeXy[0]) &&
    near(a.sizeXy[1], b.sizeXy[1]) &&
    near(a.yawRad, b.yawRad) &&
    near(a.topHeightM, b.topHeightM)
  );
}

/** 숫자 칸 하나(3D 뷰어 updateObjectEditField 의 칸과 같다). 길이는 m, 회전은 rad 다. */
export type SurfaceField = 'centerX' | 'centerY' | 'width' | 'depth' | 'topHeight' | 'yaw';

/**
 * 남긴 초안을 3D 편집에 다시 넣는 순서. 크기·높이·회전을 먼저 넣고 중심을 마지막에 옮긴다(구역의 이어 편집과 같다).
 * 뷰어 편집은 늘 등록 형상에서 시작한다.
 */
export const SURFACE_FIELDS: readonly SurfaceField[] = [
  'width',
  'depth',
  'topHeight',
  'yaw',
  'centerX',
  'centerY',
];

export function fieldValue(geometry: SurfaceGeometry, field: SurfaceField): number {
  switch (field) {
    case 'centerX':
      return geometry.centerXy[0];
    case 'centerY':
      return geometry.centerXy[1];
    case 'width':
      return geometry.sizeXy[0];
    case 'depth':
      return geometry.sizeXy[1];
    case 'topHeight':
      return geometry.topHeightM;
    case 'yaw':
      return geometry.yawRad;
  }
}

/** 칸 하나만 바꾼 형상. */
export function withField(
  geometry: SurfaceGeometry,
  field: SurfaceField,
  value: number,
): SurfaceGeometry {
  const [cx, cy] = geometry.centerXy;
  const [sx, sy] = geometry.sizeXy;
  switch (field) {
    case 'centerX':
      return { ...geometry, centerXy: [value, cy] };
    case 'centerY':
      return { ...geometry, centerXy: [cx, value] };
    case 'width':
      return { ...geometry, sizeXy: [value, sy] };
    case 'depth':
      return { ...geometry, sizeXy: [sx, value] };
    case 'topHeight':
      return { ...geometry, topHeightM: value };
    case 'yaw':
      return { ...geometry, yawRad: value };
  }
}

/** from 에서 to 로 가려면 다시 넣어야 하는 칸(SURFACE_FIELDS 순서). */
export const fieldsChanged = (from: SurfaceGeometry, to: SurfaceGeometry): SurfaceField[] =>
  SURFACE_FIELDS.filter(
    (field) => Math.abs(fieldValue(from, field) - fieldValue(to, field)) > SAME_EPS,
  );

/** 이름 초안이 저장된 이름과 같은지. 앞뒤 공백을 빼고, 비면 이름 없음(null)과 같다. */
export const sameName = (draft: string, name: string | null) =>
  (draft.trim() === '' ? null : draft.trim()) === name;

/**
 * 만들기·새 버전 본문. OBB 는 바닥(Z=0)부터 상판까지(가운데 z = 상판/2, 높이 = 상판)로 보내 서버 obb 와 상판 높이가
 * 어긋나지 않게 한다. yaw 는 [-π, π] 로 접어 보낸다(서버는 범위를 보지 않는다). 상판 높이 표준편차는 받은 값을 다시 보낸다.
 */
export function bodyOf(
  surface: WorkSurface | null,
  name: string,
  geometry: SurfaceGeometry,
): StaticObjectBody {
  const top = geometry.topHeightM;
  const trimmed = name.trim();
  return {
    kind: 'WORK_SURFACE',
    name: trimmed === '' ? null : trimmed,
    obb: {
      center: [geometry.centerXy[0], geometry.centerXy[1], top / 2],
      size: [geometry.sizeXy[0], geometry.sizeXy[1], top],
      yaw_rad: wrapYaw(geometry.yawRad),
    },
    top_height_m: top,
    top_height_sigma_m: surface?.sigma ?? null,
  };
}

/** 새 작업면이 기존 작업면과 겹쳐 생기지 않게 옆으로 비켜 두는 간격(m)과 횟수. */
const NEW_SURFACE_STEP_M = 1.5;
const NEW_SURFACE_TRIES = 6;

/**
 * 새 작업면의 기본 형상. 기존 작업면들 가운데 바닥에 교실 책상 크기로 두고(없으면 방 가운데), 기존 작업면 가운데와
 * 너무 가까우면 +X 쪽으로 비켜 둔다(같은 자리에 겹치면 3D 에서 고르기 어렵다).
 */
export function newSurfaceGeometry(surfaces: readonly WorkSurface[]): SurfaceGeometry {
  const mean = (axis: 0 | 1) =>
    surfaces.length === 0
      ? ROOM_CENTER[axis]
      : surfaces.reduce((sum, s) => sum + s.geometry.centerXy[axis], 0) / surfaces.length;
  const crowded = (x: number, y: number) =>
    surfaces.some(
      (s) =>
        Math.hypot(s.geometry.centerXy[0] - x, s.geometry.centerXy[1] - y) < NEW_SURFACE_STEP_M,
    );
  const y = mean(1);
  let x = mean(0);
  for (let i = 0; i < NEW_SURFACE_TRIES && crowded(x, y); i += 1) x += NEW_SURFACE_STEP_M;
  return {
    centerXy: [x, y],
    sizeXy: [...NEW_SURFACE_SIZE],
    yawRad: 0,
    topHeightM: NEW_SURFACE_TOP_M,
  };
}
