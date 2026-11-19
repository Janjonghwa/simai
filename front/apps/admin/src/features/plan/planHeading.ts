import { trackIds, trackLook } from '@/features/plan/plan';
import type { WorldTrack } from '@simai/contracts';

// 2D 평면 사람 표식의 진행 방향(-800). 화면 표시용이고 위치·위험 판정에 쓰지 않는다(Tier 0 값을 바꾸지 않는다).
// 방향은 World yaw(rad, +X 에서 +Y 쪽으로 반시계)로 들고, 화면 방향은 그릴 때 plan.ts 의 planDirection 으로 바꾼다.
// 틀린 방향을 보이느니 보이지 않는 편이 낫다(Lead 지시). 고르는 순서:
// ① 움직일 때: world_state 수평 속력이 HEADING_SPEED_MIN 이상으로 MOVE_CONFIRM_MS 이어지면 그 이동 방향
// ② 멈췄을 때: 관측한 두 어깨로 본 앞쪽이 두 엉덩이로 본 앞쪽과 BODY_AGREE_RAD 안에서 맞을 때만 어깨 방향
// ③ 둘 다 아니면 마지막 방향을 HEADING_HOLD_MS 동안 ④ 그 뒤에는 방향 없음(점만).
// 지금 방향과 FLIP_RAD 넘게 반대인 새 방향은 FLIP_CONFIRM_MS 동안 이어져야 바꾼다(뒤에서 찍혀 좌우가 바뀐 관절로 뒤집히지 않게).

/** 이 수평 속력(m/s) 이상이 이어질 때만 움직인다고 본다. 서 있을 때의 위치 흔들림이 방향으로 보이지 않게 한다. */
export const HEADING_SPEED_MIN = 0.3;
/** 속력이 기준 이상으로 이만큼(ms) 이어져야 이동 방향을 쓴다(10Hz 에서 세 표본). */
export const MOVE_CONFIRM_MS = 200;
/** 어깨·엉덩이 사이 수평 거리가 이보다 짧으면(m) 앞쪽을 정할 수 없다고 본다(삼각측량이 겹친 관절). */
export const SHOULDER_MIN_M = 0.12;
export const HIP_MIN_M = 0.1;
/** 어깨로 본 앞쪽과 엉덩이로 본 앞쪽이 이 각(rad) 안에서 맞아야 어깨 방향을 쓴다. */
export const BODY_AGREE_RAD = Math.PI / 4;
/** 지금 방향과 이보다 크게(rad) 다른 새 방향은 뒤집힘으로 보고 확인을 기다린다. */
export const FLIP_RAD = (2 * Math.PI) / 3;
/** 뒤집힌 새 방향이 이만큼(ms) 연속으로 이어져야 바꾼다. 한두 표본의 뒤집힘은 무시한다. */
export const FLIP_CONFIRM_MS = 500;
/** 확인 대기 중인 뒤집힌 방향과 이 각(rad) 안이면 같은 쪽이 이어진 것으로 센다. */
const FLIP_MATCH_RAD = Math.PI / 4;
/** 새 근거가 없을 때 마지막 방향을 유지하는 시간(ms). 그 뒤에는 점만 그린다. */
export const HEADING_HOLD_MS = 2000;
/** 방향 저역 통과의 시간 상수(ms). 10Hz 표본에서 한 번에 약 1/3 씩 따라간다. */
export const HEADING_SMOOTH_MS = 250;

/** COCO17 어깨·엉덩이 번호(contracts defs joint_layout). HALPE26 도 앞 17개가 같은 순서다. */
const JOINT = { lShoulder: 5, rShoulder: 6, lHip: 11, rHip: 12 } as const;
const BODY_LAYOUTS = new Set(['COCO17', 'HALPE26']);

type Skeleton = NonNullable<WorldTrack['skeleton']>;
type HeadingTrack = Pick<
  WorldTrack,
  'world_id' | 'aliases' | 'observation_state' | 'track_lifecycle' | 'velocity' | 'skeleton'
