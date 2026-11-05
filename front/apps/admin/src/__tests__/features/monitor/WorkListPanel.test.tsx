import { WorkListPanel } from '@/features/monitor/WorkListPanel';
import { TBM_WORKS_KEY } from '@/features/tbm/api';
import { todayIsoDate } from '@/features/tbm/taskInput';
import { HISTORY_WORKS_KEY } from '@/lib/queryKeys';
import { useSessionStore, type Role } from '@/store/session.store';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw/http';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

// 실시간 관제의 오늘 작업 목록(S15P31A101-486). API 주소는 존재하지 않는 도메인(.invalid)이라 새는 연결이 DNS 에서 끝난다.
const BASE = 'https://work-list.invalid';
vi.mock('@/lib/api', async () => {
  const { createHttp } = await import('@/lib/http');
  return { api: createHttp({ baseUrl: 'https://work-list.invalid' }) };
});
const navigate = vi.hoisted(() => vi.fn());
vi.mock('@tanstack/react-router', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@tanstack/react-router')>()),
  useNavigate: () => navigate,
}));

const HOUR = 3_600_000;
const NOW = Date.now();
const TODAY = todayIsoDate();
const YESTERDAY = todayIsoDate(new Date(NOW - 24 * HOUR));
const TOMORROW = todayIsoDate(new Date(NOW + 24 * HOUR));

/** 서버가 주는 작업 한 건. 시각은 epoch ms 숫자다. */
function serverWork(patch: Record<string, unknown> = {}) {
  return {
    work_id: '1',
    status: 'PLANNED',
    date: TODAY,
    name: '비계 설치',
    from_ms: NOW + HOUR,
    to_ms: NOW + 3 * HOUR,
    zone_id: 'z-04',
    place: '개구부 구역',
    planned_people: 3,
    lead: 'A조',
    layout_id: null,
    work_type: null,
    actual_started_at: null,
    actual_ended_at: null,
    active_setup_session_id: null,
    event_counts: { CRITICAL: 0, HIGH: 0 },
    ...patch,
  };
}
const running = (patch: Record<string, unknown> = {}) =>
  serverWork({
    work_id: '2',
    name: '자재 반입',
    status: 'IN_PROGRESS',
    from_ms: NOW - 2 * HOUR,
    actual_started_at: NOW - HOUR,
    ...patch,
  });
const finished = (patch: Record<string, unknown> = {}) =>
  serverWork({
    work_id: '3',
    name: '청소',
    status: 'DONE',
    from_ms: NOW - 6 * HOUR,
    actual_started_at: NOW - 5 * HOUR,
    actual_ended_at: NOW - 3 * HOUR,
    ...patch,
  });

const server = setupServer();
const calls = { start: [] as string[], end: [] as string[] };
let works: unknown[] = [];

beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
afterAll(() => server.close());
beforeEach(() => {
  navigate.mockReset();
  calls.start = [];
  calls.end = [];
  works = [];
  server.use(
    http.get(`${BASE}/api/work-units`, () => HttpResponse.json({ works })),
    http.post(`${BASE}/api/work-units/:id/start`, ({ params }) => {
      calls.start.push(String(params.id));
      return HttpResponse.json(serverWork({ work_id: params.id, status: 'IN_PROGRESS' }));
    }),
    http.post(`${BASE}/api/work-units/:id/end`, ({ params }) => {
      calls.end.push(String(params.id));
      return HttpResponse.json(
        {
          work_unit: serverWork({ work_id: params.id, status: 'DONE' }),
          wrap_up: { status: 'GENERATING', report_id: 301, reason: null },
        },
        { status: 202 },
      );
    }),
  );
});
afterEach(() => {
  cleanup();
  server.resetHandlers();
  vi.restoreAllMocks();
  useSessionStore.setState(useSessionStore.getInitialState(), true);
});

function renderPanel(role: Role = 'ADMIN') {
  useSessionStore.getState().signIn({ accessToken: 't', user: { id: 'u', role } });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidate = vi.spyOn(client, 'invalidateQueries');
  render(
    <QueryClientProvider client={client}>
      <WorkListPanel />
    </QueryClientProvider>,
  );
  return { client, invalidate };
}

const row = (name: string) => screen.getByText(name).closest('li')!;
const findPanel = () => screen.findByRole('region', { name: '오늘 작업' });

