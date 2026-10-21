import type { WorldPoint } from '@/features/viewer3d/worldCoordinates';

// Spatial 이 렌더러(-44 사람, -47 구역, -185 강조)에 넘기는 View Model. 값은 계약(contracts/)을 그대로 옮기고 단위는 World(m, Z-up)다.
// Three.js 좌표는 여기 두지 않는다. 장면에 놓을 때 worldToThree 를 한 번만 부른다.
// 모르는 값은 null 로 둔다. 0·새 관측값·안전 상태로 바꾸지 않는다.

export type Validity3 = 'VALID' | 'UNCERTAIN' | 'UNAVAILABLE';
export type EventSource = 'LIVE' | 'PLANNED' | 'REPLAY' | 'WHAT_IF';

export interface MessageMeta {
  msgId: string;
  schemaVersion: string;
  sourceId: string;
  /** 송신 측 시각(UTC ms). */
  sentAt: number;
  /** 이 브라우저 시계 기준 수신 시각(ms). */
  receivedAt: number;
}

export interface SkeletonView {
  layout: 'COCO17' | 'HALPE26' | 'MHR70' | 'MHR_NATIVE';
  /** null 은 2대 미만에서 보인 관절이다. 0 으로 채우지 않는다. */
  joints: readonly (WorldPoint | null)[];
  jointVisible: readonly boolean[];
  validity: readonly Validity3[];
  jointSource: readonly ('observed' | 'inferred')[];
}

export interface TrackView {
  worldId: string;
  tLastValid: number;
  observationState: 'OBSERVED' | 'PREDICTED' | 'UNKNOWN';
  lifecycle: 'TENTATIVE' | 'CONFIRMED' | 'OCCLUDED' | 'LOST';
  identityConfidence: 'HIGH' | 'MEDIUM' | 'LOW' | 'IDENTITY_UNCERTAIN';
  identityRevision: number;
  /** 병합으로 이 ID 에 합쳐진 이전 ID. 없으면 빈 배열. */
  aliases: readonly string[];
  /** 분할로 생긴 ID 면 원래 ID. 아니면 null. */
  splitFrom: string | null;
  frameAgeMs: number | null;
  /** 구역 판정용 수평 위치(m). Z 가 없으므로 이것만으로 3D 바닥 접지를 정하지 않는다. */
  positionXy: readonly [number, number];
  positionXySigma: readonly [number, number];
  pelvis: WorldPoint | null;
  groundAnchor: WorldPoint | null;
  groundAnchorSigma: readonly [number, number, number] | null;
  /** null 은 송신 측이 싣지 않았다는 뜻이다(출처 모름). observed 로 가정하지 않는다. */
  surfaceSource: 'observed' | 'inferred' | null;
  supportSurfaceId: string | null;
  supportSurfaceValidity: 'VALID' | 'UNCERTAIN' | 'UNKNOWN';
  contactValidity: Validity3;
  zValidity: Validity3;
  headZ: number | null;
  headZSigma: number | null;
  /** null 은 송신 측이 싣지 않았다는 뜻이다(1.3.0 이하). UNAVAILABLE 로 가정하지 않는다. */
  headZValidity: Validity3 | null;
  velocity: readonly [number, number, number] | null;
  cameras: readonly string[];
  skeleton: SkeletonView | null;
}

export interface WorldFrame {
  meta: MessageMeta;
  tLatestGlobal: number;
  calibrationVersion: string;
  sceneVersion: string;
  configVersion: string | null;
  /** input_provenance.frames.source(LIVE·FILE·SIM·DATASET). 없으면 null. */
  inputSource: string | null;
  /** SIM·DATASET_GT 입력이면 true. 제품 성능·실제 관측으로 쓰지 않는다. 없으면 null. */
  isUpperBound: boolean | null;
  tracks: readonly TrackView[];
}

export interface RiskEventView {
  meta: MessageMeta;
  eventId: string;
  /** LIVE 전이 순번. 송신 측이 싣지 않으면 null. */
  eventSequence: number | null;
  ruleId: string;
  ruleVersion: number;
  source: EventSource;
  lifecycle: 'RAISED' | 'UPDATED' | 'RESOLVED';
  verification: 'VERIFIED' | 'UNVERIFIABLE';
  severity: 'CRITICAL' | 'HIGH' | 'CAUTION' | 'INFO';
  confidence: 'HIGH' | 'LOW';
  needsCheck: boolean;
  /** 1.3.0 이전 문서는 null(이유를 모름). */
  confidenceReasons: readonly string[] | null;
  resumeCount: number;
  /** 발생 당시 원본 World ID. 병합·분할 뒤에도 바꾸지 않는다. */
  worldIds: readonly string[];
  zoneId: string | null;
  metrics: Readonly<Record<string, number>>;
  rationale: string | null;
  proposedSeverity: 'CRITICAL' | 'HIGH' | 'CAUTION' | 'INFO' | null;
  raisedAt: number | null;
  updatedAt: number | null;
  resolvedAt: number | null;
  /** 운영자 종료로 해소됐으면 true(관측 해소와 구분). */
  operatorClosed: boolean;
  ackCount: number;
  escalationCount: number;
  sceneVersion: string | null;
}

