import { createExternalSource, type SpatialSource } from '@/features/viewer3d/data/spatialSource';
import {
  createMockSource,
  type MockSpatialSource,
} from '@/features/viewer3d/dev-harness/mockSource';
import type { SpatialMockApi } from '@/features/viewer3d/dev-harness/mswHandlers';
import {
  isScenarioKey,
  SCENARIOS,
  type ScenarioKey,
} from '@/features/viewer3d/dev-harness/scenarios';

// 개발 화면의 데이터 입력 선택. 시나리오 Mock 5종, 외부 Live 주입 시험, 입력 없음.

export const DATA_SOURCES = [
  ...SCENARIOS.map((s) => ({ key: s.key as DataSourceKey, label: `Mock · ${s.label}` })),
  { key: 'external' as DataSourceKey, label: '외부 입력 시험(JSON 주입)' },
  { key: 'off' as DataSourceKey, label: '입력 없음' },
];

export type DataSourceKey = ScenarioKey | 'external' | 'off';

const PARAM = 'source';
const DEFAULT: DataSourceKey = 'normal';

const isDataSourceKey = (value: unknown): value is DataSourceKey =>
  isScenarioKey(value) || value === 'external' || value === 'off';

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
  const mock = createMockSource({ scenario, restBase });
  // REST 스냅샷이 같은 시나리오·시각축에서 나오게 맞춘다.
  api?.setScenario(scenario, mock.t0);
  return { source: key === 'external' ? relay(mock) : mock, mock };
}
