import { zoneOf } from '@/__tests__/features/zones/zoneFixture';
import type { StaticObjectItem, WorkSurface } from '@/features/surfaces/types';
import type { ZoneItem } from '@/features/zones/types';
import {
  commitArchive,
  commitCreate,
  commitPublish,
  commitSave,
  type CommitOps,
} from '@/features/zones/zoneCommit';
import { newSurfaceDraft, surfaceDraftOf } from '@/features/zones/zoneEditModel';
import { formOf, newZoneBodyOf } from '@/features/zones/zoneForm';
import { ApiError } from '@/lib/http';
import { describe, expect, it } from 'vitest';

const zone = zoneOf({ zoneId: 'z-04', name: '개구부', min: [5.8, 1.2, 0], max: [8.6, 3.8, 2.4] });
const linked: WorkSurface = {
  objectId: 'ws-1',
  version: 3,
  name: '단상',
  geometry: { centerXy: [7, 2.5], sizeXy: [1, 1], yawRad: 0, topHeightM: 0.4 },
  sigma: 0.02,
  validFrom: 1,
};
const zoneItem = { zone_id: 'z-04', zone_version: 2 } as ZoneItem;
const surfaceItem = { object_id: 'ws-1', object_version: 4 } as StaticObjectItem;

/** 부른 순서를 적는 가짜 요청. fail 에 적은 이름은 그 요청에서 실패한다. */
function fakeOps(fail: Partial<Record<keyof CommitOps, unknown>> = {}) {
  const calls: string[] = [];
  const bodies: Record<string, unknown> = {};
  const call =
    <A extends unknown[], R>(name: keyof CommitOps, result: R) =>
    (...args: A) => {
      calls.push(name);
      bodies[name] = args[0];
      return name in fail ? Promise.reject(fail[name]) : Promise.resolve(result);
    };
  const ops: CommitOps = {
    saveZone: call('saveZone', zoneItem),
    createZone: call('createZone', { ...zoneItem, zone_id: 'z-new', zone_version: 1 } as ZoneItem),
    archiveZone: call('archiveZone', undefined),
    saveSurface: call('saveSurface', surfaceItem),
    createSurface: call('createSurface', { ...surfaceItem, object_id: 'ws-new' }),
    archiveSurface: call('archiveSurface', undefined),
    publish: call('publish', {}),
  };
  return { ops, calls, bodies };
}

const base = {
  zone,
  form: { ...formOf(zone), name: '새 이름' },
  box: null,
  zoneChanged: true,
  linked,
  surface: undefined,
  surfaceChanged: false,
};
const taller = () => {
  const draft = surfaceDraftOf(linked);
  draft.geometry.topHeightM = 0.9;
  return draft;
};

