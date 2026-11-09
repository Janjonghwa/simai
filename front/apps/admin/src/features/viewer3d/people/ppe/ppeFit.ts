import { CAPSULE } from '@/features/viewer3d/people/capsuleShape';
import type { SkeletonAnchors } from '@/features/viewer3d/people/skeletonPose';

// 안전모·조끼를 사람 몸에 맞추는 순수 계산(명세 13.3: 크기는 어깨 폭·몸통 길이·머리 크기로 맞춘다).
// 모델은 가로·세로·앞뒤를 1 로 맞춘 단위 상자다(ppeAssets). 여기서 그 상자를 놓을 자리(원점·세 축)와 실제 크기(m)를 정한다.
// 단위 상자의 원점: 안전모 = 모자 아래 가운데, 조끼 = 위쪽 가운데. 앞은 +Z, 위는 +Y.

type V3 = readonly [number, number, number];

export interface GearPlacement {
  /** 단위 상자 원점의 자리(루트 기준 Three 좌표, m). */
  position: V3;
  /** 모델의 +X(사람의 왼쪽)·+Y(위)·+Z(앞)가 가리킬 방향. 서로 직교하는 단위 벡터. */
  xAxis: V3;
  yAxis: V3;
  zAxis: V3;
  /** 단위 상자를 늘릴 실제 크기(m): 가로(x)·세로(y)·앞뒤(z). */
  size: V3;
}

/** 안전모(m). 가로:세로:앞뒤는 모델 비율(1 : 0.66 : 1.41)을 따른다. */
const HELMET_SIZE: V3 = [0.26, 0.172, 0.366];
/** 귀 높이보다 이만큼 아래에 모자 아랫단을 둔다. */
const HELMET_RIM_DROP_M = 0.04;
/** 한쪽 귀만 보일 때 머리 가운데까지의 옆 거리(귀 사이 거리의 절반, 귀 관절 기준). */
const HALF_HEAD_WIDTH_M = 0.075;
/** 얼굴 관절(코·눈) 평균이 머리 가운데보다 앞쪽에 치우친 거리. 귀가 없을 때 뒤로 보정한다. */
const FACE_MEAN_FORWARD_M = 0.06;

/** 조끼 폭 = 어깨 관절 사이 거리 × 이 값(조끼는 어깨 관절보다 넓다). */
const VEST_WIDTH_PER_SHOULDER = 1.2;
/** 조끼 높이 = 어깨 가운데에서 골반 가운데까지 × 이 값(목선 위에서 엉덩이 아래까지). */
const VEST_HEIGHT_PER_TORSO = 1.2;
/** 조끼 앞뒤 = 폭 × 이 값(모델 비율). */
const VEST_DEPTH_PER_WIDTH = 0.68;
/** 조끼 윗선을 어깨선보다 올리는 높이. */
const VEST_NECK_RISE_M = 0.05;
/** 골반이 없을 때 몸통 길이 = 어깨 폭 × 이 값. */
const TORSO_PER_SHOULDER_WIDTH = 1.35;
/** 흔들리는 관절에 크기가 널뛰지 않게 막는 범위(m). */
const VEST_WIDTH_RANGE_M = [0.3, 0.65] as const;
const VEST_HEIGHT_RANGE_M = [0.35, 0.85] as const;

const UP: V3 = [0, 1, 0];

const sub = (a: V3, b: V3): V3 => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
const add = (a: V3, b: V3): V3 => [a[0] + b[0], a[1] + b[1], a[2] + b[2]];
const scale = (a: V3, k: number): V3 => [a[0] * k, a[1] * k, a[2] * k];
const dot = (a: V3, b: V3) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
const cross = (a: V3, b: V3): V3 => [
  a[1] * b[2] - a[2] * b[1],
  a[2] * b[0] - a[0] * b[2],
  a[0] * b[1] - a[1] * b[0],
];
const length = (a: V3) => Math.hypot(a[0], a[1], a[2]);
const mid = (a: V3, b: V3): V3 => scale(add(a, b), 0.5);
const clamp = (value: number, [min, max]: readonly [number, number]) =>
  Math.min(max, Math.max(min, value));

function normalize(a: V3): V3 | null {
  const n = length(a);
  return n > 1e-6 ? scale(a, 1 / n) : null;
}

const horizontal = (a: V3): V3 => [a[0], 0, a[2]];

interface Facing {
  /** 사람의 왼쪽(수평 단위 벡터). */
  left: V3;
  /** 사람이 보는 앞(수평 단위 벡터). */
  forward: V3;
}

