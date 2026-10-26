import { createExternalSource, type SpatialSource } from '@/features/viewer3d/data/spatialSource';
import {
  createMockSource,
  type MockSpatialSource,
} from '@/features/viewer3d/dev-harness/mockSource';
import type { SpatialMockApi } from '@/features/viewer3d/dev-harness/mswHandlers';
import {
  DEMO_MAX_TICKS,
  isScenarioKey,
  SCENARIOS,
  type ScenarioKey,
} from '@/features/viewer3d/dev-harness/scenarios';
import type { ZoneApplyState } from '@/features/viewer3d/zones/zonePresentation';

// 개발 화면의 데이터 입력 선택. 시나리오 Mock 5종, 외부 Live 주입 시험, 입력 없음.

export const DATA_SOURCES = [
  ...SCENARIOS.map((s) => ({ key: s.key as DataSourceKey, label: `Mock · ${s.label}` })),
  { key: 'external' as DataSourceKey, label: '외부 입력 시험(JSON 주입)' },
  { key: 'off' as DataSourceKey, label: '입력 없음' },
];

export type DataSourceKey = ScenarioKey | 'external' | 'off';

const PARAM = 'source';
const DEFAULT: DataSourceKey = 'normal';

/**
 * 개발 PC 에만 두는 XBot 모델 위치. Git 에 넣지 않는다(node_modules 는 .gitignore 대상).
 * 'simAI 관제 데모/xbot.glb.txt'(base64)를 풀어 front/apps/admin/node_modules/.cache/spatial-dev/xbot.glb 로 둔다.
 * 파일이 없으면 모델 읽기가 실패하고 캡슐 아바타로 그린다.
 */
export const DEV_XBOT_URL = '/node_modules/.cache/spatial-dev/xbot.glb';

export type AvatarKind = 'xbot' | 'capsule';

/** ?avatar=capsule 이면 캡슐만, 그 밖에는 XBot 을 먼저 시도한다. */
export function readAvatar(search: string): AvatarKind {
  return new URLSearchParams(search).get('avatar')?.toLowerCase() === 'capsule'
    ? 'capsule'
    : 'xbot';
}

const isDataSourceKey = (value: unknown): value is DataSourceKey =>
  isScenarioKey(value) || value === 'external' || value === 'off';

/** 개발 화면의 구역 적용 상태 표시 시험. ?zones=preview|applied, 없거나 모르면 적용 미확인. */
const ZONE_PARAM: Record<string, ZoneApplyState> = {
  unverified: 'UNVERIFIED',
  preview: 'DEMO_PREVIEW',
  applied: 'DEMO_ASSUMED_APPLIED',
};

export const ZONE_APPLY_OPTIONS: { key: ZoneApplyState; label: string }[] = [
  { key: 'UNVERIFIED', label: '구역 · 적용 미확인(기본)' },
  { key: 'DEMO_PREVIEW', label: '구역 · 미리보기(DEMO)' },
  { key: 'DEMO_ASSUMED_APPLIED', label: '구역 · 적용 가정(DEMO)' },
];

export function readZoneApply(search: string): ZoneApplyState {
  return ZONE_PARAM[new URLSearchParams(search).get('zones')?.toLowerCase() ?? ''] ?? 'UNVERIFIED';
}

/** zones 만 바꾼 주소. 기본값이면 매개변수를 지운다. */
export function withZoneApply(href: string, state: ZoneApplyState): string {
  const url = new URL(href);
  const key = Object.keys(ZONE_PARAM).find((k) => ZONE_PARAM[k] === state);
  if (!key || state === 'UNVERIFIED') url.searchParams.delete('zones');
  else url.searchParams.set('zones', key);
  return url.toString();
}

/** ?source=normal|observation-loss|risk-lifecycle|disconnect|invalid-input|external|off. 없거나 모르면 normal. */
export function readDataSource(search: string): DataSourceKey {
  const value = new URLSearchParams(search).get(PARAM)?.toLowerCase();
  return isDataSourceKey(value) ? value : DEFAULT;
}

/** source 만 바꾼 주소. 다른 매개변수와 해시는 그대로 둔다. */
export function withDataSource(href: string, key: DataSourceKey): string {
  const url = new URL(href);
  url.searchParams.set(PARAM, key);
  return url.toString();
}

export interface HarnessSource {
  source: SpatialSource | null;
  /** 재생 조작 대상. 입력 없음이면 null. */
  mock: MockSpatialSource | null;
}

/**
 * 외부 입력 시험: Mock 정상 시나리오를 JSON 문자열로 바꿨다가 다시 읽어 createExternalSource 로 밀어 넣는다.
 * FE 가 STOMP 본문을 받아 push 하는 경로와 같다. 구독이 모두 풀리면 Mock 도 멈춘다.
 */
function relay(mock: MockSpatialSource): SpatialSource {
  const external = createExternalSource(`external:${mock.id}`);
  let stop: (() => void) | null = null;
  return {
    id: external.id,
    kind: 'EXTERNAL',
    subscribe(sink) {
      const off = external.subscribe(sink);
      stop ??= mock.subscribe({
        onMessage: (raw) => external.push(JSON.parse(JSON.stringify(raw))),
        onConnectionChange: (change) => external.setConnection(change.state, change.reason),
        onZones: (result) => external.pushZones(result),
        onWorkSurfaces: (result) => external.pushWorkSurfaces(result),
      });
      return () => {
        off();
        if (external.subscriberCount === 0) {
          stop?.();
          stop = null;
        }
      };
    },
  };
}

/** 고른 입력의 Source 를 만든다. 다시 만들면 새 id 라 Viewer 가 데이터를 비우고 처음부터 받는다. */
export function createHarnessSource(
  key: DataSourceKey,
  restBase: string | null,
  api: SpatialMockApi | null,
): HarnessSource {
  if (key === 'off') return { source: null, mock: null };
  const scenario: ScenarioKey = key === 'external' ? 'normal' : key;
  const mock = createMockSource({
    scenario,
    restBase,
    reactivateMocks: api ? () => api.reactivate() : undefined,
    maxTicks: DEMO_MAX_TICKS[scenario],
  });
  // REST 스냅샷이 같은 시나리오·시각축에서 나오게 맞춘다.
  api?.setScenario(scenario, mock.t0);
  return { source: key === 'external' ? relay(mock) : mock, mock };
}