describe('commitSave(기존 구역 저장 순서)', () => {
  it('구역 새 버전 → 작업면 새 버전 → 발행 순서로 하나씩 보낸다', async () => {
    const { ops, calls } = fakeOps();
    const outcome = await commitSave(ops, { ...base, surface: taller(), surfaceChanged: true });
    expect(calls).toEqual(['saveZone', 'saveSurface', 'publish']);
    expect(outcome.failed).toBeNull();
    expect(outcome.saved).toEqual({ zone: true, surface: true });
    expect(outcome.zone).toBe(zoneItem);
    expect(outcome.surface).toBe(surfaceItem);
  });

  it('작업면 본문은 상판 높이로 OBB 를 바닥부터 만들고 받은 표준편차를 다시 보낸다', async () => {
    const { ops, bodies } = fakeOps();
    await commitSave(ops, { ...base, surface: taller(), surfaceChanged: true });
    expect(bodies.saveSurface).toMatchObject({
      surface: linked,
      body: {
        kind: 'WORK_SURFACE',
        name: '단상',
        top_height_m: 0.9,
        top_height_sigma_m: 0.02,
        obb: { center: [7, 2.5, 0.45], size: [1, 1, 0.9], yaw_rad: 0 },
      },
    });
  });

  it('작업면만 고쳤으면 구역은 새 버전을 만들지 않는다', async () => {
    const { ops, calls } = fakeOps();
    await commitSave(ops, {
      ...base,
      zoneChanged: false,
      surface: taller(),
      surfaceChanged: true,
    });
    expect(calls).toEqual(['saveSurface', 'publish']);
  });

  it('구역만 고쳤으면 작업면은 보내지 않는다', async () => {
    const { ops, calls } = fakeOps();
    await commitSave(ops, base);
    expect(calls).toEqual(['saveZone', 'publish']);
  });

  it('이은 작업면을 지웠으면 작업면을 보관(DELETE)한다', async () => {
    const { ops, calls, bodies } = fakeOps();
    await commitSave(ops, { ...base, zoneChanged: false, surface: null, surfaceChanged: true });
    expect(calls).toEqual(['archiveSurface', 'publish']);
    expect(bodies.archiveSurface).toBe('ws-1');
  });

  it('새 작업면은 만들고(POST), 지우고 다시 추가했으면 옛 작업면을 먼저 보관한 뒤 만든다', async () => {
    const created = fakeOps();
    await commitSave(created.ops, {
      ...base,
      zoneChanged: false,
      linked: null,
      surface: newSurfaceDraft({ min: [5.8, 1.2, 0], max: [8.6, 3.8, 2.4] }),
      surfaceChanged: true,
    });
    expect(created.calls).toEqual(['createSurface', 'publish']);

    const replaced = fakeOps();
    await commitSave(replaced.ops, {
      ...base,
      zoneChanged: false,
      surface: newSurfaceDraft({ min: [5.8, 1.2, 0], max: [8.6, 3.8, 2.4] }),
      surfaceChanged: true,
    });
    expect(replaced.calls).toEqual(['archiveSurface', 'createSurface', 'publish']);
  });

  it('저장할 것이 없으면 아무것도 보내지 않고 발행도 하지 않는다', async () => {
    const { ops, calls } = fakeOps();
    const outcome = await commitSave(ops, { ...base, zoneChanged: false });
    expect(calls).toEqual([]);
    expect(outcome.saved).toEqual({ zone: false, surface: false });
    expect(outcome.failed).toBeNull();
  });

  it('구역 저장이 409(CONCURRENT_EDIT)로 실패하면 거기서 멈춘다: 작업면·발행은 보내지 않는다', async () => {
    const conflict = new ApiError(409, { code: 'CONCURRENT_EDIT' });
    const { ops, calls } = fakeOps({ saveZone: conflict });
    const outcome = await commitSave(ops, { ...base, surface: taller(), surfaceChanged: true });
    expect(calls).toEqual(['saveZone']);
    expect(outcome.failed).toEqual({ step: 'zone', error: conflict });
    expect(outcome.saved).toEqual({ zone: false, surface: false });
  });

  it('작업면 저장이 실패하면 구역은 저장된 채로 멈추고 발행하지 않는다', async () => {
    const conflict = new ApiError(409, { code: 'CONCURRENT_EDIT' });
    const { ops, calls } = fakeOps({ saveSurface: conflict });
    const outcome = await commitSave(ops, { ...base, surface: taller(), surfaceChanged: true });
    expect(calls).toEqual(['saveZone', 'saveSurface']);
    expect(outcome.failed).toEqual({ step: 'surface', error: conflict });
    expect(outcome.saved).toEqual({ zone: true, surface: false });
  });

  it('발행이 실패해도 저장은 남고 실패 단계가 publish 다', async () => {
    const down = new ApiError(503, { code: 'PUBLIC_URL_NOT_SET' });
    const { ops, calls } = fakeOps({ publish: down });
    const outcome = await commitSave(ops, base);
    expect(calls).toEqual(['saveZone', 'publish']);
    expect(outcome.failed).toEqual({ step: 'publish', error: down });
    expect(outcome.saved.zone).toBe(true);
  });
});

