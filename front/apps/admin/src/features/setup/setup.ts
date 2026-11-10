import { ROOM } from '@/features/plan/plan';
import type {
  CameraHeightDefaultsResponse,
  CameraReceptionStatus,
  Connectivity,
  CoverageGuideItem,
  CoverageGuidesResponse,
  GuideDirection,
  GuideMagnitude,
  PairGrade,
  SetupSessionResponse,
  SetupSessionStage,
  SetupWaitReason,
  ZoneDraftCandidateKind,
  ZoneDraftItem,
} from '@/features/setup/types';
import type { ViewerBoxGhost, ViewerEditBox } from '@/features/viewer3d';
import { openingZoneZ } from '@/features/zones/zoneShape';

// 설치 · 보정 1단계(카메라 연결·각도·높이 확인, S15P31A101-448) 및 단계 상태 순수 계산.
// 걷기 보정과 실측 정물 배치 단계는 폐기되었으며(명세 v1.3 수정 54·56), 색은 위험 등급(sev-*)과 겹치지 않는다.

export type Point = readonly [number, number];

export const STEPS = ['카메라 · 높이 확인', '자동 보정', '구역'] as const;
export type Stage = 0 | 1 | 2;

/** 구역 단계(마지막 단계). 맵이 준비되면 넘어가 3D 위에서 구역을 고치고 발행한다. */
export const ZONE_STAGE = 2 satisfies Stage;

/** 지도 카드 제목. 2단계(자동 보정)는 대기 패널만 있어 지도가 없다. */
export const MAP_TITLE: Record<Exclude<Stage, 1>, string> = {
  0: '카메라 시야 · 실시간 영상',
  2: '3D 맵',
};

/** 위험 등급색(sev-critical·sev-high·sev-caution)과 겹치지 않는 세팅 화면 전용 톤. */
export type SetupTone = 'ok' | 'active' | 'delay' | 'idle';

export const DEFAULT_CAMERA_IDS = ['cam1', 'cam2', 'cam3', 'cam4'] as const;

/** 교실 4모서리 좌표(m, 인접 고리 A–B–C–D–A 순서: 왼쪽 위 → 오른쪽 위 → 오른쪽 아래 → 왼쪽 아래). */
export const CAMERA_CORNERS: readonly Point[] = [
  [0, ROOM.depth],
  [ROOM.width, ROOM.depth],
  [ROOM.width, 0],
  [0, 0],
];

/** 카메라 높이 입력 도움말(사용자 결정 2026-11-04). */
export const CAMERA_HEIGHT_HELP =
  '거치대 기둥을 늘 같은 길이로 펴고(테이프 표시 권장), 바꿨을 때만 다시 재세요';

export const MAX_CAMERA_HEIGHT_M = 20;

export const CONNECTIVITY_LABEL: Record<Connectivity, string> = {
  CONNECTED: '4대 시야 연결됨',
  DISCONNECTED: '시야 분리됨',
  UNKNOWN: '연결 확인 불가',
};

export const PAIR_GRADE_LABEL: Record<PairGrade, string> = {
  ENOUGH: '충분',
  WEAK: '약함',
  LACK: '부족',
  UNKNOWN: '미확인',
};

export const CAMERA_STATUS_LABEL: Record<CameraReceptionStatus, string> = {
  OK: '정상',
  NO_VIDEO: '영상 없음',
  STALE: '오래된 결과(STALE)',
};

export const DIRECTION_LABEL: Record<GuideDirection, string> = {
  LEFT: '왼쪽',
  RIGHT: '오른쪽',
  UP: '위',
  DOWN: '아래',
  NONE: '방향 미상',
};

/** 안내 문장용 방향(조사 포함). 받침 유무로 '으로/로'가 갈려서 방향마다 적는다. */
const DIRECTION_TOWARD: Record<Exclude<GuideDirection, 'NONE'>, string> = {
  LEFT: '왼쪽으로',
  RIGHT: '오른쪽으로',
  UP: '위로',
  DOWN: '아래로',
};

export const MAGNITUDE_LABEL: Record<GuideMagnitude, string> = {
  SMALL: '조금',
  MORE: '더',
  NONE: '',
};

export function pairTone(grade: PairGrade): SetupTone {
  if (grade === 'ENOUGH') return 'ok';
  if (grade === 'WEAK') return 'active';
  return 'idle';
}

export function cameraStatusTone(status: CameraReceptionStatus): SetupTone {
  if (status === 'OK') return 'ok';
  return 'delay';
}

