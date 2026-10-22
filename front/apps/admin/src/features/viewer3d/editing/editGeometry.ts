import { toWorldPoint, type WorldPoint } from '@/features/viewer3d/worldCoordinates';

// 공통 3D 편집 엔진의 순수 World 계산(S15P31A101-195). 렌더러·React·Store 와 떨어져 있어 시험한다.
// 엔진은 하나이고, 대상 종류마다 허용 조작(EditPolicy)만 다르다.
// - 구역(zone, -47 AABB): X·Y 이동, 6개 면 경계(반대 면 고정), 회전·기울임 없음.
// - 작업면(object, -188 OBB)·TBM 가상 객체(PLANNED, -186): 이 파일의 OBB 계산과 정책 매개변수만 시험 Fixture 로 보인다.
//   해당 도메인의 등록·상판·판정 흐름은 각 스토리가 한다.
//
// 입력 정책(명세에 정해지지 않은 것은 새로 만들지 않는다):
// - 유효한 형상: 모든 값이 유한한 숫자이고 세 축 모두 min < max(크기 > 0). 그 밖은 INVALID_GEOMETRY 로 거부한다.
// - 바닥 아래(Z < 0)는 명세에 규칙이 없어 0 으로 자르거나 거부하지 않는다. 저장 전 검증은 FE·BE(-189·-120)가 한다.
// - 스냅·최소 크기·교실 경계는 정하지 않는다. 결과 값은 반올림하지 않는다.
// - 높이를 바꾸는 조작이 없으면 Z(min·max)는 그대로다. 바닥 구역(min Z = 0)·공중 구역(min Z > 0)의 하단 의미가 유지된다.

/** World 축 정렬 상자. min·max 는 World(m, Z-up) 모서리다. */
export interface WorldBox {
  min: WorldPoint;
  max: WorldPoint;
}

/** 수평 회전 상자(OBB). yaw 는 World +Z 축 기준 반시계 회전(rad), size 는 [폭 X', 깊이 Y', 높이 Z]. */
export interface WorldObb {
  center: WorldPoint;
  size: readonly [number, number, number];
  yaw: number;
}

export type EditShape = ({ kind: 'aabb' } & WorldBox) | ({ kind: 'obb' } & WorldObb);

/** 상자의 면. x-·x+ 는 X 경계, y-·y+ 는 Y 경계, z- 는 하단(z_min), z+ 는 상단(z_max). */
export const EDIT_FACES = ['x-', 'x+', 'y-', 'y+', 'z-', 'z+'] as const;
export type EditFace = (typeof EDIT_FACES)[number];

export type EditOperation =
  /** World 이동(m). */
  | { type: 'translate'; delta: readonly [number, number, number] }
  /** 한 면을 바깥(+)·안쪽(−)이 아니라 World 축 방향으로 delta(m)만큼 옮긴다. 반대 면은 고정이다. */
  | { type: 'face'; face: EditFace; delta: number }
  /** 회전(rad). yaw 는 수평 회전, pitch·roll 은 기울임이다. */
  | { type: 'rotate'; axis: 'yaw' | 'pitch' | 'roll'; radians: number };

export interface EditPolicy {
  /** 정책 이름(진단·시험용). */
  name: string;
  translate: { x: boolean; y: boolean; z: boolean };
  faces: readonly EditFace[];
  rotate: { yaw: boolean; pitch: boolean; roll: boolean };
}

/** -47 AABB 구역. X·Y 이동과 6개 면 경계만 바꾼다. 회전·기울임과 Z 이동(하단·상단을 함께 옮김)은 없다. */
export const ZONE_AABB_POLICY: EditPolicy = {
  name: 'zone-aabb',
  translate: { x: true, y: true, z: false },
  faces: EDIT_FACES,
  rotate: { yaw: false, pitch: false, roll: false },
};

export type EditFailureReason = 'NOT_EDITABLE' | 'INVALID_GEOMETRY';

export type EditOutcome =
  { ok: true; shape: EditShape } | { ok: false; reason: EditFailureReason; detail: string };

const fail = (reason: EditFailureReason, detail: string): EditOutcome => ({
  ok: false,
  reason,
  detail,
});

const AXIS_INDEX = { x: 0, y: 1, z: 2 } as const;

/** World 상자가 그릴 수 있는 형상인지. 유한한 숫자이고 세 축 모두 min < max. */
export function isValidBox(box: WorldBox): boolean {
  return box.min.every(
    (v, i) => Number.isFinite(v) && Number.isFinite(box.max[i]) && v < box.max[i]!,
  );
}

function isValidObb(obb: WorldObb): boolean {
  return (
    obb.center.every(Number.isFinite) &&
    obb.size.every((v) => Number.isFinite(v) && v > 0) &&
    Number.isFinite(obb.yaw)
  );
}

/** 형상이 유효한지. */
export function isValidShape(shape: EditShape): boolean {
  return shape.kind === 'aabb' ? isValidBox(shape) : isValidObb(shape);
}

const world = (value: readonly number[]): WorldPoint | null => toWorldPoint([...value]);

/** -π~π 로 맞춘다. */
const wrapAngle = (radians: number) => Math.atan2(Math.sin(radians), Math.cos(radians));

