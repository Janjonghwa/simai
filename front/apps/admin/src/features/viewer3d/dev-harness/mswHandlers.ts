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

/** none 정상, error 503, slow 지연, nonjson 200 이지만 HTML(MSW 를 거치지 않은 Vite 응답과 같은 모양). */
export type MockFailure = 'none' | 'error' | 'slow' | 'nonjson';

export interface SpatialMockApi {
  handlers: ReturnType<typeof http.get>[];
  /** 지금 재생 중인 시나리오와 tick 0 시각. 스냅샷을 같은 시나리오에서 만들기 위해 맞춘다. */
  setScenario(scenario: ScenarioKey, t0: number): void;
  setFailure(mode: MockFailure): void;
  readonly failure: MockFailure;
  /**
   * 서비스 워커가 이 페이지의 Mock 활성화를 잃었을 때 다시 켠다(disable → enable 로 MOCK_ACTIVATE 를 다시 보낸다).
   * 워커는 유휴로 종료됐다 다시 뜨면 활성 페이지 목록(메모리)이 비어 모든 요청을 그대로 넘기고, 그러면 Vite 가 index.html 을 준다.
   * 동시에 여러 번 불러도 한 번만 돈다. 재활성 수단이 없으면(시험·Node) 아무것도 하지 않는다.
   */
  reactivate(): Promise<void>;
  /** 재활성한 횟수(진단용). */
  readonly reactivations: number;
  setReactivator(fn: (() => Promise<void>) | null): void;
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
  let reactivator: (() => Promise<void>) | null = null;
  let pending: Promise<void> | null = null;
  let reactivations = 0;

  const failOrWait = async () => {
    if (failure === 'error') {
      return HttpResponse.json({ message: 'Mock: 서버 오류' }, { status: 503 });
    }
    if (failure === 'nonjson') {
      return HttpResponse.html('<!doctype html><title>Mock: HTML 응답</title>');
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
    reactivate() {
      if (!reactivator) return Promise.resolve();
      pending ??= reactivator()
        .then(() => {
          reactivations += 1;
        })
        .finally(() => {
          pending = null;
        });
      return pending;
    },
    get reactivations() {
      return reactivations;
    },
    setReactivator(fn) {
      reactivator = fn;
    },
  };
}
