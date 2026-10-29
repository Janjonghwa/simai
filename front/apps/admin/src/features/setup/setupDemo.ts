import { ROOM } from '@/features/plan/plan';
import { coveredCells, coverPercent, partialPath, type Point } from '@/features/setup/setup';
import type { ViewerHealthPayload } from '@simai/contracts';

// 설치 · 보정(명세 18.1~18.6: 실측 정물 → 보정 걷기 → 보정 검증 → CALIBRATED) 데모 각본이다.
// 보정 API 가 아직 없어 가짜 값과 시간 진행만 둔다. 좌표는 World(m, +X 오른쪽, +Y 앞)다.
// 진행은 순수 함수(setupReducer·deriveSetup)라 타이머 없이 시험한다. 화면은 TICK_MS 마다 tick 만 보낸다.

export const STEPS = ['실측 정물', '보정 걷기', '보정 검증', '완료'] as const;
export type Stage = 0 | 1 | 2 | 3;

export const MAP_TITLE = [
  '실측 정물 위치',
  '걸은 경로 · 관측 범위',
  '검증 지점',
  'BEV 평면도 · cal-8',
];

export type CalibrationState = ViewerHealthPayload['calibration_state'];

const STAGE_STATE: readonly { state: CalibrationState; version: string }[] = [
  { state: 'UNCALIBRATED', version: '보정 없음' },
  { state: 'PROVISIONAL', version: '후보 cal-8' },
  { state: 'PROVISIONAL', version: '후보 cal-8' },
  { state: 'CALIBRATED', version: 'cal-8 · 10/20 14:32' },
];

/** 카메라 설치 높이(m). */
const CAMERA_HEIGHT_M = 2.7;
/** cam3 을 위로 올린 뒤의 각도(°). */
const CAM3_FIXED_TILT = 27;
const CAM3_TILT = 34;

/** 교실 모서리 카메라. 아래로 숙인 각도(°)와 보정 걷기 동안 모은 관절 수, 재투영 오차다. */
export interface DemoCamera {
  id: string;
  corner: Point;
  tilt: number;
  keypoints: number;
  reprojection: string;
}
export const CAMERAS: readonly DemoCamera[] = [
  { id: 'cam1', corner: [0, ROOM.depth], tilt: 26, keypoints: 1480, reprojection: '1.6px' },
  {
    id: 'cam2',
    corner: [ROOM.width, ROOM.depth],
    tilt: 27,
    keypoints: 1320,
    reprojection: '1.9px',
  },
  { id: 'cam3', corner: [0, 0], tilt: CAM3_TILT, keypoints: 1190, reprojection: '1.5px' },
  { id: 'cam4', corner: [ROOM.width, 0], tilt: 26, keypoints: 1410, reprojection: '2.0px' },
];

/** 높이를 잰 정물. 라바콘 C 는 cam3 을 고치기 전에는 카메라 1대에서만 보인다. */
export interface DemoProp {
  name: string;
  height: string;
  at: Point;
  oneViewUntilFixed: boolean;
}
export const PROPS: readonly DemoProp[] = [
  { name: '라바콘 A', height: '0.70m', at: [2.2, 3.4], oneViewUntilFixed: false },
  { name: '안전봉 B', height: '1.10m', at: [7.4, 6.8], oneViewUntilFixed: false },
  { name: '라바콘 C', height: '0.70m', at: [6.2, 1.8], oneViewUntilFixed: true },
];

/** 보정 걷기 경로. 처음 경로로는 왼쪽 위 구석이 덜 보여 이어 걷는다. */
export const MAIN_WALK: readonly Point[] = [
  [4.85, 9.3],
  [8.6, 8.7],
  [8.8, 5.4],
  [6.2, 4.8],
  [8.6, 1.3],
  [5, 1.2],
  [1.4, 1.2],
  [1.5, 3.8],
  [4.4, 4.6],
  [3.8, 6.6],
];
export const EXTRA_WALK: readonly Point[] = [
  [3.8, 6.6],
  [1.3, 7.0],
  [1.2, 8.8],
  [3.2, 8.9],
];

/** 검증 지점과 그 자리에서 잰 머리 높이 오차(cm). */
export const CHECKS: readonly { at: Point; errorCm: number }[] = [
  { at: [2.4, 2.6], errorCm: 4 },
  { at: [5.2, 5.4], errorCm: 7 },
  { at: [8.1, 7.6], errorCm: 11 },
  { at: [8.0, 2.4], errorCm: 6 },
  { at: [2.6, 7.8], errorCm: 9 },
];

/** 보정 뒤 BEV 평면도의 정적 객체. 사각형은 왼쪽 아래 모서리(m)와 크기(m)다. */
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
/** 출입문(뒤쪽 벽 위 X 구간, m). */
export const DOOR = { x: 4.4, width: 0.9 } as const;