export function connectivityTone(connectivity: Connectivity, stale: boolean): SetupTone {
  if (stale) return 'delay';
  if (connectivity === 'CONNECTED') return 'ok';
  if (connectivity === 'DISCONNECTED') return 'delay';
  return 'idle';
}

/** 카메라 회전 안내 항목 하나를 한국어 한 줄 문장으로 바꾼다. */
export function formatGuideItem(guide: CoverageGuideItem): string {
  const pairText = `${guide.pair[0]}–${guide.pair[1]}`;
  if (guide.direction === 'NONE' || guide.reason_code === 'NO_OVERLAP_VISIBLE') {
    return `${guide.camera_id} · ${pairText} 공통 시야 없음 — 각도를 조정해 주세요`;
  }
  const dir = DIRECTION_TOWARD[guide.direction];
  const mag = MAGNITUDE_LABEL[guide.magnitude];
  const magPart = mag ? `${mag} ` : '';
  return `${guide.camera_id} 카메라를 ${dir} ${magPart}돌려 주세요 (${pairText})`;
}

export interface CoverageCameraView {
  id: string;
  corner: Point;
  status: CameraReceptionStatus;
  statusLabel: string;
  guideText: string | null;
  angleLabel: string;
  tone: SetupTone;
}

export interface CoveragePairView {
  key: string;
  a: string;
  b: string;
  from: Point;
  to: Point;
  isDiagonal: boolean;
  grade: PairGrade;
  gradeLabel: string;
  tone: SetupTone;
}

export interface Step1CoverageView {
  connectivity: Connectivity;
  connectivityLabel: string;
  connectivityTone: SetupTone;
  stale: boolean;
  empty: boolean;
  calculationFailed: boolean;
  noVideoCameras: string[];
  staleCameras: string[];
  cameras: CoverageCameraView[];
  pairs: CoveragePairView[];
  canProceedCleanly: boolean;
  submitLabel: '다음' | '그대로 진행';
}

function defaultPairsForCameras(cameraIds: readonly string[]): { a: string; b: string }[] {
  const out: { a: string; b: string }[] = [];
  for (let i = 0; i < cameraIds.length; i++) {
    for (let j = i + 1; j < cameraIds.length; j++) {
      out.push({ a: cameraIds[i]!, b: cameraIds[j]! });
    }
  }
  return out;
}

/**
 * GET /api/cameras/coverage-guides 응답과 오류 여부에서 세팅 1단계 표시 모델을 계산한다.
 * 결과가 DISCONNECTED·UNKNOWN·STALE·NO_VIDEO·계산 실패여도 진행을 막지 않고 "그대로 진행"으로 안내한다.
 */
