import { zoneOf } from '@/__tests__/features/zones/zoneFixture';
import type { WorkSurface } from '@/features/surfaces/types';
import type { ViewerEditBox } from '@/features/viewer3d';
import {
  boxEditOf,
  changedPatch,
  currentSurface,
  defaultNewBox,
  editedBox,
  floorShift,
  formWithPatch,
  ghostsOf,
  isEmptyDraft,
  NEW_KEY,
  newSurfaceDraft,
  newZoneForm,
  surfaceBox,
  surfaceBoxEditable,
  surfaceChanged,
  surfaceDraftOf,
  surfaceOfBox,
  targetChanged,
  withPatch,
  withSurface,
  withSurfaceBox,
  withZoneBox,
  zoneChanged,
  type TargetDraft,
} from '@/features/zones/zoneEditModel';
import { DEFAULT_ROOM_LIMIT, roomLimitFrom } from '@/lib/roomLimit';
import { describe, expect, it } from 'vitest';

const zone = zoneOf(
  { zoneId: 'z-04', name: '개구부', min: [5.8, 1.2, 0], max: [8.6, 3.8, 2.4] },
  { severity: 'CRITICAL', zoneType: 'PLATFORM' },
);
const linked: WorkSurface = {
  objectId: 'ws-1',
  version: 2,
  name: '단상',
  geometry: { centerXy: [7, 2.5], sizeXy: [1, 1], yawRad: 0, topHeightM: 0.4 },
  sigma: null,
  zoneId: null,
  validFrom: 1,
};

describe('작업면 ↔ 3D 편집 상자', () => {
  it('상자는 바닥(0)부터 상판까지이고, 되돌리면 같은 형상이다', () => {
    const box = surfaceBox(linked.geometry);
    expect(box).toEqual({ min: [6.5, 2, 0], max: [7.5, 3, 0.4] });
    expect(surfaceOfBox(box, linked.geometry)).toEqual(linked.geometry);
  });

  it('상자를 고쳐도 회전은 받은 값을 그대로 둔다', () => {
    const turned = { ...linked.geometry, yawRad: -0.2618 };
    const next = surfaceOfBox({ min: [1, 1, 0], max: [2, 3, 0.7] }, turned);
    expect(next).toEqual({ centerXy: [1.5, 2], sizeXy: [1, 2], yawRad: -0.2618, topHeightM: 0.7 });
    expect(surfaceBoxEditable(turned)).toBe(false);
    expect(surfaceBoxEditable(linked.geometry)).toBe(true);
  });

  it('새 작업면은 구역 상자 가운데에 1×1 m, 상판 0.4 m 이고 아직 등록하지 않았다(id null)', () => {
    const draft = newSurfaceDraft({ min: [2, 2, 0], max: [4, 6, 2] });
    expect(draft).toEqual({
      id: null,
      name: null,
      geometry: { centerXy: [3, 4], sizeXy: [1, 1], yawRad: 0, topHeightM: 0.4 },
    });
  });
});

describe('구역 이동에 따라 작업면도 옮기기(floorShift)', () => {
  const from: ViewerEditBox = { min: [1, 1, 0], max: [3, 3, 2] };

  it('크기 그대로 바닥만 옮기면 그 이동량이다', () => {
    expect(floorShift(from, { min: [1.5, 0.5, 0], max: [3.5, 2.5, 2] })).toEqual([0.5, -0.5]);
  });

  it('크기가 바뀌었거나 높이가 바뀌었거나 그대로면 null 이다', () => {
    expect(floorShift(from, { min: [1, 1, 0], max: [4, 3, 2] })).toBeNull();
    expect(floorShift(from, { min: [1, 1, 0.5], max: [3, 3, 2.5] })).toBeNull();
    expect(floorShift(from, from)).toBeNull();
  });
});

describe('새 구역의 처음 상자', () => {
  const SCAN_ROOM = roomLimitFrom({ min: [1, 2, 0], max: [7, 12, 3] });

  it('구역이 없으면 방 가운데에 2×2×2 m 이다', () => {
    expect(defaultNewBox([], DEFAULT_ROOM_LIMIT)).toEqual({ min: [4, 4, 0], max: [6, 6, 2] });
  });

  it('스캔 범위가 주어지면 구역이 없을 때 스캔 가운데에 둔다', () => {
    expect(defaultNewBox([], SCAN_ROOM)).toEqual({ min: [3, 6, 0], max: [5, 8, 2] });
  });

  it('스캔 범위가 주어지면 구역 가운데도 스캔 경계 안으로 가둔다', () => {
    const box = defaultNewBox(
      [{ zoneId: 'a', name: 'a', min: [9, 9, 0], max: [10, 10, 2] }],
      SCAN_ROOM,
    );
    expect(box).toEqual({ min: [5, 8.5, 0], max: [7, 10.5, 2] });
  });

  it('넘긴 범위를 그대로 쓴다', () => {
    expect(defaultNewBox([], { min: [0, 0, 0], max: [4, 6, 5] })).toEqual({
      min: [1, 2, 0],
      max: [3, 4, 2],
    });
  });

  it('구역들을 감싼 상자의 가운데이고 교실 안에 가둔다', () => {
    const box = defaultNewBox(
      [
        { zoneId: 'a', name: 'a', min: [9, 9, 0], max: [10, 10, 2] },
        { zoneId: 'b', name: 'b', min: [9.5, 9.5, 0], max: [10, 10, 2] },
      ],
      DEFAULT_ROOM_LIMIT,
    );
    expect(box).toEqual({ min: [8, 8, 0], max: [10, 10, 2] });
  });
});

