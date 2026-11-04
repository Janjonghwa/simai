import { zoneOf } from '@/__tests__/features/zones/zoneFixture';
import { bodyOf as surfaceBodyOf } from '@/features/surfaces/surfaces';
import type { WorkSurface } from '@/features/surfaces/types';
import type { ViewerEditBox } from '@/features/viewer3d';
import type { ZoneCommitRequest } from '@/features/zones/types';
import {
  archiveRequestOf,
  createRequestOf,
  saveRequestOf,
  type SaveRequestInput,
} from '@/features/zones/zoneCommit';
import { newSurfaceDraft, surfaceDraftOf, surfaceZoneBox } from '@/features/zones/zoneEditModel';
import { formOf, newZoneBodyOf, bodyOf as zoneBodyOf } from '@/features/zones/zoneForm';
import { DEFAULT_ROOM_LIMIT } from '@/lib/roomLimit';
import { describe, expect, it } from 'vitest';

const zone = zoneOf({ zoneId: 'z-04', name: '개구부', min: [5.8, 1.2, 0], max: [8.6, 3.8, 2.4] });
const ZONE_BOX: ViewerEditBox = { min: [5.8, 1.2, 0], max: [8.6, 3.8, 2.4] };
const MOVED_BOX: ViewerEditBox = { min: [6, 1.2, 0], max: [8.8, 3.8, 2.4] };
const linked: WorkSurface = {
  objectId: 'ws-1',
  version: 3,
  name: '단상',
  geometry: { centerXy: [7, 2.5], sizeXy: [1, 1], yawRad: 0, topHeightM: 0.4 },
  sigma: 0.02,
  zoneId: 'z-04',
  validFrom: 1,
};
const form = { ...formOf(zone), name: '새 이름' };

const base: SaveRequestInput = {
  zone,
  form,
  box: null,
  zoneChanged: true,
  linked,
  surface: undefined,
  surfaceChanged: false,
};
/** 이은 작업면을 그대로 두고 상판만 높인 초안. */
const taller = () => {
  const draft = surfaceDraftOf(linked);
  draft.geometry.topHeightM = 0.9;
  return draft;
};
/** 아직 등록하지 않은 새 작업면 초안(id null). */
const added = () => newSurfaceDraft(ZONE_BOX);

const zoneUpdate = { op: 'UPDATE', zone_id: 'z-04', body: zoneBodyOf(zone, form, null) } as const;
const zoneKeep = { op: 'KEEP', zone_id: 'z-04' } as const;

interface Case {
  name: string;
  request: ZoneCommitRequest | null;
  expected: ZoneCommitRequest | null;
}

/** 요청 본문이 기대와 같고, 키 하나도 더 붙지 않았고, publish 는 붙이지 않았다. */
function expectRequest({ request, expected }: Case) {
  expect(request).toStrictEqual(expected);
  if (request) expect('publish' in request).toBe(false);
}

