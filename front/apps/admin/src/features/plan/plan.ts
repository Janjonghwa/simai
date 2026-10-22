import { isActive, isAlarming, SEVERITY_RANK, type EventEntry } from '@/features/events/events';
import type { Severity } from '@/features/events/types';
import type { WorldTrack } from '@simai/contracts';

// 2D 평면(명세 13.6 비상 화면, 4.1 BEV 2D 개요)의 계산. three.js 없이 World 상태만으로 그린다.

/** 교실 테스트베드 크기(m). 장면·평면도(GET /api/floor-plans/current)가 생기면 그 값을 쓴다. */
export const ROOM = { width: 10, depth: 10 } as const;

/** 방 바깥 여백(m). 방 가장자리의 사람·이름표가 잘리지 않게 한다. */
export const PLAN_MARGIN = 0.9;

/**
 * World(m, +X 오른쪽, +Y 앞) → 평면 SVG 좌표(m, +y 아래). World 와 화면 좌표의 변환은 여기서만 한다.
 * 위에서 내려다본 지도처럼 +Y 가 화면 위쪽이 되게 뒤집는다.
 */
export const toPlan = (x: number, y: number): readonly [number, number] => [x, ROOM.depth - y];

/** 사람 표시 모양(명세 13.2 관측 상태 표시를 따른다). */
export type TrackLook = 'observed' | 'predicted' | 'unknown';

export function trackLook(
  track: Pick<WorldTrack, 'observation_state' | 'track_lifecycle'>,
): TrackLook {
  if (track.track_lifecycle === 'LOST' || track.observation_state === 'UNKNOWN') return 'unknown';
  return track.observation_state === 'PREDICTED' ? 'predicted' : 'observed';
}

/** 이보다 작은 위치 불확실성(m)은 그리지 않는다. 관측 중인 사람마다 원이 생기면 평면이 어지럽다. */
export const SIGMA_MIN_M = 0.3;

/** 불확실 원 반지름(m). 작으면 null. */
export function sigmaRadius(track: Pick<WorldTrack, 'position_xy_sigma'>): number | null {
  const sigma = Math.max(...track.position_xy_sigma);
  return sigma >= SIGMA_MIN_M ? sigma : null;
}

/** 사람·구역 하나에 걸린 진행 중 이벤트 요약. */
export interface PlanMark {
  severity: Severity;
  /** 확인 대기 경보가 있다(소리·점멸 대상). */
  alarming: boolean;
}

/** 두 표시를 합친다: 더 높은 등급, 하나라도 확인 대기면 확인 대기. */
const combine = (a: PlanMark, b: PlanMark): PlanMark => ({
  severity: SEVERITY_RANK[a.severity] >= SEVERITY_RANK[b.severity] ? a.severity : b.severity,
  alarming: a.alarming || b.alarming,
});

const merge = (marks: Map<string, PlanMark>, id: string, mark: PlanMark) => {
  const prev = marks.get(id);
  marks.set(id, prev ? combine(prev, mark) : mark);
};

/**
 * 이 사람으로 볼 ID: 지금 world_id 와 병합(ID 교정)으로 합쳐진 이전 ID(aliases).
 * 이벤트의 world_ids 는 발생 당시 ID 를 그대로 두므로(계약 world_state·risk_event) 이전 ID 로 걸린 경보도 이 사람에게 보인다.
 */
export const trackIds = (track: Pick<WorldTrack, 'world_id' | 'aliases'>): readonly string[] => [
  track.world_id,
  ...(track.aliases ?? []),
];

/** 여러 ID 에 걸린 표시를 하나로 합친다. */
export function markFor(
  marks: Map<string, PlanMark>,
  ids: readonly string[],
): PlanMark | undefined {
  let result: PlanMark | undefined;
  for (const id of ids) {
    const mark = marks.get(id);
    if (mark) result = result ? combine(result, mark) : mark;
  }
  return result;
}

/** 진행 중 이벤트(CAUTION 이상)로 사람·구역마다 가장 높은 등급과 확인 대기 여부를 모은다. */
export function planMarks(entries: readonly EventEntry[]) {
  const people = new Map<string, PlanMark>();
  const zones = new Map<string, PlanMark>();
  for (const entry of entries) {
    const { event } = entry;
    if (!isActive(event) || SEVERITY_RANK[event.severity] < SEVERITY_RANK.CAUTION) continue;
    const mark = { severity: event.severity, alarming: isAlarming(entry) };
    for (const id of event.world_ids) merge(people, id, mark);
    if (event.zone_id) merge(zones, event.zone_id, mark);
  }
  return { people, zones };
}

export type PlanMarks = ReturnType<typeof planMarks>;

/** 등급 색: 빨강은 HIGH 이상, 노랑은 CAUTION(CONVENTION: 빨강·노랑은 위험 등급에만). */
export const markTone = (mark: PlanMark): 'critical' | 'caution' =>
  SEVERITY_RANK[mark.severity] >= SEVERITY_RANK.HIGH ? 'critical' : 'caution';
