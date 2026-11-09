import { CAPSULE } from '@/features/viewer3d/people/capsuleShape';
import {
  CAPSULE_HELMET,
  CAPSULE_VEST,
  skeletonHelmetPlacement,
  skeletonVestPlacement,
  type GearPlacement,
} from '@/features/viewer3d/people/ppe/ppeFit';
import type { SkeletonAnchors } from '@/features/viewer3d/people/skeletonPose';
import type { ThreePoint } from '@/features/viewer3d/worldCoordinates';
import { describe, expect, it } from 'vitest';

const p = (x: number, y: number, z: number) => [x, y, z] as unknown as ThreePoint;

/** +Z 를 보고 선 사람(왼쪽이 +X). 어깨 폭 0.4, 어깨→골반 0.5, 귀 사이 0.16. */
const FACING_Z: SkeletonAnchors = {
  head: p(0, 1.62, 0.07),
  nose: p(0, 1.62, 0.1),
  lEar: p(0.08, 1.65, 0),
  rEar: p(-0.08, 1.65, 0),
  lShoulder: p(0.2, 1.45, 0),
  rShoulder: p(-0.2, 1.45, 0),
  lHip: p(0.1, 0.95, 0),
  rHip: p(-0.1, 0.95, 0),
};

/** 몇몇 기준점을 뺀 사본(가려지거나 좌표가 없는 관절). */
const without = (anchors: SkeletonAnchors, ...keys: (keyof SkeletonAnchors)[]): SkeletonAnchors =>
  Object.fromEntries(
    Object.entries(anchors).filter(([key]) => !keys.includes(key as keyof SkeletonAnchors)),
  );

const close = (actual: readonly number[], expected: readonly number[]) =>
  actual.forEach((value, i) => expect(value).toBeCloseTo(expected[i]!, 5));

/** 세 축이 서로 직교하는 단위 벡터이고 오른손 좌표계(x × y = z)인지. */
function expectRightHandedBasis(placement: GearPlacement) {
  const { xAxis: x, yAxis: y, zAxis: z } = placement;
  const dot = (a: readonly number[], b: readonly number[]) =>
    a[0]! * b[0]! + a[1]! * b[1]! + a[2]! * b[2]!;
  expect(dot(x, x)).toBeCloseTo(1, 5);
  expect(dot(y, y)).toBeCloseTo(1, 5);
  expect(dot(z, z)).toBeCloseTo(1, 5);
  expect(dot(x, y)).toBeCloseTo(0, 5);
  expect(dot(y, z)).toBeCloseTo(0, 5);
  close(z, [
    x[1]! * y[2]! - x[2]! * y[1]!,
    x[2]! * y[0]! - x[0]! * y[2]!,
    x[0]! * y[1]! - x[1]! * y[0]!,
  ]);
}

describe('skeletonHelmetPlacement', () => {
  it('두 귀의 가운데에서 귀 높이보다 조금 아래에 놓고, 앞은 사람이 보는 쪽이다', () => {
    const placement = skeletonHelmetPlacement(FACING_Z)!;
    close(placement.position, [0, 1.65 - 0.04, 0]);
    close(placement.xAxis, [1, 0, 0]);
    close(placement.yAxis, [0, 1, 0]);
    close(placement.zAxis, [0, 0, 1]);
    expectRightHandedBasis(placement);
  });

  it('-Z 를 보는 사람에게는 앞이 -Z 다', () => {
    const facingBack: SkeletonAnchors = {
      ...FACING_Z,
      nose: p(0, 1.62, -0.1),
      lEar: p(-0.08, 1.65, 0),
      rEar: p(0.08, 1.65, 0),
      lShoulder: p(-0.2, 1.45, 0),
      rShoulder: p(0.2, 1.45, 0),
    };
    const placement = skeletonHelmetPlacement(facingBack)!;
    close(placement.zAxis, [0, 0, -1]);
    close(placement.xAxis, [-1, 0, 0]);
  });

  it('좌우 표시가 뒤바뀌어도 코가 있는 쪽을 앞으로 맞춘다', () => {
    const swapped: SkeletonAnchors = {
      ...FACING_Z,
      lShoulder: p(-0.2, 1.45, 0),
      rShoulder: p(0.2, 1.45, 0),
    };
    close(skeletonHelmetPlacement(swapped)!.zAxis, [0, 0, 1]);
  });

  it('귀가 하나뿐이면 그 귀에서 머리 안쪽으로 옮긴 곳이 머리 가운데다', () => {
    const oneEar = without(FACING_Z, 'rEar');
    const placement = skeletonHelmetPlacement(oneEar)!;
    // 왼쪽 귀(+X)에서 오른쪽(−X)으로 반 머리 폭.
    close(placement.position, [0.08 - 0.075, 1.65 - 0.04, 0]);
  });

  it('귀가 없으면 얼굴 관절 평균을 앞 방향의 반대로 보정한다', () => {
    const noEars = without(FACING_Z, 'lEar', 'rEar');
    const placement = skeletonHelmetPlacement(noEars)!;
    close(placement.position, [0, 1.62 - 0.04, 0.07 - 0.06]);
  });

  it('좌우·앞을 모르거나 머리 위치가 없으면 그리지 않는다', () => {
    expect(skeletonHelmetPlacement({ head: p(0, 1.6, 0) })).toBeNull();
    expect(
      skeletonHelmetPlacement({ lShoulder: p(0.2, 1.45, 0), rShoulder: p(-0.2, 1.45, 0) }),
    ).toBeNull();
    // 좌우 어깨가 같은 위치(방향 계산 불가).
    expect(
      skeletonHelmetPlacement({
        head: p(0, 1.6, 0),
        lShoulder: p(0, 1.45, 0),
        rShoulder: p(0, 1.45, 0),
      }),
    ).toBeNull();
  });
});

