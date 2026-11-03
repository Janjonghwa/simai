import { STARTED_RECHECK_MS } from '@/features/history/constants';
import { HistoryListPage } from '@/features/history/HistoryListPage';
import { historyWorks } from '@/mocks/history';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const navigate = vi.fn();
const TODAY = '2026-10-29';
// 오늘 11시. 서버·mock 은 오늘 13시 작업도 주지만 시작한 것은 09·10시 작업뿐이다. answer 로 목록 응답을 바꾼다.
// zero: 모든 작업이 위험·경고 0 건, partial: 위험만 2 건(경고 수는 없다).
const state = vi.hoisted(() => ({
  now: new Date(2026, 9, 29, 11, 0).getTime(),
  answer: 'ok' as 'ok' | 'empty' | 'fail' | 'zero' | 'partial',
}));

vi.mock('@tanstack/react-router', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@tanstack/react-router')>()),
  useNavigate: () => navigate,
}));
vi.mock('@/lib/api', async () => {
  const { historyWorks } = await import('@/mocks/history');
  return {
    api: (path: string) => {
      if (!path.startsWith('/api/work-units')) return Promise.reject(new Error(path));
      if (state.answer === 'fail') return Promise.reject(new Error('down'));
      const counts = {
        zero: { CRITICAL: 0, HIGH: 0 },
        partial: { CRITICAL: 2 },
      } as const;
      const works =
        state.answer === 'empty'
          ? []
          : historyWorks(state.now).map((work) =>
              state.answer === 'zero' || state.answer === 'partial'
                ? { ...work, event_counts: counts[state.answer] }
                : work,
            );
      return Promise.resolve({ works });
    },
  };
});

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <HistoryListPage />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.useFakeTimers({ now: state.now, shouldAdvanceTime: true });
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  navigate.mockClear();
  state.answer = 'ok';
});

describe('히스토리 목록', () => {
  it('최근 작업을 새 날짜부터 날짜별로 보이고, 누르면 그 작업 다시 보기로 간다', async () => {
    renderPage();
    const today = await screen.findByRole('region', { name: '10월 29일 (목)' });
    expect(screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent)).toEqual([
      '10월 29일 (목)',
      '10월 28일 (수)',
      '10월 27일 (화)',
    ]);

    const rows = within(today).getAllByRole('button');
    expect(rows).toHaveLength(2);
    expect(rows[0]!.textContent).toContain('09:00–12:00');
    expect(rows[0]!.textContent).toContain('계획 4명');

    rows[1]!.click();
    expect(navigate).toHaveBeenCalledWith({
      to: '/history/$workId',
      params: { workId: '2026-10-29-T2' },
    });
  });

  it('시작하지 않은 작업은 목록에 없고, 시작 시각이 지나면 1분 안에 나타난다', async () => {
    renderPage();
    const today = await screen.findByRole('region', { name: '10월 29일 (목)' });
    expect(within(today).getAllByRole('button')).toHaveLength(2);
    expect(within(today).queryByText('13:00–17:00')).toBeNull();

    // 12시 58분: 아직 시작 전이다.
    act(() => vi.advanceTimersByTime(118 * STARTED_RECHECK_MS));
    expect(within(today).queryByText('13:00–17:00')).toBeNull();

    // 13시를 지나 한 번 더 거르면 나타난다.
    act(() => vi.advanceTimersByTime(2 * STARTED_RECHECK_MS));
    expect(within(today).getByText('13:00–17:00')).toBeTruthy();
    expect(within(today).getAllByRole('button')).toHaveLength(3);
  });

  it('작업마다 위험·경고 수를 읽기 쉬운 말로 보인다(CRITICAL·HIGH 가 아니다)', async () => {
    renderPage();
    const today = await screen.findByRole('region', { name: '10월 29일 (목)' });
    const expected = historyWorks(state.now)
      .filter((work) => work.date === TODAY && work.from_ms <= state.now)
      .sort((a, b) => a.from_ms - b.from_ms);
    const rows = within(today).getAllByRole('button');
    expect(rows).toHaveLength(expected.length);

    // 센 값이 있는 작업이 있어야 0 만 보는 시험이 되지 않는다.
    expect(expected.some((work) => (work.event_counts?.CRITICAL ?? 0) > 0)).toBe(true);
    expected.forEach((work, i) => {
      const counts = work.event_counts!;
      expect(counts.CRITICAL + counts.HIGH).toBeGreaterThan(0);
      expect(within(rows[i]!).getByText(`위험 ${counts.CRITICAL}`)).toBeTruthy();
      expect(within(rows[i]!).getByText(`경고 ${counts.HIGH}`)).toBeTruthy();
      expect(rows[i]!.textContent).not.toMatch(/CRITICAL|HIGH|위험·경고 없음/);
    });
  });

  it('위험·경고가 하나도 없는 작업은 위험·경고 없음이라고만 한다', async () => {
    state.answer = 'zero';
    renderPage();
    const today = await screen.findByRole('region', { name: '10월 29일 (목)' });
    const rows = within(today).getAllByRole('button');
    expect(rows).toHaveLength(2);
    for (const row of rows) {
      expect(within(row).getByText('위험·경고 없음')).toBeTruthy();
      expect(row.textContent).not.toMatch(/위험 \d|경고 \d|이벤트 없음/);
    }
    expect(screen.getAllByText('위험·경고 없음').length).toBeGreaterThanOrEqual(2);
  });

  it('한쪽 수만 알려 오면 모르는 쪽은 0 으로 보인다', async () => {
    state.answer = 'partial';
    renderPage();
    const today = await screen.findByRole('region', { name: '10월 29일 (목)' });
    for (const row of within(today).getAllByRole('button')) {
      expect(within(row).getByText('위험 2')).toBeTruthy();
      expect(within(row).getByText('경고 0')).toBeTruthy();
    }
  });

  it('기록이 없는 작업은 이벤트 수를 모른다(—)', async () => {
    renderPage();
    const oldest = await screen.findByRole('region', { name: '10월 27일 (화)' });
    const last = within(oldest).getAllByRole('button').at(-1)!;
    expect(last.textContent).toContain('—');
    expect(last.textContent).not.toMatch(/CRITICAL|HIGH|위험|경고/);
  });

  it('작업이 없거나 받지 못하면 그렇게 말한다', async () => {
    state.answer = 'empty';
    renderPage();
    expect(await screen.findByText('기록된 작업이 없습니다.')).toBeTruthy();
    cleanup();

    state.answer = 'fail';
    renderPage();
    expect(await screen.findByText('작업 목록을 불러오지 못했습니다.')).toBeTruthy();
  });
});
