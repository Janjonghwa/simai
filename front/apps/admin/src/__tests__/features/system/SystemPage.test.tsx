import { SystemPage } from '@/features/system/SystemPage';
import { METRICS_KEY } from '@/features/system/useMetrics';
import { viewerHealthAt } from '@/mocks/realtimeScenario';
import { useRealtimeStore } from '@/store/realtime.store';
import { useSessionStore } from '@/store/session.store';
import type { ViewerHealthPayload } from '@simai/contracts';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw/http';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/api', async () => {
  const { createHttp } = await import('@/lib/http');
  return { api: createHttp({ baseUrl: 'http://api.test' }) };
});

let metricsCalls = 0;
const server = setupServer(
  http.get('http://api.test/api/metrics', () => {
    metricsCalls += 1;
    return HttpResponse.json({
      contract: 'DEMO_UNCONFIRMED',
      as_of: 1,
      stages: {
        capture_to_site_alert_ms: { p50: 340, p95: 520 },
        capture_to_remote_delivery_ms: {
          viewer: { p50: 620, p95: 980 },
          pwa: { p50: 1400, p95: 2300 },
        },
      },
    });
  }),
);
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
afterAll(() => server.close());

// 바퀴(40초)의 0초라 카메라 4대가 정상이고 값이 정해져 있다.
const T = 1_760_342_400_000;

function seed(t = T, change: Partial<ViewerHealthPayload> = {}) {
  const now = Date.now();
  useRealtimeStore.setState({
    phase: 'OPEN',
    openedAt: now,
    health: {
      payload: { ...viewerHealthAt(t).payload, ...change },
      receivedAt: now,
      clockOffsetMs: 0,
    },
  });
}

beforeEach(() => {
  metricsCalls = 0;
  useSessionStore.getState().signIn({ accessToken: 't', user: { id: 'u', role: 'ADMIN' } });
  seed();
});
afterEach(() => {
  vi.useRealTimers();
  cleanup();
  server.resetHandlers();
  useSessionStore.setState(useSessionStore.getInitialState(), true);
  useRealtimeStore.setState(useRealtimeStore.getInitialState(), true);
});

function renderPage() {
  const queryClient = new QueryClient();
  render(
    <QueryClientProvider client={queryClient}>
      <SystemPage />
    </QueryClientProvider>,
  );
  return queryClient;
}

/** 빈칸이 읽히는 글자(보이는 — 와 화면 낭독기용 값 없음). */
const BLANK = '—값 없음';

/** 표(name)에서 행 머리(row)와 열 머리(column)가 만나는 칸. */
function cell(name: string, row: string, column: string): HTMLElement {
  const table = within(screen.getByRole('table', { name }));
  const tr = table.getByRole('rowheader', { name: row }).closest('tr') as HTMLTableRowElement;
  const index = (table.getByRole('columnheader', { name: column }) as HTMLTableCellElement)
    .cellIndex;
  return tr.cells[index] as HTMLElement;
}
const camera = (id: string, column: string) => cell('카메라', id, column);
const latency = (stage: string, column: 'p50' | 'p95') => cell('구간별 지연', stage, column);
const cameraIds = () =>
  within(screen.getByRole('table', { name: '카메라' }))
    .queryAllByRole('rowheader')
    .map((th) => th.textContent);
/** 목록(dl)에서 이름(dt) 옆 값. */
const dd = (label: string) =>
  screen.getByText(label, { selector: 'dt' }).nextElementSibling as HTMLElement;
const orange = (element: HTMLElement) =>
  element.classList.contains('text-delay') || element.querySelector('.text-delay') !== null;

