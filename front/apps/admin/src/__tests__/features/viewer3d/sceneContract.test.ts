import {
  IDLE_LOAD,
  readCurrentScene,
  sceneStatusOf,
  verifyScene,
  type CurrentScene,
  type SceneLoad,
  type SceneVersions,
} from '@/features/viewer3d/scene/sceneContract';
import { describe, expect, it } from 'vitest';

// -59 배경 Scene 입력 계약(BE -264 GET /api/scenes/current 본문)과 상태·표시 판정.

const NOW = 1_760_342_400_000;
const committed = (patch: Record<string, unknown> = {}) => ({
  scene_version: 'scene-4',
  state: 'COMMITTED',
  default: false,
  calibration_version: 'cal-2',
  committed_at_ms: NOW - 60_000,
  quality: { reprojection_px: 1.2 },
  gltf_url: 'https://bucket.example/scenes/scene-4.glb?X-Amz-Signature=abc',
  gltf_expires_at_ms: NOW + 300_000,
  ...patch,
});
const defaultScene = (patch: Record<string, unknown> = {}) =>
  committed({
    scene_version: 'scene-default',
    state: 'DEFAULT',
    default: true,
    calibration_version: null,
    committed_at_ms: null,
    ...patch,
  });

const MATCH: SceneVersions = {
  worldScene: 'scene-4',
  worldCalibration: 'cal-2',
  healthCommitted: 'scene-4',
  healthLive: 'scene-4',
  healthMismatch: false,
};
const UNKNOWN: SceneVersions = {
  worldScene: null,
  worldCalibration: null,
  healthCommitted: null,
  healthLive: null,
  healthMismatch: null,
};
const loaded = (sceneVersion = 'scene-4'): SceneLoad => ({
  phase: 'LOADED',
  sceneVersion,
  reason: null,
  boundsWorld: { min: [0, 0, 0], max: [10, 10, 3] },
});

const sceneOf = (raw: unknown): CurrentScene => {
  const read = readCurrentScene(raw);
  if (!read.ok) throw new Error(read.reason);
  return read.scene;
};

describe('readCurrentScene', () => {
  it('COMMITTED·DEFAULT 응답을 그대로 읽는다(quality 는 쓰지 않는다)', () => {
    expect(sceneOf(committed())).toEqual({
      sceneVersion: 'scene-4',
      state: 'COMMITTED',
      calibrationVersion: 'cal-2',
      committedAtMs: NOW - 60_000,
      gltfUrl: 'https://bucket.example/scenes/scene-4.glb?X-Amz-Signature=abc',
      gltfExpiresAtMs: NOW + 300_000,
    });
    expect(sceneOf(defaultScene())).toMatchObject({
      state: 'DEFAULT',
      calibrationVersion: null,
      committedAtMs: null,
    });
    // 저장소를 쓸 수 없으면 주소 없이 Scene 정보만 온다.
    expect(sceneOf(committed({ gltf_url: null, gltf_expires_at_ms: null }))).toMatchObject({
      gltfUrl: null,
      gltfExpiresAtMs: null,
    });
    // 같은 출처 경로·blob 주소도 받는다.
    expect(sceneOf(committed({ gltf_url: '/assets/scene.gltf' })).gltfUrl).toBe(
      '/assets/scene.gltf',
    );
  });

  it.each([
    ['객체 아님', null, '응답이 객체가 아니다'],
    ['배열', [], '응답이 객체가 아니다'],
    ['버전 없음', committed({ scene_version: ' ' }), 'scene_version 이 비었다'],
    ['상태 오류', committed({ state: 'DRAFT' }), 'state 는 COMMITTED 또는 DEFAULT 여야 한다'],
    ['default 불일치', committed({ default: true }), 'default 와 state 가 맞지 않는다'],
    ['default 누락', committed({ default: undefined }), 'default 와 state 가 맞지 않는다'],
    [
      'COMMITTED 보정 없음',
      committed({ calibration_version: null }),
      'COMMITTED Scene 에 calibration_version 이 없다',
    ],
    ['보정 형식', committed({ calibration_version: 3 }), 'calibration_version 형식이 틀렸다'],
    ['확정 시각 형식', committed({ committed_at_ms: '1' }), 'committed_at_ms 형식이 틀렸다'],
    [
      'javascript 주소',
      committed({ gltf_url: 'javascript:alert(1)' }),
      'gltf_url 이 읽을 수 있는 주소가 아니다',
    ],
    [
      'file 주소',
      committed({ gltf_url: 'file:///c/x.glb' }),
      'gltf_url 이 읽을 수 있는 주소가 아니다',
    ],
    ['만료 형식', committed({ gltf_expires_at_ms: NaN }), 'gltf_expires_at_ms 형식이 틀렸다'],
  ])('%s 이면 거부하고 주소를 이유에 넣지 않는다', (_name, raw, reason) => {
    const read = readCurrentScene(raw);
    expect(read).toEqual({ ok: false, reason });
    expect(JSON.stringify(read)).not.toContain('X-Amz');
  });
});