describe('오늘 작업 목록', () => {
  it('오늘 날짜 작업과 진행 중 작업을 계획 시각순으로 보이고, 오늘이 아닌 끝난 작업은 보이지 않는다', async () => {
    works = [
      serverWork({ work_id: '9', name: '내일 작업', date: TOMORROW }),
      serverWork({ work_id: '8', name: '어제 끝난 작업', date: YESTERDAY, status: 'DONE' }),
      running({
        work_id: '7',
        name: '어제 시작한 작업',
        date: YESTERDAY,
        from_ms: NOW - 20 * HOUR,
      }),
      serverWork({
        work_id: '5',
        name: '오후 작업',
        from_ms: NOW + 4 * HOUR,
        to_ms: NOW + 6 * HOUR,
      }),
      serverWork({ name: '비계 설치' }),
    ];
    renderPanel();
    const panel = await findPanel();
    const names = within(panel)
      .getAllByRole('listitem')
      .map((item) => item.querySelector('b')!.textContent);
    expect(names).toEqual(['어제 시작한 작업', '비계 설치', '오후 작업']);
  });

  it('예정 작업은 작업 시작, 진행 중 작업은 작업 종료 버튼이 있고 끝난 작업은 버튼 없이 종료로 보인다', async () => {
    works = [serverWork(), running(), finished()];
    renderPanel();
    await findPanel();
    expect(within(row('비계 설치')).getByRole('button', { name: '작업 시작' })).toBeTruthy();
    expect(within(row('자재 반입')).getByRole('button', { name: '작업 종료' })).toBeTruthy();
    expect(within(row('청소')).queryByRole('button')).toBeNull();
    expect(within(row('청소')).getByText('종료')).toBeTruthy();
    // 시간 줄: 진행 중은 시작 시각, 끝난 작업은 실제 시간대. 작업 구역도 보인다.
    expect(row('자재 반입').textContent).toMatch(/시작 \d\d:\d\d/);
    expect(row('청소').textContent).toMatch(/\d\d:\d\d–\d\d:\d\d/);
    expect(row('비계 설치').textContent).toContain('개구부 구역');
  });

  it('진행 중인 작업이 있으면 다른 작업은 시작할 수 없다', async () => {
    works = [serverWork(), running()];
    renderPanel();
    await findPanel();
    const start = within(row('비계 설치')).getByRole('button', { name: '작업 시작' });
    expect((start as HTMLButtonElement).disabled).toBe(true);
    const end = within(row('자재 반입')).getByRole('button', { name: '작업 종료' });
    expect((end as HTMLButtonElement).disabled).toBe(false);
  });

  it.each(['FOREMAN', 'VIEWER'] as const)('%s 는 버튼 없이 상태만 본다', async (role) => {
    works = [serverWork(), running(), finished()];
    renderPanel(role);
    const panel = await findPanel();
    expect(within(panel).queryAllByRole('button')).toHaveLength(0);
    expect(within(row('비계 설치')).getByText('예정')).toBeTruthy();
    expect(within(row('자재 반입')).getByText('진행 중')).toBeTruthy();
    expect(within(row('청소')).getByText('종료')).toBeTruthy();
  });

  it('오늘 작업이 없으면 그리지 않는다(없음은 대기 화면이 보인다)', async () => {
    works = [finished({ date: YESTERDAY })];
    const { client } = renderPanel();
    await waitFor(() => expect(client.getQueryData(TBM_WORKS_KEY)).toBeTruthy());
    expect(screen.queryByRole('region', { name: '오늘 작업' })).toBeNull();
  });

  it('목록을 받지 못하면 한 줄로 알린다', async () => {
    server.use(http.get(`${BASE}/api/work-units`, () => HttpResponse.json({}, { status: 500 })));
    renderPanel();
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toBe('작업 목록을 불러오지 못했습니다.');
  });
});