/** 보정 걷는 사람 수, 검증의 카메라 쌍 불일치, 보정 결과 값. */
export const WALKERS = 1;
export const PAIR_MISMATCH = '최대 6cm';
export const RESULT = { reprojection: '1.7 · 3.9px', floorDiff: '1.8cm' } as const;

/** 보정 걷기 통로 관측 기준(%). */
export const COVER_MIN_PCT = 80;
/** 보정 검증 머리 높이 오차 기준(cm). */
export const HEAD_ERR_MAX_CM = 15;

/** 화면이 tick 을 보내는 간격(ms). */
export const TICK_MS = 100;
/** 처음 경로·이어 걷기를 다 걷는 데 드는 화면 시간(ms). */
export const WALK_MAIN_MS = 8000;
export const WALK_EXTRA_MS = 3400;
/** 검증 지점 하나를 재는 데 드는 화면 시간(ms). */
export const CHECK_MS = 1600;
/** 처음 경로·이어 걷기의 현장 시간(초). 경과 시각과 진행 막대는 이 시간으로 보인다. */
const WALK_MAIN_SECS = 80;
const WALK_EXTRA_SECS = 30;
const WALK_TOTAL_SECS = WALK_MAIN_SECS + WALK_EXTRA_SECS;
const CHECK_TOTAL_MS = CHECK_MS * CHECKS.length;

export interface SetupState {
  stage: Stage;
  /** cam3 각도를 고치고 다시 확인했다. */
  fixed: boolean;
  /** 덜 보인 구석을 이어 걷는다. */
  more: boolean;
  walkMs: number;
  moreMs: number;
  checkMs: number;
}

export type SetupAction =
  | { type: 'go'; stage: Stage }
  | { type: 'recheck' }
  | { type: 'walkMore' }
  | { type: 'tick'; ms: number };

export const initialSetup: SetupState = {
  stage: 0,
  fixed: false,
  more: false,
  walkMs: 0,
  moreMs: 0,
  checkMs: 0,
};

/** 시간이 흘러야 바뀌는 단계다(걷는 중, 재는 중). 아니면 tick 을 보내지 않는다. */
export function isRunning(s: SetupState): boolean {
  if (s.stage === 1) return s.walkMs < WALK_MAIN_MS || (s.more && s.moreMs < WALK_EXTRA_MS);
  if (s.stage === 2) return s.checkMs < CHECK_TOTAL_MS;
  return false;
}

export function setupReducer(s: SetupState, action: SetupAction): SetupState {
  switch (action.type) {
    case 'go': {
      // 단계에 다시 들어가면 그 단계를 처음부터 한다.
      const next = { ...s, stage: action.stage };
      if (action.stage === 0) next.fixed = false;
      if (action.stage === 1) Object.assign(next, { walkMs: 0, moreMs: 0, more: false });
      if (action.stage === 2) next.checkMs = 0;
      return next;
    }
    case 'recheck':
      return s.stage === 0 ? { ...s, fixed: true } : s;
    case 'walkMore':
      return s.stage === 1 && s.walkMs >= WALK_MAIN_MS && !s.more ? { ...s, more: true } : s;
    case 'tick': {
      if (!isRunning(s) || action.ms <= 0) return s;
      if (s.stage === 2) return { ...s, checkMs: Math.min(CHECK_TOTAL_MS, s.checkMs + action.ms) };
      if (s.walkMs < WALK_MAIN_MS)
        return { ...s, walkMs: Math.min(WALK_MAIN_MS, s.walkMs + action.ms) };
      return { ...s, moreMs: Math.min(WALK_EXTRA_MS, s.moreMs + action.ms) };
    }
  }
}

/** 머리 높이 오차가 기준 안이다. */
export const headErrorPass = (cm: number): boolean => cm <= HEAD_ERR_MAX_CM;

export type ActionKey =
  'recheck' | 'start' | 'walkMore' | 'verify' | 'rewalk' | 'confirm' | 'restart' | 'zones';

export const ACTION_LABEL: Record<ActionKey, string> = {
  recheck: '다시 확인',
  start: '보정 시작',
  walkMore: '이어 걷기',
  verify: '검증 시작',
  rewalk: '다시 걷기',
  confirm: '보정 확정',
  restart: '다시 보정',
  zones: '구역 편집',
};

/** 주 버튼(오른쪽 끝)이 아닌 버튼. */
export const SECONDARY_ACTIONS: ReadonlySet<ActionKey> = new Set(['recheck', 'rewalk', 'restart']);

export type Tone = 'ok' | 'warn' | 'active' | 'idle';

export interface Row {
  key: string;
  tone: Tone;
  label: string;
  value: string;
}