describe('verifyScene', () => {
  const scene = sceneOf(committed());

  it('World·Health 버전이 모두 같으면 VERIFIED', () => {
    expect(verifyScene(scene, MATCH)).toEqual({ verification: 'VERIFIED', mismatches: [] });
  });

  it('DEFAULT 는 버전과 관계없이 정합 근거가 없다', () => {
    expect(verifyScene(sceneOf(defaultScene()), MATCH).verification).toBe('DEFAULT_UNVERIFIED');
  });

  it('World 가 아직 없으면 UNCONFIRMED', () => {
    expect(verifyScene(scene, UNKNOWN)).toEqual({ verification: 'UNCONFIRMED', mismatches: [] });
    expect(verifyScene(scene, { ...UNKNOWN, worldScene: 'scene-4' }).verification).toBe(
      'UNCONFIRMED',
    );
  });

  it('어긋난 항목을 모두 적는다', () => {
    expect(
      verifyScene(scene, {
        worldScene: 'scene-5',
        worldCalibration: 'cal-3',
        healthCommitted: 'scene-5',
        healthLive: 'scene-5',
        healthMismatch: true,
      }),
    ).toEqual({
      verification: 'MISMATCH',
      mismatches: [
        'world.scene_version=scene-5',
        'world.calibration_version=cal-3',
        'health.scene.committed_version=scene-5',
        'health.scene.mismatch=true',
      ],
    });
    // World 가 없어도 Health 가 어긋남을 알리면 MISMATCH 다.
    expect(verifyScene(scene, { ...UNKNOWN, healthMismatch: true }).verification).toBe('MISMATCH');
  });
});

describe('sceneStatusOf', () => {
  it('입력이 없으면 NONE·HIDDEN', () => {
    expect(sceneStatusOf(null, IDLE_LOAD, MATCH)).toMatchObject({
      phase: 'NONE',
      display: 'HIDDEN',
      needsRefetch: false,
      axisConvention: 'GLTF_Y_UP_ASSUMED',
    });
  });

  it('조회 실패는 UNAVAILABLE 이고 401·403 만 다시 받기를 요청한다', () => {
    const notFound = sceneStatusOf(
      { ok: false, reason: 'SCENE_NOT_FOUND', httpStatus: 404 },
      IDLE_LOAD,
      MATCH,
    );
    expect(notFound).toMatchObject({
      phase: 'UNAVAILABLE',
      reason: 'SCENE_NOT_FOUND',
      needsRefetch: false,
      display: 'HIDDEN',
    });
    for (const httpStatus of [401, 403]) {
      expect(
        sceneStatusOf({ ok: false, reason: 'AUTH', httpStatus }, IDLE_LOAD, MATCH).needsRefetch,
      ).toBe(true);
    }
  });

  it('형식 오류는 REJECTED, 주소 없음은 NO_ASSET 이고 둘 다 숨긴다', () => {
    expect(
      sceneStatusOf({ ok: true, raw: committed({ state: 'X' }) }, IDLE_LOAD, MATCH),
    ).toMatchObject({ phase: 'REJECTED', display: 'HIDDEN', sceneVersion: null });
    expect(
      sceneStatusOf({ ok: true, raw: committed({ gltf_url: null }) }, IDLE_LOAD, MATCH),
    ).toMatchObject({
      phase: 'NO_ASSET',
      display: 'HIDDEN',
      sceneVersion: 'scene-4',
      verification: 'VERIFIED',
    });
  });

  it('표시 규칙: VERIFIED·LOADED 만 FULL, 근거 없음은 DIMMED, 불일치·미적재는 HIDDEN', () => {
    const input = { ok: true as const, raw: committed() };
    expect(sceneStatusOf(input, loaded(), MATCH)).toMatchObject({
      phase: 'LOADED',
      display: 'FULL',
      boundsWorld: { min: [0, 0, 0], max: [10, 10, 3] },
    });
    expect(sceneStatusOf(input, loaded(), UNKNOWN).display).toBe('DIMMED');
    expect(
      sceneStatusOf({ ok: true, raw: defaultScene() }, loaded('scene-default'), MATCH).display,
    ).toBe('DIMMED');
    expect(sceneStatusOf(input, loaded(), { ...MATCH, worldScene: 'scene-5' })).toMatchObject({
      display: 'HIDDEN',
      verification: 'MISMATCH',
    });
    // 읽기 전(IDLE)은 LOADING 으로 보인다.
    expect(sceneStatusOf(input, IDLE_LOAD, MATCH)).toMatchObject({
      phase: 'LOADING',
      display: 'HIDDEN',
    });
  });

  it('다른 Scene 의 읽기 결과는 섞지 않는다', () => {
    expect(sceneStatusOf({ ok: true, raw: committed() }, loaded('scene-3'), MATCH)).toMatchObject({
      phase: 'LOADING',
      display: 'HIDDEN',
      boundsWorld: null,
    });
  });

  it('같은 Scene 을 이미 읽었으면 주소 없음·만료 응답에도 그 자산을 쓴다', () => {
    const noUrl = {
      ok: true as const,
      raw: committed({ gltf_url: null, gltf_expires_at_ms: null }),
    };
    expect(sceneStatusOf(noUrl, loaded(), MATCH)).toMatchObject({
      phase: 'LOADED',
      display: 'FULL',
    });
    expect(sceneStatusOf(noUrl, loaded('scene-3'), MATCH).phase).toBe('NO_ASSET');
  });

  it('만료면 다시 받기를 요청하고, 실패는 이유만 둔다', () => {
    const input = { ok: true as const, raw: committed() };
    const at = (phase: 'EXPIRED' | 'FAILED', reason: string): SceneLoad => ({
      phase,
      sceneVersion: 'scene-4',
      reason,
      boundsWorld: null,
    });
    expect(sceneStatusOf(input, at('EXPIRED', 'HTTP 403'), MATCH)).toMatchObject({
      phase: 'EXPIRED',
      needsRefetch: true,
      display: 'HIDDEN',
      reason: 'HTTP 403',
    });
    expect(sceneStatusOf(input, at('FAILED', 'HTTP 404'), MATCH)).toMatchObject({
      phase: 'FAILED',
      needsRefetch: false,
      display: 'HIDDEN',
    });
  });
});