export function deriveStep1Coverage(
  response: CoverageGuidesResponse | undefined,
  fetchError = false,
  fallbackCameraIds: readonly string[] = DEFAULT_CAMERA_IDS,
): Step1CoverageView {
  const latest = response?.latest ?? null;
  const empty = !fetchError && (!response || response.status === 'EMPTY' || latest === null);
  const connectivity: Connectivity = latest ? latest.connectivity : 'UNKNOWN';
  const cameraIds =
    latest && latest.cameras.length > 0
      ? latest.cameras.map((c) => c.camera_id)
      : fallbackCameraIds.length > 0
        ? [...fallbackCameraIds]
        : [...DEFAULT_CAMERA_IDS];

  const cornerByCam = new Map<string, { corner: Point; index: number }>();
  cameraIds.forEach((id, idx) => {
    cornerByCam.set(id, {
      corner: CAMERA_CORNERS[idx % CAMERA_CORNERS.length]!,
      index: idx,
    });
  });

  const noVideoCameras: string[] = [];
  const staleCameras: string[] = [];
  const resultFresh =
    response?.status === 'OK' &&
    !response.stale &&
    !fetchError &&
    latest !== null &&
    latest.connectivity !== 'UNKNOWN';

  // OPTIONAL_IMPROVE 는 이미 4대가 연결된 뒤의 선택 보강이다(계약 coverage_guide). 지시로 보이면
  // "정상"인데 "돌려 주세요"가 같이 뜨고, 약한 쌍 점수가 기준선 근처라 결과마다 바뀌어 헷갈린다.
  const requiredGuides = (latest?.guides ?? []).filter((g) => g.reason_code !== 'OPTIONAL_IMPROVE');

  // 한 카메라에 안내가 둘 이상이면 모두 이어 붙인다(따로 모아 보이는 줄이 없어 이 칸이 유일한 안내다).
  const guidesByCam = new Map<string, string[]>();
  for (const g of requiredGuides) {
    guidesByCam.set(g.camera_id, [...(guidesByCam.get(g.camera_id) ?? []), formatGuideItem(g)]);
  }
  const guideByCam = new Map([...guidesByCam].map(([id, texts]) => [id, texts.join(' · ')]));

  const cameras: CoverageCameraView[] = cameraIds.map((id, idx) => {
    const item = latest?.cameras.find((c) => c.camera_id === id);
    const status: CameraReceptionStatus = item?.status ?? 'OK';
    if (latest && status === 'NO_VIDEO') noVideoCameras.push(id);
    if (latest && status === 'STALE') staleCameras.push(id);
    return {
      id,
      corner: CAMERA_CORNERS[idx % CAMERA_CORNERS.length]!,
      status,
      statusLabel: latest ? CAMERA_STATUS_LABEL[status] : '확인 중',
      guideText: guideByCam.get(id) ?? null,
      angleLabel: guideByCam.get(id) ?? (resultFresh && item?.status === 'OK' ? '각도 양호' : '—'),
      tone: latest ? cameraStatusTone(status) : 'idle',
    };
  });

  const stale = Boolean(response?.stale || response?.status === 'STALE' || staleCameras.length > 0);
  const calculationFailed =
    fetchError || (!empty && latest !== null && latest.connectivity === 'UNKNOWN');

  const rawPairs =
    latest && latest.pairs.length > 0
      ? latest.pairs
      : defaultPairsForCameras(cameraIds).map(({ a, b }) => ({
          a,
          b,
          raw_matches: null,
          inliers: null,
          inlier_ratio: null,
          grade: 'UNKNOWN' as PairGrade,
          centroid_a: null,
          centroid_b: null,
        }));

  const pairs: CoveragePairView[] = rawPairs.map((p) => {
    const fromInfo = cornerByCam.get(p.a) ?? { corner: CAMERA_CORNERS[0]!, index: 0 };
    const toInfo = cornerByCam.get(p.b) ?? { corner: CAMERA_CORNERS[1]!, index: 1 };
    const diff = Math.abs(fromInfo.index - toInfo.index);
    const isDiagonal = diff === 2;
    return {
      key: `${p.a}–${p.b}`,
      a: p.a,
      b: p.b,
      from: fromInfo.corner,
      to: toInfo.corner,
      isDiagonal,
      grade: p.grade,
      gradeLabel: PAIR_GRADE_LABEL[p.grade],
      tone: pairTone(p.grade),
    };
  });

  const canProceedCleanly =
    !fetchError && !empty && !stale && connectivity === 'CONNECTED' && noVideoCameras.length === 0;

  return {
    connectivity,
    connectivityLabel: CONNECTIVITY_LABEL[connectivity],
    connectivityTone: connectivityTone(connectivity, stale),
    stale,
    empty,
    calculationFailed,
    noVideoCameras,
    staleCameras,
    cameras,
    pairs,
    canProceedCleanly,
    submitLabel: canProceedCleanly ? '다음' : '그대로 진행',
  };
}

export interface HeightFieldView {
  cameraId: string;
  defaultHeightM: number | null;
  rawValue: string;
  /** 기본값이 없어 사용자가 반드시 입력해야 하는 카메라. */
  required: boolean;
  missing: boolean;
  invalid: boolean;
  valueM: number | null;
}

export interface HeightValidationResult {
  valid: boolean;
  cameraHeightsM: Record<string, number>;
  missingIds: string[];
  invalidIds: string[];
}

export function resolveHeightCameraIds(
  defaults: CameraHeightDefaultsResponse | undefined,
  coverage?: CoverageGuidesResponse,
): string[] {
  const defaultKeys = defaults ? Object.keys(defaults.camera_heights_m) : [];
  if (defaultKeys.length > 0) return defaultKeys;
  const coverageKeys = coverage?.latest?.cameras.map((c) => c.camera_id) ?? [];
  if (coverageKeys.length > 0) return coverageKeys;
  return [...DEFAULT_CAMERA_IDS];
}

