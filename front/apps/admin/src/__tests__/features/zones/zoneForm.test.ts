import type { Zone } from '@/features/zones/types';
import {
  bodyOf,
  formErrors,
  formOf,
  NEW_ZONE_FORM,
  newZoneBodyOf,
  newZoneBox,
  newZoneErrors,
  patchOf,
  sameForm,
  type ZoneForm,
} from '@/features/zones/zoneForm';
import { describe, expect, it } from 'vitest';

const ZONE: Zone = {
  zoneId: 'z-04',
  name: '개구부 위',
  min: [0, 0, 0],
  max: [2, 3, 1],
  version: 2,
  judgeType: 'VOLUME_3D',
  zoneType: 'OPENING',
  severity: 'CRITICAL',
  polygon: [
    [0, 0],
    [2, 0],
    [2, 3],
    [0, 3],
  ],
  zBounds: [0, 1],
  zRange: [0, 1],
  hasGuardrail: true,
  requirements: { helmet: true },
  validFrom: 100,
  shapeEditable: true,
};

describe('zoneForm', () => {
  it('이름은 비우거나 40자를 넘길 수 없다', () => {
    const form = formOf(ZONE);
    const check = (next: ZoneForm) => formErrors(next, { saved: form, hasHeight: true });
    expect(check(form)).toEqual({});
    expect(check({ ...form, name: '  ' }).name).toBeDefined();
    expect(check({ ...form, name: 'ㄱ'.repeat(41) }).name).toBeDefined();
    expect(check({ ...form, name: ` ${'ㄱ'.repeat(40)} ` }).name).toBeUndefined();
  });

  it('받은 이름이 이미 길면 그 이름을 그대로 둔 채 다른 칸을 저장할 수 있다(길이는 고친 이름만 본다)', () => {
    const saved = { ...formOf(ZONE), name: 'ㄱ'.repeat(50) };
    expect(formErrors({ ...saved, severity: 'INFO' }, { saved, hasHeight: true })).toEqual({});
    expect(
      formErrors({ ...saved, name: 'ㄴ'.repeat(41) }, { saved, hasHeight: true }).name,
    ).toBeDefined();
  });

  it('높이가 없는 구역은 입체(VOLUME_3D)로 판정할 수 없다(서버 INVALID_Z_RANGE)', () => {
    const saved = { ...formOf(ZONE), judgeType: 'GROUND_FOOTPRINT' as const };
    const volume = { ...saved, judgeType: 'VOLUME_3D' as const };
    expect(formErrors(volume, { saved, hasHeight: false }).judgeType).toBeDefined();
    expect(formErrors(volume, { saved, hasHeight: true }).judgeType).toBeUndefined();
    expect(formErrors(saved, { saved, hasHeight: false })).toEqual({});
  });

  it('이름 앞뒤 공백만 다르면 고친 것으로 보지 않고, 고친 칸만 남긴다', () => {
    const saved = formOf(ZONE);
    expect(sameForm(saved, { ...saved, name: ` ${saved.name} ` })).toBe(true);
    expect(patchOf(saved, { ...saved, severity: 'INFO' })).toEqual({ severity: 'INFO' });
    expect(patchOf(saved, saved)).toEqual({});
  });

  it('형상을 고쳤으면 그 상자를 서버 형식(eval_type·zone_kind·z_min_m·z_max_m)으로 보내고, 고치지 않는 칸은 받은 값을 그대로 보낸다', () => {
    const form = { ...formOf(ZONE), name: ' 새 이름 ', zoneType: 'LIFTING' };
    expect(bodyOf(ZONE, form, { min: [1, 1, 0.5], max: [4, 2, 3] })).toEqual({
      name: '새 이름',
      zone_kind: 'LIFTING',
      eval_type: 'VOLUME_3D',
      polygon_xy: [
        [1, 1],
        [4, 1],
        [4, 2],
        [1, 2],
      ],
      z_min_m: 0.5,
      z_max_m: 3,
      severity: 'CRITICAL',
      has_guardrail: true,
      requirements: { helmet: true },
    });
  });

  it('이름을 고치지 않았으면 받은 이름(공백 포함)을 그대로 보낸다', () => {
    const zone = { ...ZONE, name: '개구부 위 ' };
    expect(bodyOf(zone, { ...formOf(zone), severity: 'INFO' }, null).name).toBe('개구부 위 ');
  });

  it('직사각형이 아니거나 높이가 없는 구역은 속성만 저장해도 받은 다각형과 빈 높이를 그대로 보낸다', () => {
    const triangle: Zone = {
      ...ZONE,
      judgeType: 'GROUND_FOOTPRINT',
      polygon: [
        [0, 0],
        [4, 0],
        [0, 3],
      ],
      zBounds: [null, null],
      zRange: null,
      hasGuardrail: null,
      requirements: null,
      shapeEditable: false,
    };
    const body = bodyOf(triangle, { ...formOf(triangle), name: '삼각 구역' }, null);
    expect(body).toMatchObject({
      polygon_xy: [
        [0, 0],
        [4, 0],
        [0, 3],
      ],
      z_min_m: null,
      z_max_m: null,
      has_guardrail: null,
      requirements: null,
    });
  });

  it('높이가 한쪽만 있는 구역은 속성만 저장해도 그 높이를 지우지 않는다', () => {
    const half: Zone = {
      ...ZONE,
      judgeType: 'GROUND_FOOTPRINT',
      zBounds: [0.5, null],
      zRange: null,
      shapeEditable: false,
    };
    expect(bodyOf(half, { ...formOf(half), severity: 'INFO' }, null)).toMatchObject({
      z_min_m: 0.5,
      z_max_m: null,
    });
  });

  it('새 구역은 기존 구역들의 가운데 바닥에 2×2 m, 높이 2 m 로 고른 유형으로 만들고, 구역이 없으면 원점에 둔다', () => {
    const body = newZoneBodyOf(
      { ...NEW_ZONE_FORM, zoneType: 'LIFTING' },
      newZoneBox([ZONE, { ...ZONE, min: [6, 7, 0], max: [8, 9, 1] }]),
    );
    expect(body.polygon_xy).toEqual([
      [3, 3.5],
      [5, 3.5],
      [5, 5.5],
      [3, 5.5],
    ]);
    expect(body).toMatchObject({ zone_kind: 'LIFTING', z_min_m: 0, z_max_m: 2 });
    expect(newZoneBox([]).min).toEqual([-1, -1, 0]);
  });
});