export interface HealthView {
  meta: MessageMeta;
  asOf: number;
  systemHealth: 'NOMINAL' | 'DEGRADED' | 'UNSAFE_UNKNOWN';
  remoteState: 'NORMAL' | 'REMOTE_LINK_LOST' | 'CONTROL_HALTED';
  tier0Link: 'CONNECTED' | 'DISCONNECTED';
  laptopLink: 'CONNECTED' | 'DISCONNECTED';
  laptopLastHeartbeatAt: number | null;
  /** EC2 가 계산한 데이터 연령. 모르면 null. */
  dataAgeMs: number | null;
  calibrationState: 'UNCALIBRATED' | 'PROVISIONAL' | 'CALIBRATED' | 'DRIFTED';
  config: {
    publishedVersion: string | null;
    appliedVersion: string | null;
    sync: 'CURRENT' | 'PENDING' | 'UNVERIFIED' | 'STALE';
  };
  scene: { committedVersion: string | null; liveVersion: string | null; mismatch: boolean };
}

/** World 축 정렬 상자 구역. 형상 계약이 아직 없어 DEMO 형상으로만 받는다. */
export interface ZoneView {
  zoneId: string;
  name: string;
  min: WorldPoint;
  max: WorldPoint;
}

export interface ZoneSnapshot {
  status: 'NONE' | 'READY' | 'REJECTED';
  /**
   * GET /api/zones 응답 형상 계약이 아직 없다(BE -120·FE -189 협의). DEMO 형상만 받는다는 표시다.
   * 실제 형식이 정해지면 이 값과 adaptDemoZones 를 바꾼다.
   */
  contract: 'DEMO_UNCONFIRMED';
  zones: readonly ZoneView[];
  /** 형상이 틀려 뺀 구역과 이유. */
  invalid: readonly { zoneId: string | null; reason: string }[];
  receivedAt: number | null;
  reason: string | null;
}

/** 사람을 어디에 둘지(명세 13.2). 접지가 확인되면 발을 접지점에, 아니면 골반 기준에 두고 `접지 미확인`을 표시한다. */
export type TrackPlacement =
  | { basis: 'GROUND_ANCHOR'; world: WorldPoint; groundConfirmed: true }
  | { basis: 'PELVIS'; world: WorldPoint; groundConfirmed: false }
  | { basis: 'GROUND_ANCHOR_UNCERTAIN'; world: WorldPoint; groundConfirmed: false }
  /** 3D 높이를 모른다. 수평 위치만 있고 Z 를 지어내지 않는다. */
  | { basis: 'XY_ONLY'; xy: readonly [number, number]; groundConfirmed: false };

export function trackPlacement(track: TrackView): TrackPlacement {
  const { groundAnchor, pelvis } = track;
  if (groundAnchor && track.contactValidity === 'VALID' && track.zValidity === 'VALID') {
    return { basis: 'GROUND_ANCHOR', world: groundAnchor, groundConfirmed: true };
  }
  if (pelvis) return { basis: 'PELVIS', world: pelvis, groundConfirmed: false };
  if (groundAnchor)
    return { basis: 'GROUND_ANCHOR_UNCERTAIN', world: groundAnchor, groundConfirmed: false };
  return { basis: 'XY_ONLY', xy: track.positionXy, groundConfirmed: false };
}

/**
 * 이벤트가 이 Track 을 가리키는지. 이벤트의 world_ids 는 발생 당시 원본 ID 라서 지금 ID 와 병합 별칭을 함께 본다.
 * 분할로 생긴 새 ID(splitFrom)에는 원래 ID 의 이벤트를 옮기지 않는다(ID 교정 전이는 따로 온다).
 */
export function eventTargetsTrack(event: RiskEventView, track: TrackView): boolean {
  return event.worldIds.some((id) => id === track.worldId || track.aliases.includes(id));
}

export const isActiveEvent = (event: RiskEventView) => event.lifecycle !== 'RESOLVED';