/**
 * 어깨(없으면 귀)의 좌우 선에서 사람이 보는 방향을 구한다. 골격 막대에는 방향 값이 없다.
 * 왼쪽 → 앞은 Y-up 에서 앞 = (−left.z, 0, left.x) 다(+Z 를 보면 왼쪽이 +X). 코가 있으면 코 쪽이 앞이 되게 좌우를 맞춘다.
 */
function facingOf(anchors: SkeletonAnchors): Facing | null {
  const { lShoulder, rShoulder, lEar, rEar, nose } = anchors;
  const shoulders = lShoulder && rShoulder;
  const across = shoulders ? sub(lShoulder, rShoulder) : lEar && rEar ? sub(lEar, rEar) : null;
  const left = across && normalize(horizontal(across));
  if (!left) return null;
  const forward: V3 = [-left[2], 0, left[0]];
  const origin = shoulders ? mid(lShoulder, rShoulder) : lEar && rEar ? mid(lEar, rEar) : null;
  if (nose && origin && dot(forward, horizontal(sub(nose, origin))) < 0) {
    return { left: scale(left, -1), forward: scale(forward, -1) };
  }
  return { left, forward };
}

function headCenterOf(anchors: SkeletonAnchors, facing: Facing): V3 | null {
  const { lEar, rEar, head } = anchors;
  if (lEar && rEar) return mid(lEar, rEar);
  if (lEar) return sub(lEar, scale(facing.left, HALF_HEAD_WIDTH_M));
  if (rEar) return add(rEar, scale(facing.left, HALF_HEAD_WIDTH_M));
  if (head) return sub(head, scale(facing.forward, FACE_MEAN_FORWARD_M));
  return null;
}

/** 골격 막대 사람의 안전모 자리. 좌우·앞을 모르거나 머리 위치가 없으면 null(지어내지 않는다). */
export function skeletonHelmetPlacement(anchors: SkeletonAnchors): GearPlacement | null {
  const facing = facingOf(anchors);
  const center = facing && headCenterOf(anchors, facing);
  if (!facing || !center) return null;
  return {
    position: add(center, [0, -HELMET_RIM_DROP_M, 0]),
    xAxis: facing.left,
    yAxis: UP,
    zAxis: facing.forward,
    size: HELMET_SIZE,
  };
}

/** 골격 막대 사람의 조끼 자리. 양 어깨가 없으면 null. 골반이 없으면 어깨 폭으로 몸통 길이를 어림한다. */
export function skeletonVestPlacement(anchors: SkeletonAnchors): GearPlacement | null {
  const { lShoulder, rShoulder, lHip, rHip } = anchors;
  const facing = facingOf(anchors);
  if (!lShoulder || !rShoulder || !facing) return null;
  const neck = mid(lShoulder, rShoulder);
  const shoulderWidth = length(sub(lShoulder, rShoulder));
  const hips = lHip && rHip ? mid(lHip, rHip) : null;
  const up = (hips && normalize(sub(neck, hips))) ?? UP;
  const torsoLength = hips ? length(sub(neck, hips)) : shoulderWidth * TORSO_PER_SHOULDER_WIDTH;
  // 좌우를 몸통 축에 직교하게 다듬는다. 앞 = 좌우 × 위(오른손 좌표계).
  const xAxis = normalize(sub(facing.left, scale(up, dot(facing.left, up))));
  if (!xAxis) return null;
  const width = clamp(shoulderWidth * VEST_WIDTH_PER_SHOULDER, VEST_WIDTH_RANGE_M);
  return {
    position: add(neck, scale(up, VEST_NECK_RISE_M)),
    xAxis,
    yAxis: up,
    zAxis: cross(xAxis, up),
    size: [
      width,
      clamp(torsoLength * VEST_HEIGHT_PER_TORSO, VEST_HEIGHT_RANGE_M),
      width * VEST_DEPTH_PER_WIDTH,
    ],
  };
}

const IDENTITY_AXES = { xAxis: [1, 0, 0], yAxis: UP, zAxis: [0, 0, 1] } as const;

/** 캡슐 사람의 안전모 자리. 캡슐은 몸 그룹 안에서 늘 앞이 +Z 다. 머리 구(반지름 0.13)보다 조금 크게 씌운다. */
export const CAPSULE_HELMET: GearPlacement = {
  position: [0, CAPSULE.headY - 0.05, 0],
  ...IDENTITY_AXES,
  size: [0.31, 0.205, 0.437],
};

/** 캡슐 사람의 조끼 자리. 몸통(반지름 0.2) 위쪽을 감싼다. */
export const CAPSULE_VEST: GearPlacement = {
  position: [0, CAPSULE.bodyY + CAPSULE.bodyLength / 2 + CAPSULE.bodyRadius - 0.05, 0],
  ...IDENTITY_AXES,
  size: [0.46, 0.6, 0.44],
};