describe('저장 전에 고친 것', () => {
  it('이름·심각도는 저장된 값과 같으면 고친 것이 아니다', () => {
    expect(changedPatch(zone, { name: '개구부', severity: 'HIGH' })).toEqual({ severity: 'HIGH' });
    expect(formWithPatch(zone, { name: 'x' })).toMatchObject({
      name: 'x',
      severity: 'CRITICAL',
      zoneType: 'PLATFORM',
      judgeType: 'VOLUME_3D',
    });
  });

  it('상자는 원본과 같으면 고친 것이 아니다', () => {
    const same: TargetDraft = { patch: {}, box: { min: [5.8, 1.2, 0], max: [8.6, 3.8, 2.4] } };
    const moved: TargetDraft = { patch: {}, box: { min: [5.9, 1.2, 0], max: [8.7, 3.8, 2.4] } };
    expect(zoneChanged(zone, same)).toBe(false);
    expect(zoneChanged(zone, moved)).toBe(true);
    expect(zoneChanged(zone, undefined)).toBe(false);
  });

  it('작업면: 건드리지 않음 · 같은 값 · 지움 · 새로 만듦 · 값을 바꿈', () => {
    expect(surfaceChanged(undefined, linked)).toBe(false);
    expect(surfaceChanged(surfaceDraftOf(linked), linked)).toBe(false);
    expect(surfaceChanged(null, linked)).toBe(true);
    expect(surfaceChanged(null, null)).toBe(false);
    expect(surfaceChanged(newSurfaceDraft({ min: [0, 0, 0], max: [2, 2, 2] }), null)).toBe(true);
    const taller = surfaceDraftOf(linked);
    taller.geometry.topHeightM = 0.9;
    expect(surfaceChanged(taller, linked)).toBe(true);
    // 이름 앞뒤 공백은 같은 이름이다
    expect(surfaceChanged({ ...surfaceDraftOf(linked), name: ' 단상 ' }, linked)).toBe(false);
  });

  it('지금 보이는 작업면: 건드렸으면 그 값, 아니면 이은 작업면', () => {
    expect(currentSurface(undefined, linked)?.id).toBe('ws-1');
    expect(currentSurface({ patch: {}, box: null, surface: null }, linked)).toBeNull();
    expect(currentSurface(undefined, null)).toBeNull();
  });

  it('구역이나 작업면 하나라도 고쳤으면 고친 대상이다', () => {
    expect(targetChanged(zone, undefined, linked)).toBe(false);
    expect(targetChanged(zone, { patch: {}, box: null, surface: null }, linked)).toBe(true);
    expect(targetChanged(zone, { patch: { name: 'x' }, box: null }, null)).toBe(true);
  });
});