export function deriveHeightFields(
  cameraIds: readonly string[],
  defaults: Record<string, number | null> | undefined,
  edits: Readonly<Record<string, string>>,
): HeightFieldView[] {
  return cameraIds.map((cameraId) => {
    const defaultHeightM = defaults?.[cameraId] ?? null;
    const rawValue =
      cameraId in edits
        ? edits[cameraId]!
        : defaultHeightM !== null && defaultHeightM !== undefined
          ? String(defaultHeightM)
          : '';
    const trimmed = rawValue.trim();
    const required = defaultHeightM === null || defaultHeightM === undefined;
    const missing = trimmed === '';
    const parsed = missing ? Number.NaN : Number(trimmed);
    const invalid =
      !missing && (!Number.isFinite(parsed) || parsed <= 0 || parsed > MAX_CAMERA_HEIGHT_M);
    const valueM = !missing && !invalid ? parsed : null;
    return {
      cameraId,
      defaultHeightM,
      rawValue,
      required,
      missing,
      invalid,
      valueM,
    };
  });
}

export function validateHeightFields(fields: readonly HeightFieldView[]): HeightValidationResult {
  const missingIds: string[] = [];
  const invalidIds: string[] = [];
  const cameraHeightsM: Record<string, number> = {};

  for (const f of fields) {
    if (f.missing) {
      missingIds.push(f.cameraId);
    } else if (f.invalid || f.valueM === null) {
      invalidIds.push(f.cameraId);
    } else {
      cameraHeightsM[f.cameraId] = f.valueM;
    }
  }

  return {
    valid: fields.length > 0 && missingIds.length === 0 && invalidIds.length === 0,
    cameraHeightsM,
    missingIds,
    invalidIds,
  };
}

/** 초를 「분:초」로 보인다(예: 1:05). */
export const formatClock = (secs: number): string =>
  `${Math.floor(secs / 60)}:${String(Math.floor(secs % 60)).padStart(2, '0')}`;

export interface PlanRect {
  x: number;
  y: number;
  width: number;
  depth: number;
}
export const DESKS: readonly PlanRect[] = [
  { x: 1.6, y: 7.0, width: 2.2, depth: 0.8 },
  { x: 6.2, y: 7.0, width: 2.2, depth: 0.8 },
  { x: 1.6, y: 4.8, width: 2.2, depth: 0.8 },
  { x: 6.2, y: 4.8, width: 2.2, depth: 0.8 },
  { x: 1.6, y: 2.6, width: 2.2, depth: 0.8 },
  { x: 6.2, y: 2.6, width: 2.2, depth: 0.8 },
];
export const PODIUM: PlanRect = { x: 3.5, y: 0.4, width: 3, depth: 0.6 };
export const OPENING: PlanRect = { x: 7.2, y: 8.2, width: 1.2, depth: 0.6 };
export const DOOR = { x: 4.4, width: 0.9 } as const;

export type ActionKey = 'start' | 'confirm' | 'restart';

export const ACTION_LABEL: Record<ActionKey, string> = {
  start: '다음',
  confirm: '구역으로',
  restart: '다시 보정',
};

export const SECONDARY_ACTIONS: ReadonlySet<ActionKey> = new Set(['restart']);

// ---- 2단계 이후: 대기 화면 · 미리보기 맵 전환 · AI 구역 초안(S15P31A101-453) ----

/** 대기 화면 목표 시간(약 1분 30초 = 90초, 명세 8.1). */
export const SETUP_TARGET_DURATION_SEC = 90;

export interface WaitingStageDef {
  stage: SetupSessionStage;
  label: string;
  description: string;
}

/** 세팅 대기 화면 4단계 표시 정의. */
export const WAITING_STAGES: readonly WaitingStageDef[] = [
  {
    stage: 'COLLECTING_BACKGROUND',
    label: '배경 수집',
    description: '4대 카메라에서 사람 없는 배경 프레임을 모읍니다',
  },
  {
    stage: 'INITIAL_POSE_AND_WORLD',
    label: '3D 공간·좌표계',
    description: '초기 카메라 포즈와 바닥(Z=0)·실측 스케일을 복원합니다',
  },
  {
    stage: 'JOINT_BA_AND_VERIFY',
    label: '카메라 정밀 보정',
    description: '공동 번들 조정과 카메라 높이·교차 검증을 수행합니다',
  },
  {
    stage: 'PREVIEW_AND_VLM_DRAFT',
    label: '미리보기 맵·구역 초안',
    description: '미리보기 3D 맵을 만들고 AI 위험 구역 초안을 준비합니다',
  },
] as const;

export type WaitingStepStatus = 'done' | 'current' | 'upcoming' | 'failed';

export interface WaitingStageStepView extends WaitingStageDef {
  index: number;
  status: WaitingStepStatus;
}