describe('새 구역 양식', () => {
  it('유형을 고르기 전에는 만들 수 없고, 이름 규칙은 저장과 같다', () => {
    expect(newZoneErrors(NEW_ZONE_FORM)).toEqual({ zoneType: '유형을 고르세요.' });
    expect(newZoneErrors({ ...NEW_ZONE_FORM, zoneType: 'LIFTING' })).toEqual({});
    expect(newZoneErrors({ ...NEW_ZONE_FORM, zoneType: 'LIFTING', name: ' ' }).name).toBe(
      '이름을 입력하세요.',
    );
  });

  it('처음 상자는 기존 구역들 가운데 바닥의 2×2×2m 이고, 본문은 양식 값과 상자를 그대로 싣는다', () => {
    const zones = [ZONE, { ...ZONE, min: [6, 7, 0] as const, max: [8, 9, 1] as const }];
    const box = newZoneBox(zones);
    expect(box.max[0] - box.min[0]).toBe(2);
    expect(box.max[2]).toBe(2);
    const body = newZoneBodyOf(
      { ...NEW_ZONE_FORM, zoneType: 'LIFTING', severity: 'CRITICAL' },
      box,
    );
    expect(body).toMatchObject({
      name: '새 구역',
      zone_kind: 'LIFTING',
      eval_type: 'VOLUME_3D',
      severity: 'CRITICAL',
      z_min_m: box.min[2],
      z_max_m: box.max[2],
    });
  });
});
