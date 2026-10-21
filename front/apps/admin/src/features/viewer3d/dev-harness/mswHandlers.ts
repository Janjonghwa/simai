import {
  DEMO_ZONES_RESPONSE,
  isScenarioKey,
  scenarioWorld,
  TICK_MS,
  type ScenarioKey,
} from '@/features/viewer3d/dev-harness/scenarios';
import { delay, http, HttpResponse } from 'msw';

// Spatial 개발 화면 전용 REST Mock. 앱 공용 핸들러(src/mocks/handlers.ts)와 따로 두고 개발 화면 진입점에서만 켠다.
// 응답은 MockSource 스트림과 같은 시나리오 데이터(scenarios.ts)에서 나온다. 경로는 명세 12.6 기본안이다.

export type MockFailure = 'none' | 'error' | 'slow';

export interface SpatialMockApi {
  handlers: ReturnType<typeof http.get>[];
  /** 지금 재생 중인 시나리오와 tick 0 시각. 스냅샷을 같은 시나리오에서 만들기 위해 맞춘다. */
  setScenario(scenario: ScenarioKey, t0: number): void;
  setFailure(mode: MockFailure): void;
  readonly failure: MockFailure;
}

export function createSpatialMockApi(initial: {
  scenario: ScenarioKey;
  t0: number;
  slowMs?: number;
}): SpatialMockApi {
  let scenario = initial.scenario;
  let t0 = initial.t0;
  let failure: MockFailure = 'none';
  const slowMs = initial.slowMs ?? 1500;

  const failOrWait = async () => {
    if (failure === 'error') {
      return HttpResponse.json({ message: 'Mock: 서버 오류' }, { status: 503 });
    }
    if (failure === 'slow') await delay(slowMs);
    return null;
  };

  // 경로 앞의 * 는 기준 주소가 무엇이든 맞추려는 것이다(앱 핸들러와 같은 방식).
  const handlers = [
    http.get('*/api/world', async ({ request }) => {
      const failed = await failOrWait();
      if (failed) return failed;
      const url = new URL(request.url);
      const requested = url.searchParams.get('scenario');
      const key = isScenarioKey(requested) ? requested : scenario;
      const at = Number(url.searchParams.get('at'));
      if (!url.searchParams.has('at') || !Number.isFinite(at)) {
        return HttpResponse.json({ message: 'at 은 UTC 밀리초 숫자다' }, { status: 400 });
      }
      if (at < t0)
        return HttpResponse.json({ message: '그 시각의 데이터가 없다' }, { status: 404 });
      return HttpResponse.json(scenarioWorld(key, Math.floor((at - t0) / TICK_MS), t0));
    }),
    http.get('*/api/zones', async () => {
      const failed = await failOrWait();
      if (failed) return failed;
      return HttpResponse.json(DEMO_ZONES_RESPONSE);
    }),
  ];

  return {
    handlers,
    setScenario(next, nextT0) {
      scenario = next;
      t0 = nextT0;
    },
    setFailure(mode) {
      failure = mode;
    },
    get failure() {
      return failure;
    },
  };
}