export interface HeightWarningItem {
  cameraId: string;
  inputHeightM: number | null;
  restoredHeightM: number | null;
  differenceCm: number | null;
  message: string;
}

export type SceneTier = 'NONE' | 'PREVIEW' | 'FINAL';

export interface SceneTierStatusView {
  tier: SceneTier;
  previewSceneVersion: string | null;
  finalSceneVersion: string | null;
  bakingTexture: boolean;
  label: string;
  description: string;
}

/** 세팅이 왜 기다리는지 화면 표시(S15P31A101-583). 경과 시간·마지막 연결은 화면이 매초 계산한다. */
export interface SetupWaitReasonView {
  code: string;
  /** 무엇 때문에 기다리는지 한 줄 */
  title: string;
  /** 관리자가 할 수 있는 일 */
  hint: string;
  /** 덧붙일 사실(지연 값·앞 시도 실패 등) */
  notes: string[];
  /** 문제라 눈에 띄게 보일지(delay) 안내만 할지(idle) */
  tone: 'delay' | 'idle';
  sinceMs: number;
  /** WORKER_NOT_POLLING: 워커 마지막 대여 요청 시각(없으면 null) */
  lastSeenMs: number | null;
  showLastSeen: boolean;
}

/** 다른 작업 유형의 쉬운 이름(없으면 유형 그대로). */
const JOB_TYPE_LABEL: Record<string, string> = {
  scene_regenerate: '3D 맵 다시 만들기',
  static_reconstruct: '정적 3D 복원',
  vlm_rationale: 'AI 위험 설명',
  vlm_zone_draft: 'AI 구역 초안',
  incident_summary: '사고 요약',
  tbm_report: 'TBM 보고서',
  bev_floorplan: '평면도',
};

const STEP_ASIDE_HINT = '사람이 카메라 앞에서 잠시 비켜 주세요.';

/** 서버 wait_reason 을 짧은 한국어 안내로 바꾼다. 없으면 null. */
export function deriveWaitReasonView(
  reason: SetupWaitReason | null | undefined,
): SetupWaitReasonView | null {
  if (!reason) return null;
  const notes: string[] = [];
  let title: string;
  let hint: string;
  let tone: SetupWaitReasonView['tone'] = 'delay';
  switch (reason.code) {
    case 'NO_WORKER':
      title = 'GPU 서버(분석 워커)가 연결돼 있지 않습니다';
      hint = 'GPU 서버를 켜고 분석 워커가 서버에 연결됐는지 확인하세요.';
      break;
    case 'WORKER_BUSY_OTHER_JOB': {
      const type = reason.other_job_type;
      const label = type ? (JOB_TYPE_LABEL[type] ?? type) : null;
      title = `GPU 서버가 다른 작업${label ? `(${label})` : ''}을 먼저 처리하고 있습니다`;
      hint = '끝나면 바로 세팅을 시작합니다. 잠시 기다려 주세요.';
      tone = 'idle';
      break;
    }
    case 'WAITING_FOR_WORKER':
      title = 'GPU 서버가 세팅 작업을 가져가기를 기다리고 있습니다';
      hint = '보통 30초 안에 시작합니다. 잠시 기다려 주세요.';
      tone = 'idle';
      break;
    case 'WORKER_PAUSED_TIER0':
      title = 'GPU 서버가 관제(Tier 0) 우선 규칙으로 세팅을 아직 받지 않았습니다';
      hint = '관제가 안정되면 저절로 시작합니다. 잠시 기다리거나 GPU 서버 상태를 확인하세요.';
      if (reason.basis === 'WORKER_RELEASE') {
        notes.push(reason.detail ? `GPU 서버가 알려 옴: ${reason.detail}` : 'GPU 서버가 알려 옴');
      } else if (reason.basis === 'TIER0_HEALTH') {
        notes.push('관제 지연을 알 수 없다는 기록으로 추정');
      }
      break;
    case 'WORKER_NOT_POLLING':
      title = 'GPU 서버가 세팅 작업을 가져가지 않고 있습니다';
      hint = '분석 워커가 켜져 있는지, GPU 서버 상태를 확인하세요.';
      break;
    case 'WORKER_STARTING':
      title = 'GPU 서버가 세팅 작업을 받아 시작을 준비하고 있습니다';
      hint = '잠시 기다려 주세요.';
      tone = 'idle';
      break;
    case 'BACKGROUND_INCOMPLETE': {
      const cams = reason.cameras.map((c) => `${c.camera_id} ${Math.round(c.collection_pct)}%`);
      title = `사람 없는 배경을 아직 다 모으지 못했습니다${cams.length ? ` (${cams.join(', ')})` : ''}`;
      hint = STEP_ASIDE_HINT;
      break;
    }
    default:
      title = `세팅이 기다리는 중입니다 (${reason.code})`;
      hint = '잠시 기다려 주세요.';
      tone = 'idle';
  }
  if (reason.tier0_lag_ms !== null) {
    notes.push(`참고: 최근 관제 지연 ${Math.round(reason.tier0_lag_ms)} ms`);
  }
  if (reason.previous_error_code) {
    notes.push(
      reason.previous_error_code === 'BACKGROUND_INCOMPLETE'
        ? `앞 시도에서 배경을 다 모으지 못해 다시 시도합니다. ${STEP_ASIDE_HINT}`
        : `앞 시도가 실패(${reason.previous_error_code})해 다시 시도합니다.`,
    );
  }
  return {
    code: reason.code,
    title,
    hint,
    notes,
    tone,
    sinceMs: reason.since_ms,
    lastSeenMs: reason.worker_last_seen_ms,
    showLastSeen: reason.code === 'WORKER_NOT_POLLING',
  };
}