describe('commitCreate(새 구역)', () => {
  const body = newZoneBodyOf(
    { name: '새 구역', judgeType: 'VOLUME_3D', zoneType: 'OPENING', severity: 'HIGH' },
    { min: [4, 4, 0], max: [6, 6, 2] },
  );

  it('구역 만들기 → 작업면 만들기 → 발행', async () => {
    const { ops, calls } = fakeOps();
    const outcome = await commitCreate(ops, {
      body,
      surface: newSurfaceDraft({ min: [4, 4, 0], max: [6, 6, 2] }),
    });
    expect(calls).toEqual(['createZone', 'createSurface', 'publish']);
    expect(outcome.zone?.zone_id).toBe('z-new');
    expect(outcome.surface?.object_id).toBe('ws-new');
  });

  it('작업면이 없으면 구역과 발행만 보낸다', async () => {
    const { ops, calls } = fakeOps();
    await commitCreate(ops, { body, surface: undefined });
    expect(calls).toEqual(['createZone', 'publish']);
  });

  it('구역을 만든 뒤 작업면이 실패하면 구역은 남고 발행하지 않는다', async () => {
    const bad = new ApiError(400, { code: 'INVALID_STATIC_OBJECT' });
    const { ops, calls } = fakeOps({ createSurface: bad });
    const outcome = await commitCreate(ops, {
      body,
      surface: newSurfaceDraft({ min: [4, 4, 0], max: [6, 6, 2] }),
    });
    expect(calls).toEqual(['createZone', 'createSurface']);
    expect(outcome.zone?.zone_id).toBe('z-new');
    expect(outcome.failed).toEqual({ step: 'surface', error: bad });
  });

  it('구역 만들기가 실패하면 아무것도 더 보내지 않는다', async () => {
    const bad = new ApiError(400, { code: 'INVALID_POLYGON' });
    const { ops, calls } = fakeOps({ createZone: bad });
    const outcome = await commitCreate(ops, {
      body,
      surface: newSurfaceDraft({ min: [4, 4, 0], max: [6, 6, 2] }),
    });
    expect(calls).toEqual(['createZone']);
    expect(outcome.failed?.step).toBe('zone');
    expect(outcome.zone).toBeNull();
  });
});

describe('commitArchive(구역 삭제 = 보관)', () => {
  it('구역 보관 → 이은 작업면 보관 → 발행', async () => {
    const { ops, calls, bodies } = fakeOps();
    const outcome = await commitArchive(ops, { zoneId: 'z-04', linked });
    expect(calls).toEqual(['archiveZone', 'archiveSurface', 'publish']);
    expect(bodies.archiveZone).toBe('z-04');
    expect(bodies.archiveSurface).toBe('ws-1');
    expect(outcome.saved).toEqual({ zone: true, surface: true });
  });

  it('이은 작업면이 없으면 구역과 발행만 보낸다', async () => {
    const { ops, calls } = fakeOps();
    await commitArchive(ops, { zoneId: 'z-04', linked: null });
    expect(calls).toEqual(['archiveZone', 'publish']);
  });

  it('구역 보관이 실패하면 작업면은 건드리지 않는다', async () => {
    const down = new ApiError(500);
    const { ops, calls } = fakeOps({ archiveZone: down });
    const outcome = await commitArchive(ops, { zoneId: 'z-04', linked });
    expect(calls).toEqual(['archiveZone']);
    expect(outcome.failed).toEqual({ step: 'zone', error: down });
  });
});

describe('commitPublish(발행만 다시 보내기)', () => {
  it('저장 없이 발행 하나만 보낸다', async () => {
    const { ops, calls } = fakeOps();
    const outcome = await commitPublish(ops);
    expect(calls).toEqual(['publish']);
    expect(outcome.failed).toBeNull();
    expect(outcome.saved).toEqual({ zone: false, surface: false });
  });

  it('발행이 실패하면 실패 단계가 publish 다', async () => {
    const down = new ApiError(500);
    const { ops } = fakeOps({ publish: down });
    expect((await commitPublish(ops)).failed).toEqual({ step: 'publish', error: down });
  });
});
