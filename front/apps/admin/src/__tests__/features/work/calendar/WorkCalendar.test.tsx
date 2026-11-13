import { workDayOf } from '@/__tests__/features/work/workFixture';
import { WorkCalendar } from '@/features/work/calendar/WorkCalendar';
import type { WorkDayItem } from '@/features/work/types';
import { createWorkMock } from '@/mocks/work';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw/http';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/api', async () => {
  const { createHttp } = await import('@/lib/http');
  return { api: createHttp({ baseUrl: 'http://api.test', getToken: () => 'mock-token.admin' }) };
});

// 달력은 GET /api/work-units?from&to 로 그 달이 걸치는 칸 전체의 작업을 받아 날짜별로 보인다. 오늘은 2026-11-11(수).

const TODAY = '2026-11-11';
/** 작업 목의 시계(오늘 아침 9시). */
const NOW = new Date(2026, 10, 11, 9, 0).getTime();

const daily = (date: string, overrides: Partial<WorkDayItem> = {}) =>
  workDayOf({ work_id: `d-${date}`, date, daily: true, ...overrides });
const past = (id: string, date: string, overrides: Partial<WorkDayItem> = {}) =>
  workDayOf({
    work_id: id,
    date,
    daily: false,
    progress: 'SUBMITTED',
    status: 'DONE',
    ...overrides,
  });

const FIXTURE_WORKS: WorkDayItem[] = [
  // 지난 날: 작업 2건(위험 2+1, 경고 1+없음)
  past('p1', '2026-11-09', { event_counts: { CRITICAL: 2, HIGH: 1 } }),
  past('p2', '2026-11-09', { event_counts: { CRITICAL: 1 } }),
  // 지난 날: 이벤트 수가 0
  past('p3', '2026-11-08', { event_counts: { CRITICAL: 0, HIGH: 0 } }),
  // 지난 날: 이벤트 수를 모름
  past('p4', '2026-11-10', { event_counts: null }),
  // 예정인 날: 보고서 작성 중
  daily('2026-11-12', { progress: 'REPORT' }),
];

let works: WorkDayItem[] = [];
/** 이 시험에서 달력이 보낸 날짜 범위('from..to'). */
let ranges: string[] = [];
let failList = false;
const server = setupServer();

beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
afterAll(() => server.close());
beforeEach(() => {
  works = FIXTURE_WORKS;
  ranges = [];
  failList = false;
  server.resetHandlers(
    http.get('*/api/work-units', ({ request }) => {
      const url = new URL(request.url);
      const from = url.searchParams.get('from') ?? '';
      const to = url.searchParams.get('to') ?? '';
      ranges.push(`${from}..${to}`);
      if (failList) return HttpResponse.json({ code: 'SERVER_ERROR' }, { status: 500 });
      return HttpResponse.json({ works: works.filter((w) => w.date >= from && w.date <= to) });
    }),
  );
});
afterEach(cleanup);

function renderCalendar(overrides: Partial<Parameters<typeof WorkCalendar>[0]> = {}) {
  const onPick = vi.fn();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const { container } = render(
    <QueryClientProvider client={client}>
      <WorkCalendar
        todayKey={TODAY}
        focusDate={TODAY}
        notice={null}
        onPick={onPick}
        {...overrides}
      />
    </QueryClientProvider>,
  );
  return { onPick, container };
}

const cellOf = (label: string) => screen.getByRole('button', { name: label });