/** 초를 "N분 SS초" 로 바꾼다(대기 시간 표시). */
export function formatMinSec(secs: number): string {
  const safe = Math.max(0, Math.floor(secs));
  return `${Math.floor(safe / 60)}분 ${String(safe % 60).padStart(2, '0')}초`;
}

export interface SetupWaitingView {
  sessionId: string | null;
  startedAtMs: number | null;
  updatedAtMs: number | null;
  stage: SetupSessionStage;
  stageLabel: string;
  progressPct: number;
  remainingSec: number;
  remainingClock: string;
  remainingLabel: string;
  steps: WaitingStageStepView[];
  backgroundCollection: { cameraId: string; pct: number }[];
  failed: boolean;
  failureCode: string | null;
  failureMessage: string | null;
  heightWarnings: HeightWarningItem[];
  hasHeightWarning: boolean;
  previewReady: boolean;
  sceneTier: SceneTierStatusView;
  /** 왜 기다리는지(실패·정상 진행이면 null) */
  waitReason: SetupWaitReasonView | null;
}

const STAGE_ORDER: Record<SetupSessionStage, number> = {
  COLLECTING_BACKGROUND: 0,
  INITIAL_POSE_AND_WORLD: 1,
  JOINT_BA_AND_VERIFY: 2,
  PREVIEW_AND_VLM_DRAFT: 3,
  READY_FOR_ZONES: 4,
  BAKING_TEXTURE: 4,
  COMPLETED: 4,
  FAILED: -1,
};

export const CANDIDATE_KIND_LABEL: Record<ZoneDraftCandidateKind, string> = {
  ELEVATED_WORK_SURFACE: '고소 작업면',
  OPENING: '개구부',
  FLOOR_EDGE: '바닥 단차·가장자리',
};

/** 진행률(0~100)에서 목표 1분 30초(90초) 기준 남은 초를 추정한다. */
export function estimateRemainingSeconds(progressPct: number): number {
  const clamped = Math.max(0, Math.min(100, progressPct));
  return Math.max(0, Math.round(((100 - clamped) / 100) * SETUP_TARGET_DURATION_SEC));
}

/** 남은 초를 한국어 안내 문자열(예: "약 1분 30초", "약 45초")로 바꾼다. */
export function formatRemainingKorean(secs: number): string {
  const safe = Math.max(0, Math.round(secs));
  if (safe <= 0) return '곧 완료';
  const mins = Math.floor(safe / 60);
  const rem = safe % 60;
  if (mins > 0 && rem > 0) return `약 ${mins}분 ${rem}초`;
  if (mins > 0) return `약 ${mins}분`;
  return `약 ${rem}초`;
}