describe('saveRequestOf(기존 구역 저장)', () => {
  const raised = taller();
  const replacing = { ...surfaceDraftOf(linked), id: 'ws-other', name: '다른 단상' };
  const cases: Case[] = [
    {
      name: '구역만 고침: UPDATE, 작업면 키 없음',
      request: saveRequestOf(base),
      expected: { zone: zoneUpdate },
    },
    {
      name: '구역만 고침(작업면 초안은 있으나 안 바뀜): UPDATE, 작업면 키 없음',
      request: saveRequestOf({ ...base, surface: surfaceDraftOf(linked), surfaceChanged: false }),
      expected: { zone: zoneUpdate },
    },
    {
      name: '구역 상자를 고침: UPDATE 본문이 고친 상자의 바닥·높이다',
      request: saveRequestOf({ ...base, box: MOVED_BOX }),
      expected: { zone: { ...zoneUpdate, body: zoneBodyOf(zone, form, MOVED_BOX) } },
    },
    {
      name: '작업면만 고침: 구역 KEEP + 작업면 UPDATE(같은 object_id)',
      request: saveRequestOf({
        ...base,
        zoneChanged: false,
        surface: raised,
        surfaceChanged: true,
      }),
      expected: {
        zone: zoneKeep,
        surface: {
          op: 'UPDATE',
          object_id: 'ws-1',
          body: surfaceBodyOf(linked, '단상', raised.geometry),
        },
      },
    },
    {
      name: '작업면 추가(이은 작업면 없음): 구역 KEEP + 작업면 CREATE',
      request: saveRequestOf({
        ...base,
        zoneChanged: false,
        linked: null,
        surface: added(),
        surfaceChanged: true,
      }),
      expected: {
        zone: zoneKeep,
        surface: { op: 'CREATE', body: surfaceBodyOf(null, '', added().geometry) },
      },
    },
    {
      name: '지우고 다시 추가(초안 id 가 null): 구역 KEEP + 작업면 REPLACE(옛 object_id)',
      request: saveRequestOf({
        ...base,
        zoneChanged: false,
        surface: added(),
        surfaceChanged: true,
      }),
      expected: {
        zone: zoneKeep,
        surface: {
          op: 'REPLACE',
          object_id: 'ws-1',
          body: surfaceBodyOf(null, '', added().geometry),
        },
      },
    },
    {
      name: '초안 id 가 이은 작업면과 다르면: 구역 KEEP + 작업면 REPLACE(옛 object_id)',
      request: saveRequestOf({
        ...base,
        zoneChanged: false,
        surface: replacing,
        surfaceChanged: true,
      }),
      expected: {
        zone: zoneKeep,
        surface: {
          op: 'REPLACE',
          object_id: 'ws-1',
          body: surfaceBodyOf(null, '다른 단상', replacing.geometry),
        },
      },
    },
    {
      name: '작업면 삭제: 구역 KEEP + 작업면 ARCHIVE',
      request: saveRequestOf({ ...base, zoneChanged: false, surface: null, surfaceChanged: true }),
      expected: { zone: zoneKeep, surface: { op: 'ARCHIVE', object_id: 'ws-1' } },
    },
    {
      name: '구역과 작업면 둘 다 고침: 구역 UPDATE + 작업면 op 하나',
      request: saveRequestOf({ ...base, surface: raised, surfaceChanged: true }),
      expected: {
        zone: zoneUpdate,
        surface: {
          op: 'UPDATE',
          object_id: 'ws-1',
          body: surfaceBodyOf(linked, '단상', raised.geometry),
        },
      },
    },
    {
      name: '구역 UPDATE + 작업면 삭제',
      request: saveRequestOf({ ...base, surface: null, surfaceChanged: true }),
      expected: { zone: zoneUpdate, surface: { op: 'ARCHIVE', object_id: 'ws-1' } },
    },
    {
      name: '바뀐 것 없음(구역·작업면 모두): null',
      request: saveRequestOf({
        ...base,
        zoneChanged: false,
        surface: surfaceDraftOf(linked),
        surfaceChanged: false,
      }),
      expected: null,
    },
    {
      name: '바뀐 것 없음(작업면 초안이 없고 구역도 그대로): null',
      request: saveRequestOf({ ...base, zoneChanged: false, surface: undefined }),
      expected: null,
    },
    {
      name: '작업면 초안이 undefined 이면 surfaceChanged 가 참이어도 작업면 op 가 없다: null',
      request: saveRequestOf({
        ...base,
        zoneChanged: false,
        surface: undefined,
        surfaceChanged: true,
      }),
      expected: null,
    },
    {
      name: '초안도 이은 작업면도 null 이면 surfaceChanged 가 참이어도 작업면 op 가 없다: null',
      request: saveRequestOf({
        ...base,
        zoneChanged: false,
        linked: null,
        surface: null,
        surfaceChanged: true,
      }),
      expected: null,
    },
    {
      name: '초안도 이은 작업면도 null 인데 구역은 고쳤다: UPDATE, 작업면 키 없음',
      request: saveRequestOf({ ...base, linked: null, surface: null, surfaceChanged: true }),
      expected: { zone: zoneUpdate },
    },
  ];

  it.each(cases)('$name', expectRequest);

  it('작업면 UPDATE 본문은 상판 높이로 OBB 를 바닥부터 만들고 받은 표준편차를 다시 보낸다', () => {
    const request = saveRequestOf({
      ...base,
      zoneChanged: false,
      surface: taller(),
      surfaceChanged: true,
    });
    expect(request?.surface).toMatchObject({
      op: 'UPDATE',
      body: {
        kind: 'WORK_SURFACE',
        name: '단상',
        top_height_m: 0.9,
        top_height_sigma_m: linked.sigma,
        obb: { center: [7, 2.5, 0.45], size: [1, 1, 0.9], yaw_rad: 0 },
      },
    });
  });

  it('REPLACE·CREATE 본문은 새 작업면이라 표준편차를 보내지 않는다(null)', () => {
    const replace = saveRequestOf({
      ...base,
      zoneChanged: false,
      surface: added(),
      surfaceChanged: true,
    });
    expect(replace?.surface).toMatchObject({ op: 'REPLACE', body: { top_height_sigma_m: null } });
  });
});