describe('초안 갱신', () => {
  const ORIGINAL: ViewerEditBox = { min: [5.8, 1.2, 0], max: [8.6, 3.8, 2.4] };
  const MOVED: ViewerEditBox = { min: [6.8, 1.2, 0], max: [9.6, 3.8, 2.4] };

  it('이름·심각도: 저장된 값과 같은 칸은 빼고, 새 구역은 그대로 둔다', () => {
    const named = withPatch(undefined, zone, { name: '새 이름' });
    expect(named.patch).toEqual({ name: '새 이름' });
    expect(withPatch(named, zone, { name: zone.name }).patch).toEqual({});
    expect(withPatch(undefined, null, { name: zone.name }).patch).toEqual({ name: zone.name });
  });

  it('작업면: undefined 는 작업면 칸을 지우고, null 은 지운다고 적는다', () => {
    const kept: TargetDraft = { patch: {}, box: null, surface: null };
    expect(withSurface(kept, undefined)).toEqual({ patch: {}, box: null });
    expect(withSurface(undefined, null)).toEqual({ patch: {}, box: null, surface: null });
    expect(withPatch(kept, zone, { name: 'x' }).surface).toBeNull();
  });

  it('작업면 상자를 끌면 그 형상만 바뀌고 회전은 그대로다', () => {
    const next = withSurfaceBox(undefined, surfaceDraftOf(linked), {
      min: [6.5, 2, 0],
      max: [7.5, 3, 0.9],
    });
    expect(next.surface?.geometry).toMatchObject({
      centerXy: [7, 2.5],
      topHeightM: 0.9,
      yawRad: 0,
    });
  });

  it('구역 상자를 끌면 상자가 초안이 되고, 원본과 같아지면 지운다', () => {
    expect(withZoneBox(undefined, zone, null, ORIGINAL, MOVED).box).toBe(MOVED);
    expect(withZoneBox({ patch: {}, box: MOVED }, zone, null, MOVED, ORIGINAL).box).toBeNull();
    expect(withZoneBox(undefined, null, null, ORIGINAL, MOVED).box).toBe(MOVED);
  });

  it('구역을 크기 그대로 옮기면 이은 작업면도 같이 옮기고, 크기를 바꾸면 그대로 둔다', () => {
    const surface = surfaceDraftOf(linked);
    const shifted = withZoneBox(undefined, zone, surface, ORIGINAL, MOVED);
    expect(shifted.surface?.geometry.centerXy).toEqual([8, 2.5]);
    const resized = withZoneBox(undefined, zone, surface, ORIGINAL, {
      min: [5.8, 1.2, 0],
      max: [8.1, 3.8, 2.4],
    });
    expect(resized.surface).toBeUndefined();
    const kept = withZoneBox({ patch: {}, box: null, surface: null }, zone, null, ORIGINAL, MOVED);
    expect(kept.surface).toBeNull();
  });

  it('고친 칸·상자·작업면이 하나도 없으면 빈 초안이다', () => {
    expect(isEmptyDraft({ patch: {}, box: null })).toBe(true);
    expect(isEmptyDraft({ patch: { name: 'x' }, box: null })).toBe(false);
    expect(isEmptyDraft({ patch: {}, box: MOVED })).toBe(false);
    // 작업면 칸이 null 이면 저장된 작업면을 지우겠다는 초안이라 비지 않았다
    expect(isEmptyDraft({ patch: {}, box: null, surface: null })).toBe(false);
    expect(isEmptyDraft({ patch: {}, box: null, surface: surfaceDraftOf(linked) })).toBe(false);
  });

  it('원본과 다르게 고친 상자만 저장할 상자다', () => {
    expect(editedBox(zone, undefined)).toBeNull();
    expect(editedBox(zone, { patch: {}, box: ORIGINAL })).toBeNull();
    expect(editedBox(zone, { patch: {}, box: MOVED })).toBe(MOVED);
  });

  it('새 구역 양식은 고르지 않는 칸을 기본값으로 채우고 고친 칸만 덮는다', () => {
    expect(newZoneForm({})).toEqual({
      name: '새 구역',
      judgeType: 'VOLUME_3D',
      zoneType: 'OPENING',
      severity: 'HIGH',
    });
    expect(newZoneForm({ name: 'A', severity: 'INFO' })).toMatchObject({
      name: 'A',
      severity: 'INFO',
    });
  });
});

describe('3D 로 내려 줄 값', () => {
  const ZONES = [zone];
  const BOX: ViewerEditBox = { min: [5.8, 1.2, 0], max: [8.6, 3.8, 2.4] };
  const base = {
    canEdit: true,
    surfMode: false,
    key: 'z-04',
    zone,
    zoneBox: BOX,
    surface: surfaceDraftOf(linked),
    surfaceValid: true,
  };

  it('구역 조절이면 구역 상자, 작업면 조절이면 작업면 상자다', () => {
    expect(boxEditOf(base)).toEqual({ id: 'z-04', kind: 'zone', box: BOX });
    expect(boxEditOf({ ...base, surfMode: true })).toMatchObject({
      id: 'surface:z-04',
      kind: 'surface',
    });
  });

  it('관리자가 아니거나, 형상을 못 고치는 구역이거나, 작업면이 틀렸으면 손잡이가 없다', () => {
    expect(boxEditOf({ ...base, canEdit: false })).toBeNull();
    expect(boxEditOf({ ...base, zone: { ...zone, shapeEditable: false } })).toBeNull();
    expect(boxEditOf({ ...base, surfMode: true, surfaceValid: false })).toBeNull();
    expect(boxEditOf({ ...base, surfMode: true, surface: null })).toBeNull();
    const rotated = surfaceDraftOf({ ...linked, geometry: { ...linked.geometry, yawRad: 0.5 } });
    expect(boxEditOf({ ...base, surfMode: true, surface: rotated })).toBeNull();
  });

  it('새 구역은 형상 제한이 없다', () => {
    expect(boxEditOf({ ...base, key: NEW_KEY, zone: null })).toMatchObject({ kind: 'zone' });
  });

  it('윤곽은 고른 대상 말고 고친 상자가 있는 대상이고, 작업면 조절 중에는 고른 구역도 보인다', () => {
    const drafts = {
      'z-04': { patch: {}, box: BOX },
      'z-gone': { patch: {}, box: BOX },
      [NEW_KEY]: { patch: {}, box: BOX },
      'z-02': { patch: { name: 'x' }, box: null },
    };
    expect(ghostsOf(drafts, 'z-04', false, ZONES).map((g) => g.id)).toEqual([NEW_KEY]);
    expect(ghostsOf(drafts, 'z-04', true, ZONES).map((g) => g.id)).toEqual(['z-04', NEW_KEY]);
  });
});
