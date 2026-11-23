import type {
  SpatialDataState,
  SpatialDataStore,
} from '@/features/viewer3d/data/spatialData.store';
import {
  avatarAppearanceRequestOf,
  type AvatarAppearanceRequest,
  type AvatarFacePolicy,
} from '@/features/viewer3d/people/avatarAppearance';
import type { AvatarPoseView } from '@/features/viewer3d/people/avatarFrame';

// 워치 배정이 옮겨 간 새 World ID 에 옛 World ID 의 아바타 외형을 입힌다(S15P31A101-851,
// docs/design/S15P31A101-851-carry-avatar-on-rebind.md). 표시 전용이다:
// - 위치·골격·관측 출처·메시 형상·ID 는 그대로 새 ID 것이다(Tier 0 정본, AGENTS 규칙 1·7). 바꾸는 것은 메시에 입히는 텍스처뿐이다.
// - 짝(받는 ID → 주는 ID)은 호스트가 워치 배정의 자리 이어받기(-793, handoff_from)로 정해 넘긴다. 외형 색으로 신원을 정하지 않는다(규칙 8).
// - 계약은 바꾸지 않는다. 이어받은 외형이라는 표시는 이 모듈의 `carriedFrom`(뷰어 내부)과 콘솔 기록뿐이다.

/** 한 사람에게 지금 입히는 외형(자기 것 또는 이어받은 것). */
export interface AvatarAppearanceChoice {
  request: AvatarAppearanceRequest;
  facePolicy: AvatarFacePolicy;
  /** 그 외형의 관측 비율(surface_ratios.OBSERVED). 자기 외형과 이어받은 외형 중 더 많이 본 쪽을 고를 때 쓴다. */
  observed: number;
  /** 다른 World ID 의 외형을 빌려 입혔으면 그 ID(바로 앞 주는 쪽), 자기 외형이면 null. 뷰어 내부 표시다. */
  carriedFrom: string | null;
}

/** 기억해 두는 사람 수 상한. 넘으면 가장 오래 갱신되지 않은 사람부터 잊는다. */
export const APPEARANCE_MEMORY_LIMIT = 64;

interface AppearanceMemory {
  sourceId: string | null;
  byWorld: Map<string, AvatarAppearanceChoice>;
}

const memories = new WeakMap<SpatialDataStore, AppearanceMemory>();

/**
 * 사람별 마지막 외형 기억(데이터 저장소마다 하나, Source 가 바뀌면 비운다).
 * 주는 쪽은 이미 화면에서 사라졌고 그 포즈도 30초 뒤 지워지므로(AVATAR_FORGET_MS) 받는 쪽이 읽을 외형을 여기 남긴다.
 * 이어받은 외형도 그대로 남겨 A → B → C 로 연달아 옮겨 가도 처음 외형이 이어진다.
 */
export function appearanceMemoryOf(store: SpatialDataStore): Map<string, AvatarAppearanceChoice> {
  const sourceId = store.getState().sourceId;
  let memory = memories.get(store);
  if (!memory || memory.sourceId !== sourceId) {
    memory = { sourceId, byWorld: new Map() };
    memories.set(store, memory);
  }
  return memory.byWorld;
}

/** 지금 입힌 외형을 기억한다. null(외형 없음)은 기억을 지우지 않는다: 사라지기 직전의 마지막 외형을 받는 쪽이 쓴다. */
export function rememberAppearance(
  memory: Map<string, AvatarAppearanceChoice>,
  worldId: string,
  choice: AvatarAppearanceChoice | null,
): void {
  if (!choice) return;
  memory.delete(worldId);
  memory.set(worldId, choice);
  while (memory.size > APPEARANCE_MEMORY_LIMIT) memory.delete(memory.keys().next().value!);
}

/** 그 사람 자신의 외형(avatar_frame 자산의 albedo·출처 지도). 없으면 null. */
export function ownAppearanceOf(
  worldId: string,
  avatar: AvatarPoseView | undefined,
): AvatarAppearanceChoice | null {
  const request = avatarAppearanceRequestOf(worldId, avatar);
  if (!request || !avatar?.asset) return null;
  return {
    request,
    facePolicy: avatar.asset.facePolicy,
    observed: avatar.asset.surfaceRatios.OBSERVED,
    carriedFrom: null,
  };
}

export interface ChooseAvatarAppearanceInput {
  worldId: string;
  avatars: SpatialDataState['avatars'];
  /** 호스트가 넘긴 주는 World ID(-851). 없으면 null. */
  donorId: string | null;
  /** 주는 ID 가 지금 world_state 에 다시 보이는가(LOST 제외). 보이면 다른 사람으로 보고 이어받지 않는다. */
  donorVisible: boolean;
  memory: ReadonlyMap<string, AvatarAppearanceChoice>;
}

/**
 * 입힐 외형을 고른다.
 * - 주는 ID 가 없거나 지금 다시 보이면 자기 외형.
 * - 주는 쪽 외형(기억 → 아직 남은 포즈 자산 순)이 없으면 자기 외형.
 * - 자기 외형이 없거나 주는 쪽보다 덜 봤으면(OBSERVED 비율이 작으면) 주는 쪽 외형, 같거나 많이 봤으면 자기 외형.
 */
export function chooseAvatarAppearance({
  worldId,
  avatars,
  donorId,
  donorVisible,
  memory,
}: ChooseAvatarAppearanceInput): AvatarAppearanceChoice | null {
  const own = ownAppearanceOf(worldId, avatars.get(worldId));
  if (donorId === null || donorId === worldId || donorVisible) return own;
  const donor = memory.get(donorId) ?? ownAppearanceOf(donorId, avatars.get(donorId));
  if (!donor) return own;
  if (own && own.observed >= donor.observed) return own;
  return { ...donor, carriedFrom: donorId };
}
