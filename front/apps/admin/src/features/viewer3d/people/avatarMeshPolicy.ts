import type { TrackView, WorldFrame } from '@/features/viewer3d/data/spatialFrame';
import type { AvatarPoseView } from '@/features/viewer3d/people/avatarFrame';

// EXP-02 메시를 그릴지, 기본 캡슐(-44·-56 경로)로 되돌아갈지 정한다(docs/design/avatar-pose-stream.md '뷰어 구동').
// 하나라도 맞으면 되돌아간다. 위치·접지·관측 상태는 늘 Tier 0(world_state)이고 여기서 바꾸지 않는다.

/** 새 포즈가 이만큼 오지 않으면 끊김(뷰어 수신 기준, ms). 명세 20.4 초기값. */
export const AVATAR_STALL_MS = 500;
/** world_state t_latest_global − 포즈 촬영 시각이 이만큼 넘으면 늦음(ms). 명세 20.4 초기값. */
export const AVATAR_LATE_MS = 500;
/** 관절 회전 재생 지연(ms). 5 Hz 표본 간격보다 커서 거의 늘 두 표본 사이를 보간한다. */
export const AVATAR_PLAYBACK_DELAY_MS = 250;

export const AVATAR_MESH_FALLBACKS = [
  'OFF',
  'NO_POSE',
  'IDENTITY',
  'CALIBRATION',
  'STALLED',
  'HMR_LATE',
  'NO_ASSET',
  'JOINT_MISMATCH',
  'NO_PELVIS',
  'ASSET_LOADING',
  'ASSET_ERROR',
] as const;
/**
 * OFF 메시 끔 · NO_POSE 포즈 없음 · IDENTITY ID 불확실이거나 교정 차수가 다름 · CALIBRATION 보정 버전 다름 ·
 * STALLED 0.5 s 넘게 새 포즈 없음 · HMR_LATE 촬영 시각이 World 보다 500 ms 넘게 늦음 · NO_ASSET 자산 없음 ·
 * JOINT_MISMATCH 자산 관절 수와 포즈 길이가 다름 · NO_PELVIS Tier 0 pelvis 없음(정렬 불가) ·
 * ASSET_LOADING 자산 읽는 중 · ASSET_ERROR 자산 내려받기·sha256·리그 확인 실패.
 */
export type AvatarMeshFallback = (typeof AVATAR_MESH_FALLBACKS)[number];

export type AvatarAssetState =
  | { status: 'NONE' }
  | { status: 'LOADING' }
  | { status: 'READY' }
  | { status: 'ERROR'; reason: string };

export interface AvatarMeshStatus {
  worldId: string;
  /** true 면 메시로 그린다. */
  use: boolean;
  /** 되돌아간 이유. 메시로 그리면 null. */
  fallback: AvatarMeshFallback | null;
  /** World t_latest_global − 포즈 촬영 시각(ms). HMR 지연 표시용(staleness). 포즈가 없으면 null. */
  hmrLagMs: number | null;
  /** 이 브라우저 기준 마지막 포즈 수신 뒤 지난 시간(ms). 포즈가 없으면 null. */
  poseAgeMs: number | null;
  /** 자산 오류 내용(ASSET_ERROR 일 때). */
  detail: string | null;
}

export interface AvatarMeshInput {
  enabled: boolean;
  track: TrackView;
  world: WorldFrame;
  avatar: AvatarPoseView | undefined;
  asset: AvatarAssetState;
  now: number;
}

export function avatarMeshStatus({
  enabled,
  track,
  world,
  avatar,
  asset,
  now,
}: AvatarMeshInput): AvatarMeshStatus {
  const base = {
    worldId: track.worldId,
    hmrLagMs: avatar ? world.tLatestGlobal - avatar.captureTimeMs : null,
    poseAgeMs: avatar ? now - avatar.receivedAt : null,
    detail: null,
  };
  const fallback = (
    reason: AvatarMeshFallback,
    detail: string | null = null,
  ): AvatarMeshStatus => ({
    ...base,
    use: false,
    fallback: reason,
    detail,
  });
  if (!enabled) return fallback('OFF');
  if (!avatar) return fallback('NO_POSE');
  if (
    track.identityConfidence === 'IDENTITY_UNCERTAIN' ||
    avatar.identityRevision !== track.identityRevision
  ) {
    return fallback('IDENTITY');
  }
  if (avatar.calibrationVersion !== world.calibrationVersion) return fallback('CALIBRATION');
  if (base.poseAgeMs! > AVATAR_STALL_MS) return fallback('STALLED');
  if (base.hmrLagMs! > AVATAR_LATE_MS) return fallback('HMR_LATE');
  if (!avatar.asset) return fallback('NO_ASSET', avatar.assetRejected);
  if (avatar.asset.jointCount !== avatar.jointRotations.length) return fallback('JOINT_MISMATCH');
  if (!track.pelvis) return fallback('NO_PELVIS');
  if (asset.status === 'ERROR') return fallback('ASSET_ERROR', asset.reason);
  if (asset.status !== 'READY') return fallback('ASSET_LOADING');
  return { ...base, use: true, fallback: null };
}

/**
 * 이름표에 붙일 짧은 표시. 메시로 그리면 HMR 지연(0.1 s 단위, staleness)을 함께 적는다.
 * 포즈 스트림이 없는 사람(OFF·NO_POSE)은 붙이지 않는다.
 */
export function avatarMeshTag(status: AvatarMeshStatus): string | null {
  if (status.use) {
    const lag = Math.max(0, status.hmrLagMs ?? 0);
    return `메시(추정) ${(Math.round(lag / 100) / 10).toFixed(1)}s`;
  }
  switch (status.fallback) {
    case 'OFF':
    case 'NO_POSE':
      return null;
    case 'STALLED':
    case 'HMR_LATE':
      return '메시 지연';
    case 'ASSET_LOADING':
      return '메시 읽는 중';
    case 'ASSET_ERROR':
    case 'NO_ASSET':
    case 'JOINT_MISMATCH':
      return '메시 없음';
    default:
      return '메시 보류';
  }
}
