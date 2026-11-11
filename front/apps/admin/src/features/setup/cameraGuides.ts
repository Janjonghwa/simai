import { cameraShortLabel, isNeighborPair, pairLabel } from '@/features/setup/cameraLabels';
import type {
  Connectivity,
  CoverageGuideItem,
  CoveragePairItem,
  GuideDirection,
  GuideMagnitude,
} from '@/features/setup/types';

// 세팅 1단계 카메라 안내: 서버가 준 안내·쌍 등급·연결 판정은 다시 계산하지 않고, 화면에 보일 것만 가려서 글로 만든다.
// 프런트가 새로 정하는 판정은 '옆 쌍의 등급이 LACK 이면 보정이 실패할 수 있다' 하나뿐이다.

/** 안내 문장용 방향(조사 포함). 받침 유무로 '으로/로'가 갈려서 방향마다 적는다. */
const DIRECTION_TOWARD: Record<Exclude<GuideDirection, 'NONE'>, string> = {
  LEFT: '왼쪽으로',
  RIGHT: '오른쪽으로',
  UP: '위로',
  DOWN: '아래로',
};

const MAGNITUDE_WORD: Record<GuideMagnitude, string> = {
  SMALL: '조금',
  MORE: '더',
  NONE: '',
};

const NO_OVERLAP_TEXT =
  '겹치는 곳이 없습니다. 각도를 조정하거나 겹치는 곳에 무늬 있는 물건을 두세요';

/** 옆 카메라 쌍의 겹침이 부족할 때 오른쪽 카드 안내와 '다음' 확인창이 함께 쓰는 제목. */
export const CALIBRATION_RISK_TITLE = '보정이 실패할 수 있어요';

/** 한 카메라 영상에 얹는 안내 하나(방향 꺾쇠·같이 보는 곳 동그라미)와 오른쪽 카드에 쓰는 문장. */
export interface CameraGuideView {
  /** 카메라 이름 없는 안내 문장. 예: '오른쪽으로 더 돌려 주세요 (캠2–캠3)' */
  text: string;
  /** 돌릴 쪽. 방향이 없거나 겹치는 곳이 없으면 'NONE'. */
  direction: GuideDirection;
  magnitude: GuideMagnitude;
  /** 같이 보는 이웃 카메라 이름. 예: '캠3' */
  neighborLabel: string;
  /** 이 카메라 영상에서 이웃과 같이 보는 곳(0~1 비율). 없으면 null. */
  spot: readonly [number, number] | null;
}

/** 같은 쌍(a–b 순서 무관)의 서버 판정. */
function findPair(pairs: readonly CoveragePairItem[], a: string, b: string) {
  return pairs.find((p) => (p.a === a && p.b === b) || (p.a === b && p.b === a));
}

/**
 * 화면에 보일 안내만 거른다. 보정은 옆 카메라 쌍(1–2, 2–3, 3–4, 4–1)으로 하므로 그 쌍의 안내만 보이고,
 * 선택 보강(OPTIONAL_IMPROVE)은 그 쌍 등급이 LACK 일 때만 보인다. 겹치는 곳 없음(NO_OVERLAP_VISIBLE)은 늘 보인다.
 */
export function visibleGuides(
  cameraIds: readonly string[],
  guides: readonly CoverageGuideItem[],
  pairs: readonly CoveragePairItem[],
): CoverageGuideItem[] {
  return guides.filter((guide) => {
    const [a, b] = guide.pair;
    if (!isNeighborPair(cameraIds, a, b)) return false;
    return guide.reason_code !== 'OPTIONAL_IMPROVE' || findPair(pairs, a, b)?.grade === 'LACK';
  });
}

/** 옆 카메라 쌍 가운데 겹침이 부족(LACK)해 보정이 실패할 수 있는 쌍. */
export function riskyPairs(
  cameraIds: readonly string[],
  pairs: readonly CoveragePairItem[],
): CoveragePairItem[] {
  return pairs.filter((p) => p.grade === 'LACK' && isNeighborPair(cameraIds, p.a, p.b));
}

/** 서버 안내 하나를 영상 위 표시와 문장으로 바꾼다. */
export function cameraGuideView(
  cameraIds: readonly string[],
  guide: CoverageGuideItem,
  pairs: readonly CoveragePairItem[],
): CameraGuideView {
  const [a, b] = guide.pair;
  const where = pairLabel(cameraIds, a, b);
  const pair = findPair(pairs, a, b);
  const spot =
    pair?.a === guide.camera_id
      ? pair.centroid_a
      : pair?.b === guide.camera_id
        ? pair.centroid_b
        : null;
  const neighbor = guide.pair.find((id) => id !== guide.camera_id) ?? guide.camera_id;
  const base = {
    magnitude: guide.magnitude,
    neighborLabel: cameraShortLabel(cameraIds, neighbor),
    spot,
  };
  if (guide.direction === 'NONE' || guide.reason_code === 'NO_OVERLAP_VISIBLE') {
    return { ...base, text: `${NO_OVERLAP_TEXT} (${where})`, direction: 'NONE' };
  }
  const magnitude = MAGNITUDE_WORD[guide.magnitude];
  return {
    ...base,
    text: `${DIRECTION_TOWARD[guide.direction]} ${magnitude ? `${magnitude} ` : ''}돌려 주세요 (${where})`,
    direction: guide.direction,
  };
}

/** 마지막 확인이 얼마나 지났는지. 서버가 준 age_ms 로 센다. */
export function freshnessLabel(ageMs: number): string {
  return ageMs < 1000 ? '방금' : `${Math.floor(ageMs / 1000)}초 전 확인`;
}

/**
 * 카메라 연결 요약 한 줄. 연결되면 'N대 모두 연결', 판단 중이면 '연결 확인 중', 갈라졌으면 묶음을 보인다
 * ('캠1·캠2 / 캠3·캠4 갈림'). 확인 시각(age_ms)을 알면 뒤에 붙인다.
 */
export function connectivitySummary(
  cameraIds: readonly string[],
  connectivity: Connectivity,
  components: readonly (readonly string[])[],
  ageMs: number | null,
): string {
  const state =
    connectivity === 'CONNECTED'
      ? `${cameraIds.length}대 모두 연결`
      : connectivity === 'UNKNOWN'
        ? '연결 확인 중'
        : `${components
            .map((group) => group.map((id) => cameraShortLabel(cameraIds, id)).join('·'))
            .join(' / ')} 갈림`;
  return ageMs === null ? state : `${state} · ${freshnessLabel(ageMs)}`;
}
