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
} from '@/features/setup/types';
import type { ViewerHealthPayload } from '@simai/contracts';

// 설치 · 보정 1단계(카메라 연결·각도·높이 확인, S15P31A101-448) 및 단계 상태 순수 계산.
// 걷기 보정과 실측 정물 배치 단계는 폐기되었으며(명세 v1.3 수정 54·56), 색은 위험 등급(sev-*)과 겹치지 않는다.

export type Point = readonly [number, number];

export const STEPS = ['카메라 · 높이 확인', '보정 검증', '완료'] as const;
export type Stage = 0 | 1 | 2 | 3;

export const MAP_TITLE: readonly string[] = [
  '카메라 쌍 시야 연결',
  '검증 지점',
  'BEV 평면도 · cal-8',
  'BEV 평면도 · cal-8',
];

export type CalibrationState = ViewerHealthPayload['calibration_state'];

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
  const dir = DIRECTION_LABEL[guide.direction];
  const mag = MAGNITUDE_LABEL[guide.magnitude];
  const magPart = mag ? `${mag} ` : '';
  return `${guide.camera_id} 카메라를 ${dir}으로 ${magPart}돌려 주세요 (${pairText})`;
}

export interface CoverageCameraView {
  id: string;
  corner: Point;
  status: CameraReceptionStatus;
  statusLabel: string;
  guideText: string | null;
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
  inliers: number | null;
  inlierRatio: number | null;
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
  directionLine: string;
  statusNotices: string[];
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

  const guideByCam = new Map<string, string>();
  for (const g of latest?.guides ?? []) {
    if (!guideByCam.has(g.camera_id)) {
      guideByCam.set(g.camera_id, formatGuideItem(g));
    }
  }

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
      inliers: p.inliers,
      inlierRatio: p.inlier_ratio,
      tone: pairTone(p.grade),
    };
  });

  let directionLine: string;
  if (latest && latest.guides.length > 0) {
    directionLine = latest.guides.map(formatGuideItem).join(' · ');
  } else if (fetchError) {
    directionLine = '카메라 각도 안내 계산 실패 · 각도를 직접 확인하거나 그대로 진행할 수 있습니다';
  } else if (empty) {
    directionLine = '카메라 각도 안내 대기 중 · 그대로 진행할 수 있습니다';
  } else if (connectivity === 'CONNECTED') {
    directionLine = '4대 카메라 시야가 하나로 연결되었습니다';
  } else if (connectivity === 'DISCONNECTED') {
    directionLine =
      '겹치는 시야가 부족한 카메라 쌍이 있습니다 · 각도를 조정하거나 그대로 진행하세요';
  } else {
    directionLine = '카메라 시야 연결을 계산할 수 없습니다 · 그대로 진행할 수 있습니다';
  }

  const statusNotices: string[] = [];
  if (stale) {
    statusNotices.push('오래된 결과(STALE) · 최근 수신된 각도 안내입니다');
  }
  if (noVideoCameras.length > 0) {
    statusNotices.push(`영상 없음: ${noVideoCameras.join(', ')}`);
  }
  if (calculationFailed) {
    statusNotices.push('계산 실패 · 연결 상태를 확인할 수 없습니다');
  }

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
    directionLine,
    statusNotices,
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

// 2단계 이후(보정 검증·완료) 표시용 상수(-453 에서 대기 화면·구역 편집 이동으로 교체 예정).
export const HEAD_ERR_MAX_CM = 15;
export const headErrorPass = (cm: number): boolean => cm <= HEAD_ERR_MAX_CM;

export const CHECKS: readonly { at: Point; errorCm: number }[] = [
  { at: [2.4, 2.6], errorCm: 4 },
  { at: [5.2, 5.4], errorCm: 7 },
  { at: [8.1, 7.6], errorCm: 11 },
  { at: [8.0, 2.4], errorCm: 6 },
  { at: [2.6, 7.8], errorCm: 9 },
];

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
export const PAIR_MISMATCH = '최대 6cm';
export const RESULT = { reprojection: '1.7 · 3.9px', floorDiff: '1.8cm' } as const;

export type ActionKey = 'start' | 'confirm' | 'restart' | 'zones';

export const ACTION_LABEL: Record<ActionKey, string> = {
  start: '다음',
  confirm: '보정 확정',
  restart: '다시 보정',
  zones: '구역 편집',
};

export const SECONDARY_ACTIONS: ReadonlySet<ActionKey> = new Set(['restart']);