/** 세션 응답에서 높이 불일치 경고가 있는 카메라 목록을 추출한다. */
export function deriveHeightWarnings(
  session: SetupSessionResponse | null | undefined,
): HeightWarningItem[] {
  if (!session) return [];
  const out: HeightWarningItem[] = [];
  const seen = new Set<string>();

  if (session.height_checks) {
    for (const [cameraId, check] of Object.entries(session.height_checks)) {
      if (!check.within_tolerance) {
        seen.add(cameraId);
        const diffPart =
          check.difference_cm !== null && check.difference_cm !== undefined
            ? ` (차이 ${check.difference_cm.toFixed(1)}cm)`
            : '';
        const msg =
          check.guidance ??
          `${cameraId} 입력 높이(${check.input_height_m.toFixed(2)}m)와 복원 높이(${
            check.restored_height_m !== null ? `${check.restored_height_m.toFixed(2)}m` : '미확인'
          })가 불일치합니다${diffPart}`;
        out.push({
          cameraId,
          inputHeightM: check.input_height_m,
          restoredHeightM: check.restored_height_m,
          differenceCm: check.difference_cm,
          message: msg,
        });
      }
    }
  }

  for (const warn of session.height_warnings ?? []) {
    const matchedCam = Object.keys(session.camera_heights_m).find((camId) => warn.includes(camId));
    const camId = matchedCam ?? 'cam1';
    if (!seen.has(camId)) {
      seen.add(camId);
      out.push({
        cameraId: camId,
        inputHeightM: session.camera_heights_m[camId] ?? null,
        restoredHeightM: null,
        differenceCm: null,
        message: warn,
      });
    }
  }

  return out;
}

/** 미리보기 Scene 이 준비되어 구역 편집으로 넘어갈 수 있는지 판정한다. */
export function isPreviewSceneReady(session: SetupSessionResponse | null | undefined): boolean {
  if (!session || session.status === 'FAILED') return false;
  if (session.preview_scene_version || session.scene_version) return true;
  const currentStage = session.stage ?? session.status;
  return (
    currentStage === 'READY_FOR_ZONES' ||
    currentStage === 'BAKING_TEXTURE' ||
    currentStage === 'COMPLETED'
  );
}

/** 현재 세션의 Scene 단계(미리보기 맵 vs 고화질 정식 Scene 자동 교체)를 계산한다. */
export function deriveSceneTierStatus(
  session: SetupSessionResponse | null | undefined,
): SceneTierStatusView {
  const previewSceneVersion = session?.preview_scene_version ?? null;
  const finalSceneVersion = session?.scene_version ?? null;
  const currentStage = session?.stage ?? session?.status;
  const bakingTexture = currentStage === 'BAKING_TEXTURE';

  if (finalSceneVersion || currentStage === 'COMPLETED') {
    return {
      tier: 'FINAL',
      previewSceneVersion,
      finalSceneVersion: finalSceneVersion ?? previewSceneVersion,
      bakingTexture: false,
      label: '정밀 3D 맵',
      description: '고화질 정식 Scene으로 자동 교체됨 · 편집 중인 구역 초안 유지',
    };
  }

  if (isPreviewSceneReady(session)) {
    return {
      tier: 'PREVIEW',
      previewSceneVersion,
      finalSceneVersion: null,
      bakingTexture,
      label: '미리보기 맵',
      description: bakingTexture
        ? '미리보기 맵 표시 중 · 뒤에서 정밀 텍스처 Scene 생성 중(완성 시 자동 교체)'
        : '미리보기 맵 표시 중 · 정식 Scene 준비 시 자동 교체됩니다',
    };
  }

  return {
    tier: 'NONE',
    previewSceneVersion: null,
    finalSceneVersion: null,
    bakingTexture: false,
    label: '맵 생성 대기',
    description: '3D 미리보기 맵을 생성하고 있습니다',
  };
}

/** 진행률로 실패 시점 단계 인덱스를 추정한다. */
function inferStageIndexFromProgress(progressPct: number): number {
  if (progressPct < 25) return 0;
  if (progressPct < 55) return 1;
  if (progressPct < 80) return 2;
  return 3;
}

