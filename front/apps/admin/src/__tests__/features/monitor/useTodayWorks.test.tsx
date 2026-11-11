import { useMonitorStandby, useTodayWorks } from '@/features/monitor/useTodayWorks';
import { TBM_WORKS_KEY } from '@/features/tbm/api';
import { todayIsoDate } from '@/features/tbm/taskInput';
import type { TbmWorkUnit } from '@/features/tbm/types';
import { useSessionStore } from '@/store/session.store';
import { startZoneEdit, stopZoneEdit } from '@/store/zoneEditMode.store';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, renderHook } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const NIGHT = new Date(2026, 10, 5, 23, 59, 30).getTime();

const work = (patch: Partial<TbmWorkUnit>): TbmWorkUnit => ({
  work_id: '1',
  status: 'PLANNED',
  date: '2026-11-05',
  name: '작업',
  from_ms: 1,
  to_ms: 2,
  zone_id: null,
  place: null,
  planned_people: 1,
  lead: null,
  layout_id: null,
  work_type: null,
  ...patch,
});

function wrapperWith(works?: TbmWorkUnit[]) {
  const client = new QueryClient();
  if (works) client.setQueryData(TBM_WORKS_KEY, works);
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

beforeEach(() => vi.useFakeTimers({ now: NIGHT, toFake: ['Date', 'setTimeout', 'clearTimeout'] }));
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  stopZoneEdit();
  useSessionStore.setState(useSessionStore.getInitialState(), true);
});

describe('useTodayWorks', () => {
  it('목록을 받기 전에는 대기라고 하지 않는다', () => {
    const { result } = renderHook(() => useTodayWorks({ passive: true }), {
      wrapper: wrapperWith(),
    });
    expect(result.current.state).toBe('loading');
    const standby = renderHook(() => useMonitorStandby(), { wrapper: wrapperWith() });
    expect(standby.result.current).toBe(false);
  });

  it('남은 작업이 없을 때만 standby 다', () => {
    const { result } = renderHook(() => useTodayWorks({ passive: true }), {
      wrapper: wrapperWith([work({ work_id: 'b' }), work({ work_id: 'old', date: '2026-11-04' })]),
    });
    expect(result.current).toMatchObject({ state: 'ready', standby: false });
    const done = renderHook(() => useTodayWorks({ passive: true }), {
      wrapper: wrapperWith([work({ status: 'DONE' })]),
    });
    expect(done.result.current).toMatchObject({ state: 'ready', standby: true });
  });

  it('자정을 넘기면 오늘이 바뀌어 어제의 예정 작업은 남은 작업이 아니다', () => {
    expect(todayIsoDate(new Date(NIGHT))).toBe('2026-11-05');
    const { result } = renderHook(() => useTodayWorks({ passive: true }), {
      wrapper: wrapperWith([work({ status: 'PLANNED' })]),
    });
    expect(result.current).toMatchObject({ standby: false });

    act(() => {
      vi.advanceTimersByTime(60_000);
    });
    expect(result.current).toMatchObject({ state: 'ready', standby: true });
  });
});

describe('useMonitorStandby', () => {
  it('구역 편집 중에는 대기 화면을 그리지 않으므로 false 다', () => {
    useSessionStore.getState().signIn({ accessToken: 't', user: { id: 'u', role: 'ADMIN' } });
    const wrapper = wrapperWith([work({ status: 'DONE' })]);
    const { result } = renderHook(() => useMonitorStandby(), { wrapper });
    expect(result.current).toBe(true);
    act(() => startZoneEdit());
    expect(result.current).toBe(false);
  });
});
