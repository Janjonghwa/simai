import { worldPoint, worldToThree, type ThreePoint } from '@/features/viewer3d/worldCoordinates';
import {
  DEFAULT_ROOM_LIMIT,
  usableScanBounds,
  type RoomBounds,
  type RoomLimit,
} from '@/lib/roomLimit';

// 교실 범위에서 정하는 처음 시점의 순수 계산. 좌표는 World 로 계산해 Three 로 한 번만 바꾼다.

/** 처음 시점: 회전 중심과 카메라 자리(Three 좌표). */
export interface RoomView {
  target: ThreePoint;
  home: ThreePoint;
}

/** 처음 시점 계산에 쓰는 카메라 조건. 생략하면 뷰어 기본값을 쓴다. */
export interface RoomViewCamera {
  /** 카메라 세로 시야각(도). */
  fovDeg?: number;
  /** 화면 가로/세로 비율(width / height). */
  aspect?: number;
}

/** 뷰어 기본 세로 시야각(도). ViewerCanvas 의 PerspectiveCamera 와 같은 값이다. */
export const DEFAULT_VIEW_FOV_DEG = 50;
/** 화면 크기를 아직 알지 못할 때(Canvas 초기 마운트) 쓰는 기본 화면 비율(1440×900 기준). */
export const DEFAULT_VIEW_ASPECT = 16 / 10;
/** 기본 시점 올려다보는 각도(수평 위 35°, World 남쪽 −Y 에서 내려다봄). */
export const DEFAULT_VIEW_ELEVATION_DEG = 35;

/** 기본 시점 바라보는 점 높이 상한(m)과 Scene 높이 비율. */
const TARGET_HEIGHT_MAX_M = 0.8;
const TARGET_HEIGHT_RATIO = 0.3;
/** 경계 상자 8개 꼭짓점이 화면 가장자리에 닿지 않도록 두는 여유 배수. */
const FIT_MARGIN = 1.15;
/** 퇴화된 경계 상자에서도 카메라가 회전 중심과 겹치지 않게 두는 최소 거리(m). */
const MIN_VIEW_DISTANCE_M = 1;
/** 스캔 높이(z) 이상치 상한(m). 이보다 크면 튀는 점으로 보고 기본 높이를 쓴다. */
const MAX_VIEW_HEIGHT_M = 50;

const ELEVATION_RAD = (DEFAULT_VIEW_ELEVATION_DEG * Math.PI) / 180;
const COS_ELEV = Math.cos(ELEVATION_RAD);
const SIN_ELEV = Math.sin(ELEVATION_RAD);

const TOP_VIEW_DISTANCE_PER_SIZE = 1.25;
/** Three 카메라의 기본 위쪽 축(+Y = World +Z)과 시선 벡터가 평행하지 않도록 두는 미세 Y 오프셋(m). */
const TOP_VIEW_Y_OFFSET = 0.01;

/**
 * 시점 계산에 쓸 World 경계 상자. 쓸 수 있는 스캔이면 x·y 는 그대로 두고,
 * z 도 상식 범위(0 ~ 50m)면 스캔 실측 높이를 유지한다. 튀는 점이 z 를 키웠으면 기본 높이(0~5m)로 되돌린다.
 */
export function viewBoundsFrom(bounds: RoomBounds | null): RoomBounds {
  const scan = usableScanBounds(bounds);
  if (!scan) return DEFAULT_ROOM_LIMIT;
  const dz = scan.max[2] - scan.min[2];
  if (dz < 0 || dz > MAX_VIEW_HEIGHT_M) {
    return {
      min: [scan.min[0], scan.min[1], DEFAULT_ROOM_LIMIT.min[2]],
      max: [scan.max[0], scan.max[1], DEFAULT_ROOM_LIMIT.max[2]],
    };
  }
  return scan;
}

/**
 * Scene 경계 상자 중심(xy), 높이 min(0.8m, 높이×0.3)을 바라보고,
 * World 남쪽(−Y)에서 수평 위 35° 로 방 전체가 화면에 들어오는 거리에서 내려다보는 기본 시점.
 * 좌표 계산은 모두 World 에서 하고 마지막에 worldToThree 한 곳에서만 Three 좌표로 바꾼다.
 */
export function roomViewOf({ min, max }: RoomBounds, camera?: RoomViewCamera): RoomView {
  const cx = (min[0] + max[0]) / 2;
  const cy = (min[1] + max[1]) / 2;
  const dz = Math.max(0, max[2] - min[2]);
  const tz = min[2] + Math.min(TARGET_HEIGHT_MAX_M, dz * TARGET_HEIGHT_RATIO);

  const fovDeg =
    camera?.fovDeg !== undefined &&
    Number.isFinite(camera.fovDeg) &&
    camera.fovDeg > 0 &&
    camera.fovDeg < 180
      ? camera.fovDeg
      : DEFAULT_VIEW_FOV_DEG;
  const aspect =
    camera?.aspect !== undefined && Number.isFinite(camera.aspect) && camera.aspect > 0
      ? camera.aspect
      : DEFAULT_VIEW_ASPECT;

  const tanHalfV = Math.tan(((fovDeg / 2) * Math.PI) / 180);
  const tanHalfH = tanHalfV * aspect;

  // 카메라 좌표축(World 기준, 남쪽 −Y 에서 수평 위 35° 로 바라봄):
  //   forward = (0,  cos θ, -sin θ)
  //   right   = (1,  0,      0    )
  //   up      = (0,  sin θ,  cos θ)
  // 경계 상자 8개 꼭짓점이 모두 시야 절두체(여유 FIT_MARGIN 포함) 안에 들어오는 최소 거리를 구한다.
  let distance = MIN_VIEW_DISTANCE_M;
  for (const x of [min[0], max[0]]) {
    const rx = x - cx;
    for (const y of [min[1], max[1]]) {
      const ry = y - cy;
      for (const z of [min[2], max[2]]) {
        const rz = z - tz;
        const camX = Math.abs(rx);
        const camY = Math.abs(ry * SIN_ELEV + rz * COS_ELEV);
        // target 기준 전방 좌표(양수면 카메라에서 더 멀고, 음수면 카메라에 더 가깝다).
        const camZ = ry * COS_ELEV - rz * SIN_ELEV;
        const needH = (camX * FIT_MARGIN) / tanHalfH - camZ;
        const needV = (camY * FIT_MARGIN) / tanHalfV - camZ;
        if (needH > distance) distance = needH;
        if (needV > distance) distance = needV;
      }
    }
  }

  // 카메라 위치(World): target 에서 남쪽(−Y)·위(+Z) 35° 방향으로 distance 만큼 물러난다.
  const camY = cy - distance * COS_ELEV;
  const camZ = tz + distance * SIN_ELEV;

  return {
    target: worldToThree(worldPoint(cx, cy, tz)),
    home: worldToThree(worldPoint(cx, camY, camZ)),
  };
}

/** 범위 가운데 바닥을 바로 위(+Z)에서 수직으로 내려다보는 시점(위에서 보기). */
export function topRoomViewOf({ min, max }: RoomLimit): RoomView {
  const [cx, cy, floor] = [(min[0] + max[0]) / 2, (min[1] + max[1]) / 2, min[2]] as const;
  const distance = Math.max(max[0] - min[0], max[1] - min[1]) * TOP_VIEW_DISTANCE_PER_SIZE;
  return {
    target: worldToThree(worldPoint(cx, cy, floor)),
    home: worldToThree(worldPoint(cx, cy - TOP_VIEW_Y_OFFSET, floor + distance)),
  };
}
