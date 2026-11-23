import { createSpatialDataStore } from '@/features/viewer3d/data/spatialData.store';
import {
  APPEARANCE_MEMORY_LIMIT,
  appearanceMemoryOf,
  chooseAvatarAppearance,
  ownAppearanceOf,
  rememberAppearance,
  type AvatarAppearanceChoice,
} from '@/features/viewer3d/people/avatarAppearanceCarry';
import type { AvatarPoseView } from '@/features/viewer3d/people/avatarFrame';
import { describe, expect, it } from 'vitest';

// S15P31A101-851: 워치 자리 이어받기로 새 ID 에 옛 ID 외형을 입히는 선택 규칙(표시 전용).

function avatar(worldId: string, observed: number | null): AvatarPoseView {
  return {
    msgId: 'm',
    worldId,
    identityRevision: 0,
    captureTimeMs: 0,
    relayReceivedAtMs: 0,
    receivedAt: 0,
    calibrationVersion: 'cal',
    appearanceVersion: 3,
    rootRotation: [0, 0, 0],
    jointRotations: [],
    asset: {
      streamEpoch: 7,
      identityRevision: 0,
      meshUrl: `/api/avatars/${worldId}/mesh.glb`,
      meshSha256: 'sha256:00',
      meshFormat: 'GLB_SKINNED',
      jointCount: 1,
      jointParentsSha256: 'sha256:00',
      pelvisJointIndex: 1,
      textureUrl: observed === null ? null : `/api/avatars/${worldId}/albedo.png`,
      surfaceSourceUrl: observed === null ? null : `/api/avatars/${worldId}/surface_source.png`,
      surfaceRatios: {
        OBSERVED: observed ?? 0,
        INTERPOLATED: 0,
        UNOBSERVED: 1 - (observed ?? 0),
      },
      facePolicy: 'OBSERVED',
      faceRatios: { OBSERVED: 0, UNOBSERVED: 1 },
    },
    assetRejected: null,
  };
}

const avatars = (...list: AvatarPoseView[]) => new Map(list.map((a) => [a.worldId, a]));

describe('chooseAvatarAppearance(-851)', () => {
  it('짝이 없으면 자기 외형(없으면 null)이다', () => {
    const map = avatars(avatar('w-new', null), avatar('w-old', 0.9));
    const none = new Map<string, AvatarAppearanceChoice>();
    expect(
      chooseAvatarAppearance({
        worldId: 'w-new',
        avatars: map,
        donorId: null,
        donorVisible: false,
        memory: none,
      }),
    ).toBeNull();
    expect(
      chooseAvatarAppearance({
        worldId: 'w-old',
        avatars: map,
        donorId: null,
        donorVisible: false,
        memory: none,
      })?.carriedFrom,
    ).toBeNull();
  });

  it('자기 외형이 없으면 아직 남은 옛 ID 포즈 자산의 외형을 이어받는다', () => {
    const choice = chooseAvatarAppearance({
      worldId: 'w-new',
      avatars: avatars(avatar('w-new', null), avatar('w-old', 0.9)),
      donorId: 'w-old',
      donorVisible: false,
      memory: new Map(),
    });
    expect(choice?.carriedFrom).toBe('w-old');
    expect(choice?.request.worldId).toBe('w-old');
    expect(choice?.request.textureUrl).toBe('/api/avatars/w-old/albedo.png');
  });

  it('옛 ID 포즈가 지워져도(30초) 기억해 둔 외형을 쓰고, 기억에는 이어받은 외형도 남아 연달아 옮겨 가도 처음 외형이 이어진다', () => {
    const memory = new Map<string, AvatarAppearanceChoice>();
    rememberAppearance(memory, 'w-a', ownAppearanceOf('w-a', avatar('w-a', 0.8)));
    const b = chooseAvatarAppearance({
      worldId: 'w-b',
      avatars: avatars(avatar('w-b', null)),
      donorId: 'w-a',
      donorVisible: false,
      memory,
    });
    expect(b?.request.worldId).toBe('w-a');
    rememberAppearance(memory, 'w-b', b);
    const c = chooseAvatarAppearance({
      worldId: 'w-c',
      avatars: avatars(avatar('w-c', null)),
      donorId: 'w-b',
      donorVisible: false,
      memory,
    });
    expect(c?.carriedFrom).toBe('w-b');
    expect(c?.request.worldId).toBe('w-a');
  });

  it('자기 외형이 옛 외형만큼 봤으면 자기 외형으로 넘어가고, 덜 봤으면 옛 외형을 계속 입는다', () => {
    const input = (own: number) => ({
      worldId: 'w-new',
      avatars: avatars(avatar('w-new', own), avatar('w-old', 0.6)),
      donorId: 'w-old',
      donorVisible: false,
      memory: new Map<string, AvatarAppearanceChoice>(),
    });
    expect(chooseAvatarAppearance(input(0.1))?.carriedFrom).toBe('w-old');
    expect(chooseAvatarAppearance(input(0.6))?.carriedFrom).toBeNull();
    expect(chooseAvatarAppearance(input(0.7))?.request.worldId).toBe('w-new');
  });

  it('주는 ID 가 지금 다시 보이거나 주는 쪽 외형을 모르면 이어받지 않는다', () => {
    const map = avatars(avatar('w-new', null), avatar('w-old', 0.9));
    expect(
      chooseAvatarAppearance({
        worldId: 'w-new',
        avatars: map,
        donorId: 'w-old',
        donorVisible: true,
        memory: new Map(),
      }),
    ).toBeNull();
    expect(
      chooseAvatarAppearance({
        worldId: 'w-new',
        avatars: map,
        donorId: 'w-unknown',
        donorVisible: false,
        memory: new Map(),
      }),
    ).toBeNull();
  });
});

describe('외형 기억(-851)', () => {
  it('null 은 기억을 지우지 않고, 상한을 넘으면 가장 오래 갱신되지 않은 사람부터 잊는다', () => {
    const memory = new Map<string, AvatarAppearanceChoice>();
    const own = ownAppearanceOf('w-0', avatar('w-0', 0.5))!;
    rememberAppearance(memory, 'w-0', own);
    rememberAppearance(memory, 'w-0', null);
    expect(memory.get('w-0')).toBe(own);
    for (let i = 1; i <= APPEARANCE_MEMORY_LIMIT; i++) {
      rememberAppearance(memory, `w-${i}`, ownAppearanceOf(`w-${i}`, avatar(`w-${i}`, 0.5)));
    }
    expect(memory.size).toBe(APPEARANCE_MEMORY_LIMIT);
    expect(memory.has('w-0')).toBe(false);
    expect(memory.has(`w-${APPEARANCE_MEMORY_LIMIT}`)).toBe(true);
  });

  it('데이터 저장소마다 따로이고 Source 가 바뀌면 비운다', () => {
    const store = createSpatialDataStore();
    const other = createSpatialDataStore();
    const memory = appearanceMemoryOf(store);
    rememberAppearance(memory, 'w-1', ownAppearanceOf('w-1', avatar('w-1', 0.5)));
    expect(appearanceMemoryOf(store).has('w-1')).toBe(true);
    expect(appearanceMemoryOf(other).has('w-1')).toBe(false);
    store.getState().reset('replay:1');
    expect(appearanceMemoryOf(store).has('w-1')).toBe(false);
  });
});