>;

/** 각도 차를 (−π, π] 로 접는다. */
const wrap = (angle: number) => {
  const a = (angle + Math.PI) % (2 * Math.PI);
  return (a <= 0 ? a + 2 * Math.PI : a) - Math.PI;
};

const gap = (a: number, b: number) => Math.abs(wrap(a - b));

/** 수평 이동 방향. 속력이 HEADING_SPEED_MIN 보다 느리거나 없으면 null(이어짐은 nextHeadings 가 센다). */
export function velocityYaw(track: Pick<WorldTrack, 'velocity'>): number | null {
  const v = track.velocity;
  if (!v) return null;
  const [vx, vy] = v;
  if (!Number.isFinite(vx) || !Number.isFinite(vy)) return null;
  return Math.hypot(vx, vy) >= HEADING_SPEED_MIN ? Math.atan2(vy, vx) : null;
}

/** 관측(observed)·유효(VALID)·보임인 관절만 쓴다. 추정(inferred)·불확실 관절로 방향을 만들지 않는다(AGENTS 규칙 7). */
function observedJoint(skeleton: Skeleton, index: number): readonly number[] | null {
  const point = skeleton.joints[index];
  if (!point) return null;
  if (skeleton.validity[index] !== 'VALID') return null;
  if (skeleton.joint_source[index] !== 'observed') return null;
  if (skeleton.visible[index] !== true) return null;
  return point;
}

/**
 * 좌우 한 쌍(왼 → 오른)으로 본 앞쪽. +Z 가 위인 오른손 좌표라 앞쪽 = 위 × (왼 → 오른) = (−sy, sx) 이다.
 * 둘 중 하나라도 관측이 아니거나 너무 가까우면 null.
 */
function pairYaw(skeleton: Skeleton, left: number, right: number, minM: number): number | null {
  const l = observedJoint(skeleton, left);
  const r = observedJoint(skeleton, right);
  if (!l || !r) return null;
  const sx = r[0]! - l[0]!;
  const sy = r[1]! - l[1]!;
  if (!Number.isFinite(sx) || !Number.isFinite(sy) || Math.hypot(sx, sy) < minM) return null;
  return Math.atan2(sx, -sy);
}

function wellFormed(skeleton: Skeleton): boolean {
  const n = skeleton.joints.length;
  return (
    BODY_LAYOUTS.has(skeleton.joint_layout) &&
    n > JOINT.rHip &&
    skeleton.validity.length === n &&
    skeleton.joint_source.length === n &&
    skeleton.visible.length === n
  );
}

/**
 * 멈춰 선 사람의 몸 방향(어깨). 두 어깨와 두 엉덩이가 모두 관측이고, 둘로 본 앞쪽이 BODY_AGREE_RAD 안에서 맞을 때만.
 * 어깨만 좌우가 바뀐 관절은 엉덩이와 맞지 않아 쓰지 않는다. 이번 표본이 관측(OBSERVED)일 때만 쓴다(예측 표본의 관절은 지금 자세가 아니다).
 */
export function bodyYaw(track: Pick<WorldTrack, 'observation_state' | 'skeleton'>): number | null {
  const skeleton = track.skeleton;
  if (!skeleton || track.observation_state !== 'OBSERVED' || !wellFormed(skeleton)) return null;
  const shoulders = pairYaw(skeleton, JOINT.lShoulder, JOINT.rShoulder, SHOULDER_MIN_M);
  if (shoulders === null) return null;
  const hips = pairYaw(skeleton, JOINT.lHip, JOINT.rHip, HIP_MIN_M);
  if (hips === null || gap(shoulders, hips) > BODY_AGREE_RAD) return null;
  return shoulders;
}

/** 각도를 고려한 저역 통과: 짧은 쪽으로 돌아 따라간다(170° → −170° 는 180° 를 지난다). */
export function smoothYaw(prev: number, next: number, dtMs: number): number {
  if (dtMs <= 0) return prev;
  const alpha = 1 - Math.exp(-dtMs / HEADING_SMOOTH_MS);
  return wrap(prev + wrap(next - prev) * alpha);
}