export interface SetupView {
  stage: Stage;
  calibration: CalibrationState;
  version: string;
  guide: { tone: 'warn' | 'info'; text: string } | null;
  actions: ActionKey[];
  mainPath: Point[];
  extraPath: Point[];
  cells: Point[];
  /** 덜 보인 구석이 남아 이어 걸을 차례다. */
  gap: boolean;
  /** 보정 걷기를 마쳤다. */
  walked: boolean;
  /** 현장 경과(초)와 보정 걷기 진행(0~1). */
  walkSecs: number;
  walkProgress: number;
  coverPct: number;
  coverOk: boolean;
  /** 잰 검증 지점 수. */
  checked: number;
  /** 잰 지점 가운데 가장 큰 머리 높이 오차(cm). 잰 지점이 없으면 null. */
  maxErrorCm: number | null;
  headPass: boolean;
  /** 정물마다 2대 이상에서 보였는지. */
  propsSeen: boolean[];
  cameras: Row[];
}

const count = new Intl.NumberFormat('ko-KR');

function cameraRow(cam: DemoCamera, s: SetupState, walkProgress: number): Omit<Row, 'key'> {
  if (s.stage === 1)
    return {
      tone: 'active',
      label: '관절 관측',
      value: count.format(Math.round(cam.keypoints * walkProgress)),
    };
  if (s.stage >= 2) return { tone: 'ok', label: '재투영 오차', value: cam.reprojection };
  if (cam.id === 'cam3' && !s.fixed)
    return { tone: 'warn', label: '아래를 너무 봄', value: `${CAMERA_HEIGHT_M}m · ${cam.tilt}°` };
  const tilt = cam.id === 'cam3' ? CAM3_FIXED_TILT : cam.tilt;
  return { tone: 'ok', label: '각도 양호', value: `${CAMERA_HEIGHT_M}m · ${tilt}°` };
}

/** 상태에서 화면에 보일 값을 모두 계산한다. */
export function deriveSetup(s: SetupState): SetupView {
  const { stage } = s;
  const walking = stage === 1;
  const main = Math.min(1, s.walkMs / WALK_MAIN_MS);
  const extra = s.more ? Math.min(1, s.moreMs / WALK_EXTRA_MS) : 0;
  const gap = walking && main >= 1 && !s.more;
  const walked = walking && s.more && extra >= 1;
  const walkSecs = main * WALK_MAIN_SECS + extra * WALK_EXTRA_SECS;
  const walkProgress = Math.min(1, walkSecs / WALK_TOTAL_SECS);

  const mainPath = walking ? partialPath(MAIN_WALK, main) : [...MAIN_WALK];
  const extraPath = walking ? (s.more ? partialPath(EXTRA_WALK, extra) : []) : [...EXTRA_WALK];
  const cells = stage === 1 || stage === 2 ? coveredCells([mainPath, extraPath]) : [];
  const coverPct = coverPercent(
    stage === 3 ? coveredCells([MAIN_WALK, EXTRA_WALK]).length : cells.length,
  );

  const checked =
    stage === 3
      ? CHECKS.length
      : stage === 2
        ? Math.min(CHECKS.length, Math.floor(s.checkMs / CHECK_MS))
        : 0;
  const errors = CHECKS.slice(0, checked).map((c) => c.errorCm);
  const maxErrorCm = errors.length ? Math.max(...errors) : null;
  const headPass = maxErrorCm !== null && headErrorPass(maxErrorCm);

  let guide: SetupView['guide'] = null;
  if (stage === 0 && !s.fixed)
    guide = {
      tone: 'warn',
      text: `cam3 이 아래를 너무 봅니다 (${CAM3_TILT}°) · 25~28° 가 되도록 위로 올려 주세요`,
    };
  else if (gap)
    guide = { tone: 'warn', text: '왼쪽 위 구석이 덜 보였습니다 · 점선을 따라 더 걸어 주세요' };
  else if (walking && !walked)
    guide = { tone: 'info', text: '키 174cm 팀원이 교실 전체를 천천히 걷는 중' };
  else if (stage === 2 && checked < CHECKS.length)
    guide = { tone: 'info', text: `키 174cm 팀원이 ${checked + 1}번 지점에 서 주세요` };

  const actions: ActionKey[] = [];
  if (stage === 0) {
    if (!s.fixed) actions.push('recheck');
    actions.push('start');
  } else if (stage === 1) {
    if (gap) actions.push('walkMore');
    if (walked) actions.push('verify');
  } else if (stage === 2) {
    actions.push('rewalk');
    if (checked === CHECKS.length && headPass) actions.push('confirm');
  } else {
    actions.push('restart', 'zones');
  }

  return {
    stage,
    calibration: STAGE_STATE[stage]!.state,
    version: STAGE_STATE[stage]!.version,
    guide,
    actions,
    mainPath,
    extraPath,
    cells,
    gap,
    walked,
    walkSecs,
    walkProgress,
    coverPct,
    coverOk: coverPct >= COVER_MIN_PCT,
    checked,
    maxErrorCm,
    headPass,
    propsSeen: PROPS.map((p) => !p.oneViewUntilFixed || s.fixed || stage > 0),
    cameras: CAMERAS.map((cam) => ({ key: cam.id, ...cameraRow(cam, s, walkProgress) })),
  };
}