describe('WorkCalendar', () => {
  it('머리줄에 연·월과 이전·다음 달·오늘 단추, 요일 줄과 그 달이 걸치는 5주를 보인다', async () => {
    renderCalendar();
    expect(screen.getByRole('heading', { name: '2026년 11월' })).toBeTruthy();
    expect(screen.getByRole('button', { name: '이전 달' })).toBeTruthy();
    expect(screen.getByRole('button', { name: '다음 달' })).toBeTruthy();
    expect(screen.getByRole('button', { name: '오늘' })).toBeTruthy();
    expect(screen.getAllByText(/^[일월화수목금토]$/).map((n) => n.textContent)).toEqual([
      '일',
      '월',
      '화',
      '수',
      '목',
      '금',
      '토',
    ]);
    // 5주 = 35칸(11월 1일 ~ 12월 5일).
    expect(screen.getAllByRole('button', { name: /^\d+월 \d+일$/ })).toHaveLength(35);
    expect(cellOf('11월 1일')).toBeTruthy();
    expect(cellOf('12월 5일')).toBeTruthy();
    await screen.findByText('작업 2건');
  });

  it('5주 달은 행이 5개, 6주 달은 6개다(행 높이는 최소 88px)', async () => {
    const rowsOf = (container: HTMLElement) =>
      container.querySelector<HTMLElement>('[style*="grid-template-rows"]')?.style.gridTemplateRows;
    const five = renderCalendar();
    expect(rowsOf(five.container)).toBe('auto repeat(5, minmax(88px, 1fr))');
    cleanup();
    // 2026년 8월은 6주(7월 26일 ~ 9월 5일).
    const six = renderCalendar({ focusDate: '2026-08-15' });
    expect(rowsOf(six.container)).toBe('auto repeat(6, minmax(88px, 1fr))');
    expect(screen.getAllByRole('button', { name: /^\d+월 \d+일$/ })).toHaveLength(42);
    await waitFor(() => expect(ranges).toContain('2026-07-26..2026-09-05'));
  });

  it('처음엔 마지막에 연 날짜(focusDate)가 든 달을 보인다', () => {
    renderCalendar({ focusDate: '2026-12-15' });
    expect(screen.getByRole('heading', { name: '2026년 12월' })).toBeTruthy();
  });

  it('오늘 칸에는 오늘 태그가 붙고, 작업이 없으면 작업 시작 단추 모양이 있다', async () => {
    renderCalendar();
    const today = cellOf('11월 11일');
    expect(within(today).getByText('오늘')).toBeTruthy();
    expect(within(today).getByText('작업 시작')).toBeTruthy();
    // 오늘 태그·작업 시작 단추는 오늘 칸에만 있다.
    expect(screen.getAllByText('작업 시작')).toHaveLength(1);
    expect(screen.getAllByText('오늘', { selector: 'span' })).toHaveLength(1);
    await screen.findByText('작업 2건');
  });

  it('오늘 칸은 옅은 주색 면과 2px 주색 테두리로 강조하고, 다른 칸은 그렇지 않다', async () => {
    renderCalendar();
    const today = cellOf('11월 11일');
    expect(today.className).toContain('bg-primary/10');
    expect(today.className).toContain('border-2');
    expect(today.className).toContain('border-primary');
    expect(today.className).not.toContain('ring-1');
    const other = cellOf('11월 12일');
    expect(other.className).not.toContain('bg-primary/10');
    expect(other.className).not.toContain('border-primary');
    await screen.findByText('작업 2건');
  });

  it('지난 날 + 작업 있음: 작업 종료 표시, 작업 건수, 위험·경고 합계를 보인다', async () => {
    renderCalendar();
    const day = cellOf('11월 9일');
    expect(await within(day).findByText('작업 종료')).toBeTruthy();
    expect(within(day).getByText('작업 2건')).toBeTruthy();
    expect(within(day).getByText('위험 3')).toBeTruthy();
    expect(within(day).getByText('경고 1')).toBeTruthy();
  });

  it('위험·경고가 모두 0 이거나 모르면 위험·경고 없음, 0 인 쪽은 회색이다', async () => {
    renderCalendar();
    await screen.findByText('작업 2건');
    expect(within(cellOf('11월 8일')).getByText('위험·경고 없음')).toBeTruthy();
    expect(within(cellOf('11월 8일')).getByText('작업 1건')).toBeTruthy();
    expect(within(cellOf('11월 10일')).getByText('위험·경고 없음')).toBeTruthy();
    // 경고만 있는 날의 위험 0 은 회색(muted), 경고는 회색이 아니다.
    works = [past('p5', '2026-11-07', { event_counts: { HIGH: 2 } })];
    cleanup();
    renderCalendar();
    const day = cellOf('11월 7일');
    expect((await within(day).findByText('위험 0')).className).toContain('text-muted-foreground');
    expect(within(day).getByText('경고 2').className).not.toContain('text-muted-foreground');
  });

  it('지난 날 표시는 제출 여부와 상관없이 작업 종료다(종료·제출·진행 값 없는 옛 응답 모두)', async () => {
    works = [
      past('p1', '2026-11-09', { progress: 'ENDED' }),
      past('p2', '2026-11-08', { progress: 'SUBMITTED' }),
      past('p3', '2026-11-07', { progress: null, actual_ended_at: 1_000 }),
    ];
    renderCalendar();
    const ended = within(cellOf('11월 9일'));
    expect(await ended.findByText('작업 종료')).toBeTruthy();
    expect(ended.getByText('작업 1건')).toBeTruthy();
    expect(within(cellOf('11월 8일')).getByText('작업 종료')).toBeTruthy();
    // 진행 값을 모르는 옛 응답도 실제로 끝났으면 작업 종료로 본다.
    expect(within(cellOf('11월 7일')).getByText('작업 종료')).toBeTruthy();
  });

  it('작업 종료 표시는 알약이 아니라 아이콘과 글자뿐이다(흐린 글자색)', async () => {
    works = [past('p1', '2026-11-09', { progress: 'ENDED' })];
    renderCalendar();
    const ended = await within(cellOf('11월 9일')).findByText('작업 종료');
    expect(ended.className).toContain('text-muted-foreground');
    expect(ended.className).not.toMatch(/border|bg-/);
    expect(ended.querySelector('svg')).not.toBeNull();
  });

  it('지난 날에 끝나지 않은 작업(보고서·세팅·세팅 완료·작업 중)만 있으면 빈칸이다', async () => {
    works = [
      daily('2026-11-10', { progress: 'REPORT' }),
      daily('2026-11-09', { progress: 'SETUP' }),
      daily('2026-11-08', { progress: 'SETUP_DONE' }),
      daily('2026-11-07', { progress: 'IN_PROGRESS' }),
      past('p5', '2026-11-06', { progress: null, actual_ended_at: null }),
      // 같은 날 끝난 작업이 있으면 끝난 작업만 센다.
      past('p6', '2026-11-05', { progress: 'SUBMITTED', event_counts: { CRITICAL: 1 } }),
      daily('2026-11-05', { progress: 'IN_PROGRESS', event_counts: { CRITICAL: 9, HIGH: 9 } }),
    ];
    renderCalendar();
    const counted = within(cellOf('11월 5일'));
    expect(await counted.findByText('작업 1건')).toBeTruthy();
    expect(counted.getByText('위험 1')).toBeTruthy();
    expect(counted.getByText('경고 0')).toBeTruthy();
    for (const day of [10, 9, 8, 7, 6]) {
      const empty = cellOf(`11월 ${day}일`);
      expect(empty.textContent).toBe(String(day));
    }
  });

  it('작업 목의 지난 이력은 제출한 날도 제출하지 않은 날도 작업 종료로 보인다', async () => {
    server.use(...createWorkMock({ now: () => NOW }).handlers);
    renderCalendar();
    expect(await within(cellOf('11월 10일')).findByText('작업 종료')).toBeTruthy();
    expect(within(cellOf('11월 9일')).getByText('작업 종료')).toBeTruthy();
  });

  it('작업이 없는 지난 날은 표시도 요약도 없고 누를 수 없다', async () => {
    const { onPick } = renderCalendar();
    await screen.findByText('작업 2건');
    const empty = cellOf('11월 5일') as HTMLButtonElement;
    expect(empty.textContent).toBe('5');
    expect(empty.disabled).toBe(true);
    fireEvent.click(empty);
    expect(onPick).not.toHaveBeenCalled();
    // 끝난 작업이 있는 지난 날·오늘·예정인 날은 누를 수 있다.
    for (const label of ['11월 9일', '11월 11일', '11월 20일']) {
      expect((cellOf(label) as HTMLButtonElement).disabled).toBe(false);
    }
  });

  it.each([
    ['REPORT', '작업 시작'],
    ['SETUP', '세팅 이어하기'],
    ['SETUP_DONE', '작업 전 확인'],
    ['IN_PROGRESS', '관제 중'],
    ['ENDED', '보고서 보내기'],
  ] as const)('오늘 칸 단추 글: 진행 값 %s 이면 `%s`', async (progress, label) => {
    works = [daily(TODAY, { progress })];
    renderCalendar();
    const button = await within(cellOf('11월 11일')).findByText(label);
    // 칸 전체가 이미 단추라 안에 단추를 또 두지 않는다(단추 모양의 span).
    expect(button.tagName).toBe('SPAN');
    expect(button.className).toContain('bg-primary');
    expect(cellOf('11월 11일').querySelectorAll('button')).toHaveLength(0);
  });

  it('오늘 작업을 제출했으면 단추 없이 작업 종료 표시다', async () => {
    works = [daily(TODAY, { progress: 'SUBMITTED' })];
    renderCalendar();
    const today = within(cellOf('11월 11일'));
    const done = await today.findByText('작업 종료');
    expect(done.className).toContain('text-muted-foreground');
    expect(done.className).not.toContain('bg-primary');
    expect(today.queryByText('작업 시작')).toBeNull();
  });

  it('오늘 칸의 단추 모양을 눌러도 칸처럼 그 날로 간다', async () => {
    const { onPick } = renderCalendar();
    fireEvent.click(within(cellOf('11월 11일')).getByText('작업 시작'));
    expect(onPick).toHaveBeenCalledTimes(1);
    expect(onPick).toHaveBeenCalledWith(TODAY);
    await screen.findByText('작업 2건');
  });

  it('예정인 날은 작업이 있어도 아무것도 그리지 않는다', async () => {
    works = [
      past('p1', '2026-11-09'),
      daily('2026-11-12', { progress: 'REPORT' }),
      daily('2026-11-13', { progress: 'SETUP_DONE' }),
      daily('2026-11-14', { progress: 'SUBMITTED' }),
    ];
    renderCalendar();
    // 작업 목록이 칸에 들어온 뒤에도(지난 날 칸에 작업 건수가 보인다) 예정인 칸은 날짜만 있다.
    await screen.findByText('작업 1건');
    for (const day of [12, 13, 14, 15]) {
      expect(cellOf(`11월 ${day}일`).textContent).toBe(String(day));
    }
  });

  it('이전·다음 달 단추로 달을 옮기고 그 달이 걸치는 칸 전체의 작업을 받는다', async () => {
    renderCalendar();
    await waitFor(() => expect(ranges).toEqual(['2026-11-01..2026-12-05']));

    fireEvent.click(screen.getByRole('button', { name: '다음 달' }));
    expect(screen.getByRole('heading', { name: '2026년 12월' })).toBeTruthy();
    await waitFor(() => expect(ranges).toContain('2026-11-29..2027-01-02'));

    fireEvent.click(screen.getByRole('button', { name: '이전 달' }));
    fireEvent.click(screen.getByRole('button', { name: '이전 달' }));
    expect(screen.getByRole('heading', { name: '2026년 10월' })).toBeTruthy();
    await waitFor(() => expect(ranges).toContain('2026-09-27..2026-10-31'));
  });

  it('오늘 단추는 오늘이 든 달로 돌아온다', () => {
    renderCalendar();
    fireEvent.click(screen.getByRole('button', { name: '다음 달' }));
    fireEvent.click(screen.getByRole('button', { name: '다음 달' }));
    expect(screen.getByRole('heading', { name: '2027년 1월' })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '오늘' }));
    expect(screen.getByRole('heading', { name: '2026년 11월' })).toBeTruthy();
  });

  it('칸을 누르면 그 날짜 키로 알린다(지난 날·오늘·예정인 날 모두)', async () => {
    const { onPick } = renderCalendar();
    await screen.findByText('작업 2건');
    fireEvent.click(cellOf('11월 9일'));
    fireEvent.click(cellOf('11월 11일'));
    fireEvent.click(cellOf('11월 20일'));
    expect(onPick.mock.calls.map((c) => c[0])).toEqual(['2026-11-09', '2026-11-11', '2026-11-20']);
  });

  it('달 밖 날짜는 흐리게 보이지만 눌린다', async () => {
    const { onPick } = renderCalendar();
    const outside = cellOf('12월 1일');
    expect(outside.className).toContain('opacity-45');
    expect(cellOf('11월 30일').className).not.toContain('opacity-45');
    fireEvent.click(outside);
    expect(onPick).toHaveBeenCalledWith('2026-12-01');
    await screen.findByText('작업 2건');
  });

  it('달 밖 날짜에도 그 날 작업의 표시가 보인다', async () => {
    // 10월 달력의 첫 줄은 9월 27~30일(달 밖, 지난 날)이다.
    works = [past('p1', '2026-09-30', { progress: 'SUBMITTED' })];
    renderCalendar({ focusDate: '2026-10-15' });
    const outside = within(cellOf('9월 30일'));
    expect(await outside.findByText('작업 종료')).toBeTruthy();
    expect(outside.getByText('작업 1건')).toBeTruthy();
  });

  it('알림이 있으면 머리줄에 보인다', async () => {
    renderCalendar({ notice: '보고서를 보냈습니다' });
    expect(screen.getByRole('status').textContent).toBe('보고서를 보냈습니다');
    await screen.findByText('작업 2건');
  });

  it('작업 목록을 못 받으면 알리고, 다시 시도하면 받아 온다', async () => {
    failList = true;
    renderCalendar();
    expect(await screen.findByText('작업 목록을 불러오지 못했습니다.')).toBeTruthy();
    failList = false;
    fireEvent.click(screen.getByRole('button', { name: '다시 시도' }));
    expect(await screen.findByText('작업 2건')).toBeTruthy();
    expect(screen.queryByText('작업 목록을 불러오지 못했습니다.')).toBeNull();
  });
});
