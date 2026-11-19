import { useCctvStore } from '@/features/cctv/cctv.store';
import type { CameraBadge } from '@/features/setup/cameraBadge';
import type { CoverageCameraView, Step1CoverageView } from '@/features/setup/setup';
import type { SetupMode } from '@/features/setup/types';
import {
  mapDateLabel,
  type ReuseChoiceState,
  type ReuseMapOption,
} from '@/features/setup/useReuseScene';
import { useMemo } from 'react';

// 시연 계정(features/demo/demoAccount)의 세팅 1단계 화면. 영상이 나오는 카메라는 연결됨으로 보이고, 「맵」 칸은 새 맵을 고르면 이전 맵 목록을
// 숨기고 재사용을 고르면 하루 전 맵(고를 수 있음)과 이틀 전 맵(보정이 맞지 않아 막힘) 둘을 보인다. 날짜만 바꿔 보이고 실제로 고르는 맵은
// 서버의 최근 맵이다(3D 는 어느 맵이든 코드에 넣은 GLB, demoMap). 맵은 카메라 시점 확인·추천 없이 고른다(demoReuseOpen, 서버
// SetupDemoAccounts 와 같은 판정). 보정이 없거나 완료 전인 맵(EXCLUDED)만 기술적으로 쓸 수 없어 꺼 둔다.

const DAY_MS = 86_400_000;
/** 한국 시간(UTC+9, 일광 절약 시간 없음). 날짜를 한국 날짜로 센다. */
const KST_OFFSET_MS = 9 * 3_600_000;

/** 고를 수 있게 보이는 맵(하루 전)과 막힌 맵(이틀 전)이 오늘에서 며칠 전인지. */
const OPEN_MAP_DAYS_AGO = 1;
const BLOCKED_MAP_DAYS_AGO = 2;

/** 이틀 전 맵의 id(화면에만 있는 맵이라 고를 수 없다)와, 서버 목록에 막힌 맵이 없을 때 보이는 이유. */
const BLOCKED_MAP_ID = 'demo-blocked-map';
const BLOCKED_MAP_NOTE = '지금 카메라 보정과 맞지 않아요';

const kstDay = (ms: number) => Math.floor((ms + KST_OFFSET_MS) / DAY_MS);

/** 맵의 한국 시각은 그대로 두고 날짜만 오늘에서 days 일 전으로 옮긴다. */
function shownDaysAgo(map: ReuseMapOption, days: number, nowMs: number): ReuseMapOption {
  const at = map.completedAtMs + (kstDay(nowMs) - days - kstDay(map.completedAtMs)) * DAY_MS;
  return { ...map, completedAtMs: at, dateLabel: mapDateLabel(at) };
}

const CONNECTED_BADGE: CameraBadge = { kind: 'READY', label: '연결됨', tone: 'ok' };

/**
 * 지금 영상이 나오는 카메라 ID 를 쉼표로 이은 글. 스토어는 영상 한 장마다 바뀌므로 연결 상태가 바뀔 때만 다시 그리게 글로 견준다.
 * 꺼 두면(시연 계정이 아니면) 늘 빈 글이다.
 */
function usePlayingCameraKey(cameraIds: readonly string[], enabled: boolean): string {
  return useCctvStore((s) =>
    enabled && s.link === 'open'
      ? cameraIds.filter((id) => s.views[id]?.phase === 'playing').join(',')
      : '',
  );
}

function asConnected(camera: CoverageCameraView): CoverageCameraView {
  return {
    ...camera,
    badge: CONNECTED_BADGE,
    tone: CONNECTED_BADGE.tone,
    guideLabel: camera.guides.length > 0 ? camera.guideLabel : '',
  };
}

/** 영상이 나오는 카메라를 연결됨으로 바꾼 1단계 카메라 상태. 모든 카메라가 그렇다면 연결 요약 글도 뺀다. 시연 계정이 아니면 그대로다. */
export function useDemoCoverage(coverage: Step1CoverageView, demo: boolean): Step1CoverageView {
  const playingKey = usePlayingCameraKey(
    coverage.cameras.map((camera) => camera.id),
    demo,
  );
  return useMemo(() => {
    if (playingKey === '') return coverage;
    const playing = new Set(playingKey.split(','));
    const cameras = coverage.cameras.map((camera) =>
      playing.has(camera.id) ? asConnected(camera) : camera,
    );
    const allPlaying = cameras.every((camera) => playing.has(camera.id));
    return {
      ...coverage,
      cameras,
      connectivitySummary: allPlaying ? '' : coverage.connectivitySummary,
    };
  }, [coverage, playingKey]);
}

/**
 * 「맵」 칸에 보일 이전 맵: 새 맵을 골랐으면 없다. 재사용이면 시작할 맵(recommendedId)을 하루 전 날짜로 보이고, 그 아래 이틀 전 날짜의
 * 막힌 맵을 하나 보인다. 막힌 맵은 서버 목록의 막힌 맵(지금 보이는 이유 그대로)이고, 없으면 시작할 맵에 보정이 맞지 않는다는 이유를 단다.
 * 시작할 맵이 없으면(재사용이 막힘) 서버 목록 그대로다.
 */
export function demoReuseShown(
  reuse: ReuseChoiceState,
  mode: SetupMode,
  nowMs: number = Date.now(),
): ReuseChoiceState {
  if (mode === 'NEW') return { ...reuse, maps: [] };
  const open = reuse.maps.find((map) => map.id === reuse.recommendedId && map.selectable);
  if (!open) return reuse;
  const blockedBase = reuse.maps.find((map) => !map.selectable) ?? {
    ...open,
    status: 'CHANGED',
    note: BLOCKED_MAP_NOTE,
  };
  const blocked: ReuseMapOption = {
    ...shownDaysAgo(blockedBase, BLOCKED_MAP_DAYS_AGO, nowMs),
    id: BLOCKED_MAP_ID,
    selectable: false,
    recommended: false,
  };
  return { ...reuse, maps: [shownDaysAgo(open, OPEN_MAP_DAYS_AGO, nowMs), blocked] };
}

/**
 * 시연 계정의 이전 맵 재사용: 제외(EXCLUDED)가 아닌 맵은 카메라 시점 확인과 관계없이 모두 고를 수 있고, 추천 표시·상태 문구가 없다.
 * 기본 선택은 가장 최근에 완료된 맵이다(서버도 고른 맵이 없으면 그 맵을 쓴다). 고를 맵이 하나라도 있으면 재사용을 막지 않는다.
 */
export function demoReuseOpen(reuse: ReuseChoiceState): ReuseChoiceState {
  const maps = reuse.maps.map((map) =>
    map.status === 'EXCLUDED' ? map : { ...map, selectable: true, recommended: false, note: '' },
  );
  const newest = maps
    .filter((map) => map.selectable)
    .sort((a, b) => b.completedAtMs - a.completedAtMs)[0];
  if (!newest) return { ...reuse, maps };
  return { ...reuse, blocked: null, note: null, maps, recommendedId: newest.id };
}