describe('SystemPage', () => {
  it('카메라 표는 열 머리마다 맞는 값을 보인다', () => {
    renderPage();

    expect(cameraIds()).toEqual(['cam1', 'cam2', 'cam3', 'cam4']);
    expect(camera('cam1', '상태').textContent).toBe('정상');
    expect(camera('cam1', 'FPS').textContent).toBe('14.6');
    expect(camera('cam1', '시각 차이').textContent).toBe('4 ms');
    expect(camera('cam1', '수집 간격 최대').textContent).toBe('70 ms');
    expect(camera('cam1', '전송 손실').textContent).toBe('0');
    expect(camera('cam1', '정책 건너뜀').textContent).toBe('10');
    expect(camera('cam1', '큐 대기 p95').textContent).toBe('36 ms');
    expect(camera('cam2', '전송 손실').textContent).toBe('1');
    expect(camera('cam2', '큐 대기 p95').textContent).toBe('48 ms');
  });

  it('지연만 주황(delay)으로 칠하고, 다른 값과 빈칸은 칠하지 않는다', async () => {
    renderPage();
    expect((await screen.findByText('980 ms')).className).toContain('text-delay');

    for (const column of ['수집 간격 최대', '큐 대기 p95']) {
      expect(orange(camera('cam1', column))).toBe(true);
    }
    for (const column of ['상태', 'FPS', '시각 차이', '전송 손실', '정책 건너뜀']) {
      expect(orange(camera('cam1', column))).toBe(false);
    }
    expect(latency('촬영 → 판정', 'p50').textContent).toBe('300 ms');
    expect(orange(latency('촬영 → 판정', 'p50'))).toBe(true);
    expect(latency('촬영 → 현장 소리', 'p95').textContent).toBe('520 ms');
    expect(latency('촬영 → 관제 화면', 'p50').textContent).toBe('620 ms');
    expect(latency('촬영 → 반장 PWA', 'p50').textContent).toBe('1400 ms');
    expect(latency('촬영 → 반장 PWA', 'p95').textContent).toBe('2300 ms');
    expect(orange(latency('촬영 → 반장 PWA', 'p95'))).toBe(true);

    expect(dd('파이프라인 지연').textContent).toBe('110 ms');
    expect(orange(dd('파이프라인 지연'))).toBe(true);
    expect(dd('GPU 사용률').textContent).toBe('68%');
    expect(orange(dd('GPU 사용률'))).toBe(false);
    expect(dd('GPU 메모리').textContent).toBe('62%');
  });

  it('키프레임을 기다리는 카메라는 상태를 굵게만 보이고(주황 아님), 잴 수 없는 칸은 비운다', () => {
    seed(T + 21_000);
    renderPage();

    expect(camera('cam3', '상태').textContent).toBe('키프레임 대기');
    expect(camera('cam3', '상태').className).toContain('font-semibold');
    expect(orange(camera('cam3', '상태'))).toBe(false);
    expect(camera('cam1', '상태').className).not.toContain('font-semibold');
    expect(camera('cam3', 'FPS').textContent).toBe('0.0');
    expect(camera('cam3', '시각 차이').textContent).toBe(BLANK);
    expect(camera('cam3', '큐 대기 p95').textContent).toBe(BLANK);
    expect(orange(camera('cam3', '큐 대기 p95'))).toBe(false);
    expect(screen.getByText('DEGRADED')).toBeTruthy();
  });

  it('위 카드는 연결 세 갈래·데이터 경과·보정·구성을 보이고, 연결이 끊기면 비운다', () => {
    seed(T, {
      laptop: { link: 'DISCONNECTED', last_heartbeat_at: T - 5000 },
      system_health: 'UNSAFE_UNKNOWN',
    });
    renderPage();

    expect(dd('노트북 ↔ Tier 0').textContent).toBe('연결됨');
    expect(dd('노트북 ↔ EC2').textContent).toBe('끊김');
    expect(dd('Tier 0 ↔ EC2').textContent).toBe('연결됨');
    expect(dd('보정').textContent).toBe('CALIBRATED cal-7');
    expect(dd('구성').textContent).toBe('cfg-12');
    const age = screen.getByText(/^\d+\.\d초$/);
    expect(age.className).not.toContain('text-delay');

    act(() => useRealtimeStore.setState({ phase: 'CLOSED' }));
    for (const label of [
      '노트북 ↔ Tier 0',
      '노트북 ↔ EC2',
      'Tier 0 ↔ EC2',
      '보정',
      '구성',
      '장면',
    ]) {
      expect(dd(label).textContent).toBe(BLANK);
    }
    // 데이터 경과는 끊겨도 계속 늘어나며, 늦어짐을 주황으로 보인다.
    expect(screen.getByText(/^\d+\.\d초$/).className).toContain('text-delay');
    expect(screen.getByText('연결 끊김')).toBeTruthy();
  });

  it('현장이 다른 Scene 으로 판정하면 장면 칸에 두 버전을 줄무늬로 보인다', () => {
    renderPage();
    expect(dd('장면').textContent).toBe('scene-3');
    expect(dd('장면').querySelector('.bg-stripes')).toBeNull();

    act(() =>
      seed(T, {
        scene: { committed_version: 'scene-3', live_version: 'scene-2', mismatch: true },
      }),
    );
    expect(dd('장면').textContent).toBe('scene-3 ≠ 현장 scene-2');
    expect(dd('장면').querySelector('.bg-stripes')).toBeTruthy();
    expect(dd('장면').innerHTML).not.toMatch(/sev-|text-delay/);
  });

  it('Tier 0 가 끊기면 Tier 0 에서 온 값을 비우고 EC2 가 아는 연결 상태만 보인다', async () => {
    seed(T, { tier0_link: 'DISCONNECTED', system_health: 'UNSAFE_UNKNOWN' });
    renderPage();

    expect(cameraIds()).toEqual([]);
    expect(latency('촬영 → 판정', 'p50').textContent).toBe(BLANK);
    expect(dd('파이프라인 지연').textContent).toBe(BLANK);
    expect(dd('GPU 사용률').textContent).toBe(BLANK);
    expect(dd('노트북 ↔ Tier 0').textContent).toBe(BLANK);
    expect(dd('노트북 ↔ EC2').textContent).toBe('연결됨');
    expect(dd('Tier 0 ↔ EC2').textContent).toBe('끊김');
    // 지표 API 는 EC2 가 잰 값이라 그대로 보인다.
    expect(await screen.findByText('340 ms')).toBeTruthy();
  });

  it('연결이 끊긴 채로 열면 지금 값을 비우고 상태를 연결 끊김으로 보인다', () => {
    useRealtimeStore.setState({ phase: 'CLOSED' });
    renderPage();

    expect(screen.getByText('연결 끊김')).toBeTruthy();
    expect(cameraIds()).toEqual([]);
    expect(latency('촬영 → 판정', 'p50').textContent).toBe(BLANK);
    expect(latency('촬영 → 판정', 'p95').textContent).toBe(BLANK);
  });

  it('받아 둔 지표도 연결이 끊기면 비운다', async () => {
    renderPage();
    expect(await screen.findByText('340 ms')).toBeTruthy();
    expect(screen.getByText('1400 ms')).toBeTruthy();

    act(() => useRealtimeStore.setState({ phase: 'CLOSED' }));
    for (const value of ['340 ms', '620 ms', '1400 ms']) {
      expect(screen.queryByText(value)).toBeNull();
    }
  });

  it('보는 동안 health 가 정한 시간 넘게 안 오면 값을 비운다', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    seed();
    renderPage();
    expect(screen.getByText('NOMINAL')).toBeTruthy();

    act(() => vi.setSystemTime(Date.now() + 4_000));
    expect(await screen.findByText('연결 끊김')).toBeTruthy();
    expect(cameraIds()).toEqual([]);
  });

  it('지표 갱신이 실패하면 받아 둔 값을 지금 값처럼 보이지 않는다', async () => {
    const queryClient = renderPage();
    expect(await screen.findByText('340 ms')).toBeTruthy();

    server.use(
      http.get('http://api.test/api/metrics', () => HttpResponse.json({}, { status: 500 })),
    );
    await act(() => queryClient.refetchQueries({ queryKey: METRICS_KEY }));
    await waitFor(() => expect(latency('촬영 → 현장 소리', 'p50').textContent).toBe(BLANK));
    expect(latency('촬영 → 판정', 'p50').textContent).toBe('300 ms');
  });

  it('관리자가 아니면 지표 API 를 부르지 않고 그 구간을 비운다', () => {
    act(() => {
      useSessionStore.getState().signIn({ accessToken: 't', user: { id: 'u', role: 'VIEWER' } });
    });
    const queryClient = renderPage();

    // 꺼 둔 조회는 시간이 지나도 요청하지 않는다. 기다리지 않고 꺼져 있는지를 본다.
    expect(queryClient.getQueryCache().find({ queryKey: METRICS_KEY })?.isDisabled()).toBe(true);
    expect(metricsCalls).toBe(0);
    expect(latency('촬영 → 현장 소리', 'p50').textContent).toBe(BLANK);
    // 빈칸은 지연 칸이어도 칠하지 않는다.
    expect(orange(latency('촬영 → 현장 소리', 'p50'))).toBe(false);
    expect(latency('촬영 → 반장 PWA', 'p95').textContent).toBe(BLANK);
    expect(latency('촬영 → 판정', 'p50').textContent).toBe('300 ms');
  });

  it('표에 이름과 행·열 머리가 있고, 빈칸은 화면 낭독기에 값 없음으로 읽힌다', () => {
    // 지표 구간이 모두 빈 응답이라 빈칸이 생긴다.
    server.use(
      http.get('http://api.test/api/metrics', () =>
        HttpResponse.json({ contract: 'DEMO_UNCONFIRMED', as_of: 1, stages: {} }),
      ),
    );
    renderPage();

    expect(screen.getByRole('table', { name: '카메라' })).toBeTruthy();
    expect(
      within(screen.getByRole('table', { name: '구간별 지연' }))
        .getAllByRole('rowheader')
        .map((th) => th.textContent),
    ).toEqual(['촬영 → 판정', '촬영 → 현장 소리', '촬영 → 관제 화면', '촬영 → 반장 PWA']);
    expect(
      within(latency('촬영 → 반장 PWA', 'p50')).getByText('—').getAttribute('aria-hidden'),
    ).toBe('true');
    expect(within(latency('촬영 → 반장 PWA', 'p50')).getByText('값 없음').className).toContain(
      'sr-only',
    );
    // 상태 바뀜은 상단 바의 칩이 알린다. 이 화면의 칩은 또 알리지 않는다.
    expect(screen.getByText('NOMINAL')).toBeTruthy();
    expect(screen.queryByRole('status')).toBeNull();
  });
});