describe('skeletonVestPlacement', () => {
  it('어깨 가운데에서 조금 위를 윗선으로, 어깨 폭·몸통 길이에 비례해 크기를 정한다', () => {
    const placement = skeletonVestPlacement(FACING_Z)!;
    close(placement.position, [0, 1.45 + 0.05, 0]);
    close(placement.xAxis, [1, 0, 0]);
    close(placement.yAxis, [0, 1, 0]);
    close(placement.zAxis, [0, 0, 1]);
    close(placement.size, [0.4 * 1.2, 0.5 * 1.2, 0.4 * 1.2 * 0.68]);
    expectRightHandedBasis(placement);
  });

  it('몸이 기울면 위 방향이 몸통 축을 따르고 세 축은 직교한다', () => {
    const leaning: SkeletonAnchors = {
      ...FACING_Z,
      lShoulder: p(0.2, 1.4, 0.2),
      rShoulder: p(-0.2, 1.4, 0.2),
    };
    const placement = skeletonVestPlacement(leaning)!;
    expect(placement.yAxis[2]).toBeGreaterThan(0.2);
    expectRightHandedBasis(placement);
  });

  it('골반이 없으면 어깨 폭으로 몸통 길이를 어림한다', () => {
    const noHips = without(FACING_Z, 'lHip', 'rHip');
    const placement = skeletonVestPlacement(noHips)!;
    close(placement.yAxis, [0, 1, 0]);
    expect(placement.size[1]).toBeCloseTo(0.4 * 1.35 * 1.2, 5);
  });

  it('흔들리는 관절에 크기가 널뛰지 않게 범위로 막는다', () => {
    const narrow = skeletonVestPlacement({
      ...FACING_Z,
      lShoulder: p(0.05, 1.45, 0),
      rShoulder: p(-0.05, 1.45, 0),
    })!;
    expect(narrow.size[0]).toBeCloseTo(0.3, 5);
    const tall = skeletonVestPlacement({ ...FACING_Z, lHip: p(0.1, 0, 0), rHip: p(-0.1, 0, 0) })!;
    expect(tall.size[1]).toBeCloseTo(0.85, 5);
  });

  it('양 어깨가 없으면 그리지 않는다', () => {
    const oneShoulder = without(FACING_Z, 'lShoulder');
    expect(skeletonVestPlacement(oneShoulder)).toBeNull();
    expect(skeletonVestPlacement({})).toBeNull();
  });
});

describe('캡슐 사람의 장비 자리', () => {
  it('안전모는 머리 구 위에, 조끼는 몸통 위쪽에 앞 +Z 로 놓인다', () => {
    close(CAPSULE_HELMET.position, [0, CAPSULE.headY - 0.05, 0]);
    close(CAPSULE_HELMET.zAxis, [0, 0, 1]);
    // 조끼 윗선은 몸통 캡슐 위 끝(1.51)에서 조금 아래다.
    close(CAPSULE_VEST.position, [0, 1.46, 0]);
    // 조끼는 캡슐 몸통(지름 0.4)을 덮을 만큼 넓고 깊다.
    expect(CAPSULE_VEST.size[0]).toBeGreaterThan(CAPSULE.bodyRadius * 2);
    expect(CAPSULE_VEST.size[2]).toBeGreaterThan(CAPSULE.bodyRadius * 2);
    expect(CAPSULE_HELMET.size[0]).toBeGreaterThan(CAPSULE.headRadius * 2);
  });
});