/** 사람 하나의 방향 기억. */
export interface HeadingEntry {
  /** 보이는 방향(World yaw, rad). 모르면 null(점만). */
  yaw: number | null;
  /** yaw 의 근거가 마지막으로 있던 world 시각(ms). */
  at: number;
  /** 수평 속력이 기준 이상으로 이어지기 시작한 world 시각. 느리면 null. */
  movingSince: number | null;
  /** 지금 방향과 반대인 새 방향이 이어지는 중(뒤집힘 확인 대기). */
  flip: { yaw: number; since: number } | null;
  /** 이 기억을 만든 world 시각(ms). */
  seen: number;
}

export type HeadingMemory = ReadonlyMap<string, HeadingEntry>;

export const NO_HEADINGS: HeadingMemory = new Map();

/** 앞 기억(병합으로 사라진 이전 ID 의 기억도 이어받는다). */
function previousEntry(memory: HeadingMemory, track: Pick<WorldTrack, 'world_id' | 'aliases'>) {
  for (const id of trackIds(track)) {
    const entry = memory.get(id);
    if (entry) return entry;
  }
  return undefined;
}

/** 사람 하나의 다음 기억. 남길 것이 없으면 null. */
function nextEntry(prev: HeadingEntry | undefined, track: HeadingTrack, now: number) {
  // 같은 world 를 다시 받았으면 그대로, 시계가 되돌아갔으면(재접속·서버 재시작) 처음부터.
  if (prev && now === prev.seen) return prev;
  const before = prev && now > prev.seen ? prev : undefined;

  const moving = velocityYaw(track);
  const movingSince = moving === null ? null : (before?.movingSince ?? now);
  // 움직이면 이동 방향(이어진 뒤에만), 멈췄으면 몸 방향. 움직이기 시작한 직후에는 근거 없음.
  const sample =
    moving === null ? bodyYaw(track) : now - movingSince! >= MOVE_CONFIRM_MS ? moving : null;
  const current =
    before && before.yaw !== null && now - before.at <= HEADING_HOLD_MS
      ? { yaw: before.yaw, at: before.at }
      : null;

  let shown = current;
  let flip: HeadingEntry['flip'] = null;
  if (sample !== null && !current) {
    shown = { yaw: sample, at: now };
  } else if (sample !== null && current) {
    if (gap(sample, current.yaw) <= FLIP_RAD) {
      shown = { yaw: smoothYaw(current.yaw, sample, now - current.at), at: now };
    } else {
      // 반대 방향: 같은 쪽으로 연속해서 FLIP_CONFIRM_MS 이어질 때만 바꾼다. 그동안은 지금 방향을 둔다.
      const since =
        before?.flip && gap(sample, before.flip.yaw) <= FLIP_MATCH_RAD ? before.flip.since : now;
      if (now - since >= FLIP_CONFIRM_MS) shown = { yaw: sample, at: now };
      else flip = { yaw: sample, since };
    }
  }
  if (!shown && movingSince === null && !flip) return null;
  return {
    yaw: shown?.yaw ?? null,
    at: shown?.at ?? now,
    movingSince,
    flip,
    seen: now,
  };
}

/**
 * 이번 world(시각 now, ms) 로 사람마다 방향 기억을 새로 만든다. 순수 함수라 앞 기억을 바꾸지 않는다.
 * 확인 불가(UNKNOWN·LOST) 사람과 이번 world 에 없는 사람의 기억은 버린다.
 */
export function nextHeadings(
  memory: HeadingMemory,
  tracks: readonly HeadingTrack[],
  now: number,
): HeadingMemory {
  const next = new Map<string, HeadingEntry>();
  for (const track of tracks) {
    if (trackLook(track) === 'unknown') continue;
    const entry = nextEntry(previousEntry(memory, track), track, now);
    if (entry) next.set(track.world_id, entry);
  }
  return next;
}