describe('createRequestOf(새 구역)', () => {
  const newForm = {
    name: ' 새 구역 ',
    judgeType: 'VOLUME_3D',
    zoneType: 'OPENING',
    severity: 'HIGH',
  } as const;
  const newBox: ViewerEditBox = { min: [4, 4, 0], max: [6, 6, 2] };
  const newBody = newZoneBodyOf(newForm, newBox);
  const named = { ...newSurfaceDraft(newBox), name: '단상' };
  const cases: Case[] = [
    {
      name: '작업면 초안이 undefined: 구역 CREATE 만, 작업면 키 없음',
      request: createRequestOf({ form: newForm, box: newBox, surface: undefined }),
      expected: { zone: { op: 'CREATE', body: newBody } },
    },
    {
      name: '작업면 초안이 null: 구역 CREATE 만, 작업면 키 없음',
      request: createRequestOf({ form: newForm, box: newBox, surface: null }),
      expected: { zone: { op: 'CREATE', body: newBody } },
    },
    {
      name: '작업면 초안이 있으면 구역 CREATE + 작업면 CREATE(이름 없음은 null)',
      request: createRequestOf({ form: newForm, box: newBox, surface: newSurfaceDraft(newBox) }),
      // 구역 몸체는 작업면이 있어도 상자·양식 그대로다(다각형·심각도·높이를 덮어쓰지 않는다).
      expected: {
        zone: { op: 'CREATE', body: newBody },
        surface: {
          op: 'CREATE',
          body: surfaceBodyOf(null, '', newSurfaceDraft(newBox).geometry),
        },
      },
    },
    {
      name: '이름이 있는 작업면 초안은 그 이름을 보낸다',
      request: createRequestOf({ form: newForm, box: newBox, surface: named }),
      expected: {
        zone: { op: 'CREATE', body: newBody },
        surface: { op: 'CREATE', body: surfaceBodyOf(null, '단상', named.geometry) },
      },
    },
  ];

  it.each(cases)('$name', expectRequest);

  it('작업면이 회전해 있어도 고른 심각도와 상자 그대로 보낸다(경고로 바꾸지 않는다)', () => {
    const surface = { ...named, geometry: { ...named.geometry, yawRad: Math.PI / 2 } };
    const box = surfaceZoneBox(surface.geometry, DEFAULT_ROOM_LIMIT);
    const request = createRequestOf({ form: { ...newForm, severity: 'CRITICAL' }, box, surface });
    expect(request.surface).toMatchObject({ op: 'CREATE', body: { kind: 'WORK_SURFACE' } });
    if (request.zone.op !== 'CREATE') throw new Error('expected CREATE');
    expect(request.zone.body).toEqual(newZoneBodyOf({ ...newForm, severity: 'CRITICAL' }, box));
    expect(request.zone.body.severity).toBe('CRITICAL');
    expect(request.publish).toBeUndefined();
  });

  it('본문은 이름 앞뒤 공백을 뺀 새 구역(v1)과 새 작업면이다', () => {
    const request = createRequestOf({ form: newForm, box: newBox, surface: named });
    expect(request.zone).toMatchObject({
      op: 'CREATE',
      body: { name: '새 구역', polygon_xy: expect.any(Array), z_min_m: 0, z_max_m: 2 },
    });
    expect(request.surface).toMatchObject({
      op: 'CREATE',
      body: { name: '단상', top_height_sigma_m: null },
    });
  });
});

describe('archiveRequestOf(구역 삭제 = 보관)', () => {
  const cases: Case[] = [
    {
      name: '구역 ARCHIVE 하나만 보내고 작업면 키는 붙이지 않는다(서버가 이은 작업면도 보관한다)',
      request: archiveRequestOf('z-04'),
      expected: { zone: { op: 'ARCHIVE', zone_id: 'z-04' } },
    },
  ];

  it.each(cases)('$name', expectRequest);
});