/** GET /api/setup-sessions/{id} 응답에서 대기 화면 표시 모델을 계산한다. */
export function deriveWaitingView(
  session: SetupSessionResponse | null | undefined,
): SetupWaitingView {
  const rawStage: SetupSessionStage = session?.stage ?? session?.status ?? 'COLLECTING_BACKGROUND';
  const failed = session?.status === 'FAILED' || rawStage === 'FAILED';
  const progressPct = session?.progress_pct ?? 0;
  const remainingSec = estimateRemainingSeconds(progressPct);

  const activeIndex = failed
    ? session?.stage && session.stage !== 'FAILED'
      ? Math.min(3, Math.max(0, STAGE_ORDER[session.stage]))
      : inferStageIndexFromProgress(progressPct)
    : STAGE_ORDER[rawStage] >= 0
      ? STAGE_ORDER[rawStage]
      : inferStageIndexFromProgress(progressPct);

  const steps: WaitingStageStepView[] = WAITING_STAGES.map((def, idx) => {
    let status: WaitingStepStatus;
    if (failed) {
      status = idx < activeIndex ? 'done' : idx === activeIndex ? 'failed' : 'upcoming';
    } else if (idx < activeIndex) {
      status = 'done';
    } else if (idx === activeIndex) {
      status = 'current';
    } else {
      status = 'upcoming';
    }
    return {
      ...def,
      index: idx,
      status,
    };
  });

  const currentDef = WAITING_STAGES[Math.min(3, Math.max(0, activeIndex))] ?? WAITING_STAGES[0]!;
  const stageLabel = failed
    ? `${currentDef.label} 실패`
    : activeIndex >= 4
      ? '미리보기 맵 준비 완료'
      : currentDef.label;

  const backgroundCollection = Object.entries(session?.background_collection_pct ?? {}).map(
    ([cameraId, pct]) => ({
      cameraId,
      pct: Math.round(pct),
    }),
  );

  const heightWarnings = deriveHeightWarnings(session);

  return {
    sessionId: session?.setup_session_id ?? null,
    startedAtMs: session?.started_at_ms ?? null,
    updatedAtMs: session?.updated_at_ms ?? null,
    stage: rawStage,
    stageLabel,
    progressPct,
    remainingSec,
    remainingClock: formatClock(remainingSec),
    remainingLabel: formatRemainingKorean(remainingSec),
    steps,
    backgroundCollection,
    failed,
    failureCode: session?.failure_code ?? null,
    failureMessage: session?.failure_message ?? null,
    heightWarnings,
    hasHeightWarning: heightWarnings.length > 0,
    previewReady: isPreviewSceneReady(session),
    sceneTier: deriveSceneTierStatus(session),
    waitReason: failed ? null : deriveWaitReasonView(session?.wait_reason),
  };
}

/** VLM 구역 초안 다각형과 Z 범위를 구역 편집기(`useZoneEditStore`)의 ViewerEditBox 로 변환한다. */
export function boxOfDraftItem(item: ZoneDraftItem): ViewerEditBox {
  const xs = item.polygon_xy.map((p) => p[0]);
  const ys = item.polygon_xy.map((p) => p[1]);
  const rawMinX = Math.min(...xs);
  const rawMaxX = Math.max(...xs);
  const rawMinY = Math.min(...ys);
  const rawMaxY = Math.max(...ys);

  const minX = Number.isFinite(rawMinX) ? rawMinX : 1;
  const maxX = Number.isFinite(rawMaxX) && rawMaxX - minX >= 0.5 ? rawMaxX : minX + 1.2;
  const minY = Number.isFinite(rawMinY) ? rawMinY : 1;
  const maxY = Number.isFinite(rawMaxY) && rawMaxY - minY >= 0.5 ? rawMaxY : minY + 1.2;

  const [minZ, maxZ] = openingZoneZ(item.z_min_m, item.z_max_m);

  return {
    min: [minX, minY, minZ],
    max: [maxX, maxY, maxZ],
  };
}

/** 구역 단계 3D 칸 제목: 미리보기 맵·정밀 3D 맵과 그 Scene 버전. 세팅 맵이 아직이면 지금 현장 맵이다. */
export function zoneMapTitle(sceneTier: SceneTierStatusView): string {
  if (sceneTier.tier === 'NONE') return MAP_TITLE[ZONE_STAGE];
  const version = sceneTier.finalSceneVersion ?? sceneTier.previewSceneVersion;
  return version ? `${sceneTier.label} · ${version}` : sceneTier.label;
}

/**
 * 아직 수락·버리기 전인 AI 구역 초안을 3D 의 읽기 전용 점선 윤곽으로 바꾼다. 수락한 초안은 구역 편집 초안(새 구역)으로
 * 보이므로 뺀다. id 는 구역 편집 초안 윤곽과 겹치지 않게 앞머리를 붙인다.
 */
export function draftGhostsOf(
  drafts: readonly ZoneDraftItem[],
  acceptedIds: readonly string[],
  discardedIds: readonly string[],
): ViewerBoxGhost[] {
  return drafts
    .filter((d) => !acceptedIds.includes(d.candidate_id) && !discardedIds.includes(d.candidate_id))
    .map((d) => ({ id: `ai-draft:${d.candidate_id}`, box: boxOfDraftItem(d) }));
}