/**
 * 형상 하나에 조작 하나를 적용한다. 입력은 바꾸지 않고 새 객체를 돌려준다.
 * 정책이 허용하지 않는 조작은 NOT_EDITABLE, 결과가 유효하지 않으면 INVALID_GEOMETRY 로 거부한다(값을 맞춰 고치지 않는다).
 */
export function applyEditOperation(
  shape: EditShape,
  operation: EditOperation,
  policy: EditPolicy,
): EditOutcome {
  if (!isValidShape(shape)) return fail('INVALID_GEOMETRY', '시작 형상이 유효하지 않다');
  switch (operation.type) {
    case 'translate': {
      const { delta } = operation;
      if (!delta.every(Number.isFinite)) return fail('INVALID_GEOMETRY', '이동량이 유한하지 않다');
      const blocked = (['x', 'y', 'z'] as const).find(
        (axis, i) => delta[i] !== 0 && !policy.translate[axis],
      );
      if (blocked) return fail('NOT_EDITABLE', `${blocked} 축 이동은 이 대상에서 허용되지 않는다`);
      return finish(
        shape.kind === 'aabb'
          ? {
              ...shape,
              min: world(shape.min.map((v, i) => v + delta[i]!)),
              max: world(shape.max.map((v, i) => v + delta[i]!)),
            }
          : { ...shape, center: world(shape.center.map((v, i) => v + delta[i]!)) },
      );
    }
    case 'face': {
      const { face, delta } = operation;
      if (!Number.isFinite(delta)) return fail('INVALID_GEOMETRY', '면 이동량이 유한하지 않다');
      if (!policy.faces.includes(face))
        return fail('NOT_EDITABLE', `${face} 면은 이 대상에서 바꿀 수 없다`);
      const axis = AXIS_INDEX[face[0] as 'x' | 'y' | 'z'];
      const upper = face[1] === '+';
      if (shape.kind === 'aabb') {
        // 이 면의 경계만 옮기고 반대 면은 그대로 둔다.
        const bound = (upper ? shape.max : shape.min).map((v, i) => (i === axis ? v + delta : v));
        return finish(upper ? { ...shape, max: world(bound) } : { ...shape, min: world(bound) });
      }
      // OBB: 지역 축 방향으로 한 면만 옮긴다. 크기는 delta 만큼, 중심은 그 절반만큼 같은 방향으로 움직인다.
      const sign = upper ? 1 : -1;
      const size = shape.size.map((v, i) => (i === axis ? v + sign * delta : v)) as [
        number,
        number,
        number,
      ];
      const local = axis === 2 ? [0, 0, 1] : rotateXY(axis === 0 ? [1, 0] : [0, 1], shape.yaw);
      const center = world(shape.center.map((v, i) => v + (local[i]! * delta) / 2));
      return finish({ ...shape, size, center });
    }
    case 'rotate': {
      const { axis, radians } = operation;
      if (!Number.isFinite(radians)) return fail('INVALID_GEOMETRY', '회전량이 유한하지 않다');
      if (radians === 0) return { ok: true, shape };
      // 축 정렬 상자는 정의상 회전할 수 없다. 기울임(pitch·roll)은 어떤 정책에서도 형상으로 표현하지 않는다.
      if (shape.kind === 'aabb' || axis !== 'yaw' || !policy.rotate.yaw) {
        return fail('NOT_EDITABLE', `${axis} 회전은 이 대상에서 허용되지 않는다`);
      }
      return finish({ ...shape, yaw: wrapAngle(shape.yaw + radians) });
    }
  }
}

function rotateXY([x, y]: readonly number[], yaw: number): [number, number, number] {
  const [c, s] = [Math.cos(yaw), Math.sin(yaw)];
  return [x! * c - y! * s, x! * s + y! * c, 0];
}

type Draft =
  | ({ kind: 'aabb' } & { min: WorldPoint | null; max: WorldPoint | null })
  | ({ kind: 'obb' } & {
      center: WorldPoint | null;
      size: readonly [number, number, number];
      yaw: number;
    });

function finish(draft: Draft): EditOutcome {
  if (draft.kind === 'aabb') {
    if (!draft.min || !draft.max) return fail('INVALID_GEOMETRY', '좌표가 유한하지 않다');
    const box = { kind: 'aabb' as const, min: draft.min, max: draft.max };
    return isValidBox(box)
      ? { ok: true, shape: box }
      : fail('INVALID_GEOMETRY', '경계가 교차하거나 크기가 0 이하다');
  }
  if (!draft.center) return fail('INVALID_GEOMETRY', '좌표가 유한하지 않다');
  const obb = { kind: 'obb' as const, center: draft.center, size: draft.size, yaw: draft.yaw };
  return isValidObb(obb) ? { ok: true, shape: obb } : fail('INVALID_GEOMETRY', '크기가 0 이하다');
}

/** 같은 상자인지(부동소수점 오차 허용). */
export function sameBox(a: WorldBox, b: WorldBox, epsilon = 1e-9): boolean {
  return (
    a.min.every((v, i) => Math.abs(v - b.min[i]!) <= epsilon) &&
    a.max.every((v, i) => Math.abs(v - b.max[i]!) <= epsilon)
  );
}

/** 바깥에 넘길 평범한 배열 사본. */
export const boxJson = (box: WorldBox) => ({
  min: [box.min[0], box.min[1], box.min[2]] as [number, number, number],
  max: [box.max[0], box.max[1], box.max[2]] as [number, number, number],
});