describe('작업 시작', () => {
  it('세팅·구역이 끝나지 않았으면 먼저 묻고, 취소하면 요청하지 않는다', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    works = [serverWork()];
    renderPanel();
    await findPanel();
    fireEvent.click(screen.getByRole('button', { name: '작업 시작' }));
    await waitFor(() => expect(confirm).toHaveBeenCalledTimes(1));
    expect(confirm.mock.calls[0]![0]).toContain('아직 끝나지 않은 단계가 있습니다');
    expect(confirm.mock.calls[0]![0]).toContain('세팅(세팅 전)');
    expect(calls.start).toEqual([]);
  });

  it('세팅 세션은 시작을 누를 때 한 번만 조회하고, 못 받으면 확인 불가로 묻는다', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    let asked = 0;
    server.use(
      http.get(`${BASE}/api/setup-sessions/:id`, () => {
        asked += 1;
        return HttpResponse.json({ code: 'INTERNAL' }, { status: 500 });
      }),
    );
    works = [
      serverWork({ active_setup_session_id: '5' }),
      serverWork({ work_id: '6', name: '오후 작업', active_setup_session_id: '6' }),
    ];
    renderPanel();
    await findPanel();
    expect(asked).toBe(0);
    fireEvent.click(within(row('비계 설치')).getByRole('button', { name: '작업 시작' }));
    await waitFor(() => expect(confirm).toHaveBeenCalledTimes(1));
    expect(asked).toBe(1);
    expect(confirm.mock.calls[0]![0]).toContain('세팅(확인 불가(세팅 조회 실패))');
  });

  it('세팅 세션을 받는 동안 시작을 다시 눌러도 확인창은 한 번만 뜬다', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    server.use(
      http.get(`${BASE}/api/setup-sessions/:id`, () =>
        HttpResponse.json({ code: 'INTERNAL' }, { status: 500 }),
      ),
    );
    works = [serverWork({ active_setup_session_id: '5' })];
    renderPanel();
    await findPanel();
    const start = screen.getByRole('button', { name: '작업 시작' });
    fireEvent.click(start);
    expect((start as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(start);
    await waitFor(() => expect(confirm).toHaveBeenCalledTimes(1));
    await waitFor(() => expect((start as HTMLButtonElement).disabled).toBe(false));
    expect(confirm).toHaveBeenCalledTimes(1);
  });

  it('확인하면 시작을 요청하고, 작업 목록·보고서·히스토리 목록을 다시 받게 한다', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    works = [serverWork()];
    const { invalidate } = renderPanel();
    await findPanel();
    fireEvent.click(screen.getByRole('button', { name: '작업 시작' }));
    await waitFor(() => expect(calls.start).toEqual(['1']));
    await waitFor(() => {
      const keys = invalidate.mock.calls.map(([filter]) => filter?.queryKey);
      expect(keys).toContainEqual(TBM_WORKS_KEY);
      expect(keys).toContainEqual(HISTORY_WORKS_KEY);
    });
    expect(navigate).not.toHaveBeenCalled();
  });

  it('다른 작업이 이미 진행 중이라고 서버가 거절하면 그 작업 이름을 알린다', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    server.use(
      http.post(`${BASE}/api/work-units/:id/start`, () =>
        HttpResponse.json(
          { code: 'WORK_ALREADY_IN_PROGRESS', detail: '자재 반입' },
          { status: 409 },
        ),
      ),
    );
    works = [serverWork()];
    renderPanel();
    await findPanel();
    fireEvent.click(screen.getByRole('button', { name: '작업 시작' }));
    const alert = await within(row('비계 설치')).findByRole('alert');
    expect(alert.textContent).toContain('진행 중인 작업이 있습니다: 자재 반입');
  });
});

describe('작업 종료', () => {
  it('취소하면 요청하지 않고, 확인하면 종료를 요청한 뒤 작업 관리(서명)로 간다', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    works = [running()];
    renderPanel();
    await findPanel();
    fireEvent.click(screen.getByRole('button', { name: '작업 종료' }));
    expect(confirm).toHaveBeenCalledWith('작업을 종료하시겠습니까?');
    expect(calls.end).toEqual([]);
    expect(navigate).not.toHaveBeenCalled();

    confirm.mockReturnValue(true);
    fireEvent.click(screen.getByRole('button', { name: '작업 종료' }));
    await waitFor(() =>
      expect(navigate).toHaveBeenCalledWith({ to: '/work', search: { workUnitId: '2' } }),
    );
    expect(calls.end).toEqual(['2']);
  });

  it('종료가 실패하면 이동하지 않고 안내한다', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    server.use(
      http.post(`${BASE}/api/work-units/:id/end`, () =>
        HttpResponse.json({ code: 'INTERNAL' }, { status: 500 }),
      ),
    );
    works = [running()];
    renderPanel();
    await findPanel();
    fireEvent.click(screen.getByRole('button', { name: '작업 종료' }));
    const alert = await within(row('자재 반입')).findByRole('alert');
    expect(alert.textContent).toContain('작업 상태를 변경하지 못했습니다');
    expect(navigate).not.toHaveBeenCalled();
  });
});
